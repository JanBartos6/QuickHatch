"""Tests for staged remote setup integration points."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from quickhatch.bridge.server import BridgeState
from quickhatch.config import AIConfig, UserProfile
from quickhatch.gui.server import (
    GUIState,
    _call_setup_model_text,
    _launch_agent,
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
    def test_runtime_prompt_suppresses_harness_search_when_native_search_is_active(self):
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
        assert "Do NOT emit QuickHatch harness lookup actions" in prompt

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
        compat_call.assert_not_called()


class TestSetupAgentWrapper:
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
