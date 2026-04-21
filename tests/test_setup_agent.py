"""Tests for _run_setup_agent — the agentic SSH loop.

SSH calls, model calls, and HTTP POSTs are all mocked. These tests cover
the control flow of the loop: SSH connectivity check, parsing multi-JSON
actions, executing commands, handling "done", honoring stop requests,
and iteration limits.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from quickhatch.bridge.server import BridgeState
from quickhatch.config import AIConfig, UserProfile
from quickhatch.gui.server import GUIState, _resolve_setup_cmd_timeout, _run_setup_agent


@pytest.fixture
def ready_state():
    """A GUIState with the minimum config needed for _run_setup_agent to proceed."""
    p = UserProfile()
    p.ai_config = AIConfig(
        provider="local_lmstudio",
        endpoint="http://localhost:1234/v1",
        model_name="test-model",
    )
    p.linux_ip = "127.0.0.1:2222"
    p.linux_user = "root"
    b = BridgeState(p, persist_path=None)
    return GUIState(p, b)


def _make_ssh_mock(*responses):
    """Build an _ssh_exec replacement that returns a fixed sequence of (rc, stdout, stderr)."""
    it = iter(responses)

    def mock(host, user, cmd, key_path=None, timeout=120, port=22):
        try:
            return next(it)
        except StopIteration:
            return (0, "", "")

    return mock


def _make_model_mock(*responses):
    """Build a fake LLM that returns each string in turn."""
    it = iter(responses)

    class FakeResponse:
        # status_code now consulted by _http_post_with_retry — int, not
        # method, so the comparator works.
        status_code = 200

        def __init__(self, text):
            self._text = text

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": self._text}}]}

        @property
        def headers(self):
            return {}

        @property
        def text(self):
            return ""

    def mock_post(url, json=None, headers=None, timeout=300, **kwargs):
        try:
            text = next(it)
        except StopIteration:
            text = '{"action": "done", "summary": "out of scripted responses"}'
        return FakeResponse(text)

    return mock_post


class TestSetupAgentBasics:
    def test_missing_ip_short_circuits(self, ready_state):
        ready_state.profile.linux_ip = ""
        # Should not raise; should add an error message
        _run_setup_agent(ready_state, 0)
        msgs = [m["content"] for m in ready_state.bridge.messages]
        assert any("No Linux IP" in m for m in msgs)

    def test_ssh_failure_aborts_gracefully(self, ready_state):
        ssh_mock = _make_ssh_mock((255, "", "Connection refused"))
        with patch("quickhatch.gui.server._ssh_exec", ssh_mock):
            _run_setup_agent(ready_state, 0)
        msgs = [m["content"] for m in ready_state.bridge.messages]
        assert any("SSH connection failed" in m for m in msgs)


class TestAgenticLoop:
    def test_model_done_ends_loop_immediately(self, ready_state):
        """Model says 'done' on first turn; no commands should be run."""
        ssh_mock = _make_ssh_mock(
            (0, "QuickHatch connected!", ""),  # initial connectivity check
            (0, "Arch Linux", ""),  # gather system info
        )
        post_mock = _make_model_mock('{"action": "done", "summary": "Nothing to do."}')
        with patch("quickhatch.gui.server._ssh_exec", ssh_mock):
            with patch("requests.post", post_mock):
                _run_setup_agent(ready_state, 0)

        msgs = [m["content"] for m in ready_state.bridge.messages]
        assert any("Finished" in m for m in msgs)

    def test_ssh_exec_action_runs_command(self, ready_state):
        """Model asks for pacman -Sy, agent runs it, sees success, then says done."""
        ssh_calls = []

        def tracking_ssh(host, user, cmd, key_path=None, timeout=120, port=22):
            ssh_calls.append(cmd)
            if "echo 'QuickHatch" in cmd:
                return (0, "QuickHatch connected!", "")
            if "/etc/os-release" in cmd:
                return (0, "Arch Linux", "")
            if "pacman" in cmd:
                return (0, "OK", "")
            return (0, "", "")

        responses = [
            '{"action": "ssh_exec", "command": "pacman -Sy --noconfirm", "reason": "sync db"}',
            '{"action": "done", "summary": "Updated package db."}',
        ]
        post_mock = _make_model_mock(*responses)
        with patch("quickhatch.gui.server._ssh_exec", side_effect=tracking_ssh):
            with patch("requests.post", post_mock):
                _run_setup_agent(ready_state, 0)

        # One of the SSH calls was the pacman command
        assert any("pacman -Sy --noconfirm" in c for c in ssh_calls)

    def test_ssh_exec_uses_30_minute_default_timeout(self, ready_state):
        seen = []

        def tracking_ssh(host, user, cmd, key_path=None, timeout=120, port=22):
            seen.append((cmd, timeout))
            if "echo 'QuickHatch" in cmd:
                return (0, "QuickHatch connected!", "")
            if "/etc/os-release" in cmd:
                return (0, "Arch Linux", "")
            if cmd == "ls /tmp":
                return (0, "ok", "")
            return (0, "", "")

        responses = [
            '{"action": "ssh_exec", "command": "ls /tmp", "reason": "check temp dir"}',
            '{"action": "done", "summary": "done"}',
        ]
        with patch("quickhatch.gui.server._ssh_exec", side_effect=tracking_ssh):
            with patch("requests.post", _make_model_mock(*responses)):
                _run_setup_agent(ready_state, 0)

        timeout_for_ls = next(timeout for cmd, timeout in seen if cmd == "ls /tmp")
        assert timeout_for_ls == 1800

    def test_ssh_exec_honors_timeout_override(self, ready_state):
        seen = []

        def tracking_ssh(host, user, cmd, key_path=None, timeout=120, port=22):
            seen.append((cmd, timeout))
            if "echo 'QuickHatch" in cmd:
                return (0, "QuickHatch connected!", "")
            if "/etc/os-release" in cmd:
                return (0, "Arch Linux", "")
            if "yay -S" in cmd:
                return (0, "ok", "")
            return (0, "", "")

        responses = [
            '{"action": "ssh_exec", "command": "yay -S --noconfirm catppuccin-gtk-theme-mocha", "reason": "install theme", "timeout_seconds": 7200}',
            '{"action": "done", "summary": "done"}',
        ]
        with patch("quickhatch.gui.server._ssh_exec", side_effect=tracking_ssh):
            with patch("requests.post", _make_model_mock(*responses)):
                _run_setup_agent(ready_state, 0)

        timeout_for_yay = next(timeout for cmd, timeout in seen if "yay -S" in cmd)
        assert timeout_for_yay == 7200

    def test_multiple_json_objects_in_one_response(self, ready_state):
        """Gemma-style: status + ssh_exec emitted together — both should be processed."""
        ssh_calls = []

        def tracking_ssh(host, user, cmd, key_path=None, timeout=120, port=22):
            ssh_calls.append(cmd)
            if "echo 'QuickHatch" in cmd:
                return (0, "connected", "")
            if "/etc/os-release" in cmd:
                return (0, "Arch Linux", "")
            return (0, "", "")

        combined = (
            '{"action": "status", "message": "About to update..."}\n'
            '{"action": "ssh_exec", "command": "pacman -Syu", "reason": "update"}'
        )
        responses = [combined, '{"action": "done", "summary": "done"}']
        with patch("quickhatch.gui.server._ssh_exec", side_effect=tracking_ssh):
            with patch("requests.post", _make_model_mock(*responses)):
                _run_setup_agent(ready_state, 0)

        msgs = [m["content"] for m in ready_state.bridge.messages]
        # Status message posted
        assert any("About to update" in m for m in msgs)
        # Command executed
        assert any("pacman -Syu" in c for c in ssh_calls)

    def test_stop_request_exits_loop(self, ready_state):
        """Setting stop_requested mid-loop makes the agent exit on next iteration."""

        def tracking_ssh(host, user, cmd, key_path=None, timeout=120, port=22):
            if "echo 'QuickHatch" in cmd:
                return (0, "connected", "")
            if "/etc/os-release" in cmd:
                return (0, "Arch Linux", "")
            return (0, "", "")

        # Flip the stop flag on the first model call
        call_count = {"n": 0}

        class StopResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "choices": [{"message": {"content": '{"action":"ssh_exec","command":"ls"}'}}]
                }

        def flip_and_respond(url, json=None, headers=None, timeout=300, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Before first model call returns, set stop
                ready_state.bridge.request_stop()
            return StopResp()

        with patch("quickhatch.gui.server._ssh_exec", side_effect=tracking_ssh):
            with patch("requests.post", flip_and_respond):
                _run_setup_agent(ready_state, 0)

        msgs = [m["content"] for m in ready_state.bridge.messages]
        assert any(m == "Stopped." for m in msgs)


class TestFeedbackLoop:
    def test_command_failure_feeds_error_back_to_model(self, ready_state):
        """If a command fails, the model sees the exit code + stderr."""
        seen_prompts = []

        def tracking_ssh(host, user, cmd, key_path=None, timeout=120, port=22):
            if "echo 'QuickHatch" in cmd:
                return (0, "connected", "")
            if "/etc/os-release" in cmd:
                return (0, "Arch Linux", "")
            return (1, "", "error: not enough free disk space")

        class FakeResp:
            status_code = 200
            headers = {}
            text = ""

            def __init__(self, text):
                self._text = text

            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": self._text}}]}

        responses = iter(
            [
                '{"action":"ssh_exec","command":"pacman -Syu","reason":"update"}',
                '{"action":"done","summary":"saw the error"}',
            ]
        )

        def capturing_post(url, json=None, headers=None, timeout=300, **kw):
            if url and "/chat/completions" in url and json:
                seen_prompts.append(json.get("messages", []))
            try:
                return FakeResp(next(responses))
            except StopIteration:
                return FakeResp('{"action":"done","summary":"end"}')

        with patch("quickhatch.gui.server._ssh_exec", side_effect=tracking_ssh):
            with patch("requests.post", capturing_post):
                _run_setup_agent(ready_state, 0)

        # The second model call's messages should include the failure output
        assert len(seen_prompts) >= 2
        second_call_messages = seen_prompts[1]
        concat = "\n".join(
            m.get("content", "") if isinstance(m.get("content"), str) else ""
            for m in second_call_messages
        )
        assert "Exit code: 1" in concat or "not enough free disk space" in concat


class TestSetupTimeoutResolution:
    def test_long_install_commands_default_to_one_hour(self):
        assert (
            _resolve_setup_cmd_timeout(
                "yay -S --noconfirm catppuccin-gtk-theme-mocha", {"action": "ssh_exec"}
            )
            == 3600
        )

    def test_timeout_override_is_clamped(self):
        assert _resolve_setup_cmd_timeout("echo hi", {"timeout_seconds": 5}) == 30
        assert _resolve_setup_cmd_timeout("echo hi", {"timeout_seconds": 999999}) == 21600


class TestRebootHandoff:
    def test_reboot_uses_live_user_then_reconnects_as_created_user(self, ready_state):
        ssh_calls = []
        reconnect_users = []

        def tracking_ssh(host, user, cmd, key_path=None, timeout=120, port=22):
            ssh_calls.append((user, cmd))
            if "echo 'QuickHatch connected!'" in cmd:
                return (0, "QuickHatch connected!", "")
            if "/etc/os-release" in cmd and "===USER===" in cmd:
                return (0, "Arch Linux", "")
            if "reboot" in cmd:
                return (255, "", "Connection closed")
            if "/etc/sudoers.d/99-quickhatch-nopasswd" in cmd:
                return (0, "", "")
            if "cat /etc/os-release 2>/dev/null; echo '===USER==='; whoami; id" in cmd:
                return (0, "NAME=Alpine\n===USER===\nalpine\nuid=1000(alpine)", "")
            return (0, "", "")

        def fake_wait(
            host, user, key_path, port=22, timeout=None, interval=5, stop_flag_getter=None
        ):
            reconnect_users.append(user)
            return True

        responses = [
            '{"action":"ssh_exec","command":"chroot /mnt /bin/sh <<\'EOF\'\\nadduser -D -G wheel alpine\\nEOF","reason":"create login user"}',
            '{"action":"ssh_exec","command":"reboot","reason":"boot installed system"}',
            '{"action":"done","summary":"post reboot complete"}',
        ]

        with patch("quickhatch.gui.server._ssh_exec", side_effect=tracking_ssh):
            with patch("quickhatch.gui.server._smoke_test_preboot_script", return_value=None):
                with patch(
                    "quickhatch.gui.server._detect_live_env", return_value=(True, "LIVE:test")
                ):
                    with patch(
                        "quickhatch.gui.server._run_pre_reboot_checks",
                        return_value=([], ["[ok] pass"]),
                    ):
                        with patch(
                            "quickhatch.gui.server._wait_for_ssh_reconnect", side_effect=fake_wait
                        ):
                            with patch("requests.post", _make_model_mock(*responses)):
                                _run_setup_agent(ready_state, 0)

        reboot_users = [u for u, cmd in ssh_calls if cmd == "reboot"]
        sudo_users = [u for u, cmd in ssh_calls if "/etc/sudoers.d/99-quickhatch-nopasswd" in cmd]
        fresh_info_users = [
            u
            for u, cmd in ssh_calls
            if "cat /etc/os-release 2>/dev/null; echo '===USER==='; whoami; id" in cmd
        ]
        assert reboot_users == ["root"]
        assert reconnect_users == ["alpine"]
        assert sudo_users == ["alpine"]
        assert fresh_info_users[-1] == "alpine"
        assert ready_state.profile.linux_user == "alpine"
