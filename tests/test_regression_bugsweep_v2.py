"""Regression tests for the second-round bug sweep fixes (2e67af2,
c099393, 8daaa11). Each test cites the HIGH/MED number from the review.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# HIGH-2 / HIGH-3 — BridgeState concurrency helpers
# (snapshot_suggestions, replace_suggestions, prepare_for_launch)
# ---------------------------------------------------------------------------


class TestBridgeStateHelpers:
    def _make(self):
        from quickhatch.bridge.server import BridgeState
        from quickhatch.config import UserProfile

        return BridgeState(UserProfile(), persist_path=None)

    def test_snapshot_returns_independent_copy(self):
        b = self._make()
        b.add_suggestion("plan", "T", "body")
        snap = b.snapshot_suggestions()
        assert len(snap) == 1
        # Mutating the snapshot must not affect the live list
        snap.clear()
        assert len(b.snapshot_suggestions()) == 1

    def test_replace_suggestions_atomic(self):
        from quickhatch.bridge.server import Suggestion

        b = self._make()
        b.add_suggestion("plan", "old", "x")
        b.add_suggestion("apps", "old2", "y")
        new = [Suggestion(id="new", category="plan", title="new", content="z")]
        b.replace_suggestions(new)
        snap = b.snapshot_suggestions()
        assert len(snap) == 1
        assert snap[0].id == "new"

    def test_prepare_for_launch_clears_transient_flags_only(self):
        b = self._make()
        b.add_suggestion("distro", "keep", "body")
        b.add_message("agent", "hello")
        b.stop_requested = True
        b.agent_done = True

        b.prepare_for_launch()

        assert b.stop_requested is False
        assert b.agent_done is False
        # Suggestions + messages MUST survive (runs resume mid-flight)
        assert len(b.snapshot_suggestions()) == 1
        assert len(b.messages) == 1


# ---------------------------------------------------------------------------
# MED-11 — GUI server uses ThreadingHTTPServer so /api/ask doesn't
# block every other request for 300s
# ---------------------------------------------------------------------------


class TestThreadingHTTPServer:
    def test_gui_server_is_threading(self):
        from http.server import ThreadingHTTPServer

        from quickhatch.bridge.server import BridgeState
        from quickhatch.config import UserProfile
        from quickhatch.gui.server import GUIState, make_gui_server

        gui = GUIState(UserProfile(), BridgeState(UserProfile(), persist_path=None))
        server, _port = make_gui_server(gui, port=0)
        try:
            assert isinstance(server, ThreadingHTTPServer), (
                "GUI server must be threading so /api/ask doesn't freeze the UI"
            )
        finally:
            server.server_close()

    def test_bridge_server_uses_threading_class(self):
        # Inspect the module source — we don't actually instantiate the
        # bridge server here (it binds a port).
        import quickhatch.bridge.server as bridge_mod

        src = bridge_mod.__file__
        with open(src, encoding="utf-8") as f:
            text = f.read()
        assert "ThreadingHTTPServer(" in text
        # No bare HTTPServer( call in instantiation position
        # (the import line is the only legitimate usage of the word).
        for line in text.splitlines():
            if line.strip().startswith("#"):
                continue
            assert "HTTPServer(" not in line or "ThreadingHTTPServer(" in line, (
                f"non-threading HTTPServer instantiation in bridge: {line!r}"
            )


# ---------------------------------------------------------------------------
# HIGH-1 — XSS: safeUrl rejects non-http(s) schemes
# ---------------------------------------------------------------------------


class TestSafeUrlSanitizer:
    """The safeUrl() logic lives in app.html (client-side). Mirror it
    here in Python so tests can assert on the inputs we care about
    without needing a browser. If app.html's function is updated,
    this test needs updating too — that's by design."""

    @staticmethod
    def _safe_url(u: str) -> str:
        s = (u or "").strip()
        if not s:
            return "#"
        import re

        if re.match(r"^(https?:|mailto:|/|\./|#)", s, re.IGNORECASE):
            return s
        return "#"

    @pytest.mark.parametrize(
        "evil",
        [
            "javascript:alert(1)",
            "JAVAscript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "vbscript:msgbox(1)",
            "file:///etc/passwd",
            "\tjavascript:alert(1)",
            "  javascript:alert(1)  ",
        ],
    )
    def test_unsafe_schemes_neutered(self, evil):
        assert self._safe_url(evil) == "#", f"letting through: {evil!r}"

    @pytest.mark.parametrize(
        "ok",
        [
            "https://example.com",
            "http://example.com",
            "HTTPS://example.com",
            "mailto:someone@example.com",
            "/wizard/profile",
            "./relative",
            "#anchor",
        ],
    )
    def test_safe_schemes_preserved(self, ok):
        assert self._safe_url(ok) == ok

    def test_app_html_uses_safeUrl(self):
        """The real client-side use sites in app.html must wrap user
        URLs in safeUrl. If a new interpolation slips in without it,
        this grep catches it."""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        html = (root / "src/quickhatch/gui/app.html").read_text(encoding="utf-8")
        # All the known LLM-origin URL interpolations must go through safeUrl
        assert "safeUrl(url)" in html or "safeUrl(" in html, (
            "safeUrl() helper is missing from app.html"
        )
        # No raw href="${escHtml(url)}"-style patterns for user-origin URLs
        assert 'href="${escHtml(url)}"' not in html
        assert 'src="${escHtml(url)}"' not in html


# ---------------------------------------------------------------------------
# HIGH-5 — defensive LLM response extraction
# ---------------------------------------------------------------------------


class TestLlmResponseExtractors:
    def test_openai_text_from_normal_response(self):
        from quickhatch.gui.server import _extract_openai_text

        payload = {"choices": [{"message": {"content": "hello world"}}]}
        assert _extract_openai_text(payload) == "hello world"

    def test_openai_text_falls_back_to_reasoning_content(self):
        from quickhatch.gui.server import _extract_openai_text

        payload = {
            "choices": [{"message": {"content": "", "reasoning_content": "I was thinking..."}}]
        }
        assert _extract_openai_text(payload) == "I was thinking..."

    @pytest.mark.parametrize(
        "bad",
        [
            {},  # no choices
            {"choices": []},  # empty
            {"choices": [{}]},  # no message
            {"choices": [{"message": {}}]},  # no content
            {"choices": "not a list"},  # wrong type
            None,
            "error string body",
        ],
    )
    def test_openai_text_empty_on_malformed(self, bad):
        from quickhatch.gui.server import _extract_openai_text

        # Must not raise
        result = _extract_openai_text(bad)
        assert result == ""

    def test_anthropic_text_normal(self):
        from quickhatch.gui.server import _extract_anthropic_text

        payload = {
            "content": [
                {"type": "text", "text": "part 1 "},
                {"type": "tool_use", "name": "ignored"},
                {"type": "text", "text": "part 2"},
            ]
        }
        assert _extract_anthropic_text(payload) == "part 1 part 2"

    @pytest.mark.parametrize("bad", [{}, {"content": None}, None, "string"])
    def test_anthropic_text_empty_on_malformed(self, bad):
        from quickhatch.gui.server import _extract_anthropic_text

        assert _extract_anthropic_text(bad) == ""


# ---------------------------------------------------------------------------
# MED-8 — _ssh_exec catches generic OSError
# ---------------------------------------------------------------------------


class TestSshExecErrorHandling:
    def test_file_not_found_returns_sentinel(self, monkeypatch):
        import subprocess

        from quickhatch.gui.server import _ssh_exec

        def bad_run(*a, **k):
            raise FileNotFoundError("ssh binary missing")

        monkeypatch.setattr(subprocess, "run", bad_run)
        rc, out, err = _ssh_exec("127.0.0.1", "root", "true")
        assert rc == -1
        assert "ssh" in err.lower()

    def test_oserror_no_longer_propagates(self, monkeypatch):
        """Previously only FileNotFoundError was caught. On Windows a
        corrupt ssh.exe raises OSError(WinError 193) which used to kill
        the agent thread. Must now return cleanly."""
        import subprocess

        from quickhatch.gui.server import _ssh_exec

        def bad_run(*a, **k):
            raise OSError("[WinError 193] %1 is not a valid Win32 application")

        monkeypatch.setattr(subprocess, "run", bad_run)
        rc, out, err = _ssh_exec("127.0.0.1", "root", "true")
        assert rc == -1
        assert "WinError" in err or "invocation failed" in err

    def test_timeout_returns_sentinel(self, monkeypatch):
        import subprocess

        from quickhatch.gui.server import _ssh_exec

        def timeout_run(*a, **k):
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=5)

        monkeypatch.setattr(subprocess, "run", timeout_run)
        rc, out, err = _ssh_exec("127.0.0.1", "root", "true", timeout=1)
        assert rc == -1
        assert "timed out" in err.lower()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
