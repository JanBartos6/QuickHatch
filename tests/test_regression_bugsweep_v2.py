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


class TestAnalysisRerunUiGating:
    """Regression: the SPA must not clear live analysis until the rerun
    endpoint has actually accepted the new run."""

    def test_app_html_waits_for_ok_before_resetting_analysis_ui(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        html = (root / "src/quickhatch/gui/app.html").read_text(encoding="utf-8")
        assert "function beginAnalysisRunUi(statusText)" in html
        assert "const resp=await api('/wizard/rerun-agent','POST',{message:''});" in html
        assert "const resp=await api('/wizard/rerun-agent','POST',{message:msg});" in html
        assert "if(resp&&resp.status==='ok'){" in html


class TestBridgeWrapperPaths:
    """Regression: helper scripts must live in a writable home
    directory and the printed commands must target those files."""

    def test_bridge_cli_uses_home_directory_wrapper_files(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        text = (root / "src/quickhatch/wizard/bridge.py").read_text(encoding="utf-8")
        assert 'Path.home() / ".quickhatch-agent-cmd.ps1"' in text
        assert 'Path.home() / ".quickhatch-agent-cmd.sh"' in text
        assert "& {ps_cmd_file}" in text
        assert "bash {sh_cmd_file}" in text


class TestFrontendProfileSync:
    """Regression: the SPA should apply only the newest authoritative
    profile snapshot instead of re-merging raw request payloads."""

    def test_app_html_uses_authoritative_profile_sync_helper(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        html = (root / "src/quickhatch/gui/app.html").read_text(encoding="utf-8")
        assert "function applyServerProfile(profile, seq)" in html
        assert "if((seq||0) < (state._profileAppliedSeq||0))return false;" in html
        assert "const seq=++state._profileRequestSeq;" in html
        assert "if(resp&&resp.profile){" in html
        assert "applyServerProfile(resp.profile,seq);" in html
        assert "state.profile.preferences=Object.assign" not in html

    def test_save_and_complete_helpers_gate_on_backend_success(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        html = (root / "src/quickhatch/gui/app.html").read_text(encoding="utf-8")
        assert "function completeStep(stepId)" in html
        assert "const resp=await api('/wizard/complete_step','POST',{step:stepId});" in html
        assert "if(!resp||resp.status!=='ok'){" in html
        assert "throw new Error(message);" in html
        assert "const resp=await api('/wizard/update','POST',updates);" in html

    def test_submit_prefs_preserves_gallery_picks(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        html = (root / "src/quickhatch/gui/app.html").read_text(encoding="utf-8")
        assert "gallery_picks:[...(state.galleryPinned||[])]" in html
        assert "gallery_picks:[]" not in html

    def test_scan_step_autosaves_non_empty_target_override(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        html = (root / "src/quickhatch/gui/app.html").read_text(encoding="utf-8")
        assert "async function autoSaveTargetOverrideIfNeeded()" in html
        assert "await autoSaveTargetOverrideIfNeeded();" in html
        assert 'onclick="completeScanStep()"' in html
        assert "async function completeScanStep()" in html
        assert "Save description" not in html
        assert "async function saveTargetOverride()" not in html

    def test_wizard_uses_local_draft_and_silent_autosave_for_mid_step_fields(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        html = (root / "src/quickhatch/gui/app.html").read_text(encoding="utf-8")
        assert "const WIZARD_DRAFT_KEY='qh-wizard-draft-v1';" in html
        assert "const PROFILE_SHADOW_KEY='qh-profile-shadow-v1';" in html
        assert "function writeProfileShadow(profile)" in html
        assert "function mergeProfileMissing(serverValue, shadowValue)" in html
        assert "state._profileShadow=loadProfileShadow();" in html
        assert "await restoreShadowProfileToServer(state.profile);" in html
        assert "writeProfileShadow(state.profile);" in html
        assert "state._draft=loadWizardDraft();" in html
        assert 'oninput="persistQuestionnaireDraft()"' in html
        assert 'oninput="persistAppsDataDraft()"' in html
        assert 'oninput="persistAdditionalDraft()"' in html
        assert "saveProfile({additional_info:text}, {silentError:true})" in html
        assert "saveProfile({\n    preferences:{" in html or "saveProfile({preferences:{" in html

    def test_export_step_keeps_success_visible_until_user_continues(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        html = (root / "src/quickhatch/gui/app.html").read_text(encoding="utf-8")
        assert "state._exportResult=data;" in html
        assert "await completeStep('export');" not in html
        assert "exportResultCard(state._exportResult)" in html

    def test_setup_launch_requires_successful_connection_test(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        html = (root / "src/quickhatch/gui/app.html").read_text(encoding="utf-8")
        assert "const canLaunch=!!(hasConn&&state._sshTestOk);" in html
        assert "state._sshTestOk=true;" in html
        assert "state._sshTestOk=false;" in html
        assert "if(state._setupStarted&&data.agent_done){" in html

    def test_live_usb_copy_guidance_no_longer_promises_pre_ssh_scp(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        html = (root / "src/quickhatch/gui/app.html").read_text(encoding="utf-8")
        assert "Do not rely on <code>scp</code> yet" in html
        assert "Replace <code>/dev/sdX1</code> with the actual bundle USB partition." in html

    def test_bootable_usb_uses_curated_distro_family_guides(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        html = (root / "src/quickhatch/gui/app.html").read_text(encoding="utf-8")
        assert "const DISTRO_USB_GUIDES=" in html
        assert "function getDistroUsbGuide(distro)" in html
        assert "guide.mountCommands" in html
        assert "guide.runCommands" in html
        assert "Arch boots straight into a root shell." in html
        assert (
            'Ubuntu, Mint, Pop!_OS, Zorin, KDE Neon, and similar ISOs usually have a "Try" or live desktop option.'
            in html
        )
        assert (
            "Fedora Workstation, Silverblue, Kinoite, Bazzite, Nobara, Rocky, and Alma usually boot into a live desktop."
            in html
        )
        assert (
            "Stay in the live environment or installer shell if QuickHatch is handling the target-side steps."
            in html
        )

    def test_bootable_usb_number_bubbles_keep_contrast_in_dark_mode(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        html = (root / "src/quickhatch/gui/app.html").read_text(encoding="utf-8")
        assert ".guide-num{" in html
        assert "background:var(--primary);color:var(--bg);" in html
        assert "background:var(--primary);color:#fff;" not in html

    def test_ssh_step_no_longer_duplicates_full_usb_walkthrough(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        html = (root / "src/quickhatch/gui/app.html").read_text(encoding="utf-8")
        assert "Target-side guide is on the previous step" in html
        assert "This step is only about getting the SSH key into the export bundle." in html
        assert "The whole Linux-side flow" not in html

    def test_bring_your_own_key_path_is_saved_instead_of_only_skipped(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        html = (root / "src/quickhatch/gui/app.html").read_text(encoding="utf-8")
        assert "async function useExistingSshKey()" in html
        assert "await saveProfile({ssh_public_key_path:keyPath});" in html
        assert "state._showOwnKeyInput" in html

    def test_offline_done_screen_no_longer_claims_remote_setup_finished(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        html = (root / "src/quickhatch/gui/app.html").read_text(encoding="utf-8")
        assert "Bundle ready" in html
        assert "Bundle exported with quickstart + plan" in html
        assert "Install guidance ready for the live environment" in html


class TestAnalysisSearchOwnership:
    """Regression: hardware search planning should belong to the selected LLM,
    not to harness-side raw-GPU loops."""

    def test_analysis_loop_uses_tool_capable_model_path(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        server_py = (root / "src/quickhatch/gui/server.py").read_text(encoding="utf-8")
        assert "content = _call_model_analysis(" in server_py
        assert "_call_model_simple(" in server_py  # helper still exists elsewhere

    def test_prefetch_search_context_is_noop_and_has_no_gpu_driver_loop(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        server_py = (root / "src/quickhatch/gui/server.py").read_text(encoding="utf-8")
        assert 'return "", []' in server_py
        assert 'queries.append(("GPU driver"' not in server_py
        assert 'bridge.add_message("agent", f"Searching: {query}")' in server_py


class TestGalleryLicensePolicy:
    """Regression: unlicensed entries may be browsed, but the UI must not
    imply they are reproducible/applyable."""

    def test_gallery_ui_calls_out_browse_only_unlicensed_entries(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        html = (root / "src/quickhatch/gui/app.html").read_text(encoding="utf-8")
        assert "Every entry links back to its source." in html
        assert "browse-only inspiration and will not be auto-applied" in html
        assert (
            "Community entries include attribution and a reproducible dotfile source." not in html
        )


class TestCodexLaunchPrelaunch:
    """Regression: the external Codex launch path must honor the same
    admitted-launch prelaunch hook as the API-backed agent path."""

    def test_codex_launch_runs_prelaunch_after_spawn(self, monkeypatch, tmp_path):
        import subprocess

        import quickhatch.gui.server as gui_server_mod
        from quickhatch.bridge.server import BridgeState
        from quickhatch.config import UserProfile
        from quickhatch.gui.server import GUIState, _launch_agent

        profile = UserProfile()
        profile.ai_config.provider = "codex"
        gui = GUIState(profile, BridgeState(profile, persist_path=None))
        spawned = []
        called = []

        monkeypatch.setattr(gui_server_mod.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(subprocess, "Popen", lambda args: spawned.append(args))

        result = _launch_agent(gui, 9999, prelaunch=lambda: called.append(True))
        assert result["status"] == "ok"
        assert spawned
        assert called == [True]


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
    @staticmethod
    def _bypass_probe(monkeypatch):
        """_ssh_exec runs a TCP probe before spawning ssh. For error-path
        tests we don't want the probe to fail first — pretend it found
        a valid SSH banner so control reaches the mocked subprocess.run."""
        import socket

        class _FakeSock:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def recv(self, n):
                return b"SSH-2.0-Fake"

        monkeypatch.setattr(socket, "create_connection", lambda *a, **k: _FakeSock())

    def test_file_not_found_returns_sentinel(self, monkeypatch):
        import subprocess

        from quickhatch.gui.server import _ssh_exec

        self._bypass_probe(monkeypatch)

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

        self._bypass_probe(monkeypatch)

        def bad_run(*a, **k):
            raise OSError("[WinError 193] %1 is not a valid Win32 application")

        monkeypatch.setattr(subprocess, "run", bad_run)
        rc, out, err = _ssh_exec("127.0.0.1", "root", "true")
        assert rc == -1
        assert "WinError" in err or "invocation failed" in err

    def test_timeout_returns_sentinel(self, monkeypatch):
        import subprocess

        from quickhatch.gui.server import _ssh_exec

        self._bypass_probe(monkeypatch)

        def timeout_run(*a, **k):
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=5)

        monkeypatch.setattr(subprocess, "run", timeout_run)
        rc, out, err = _ssh_exec("127.0.0.1", "root", "true", timeout=1)
        assert rc == -1
        assert "timed out" in err.lower()

    def test_probe_detects_unreachable_host_before_spawning_ssh(self, monkeypatch):
        """The TCP probe should short-circuit when the port is closed —
        no ssh subprocess should even be launched."""
        import socket
        import subprocess

        from quickhatch.gui.server import _ssh_exec

        def refused(*a, **k):
            raise ConnectionRefusedError("nope")

        monkeypatch.setattr(socket, "create_connection", refused)
        run_called = {"n": 0}

        def count_run(*a, **k):
            run_called["n"] += 1
            raise AssertionError("subprocess.run should not be reached")

        monkeypatch.setattr(subprocess, "run", count_run)
        rc, out, err = _ssh_exec("127.0.0.1", "root", "true")
        assert rc == -1
        assert "cannot reach" in err.lower()
        assert run_called["n"] == 0


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
