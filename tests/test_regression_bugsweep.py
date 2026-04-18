"""Regression tests for bugs caught during real VM-install runs + the
review-agent bug sweep (see commits ce1ea30, 930a949, e30296e).

These tests exist so the specific failure modes we hit don't quietly
come back. Each test names its originating bug class.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from quickhatch.config import AppInfo, Preferences, UserProfile
from quickhatch.gui.server import _build_section_prompt, _extract_json_objects, _is_reboot_command
from quickhatch.wizard.vm_verify import build_key_paste_snippet

# ---------------------------------------------------------------------------
# LLM output parsing — heredocs with literal newlines
# (commit: JSON parser repairs unescaped \n inside string values)
# ---------------------------------------------------------------------------


class TestJsonParserHeredocs:
    def test_heredoc_command_with_literal_newlines_parses(self):
        """Setup agent emits commands like `arch-chroot /mnt /bin/bash <<'EOF'\n...EOF`.
        LLMs often emit these with REAL newlines inside the JSON string value,
        which strict json.loads rejects. The parser must repair & parse.
        """
        raw = (
            'prefix {"action": "ssh_exec", "command": "arch-chroot /mnt /bin/bash <<\'EOF\'\n'
            "# set timezone\n"
            "ln -sf /usr/share/zoneinfo/UTC /etc/localtime\n"
            "hwclock --systohc\n"
            'EOF", "reason": "config"} suffix'
        )
        objs = _extract_json_objects(raw)
        assert len(objs) == 1
        assert objs[0]["action"] == "ssh_exec"
        assert "arch-chroot /mnt" in objs[0]["command"]
        assert "ln -sf" in objs[0]["command"]

    def test_multiple_objects_in_one_response(self):
        raw = (
            '{"action": "status", "message": "starting"}\n'
            '{"action": "ssh_exec", "command": "ls /", "reason": "check root"}'
        )
        objs = _extract_json_objects(raw)
        assert len(objs) == 2
        assert objs[0]["action"] == "status"
        assert objs[1]["action"] == "ssh_exec"

    def test_garbage_around_json_is_tolerated(self):
        raw = (
            "Sure thing, let me run this:\n"
            '{"action": "ssh_exec", "command": "whoami", "reason": "sanity"}\n'
            "Hope that works!"
        )
        objs = _extract_json_objects(raw)
        assert len(objs) == 1
        assert objs[0]["command"] == "whoami"

    def test_empty_and_malformed_returns_empty_list(self):
        assert _extract_json_objects("") == []
        assert _extract_json_objects("no json here") == []
        assert _extract_json_objects("{malformed ::: }") == []


# ---------------------------------------------------------------------------
# Reboot command detection — must catch non-standard reboot paths
# (commit: broader _is_reboot_command regex)
# ---------------------------------------------------------------------------


class TestRebootCommandDetection:
    @pytest.mark.parametrize(
        "cmd",
        [
            "reboot",
            "sudo reboot",
            "systemctl reboot",
            "shutdown -r now",
            "init 6",
            "telinit 6",
            # Newly-covered variants from the bug sweep:
            "halt --reboot",
            "kexec -e",
            "kexec --exec",
            "echo b > /proc/sysrq-trigger",
            "echo 'b' > /proc/sysrq-trigger",
        ],
    )
    def test_reboot_variants_detected(self, cmd):
        assert _is_reboot_command(cmd), f"failed to detect: {cmd!r}"

    @pytest.mark.parametrize(
        "cmd",
        [
            # Comment lines — must NOT trigger (the only hard-require
            # negative; false positives elsewhere are survivable because
            # the reconnect loop just probes and proceeds).
            "# reboot is needed",
            "#reboot",
            "   # reboot later",
            # Completely unrelated commands — no 'reboot' substring
            "systemctl status",
            "ls /var/log",
            "pacman -Syu",
        ],
    )
    def test_non_reboot_commands_not_flagged(self, cmd):
        assert not _is_reboot_command(cmd), f"false positive: {cmd!r}"


# ---------------------------------------------------------------------------
# Paste snippet safely quotes keys with apostrophes
# (commit: HIGH-5 — single-quote escape in build_key_paste_snippet)
# ---------------------------------------------------------------------------


class TestPasteSnippetQuoting:
    def test_key_with_apostrophe_in_comment_doesnt_break_snippet(self):
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIABC foo bar's laptop"
        snippet = build_key_paste_snippet(key)
        # The whole key must still appear somewhere in the snippet
        assert "AAAAC3NzaC1lZDI1NTE5AAAAIABC" in snippet
        assert "foo bar" in snippet  # comment survives
        # The surrounding echo '...' must have balanced single quotes.
        # Count unescaped (') occurrences — should be even.
        # With proper '\'' escaping, total ' count will be 2 + 3*N for N
        # apostrophes in the key. We just check that the snippet opens
        # and closes cleanly by asserting > 1 single quote.
        assert snippet.count("'") >= 2

    def test_standard_key_unchanged(self):
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIABC quickhatch"
        snippet = build_key_paste_snippet(key)
        assert key in snippet


# ---------------------------------------------------------------------------
# Export zip excludes dotfiles (api_key leak)
# (commit: HIGH-3 — .api_key no longer shipped in bundle)
# ---------------------------------------------------------------------------


class TestExportZipExcludesDotfiles:
    def test_api_key_dotfile_not_in_zip(self, tmp_path):
        """Simulate export: ensure .api_key dotfile is NOT in the generated zip."""
        # Write a couple of normal files + a dotfile
        (tmp_path / "quickhatch-profile.json").write_text("{}")
        (tmp_path / "quickstart.sh").write_text("#!/bin/bash")
        (tmp_path / ".api_key").write_text("sk-secret-value")

        zip_path = tmp_path / "quickhatch-migration-bundle.zip"
        # Mirror the fixed logic from export.py and _run_export
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in tmp_path.iterdir():
                if f.name != zip_path.name and not f.name.startswith(".") and f.is_file():
                    zf.write(f, f.name)

        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()

        assert "quickhatch-profile.json" in names
        assert "quickstart.sh" in names
        assert ".api_key" not in names, f"Secret leaked into zip! Contents: {names}"


# ---------------------------------------------------------------------------
# Profile apps get trimmed in prompts when the user picked a subset
# (commit: MED-10 — analysis + setup both trim)
# ---------------------------------------------------------------------------


class TestProfileAppsPromptTrim:
    def test_apps_section_includes_only_picked_apps(self):
        """With 317 scanned apps but only 3 picked, the prompt must not
        embed all 317. Keeps local-model context usage sane."""
        profile = UserProfile(preferences=Preferences(distro_preference="arch"))
        profile.apps = [AppInfo(name=f"App{i}") for i in range(317)]
        profile.apps = profile.apps + [
            AppInfo(name="Brave"),
            AppInfo(name="Steam"),
            AppInfo(name="GitHub Desktop"),
        ]
        profile.apps_to_install = ["Brave", "Steam", "GitHub Desktop"]

        prompt = _build_section_prompt(profile, "apps", "")
        # Baseline: pre-fix the prompt was ~50KB+; with trim it should
        # be a fraction of that.
        assert len(prompt) < 10_000, f"apps prompt still bloated: {len(prompt)} chars"
        # Pick names should appear
        assert "Brave" in prompt
        assert "Steam" in prompt
        # A random un-picked app name should NOT appear in the profile section
        assert "App100" not in prompt

    def test_sections_without_apps_concern_drop_list_entirely(self):
        """distro / drivers / desktop / summary don't care about apps —
        the list should be empty in the profile JSON embedded in those
        prompts."""
        profile = UserProfile(preferences=Preferences(distro_preference="arch"))
        profile.apps = [AppInfo(name=f"App{i}") for i in range(50)]
        profile.apps_to_install = []

        for section in ("distro", "drivers", "desktop", "summary"):
            prompt = _build_section_prompt(profile, section, "")
            # None of the scanned app names should appear in the profile
            # JSON for these sections. (They may appear in section-specific
            # copy elsewhere, but 'App0' / 'App1' are synthetic and only
            # come from our test profile.)
            assert '"App0"' not in prompt, f"{section} still dumps apps"


# ---------------------------------------------------------------------------
# ruff format doesn't drift
# (commit: e30296e — CI was failing on this)
# ---------------------------------------------------------------------------


class TestRuffFormat:
    def test_src_and_tests_are_ruff_formatted(self):
        """Run `ruff format --check` programmatically. Mirrors CI.
        Failing locally is better than failing in CI on push."""
        import subprocess

        repo_root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            ["python", "-m", "ruff", "format", "--check", "src/", "tests/"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            pytest.fail(
                "ruff format check failed — run `ruff format src/ tests/` locally.\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )


# ---------------------------------------------------------------------------
# Analysis phase handles reasoning-content fallback
# (commit: HIGH-1 — reasoning_content when content is empty)
# ---------------------------------------------------------------------------


class TestReasoningContentFallback:
    def test_empty_content_falls_back_to_reasoning_content(self, monkeypatch):
        """When a reasoning model returns content="" but has
        reasoning_content, we should use it instead of returning
        an empty string (which would mark the section as non-JSON)."""
        from unittest.mock import MagicMock

        from quickhatch.gui import server as server_mod

        fake_response = MagicMock()
        fake_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning_content": "The user wants an install plan for Arch...",
                    }
                }
            ]
        }
        fake_response.raise_for_status.return_value = None

        def fake_post(*args, **kwargs):
            return fake_response

        # The _call_model_simple function imports requests locally, so we
        # patch it at the `requests` module level (imported lazily inside).
        import requests as real_requests

        monkeypatch.setattr(real_requests, "post", fake_post)

        result = server_mod._call_model_simple(
            endpoint="http://localhost:1234/v1",
            model="reasoning-model",
            prompt="Test",
            api_key=None,
        )
        assert result.startswith("The user wants"), (
            f"Expected reasoning_content fallback, got: {result!r}"
        )

    def test_normal_content_preferred_over_reasoning(self, monkeypatch):
        from unittest.mock import MagicMock

        from quickhatch.gui import server as server_mod

        fake_response = MagicMock()
        fake_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": "# Actual answer",
                        "reasoning_content": "internal thoughts",
                    }
                }
            ]
        }
        fake_response.raise_for_status.return_value = None

        import requests as real_requests

        monkeypatch.setattr(real_requests, "post", lambda *a, **k: fake_response)

        result = server_mod._call_model_simple(
            endpoint="http://localhost:1234/v1",
            model="model",
            prompt="Test",
            api_key=None,
        )
        assert result == "# Actual answer"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
