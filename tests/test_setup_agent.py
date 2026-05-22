"""Tests for staged remote setup integration points."""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

from quickhatch.bridge.server import BridgeState
from quickhatch.config import AIConfig, UserProfile
from quickhatch.gui.server import (
    GUIState,
    _build_setup_repo_search_command,
    _call_setup_model_text,
    _launch_agent,
    _maybe_bootstrap_nopasswd_sudo,
    _run_setup_agent,
    _setup_runtime_prompt_block,
)


@pytest.fixture
def ready_state():
    profile = UserProfile()
    profile.ai_config = AIConfig(
        provider="local_lmstudio",
        endpoint="http://localhost:1234/v1",
        model_name="test-model",
    )
    profile.linux_ip = "127.0.0.1:2222"
    profile.linux_user = "root"
    bridge = BridgeState(profile, persist_path=None)
    return GUIState(profile, bridge)


class TestNativeSearchSetupContract:
    def test_runtime_prompt_allows_harness_lookup_when_native_search_is_active(self):
        prompt = _setup_runtime_prompt_block(
            {
                "package_manager": "pacman",
                "aur_helper": "yay",
                "current_user": "root",
                "is_root": "yes",
                "sudo_nopasswd": "yes",
            },
            False,
            native_search_active=True,
        )

        assert "SEARCH MODE:" in prompt
        assert "provider-native web search is enabled" in prompt.lower()
        assert "LOOKUP ACTIONS:" not in prompt
        assert "Harness lookup actions remain allowed" in prompt
        assert "community_search" in prompt

    def test_setup_model_uses_native_search_for_supported_provider(self):
        config = AIConfig(
            provider="api_openai",
            endpoint="https://api.openai.com/v1",
            model_name="gpt-test",
            api_key="secret",
            native_search=True,
        )
        messages = [
            {"role": "user", "content": "SYSTEM PROMPT"},
            {"role": "assistant", "content": "Previous step"},
            {"role": "user", "content": "Continue"},
        ]

        with patch(
            "quickhatch.gui.server._call_model_with_native_search",
            return_value='{"action":"done","summary":"ok"}',
        ) as native_call:
            with patch("quickhatch.gui.server._post_openai_compat") as compat_call:
                result = _call_setup_model_text(
                    config,
                    config.endpoint or "",
                    config.model_name or "",
                    config.api_key,
                    messages,
                )

        assert result == '{"action":"done","summary":"ok"}'
        native_call.assert_called_once()
        assert native_call.call_args.kwargs["timeout"] == 900
        compat_call.assert_not_called()

    def test_setup_model_passes_custom_timeout_to_native_search(self):
        config = AIConfig(
            provider="api_gemini",
            endpoint="https://generativelanguage.googleapis.com/v1beta/openai",
            model_name="gemini-flash-latest",
            api_key="secret",
            native_search=True,
        )
        messages = [{"role": "user", "content": "Plan"}]

        with patch(
            "quickhatch.gui.server._call_model_with_native_search",
            return_value='{"action":"done","summary":"ok"}',
        ) as native_call:
            result = _call_setup_model_text(
                config,
                config.endpoint or "",
                config.model_name or "",
                config.api_key,
                messages,
                timeout_seconds=45,
            )

        assert result == '{"action":"done","summary":"ok"}'
        assert native_call.call_args.kwargs["timeout"] == 45

    def test_setup_model_passes_custom_timeout_to_openai_compat(self):
        config = AIConfig(
            provider="local_lmstudio",
            endpoint="http://localhost:1234/v1",
            model_name="local-model",
            api_key="",
            native_search=False,
        )
        messages = [{"role": "user", "content": "Plan"}]

        with patch(
            "quickhatch.gui.server._post_openai_compat_timeout",
            return_value='{"action":"done","summary":"ok"}',
        ) as compat_call:
            result = _call_setup_model_text(
                config,
                config.endpoint or "",
                config.model_name or "",
                config.api_key,
                messages,
                timeout_seconds=45,
            )

        assert result == '{"action":"done","summary":"ok"}'
        assert compat_call.call_args.kwargs["timeout"] == 45

    def test_setup_model_prefixes_gemini_gemma_for_openai_compat_chat(self):
        config = AIConfig(
            provider="api_gemini",
            endpoint="https://generativelanguage.googleapis.com/v1beta/openai",
            model_name="gemma-4-31b-it",
            api_key="secret",
            native_search=False,
        )
        messages = [{"role": "user", "content": "Plan"}]

        with patch(
            "quickhatch.gui.server._post_openai_compat_timeout",
            return_value='{"action":"done","summary":"ok"}',
        ) as compat_call:
            result = _call_setup_model_text(
                config,
                config.endpoint or "",
                config.model_name or "",
                config.api_key,
                messages,
                timeout_seconds=45,
            )

        assert result == '{"action":"done","summary":"ok"}'
        assert compat_call.call_args.kwargs["body"]["model"] == "models/gemma-4-31b-it"


class TestSetupRepoSearch:
    def test_pacman_repo_search_refreshes_missing_sync_database(self):
        command = _build_setup_repo_search_command("pacman", "plasma-desktop")

        assert command is not None
        assert "/var/lib/pacman/sync/*.db" in command
        assert "pacman -Sy --noconfirm" in command
        assert "pacman -Ss 'plasma-desktop'" in command

    def test_repo_search_normalizes_command_shaped_model_query(self):
        command = _build_setup_repo_search_command(
            "pacman",
            "pacman -Sy && pacman -Ss papirus-icon-theme | sed -n '1,40p'",
        )

        assert command is not None
        assert "pacman -Ss 'papirus-icon-theme'" in command
        assert "pacman -Ss pacman" not in command
        assert "-Sy &&" not in command


class TestSetupAgentWrapper:
    def test_sudo_bootstrap_uses_stdin_password_and_verifies_nopasswd(self, monkeypatch):
        profile = UserProfile()
        profile.default_account_password = "p'a ss"
        bridge = BridgeState(profile, persist_path=None)
        calls: list[str] = []

        def fake_ssh_exec(host, user, command, key_path, *, timeout, port):
            calls.append(command)
            return 0, "", ""

        monkeypatch.setattr("quickhatch.gui.server._ssh_exec", fake_ssh_exec)

        facts = _maybe_bootstrap_nopasswd_sudo(
            "127.0.0.1",
            "jan",
            None,
            profile,
            bridge,
            facts={"is_root": "no", "sudo_nopasswd": "no"},
        )

        assert facts["sudo_nopasswd"] == "yes"
        assert len(calls) == 1
        assert "sudo -k -S -p ''" in calls[0]
        assert "sudo -n true" in calls[0]
        assert "99-quickhatch-nopasswd" in calls[0]
        assert "jan ALL=(ALL:ALL) NOPASSWD: ALL" in calls[0]

    def test_missing_ip_short_circuits(self, ready_state):
        ready_state.profile.linux_ip = ""

        _run_setup_agent(ready_state, 0)

        msgs = [m["content"] for m in ready_state.bridge.messages]
        assert any("No Linux IP" in m for m in msgs)

    def test_wrapper_delegates_to_orchestrator_and_posts_done(self, ready_state):
        fake_orchestrator = type(
            "FakeOrchestrator",
            (),
            {"__init__": lambda self, runtime: None, "run": lambda self: "staged summary"},
        )

        with patch("quickhatch.gui.server.SetupOrchestrator", fake_orchestrator):
            with patch("requests.post") as post_mock:
                _run_setup_agent(ready_state, 8123)

        post_mock.assert_called_once()
        assert post_mock.call_args.kwargs["json"]["summary"] == "staged summary"

    def test_wrapper_surfaces_orchestrator_failure(self, ready_state):
        class FailingOrchestrator:
            def __init__(self, runtime):
                self.runtime = runtime

            def run(self):
                raise RuntimeError("planner failed")

        with patch("quickhatch.gui.server.SetupOrchestrator", FailingOrchestrator):
            with patch("requests.post") as post_mock:
                _run_setup_agent(ready_state, 8123)

        msgs = [m["content"] for m in ready_state.bridge.messages]
        assert any("planner failed" in m for m in msgs)
        assert "planner failed" in post_mock.call_args.kwargs["json"]["summary"]


class TestAgentLifecycle:
    def test_launch_wrapper_clears_runtime_state_when_setup_thread_exits(self, ready_state):
        with patch("quickhatch.gui.server._run_setup_agent", return_value=None):
            result = _launch_agent(ready_state, 8765, phase="setup")

        assert result["status"] == "ok"
        thread = ready_state._agent_thread
        assert thread is not None
        thread.join(timeout=1)

        assert ready_state._agent_thread is None
        assert ready_state.bridge.agent_connected is False
        assert ready_state.bridge.current_phase == ""

    def test_launch_heals_dead_thread_reference_before_rejecting(self, ready_state):
        dead_thread = threading.Thread(target=lambda: None)
        dead_thread.start()
        dead_thread.join(timeout=1)
        ready_state._agent_thread = dead_thread

        with patch("quickhatch.gui.server._run_setup_agent", return_value=None):
            result = _launch_agent(ready_state, 8765, phase="setup")
            launched_thread = ready_state._agent_thread

        assert result["status"] != "error" or "already running" not in result["message"]
        if launched_thread is not None:
            launched_thread.join(timeout=1)
        assert ready_state._agent_thread is None
