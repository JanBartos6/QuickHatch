"""Tests for the sectioned analysis-agent loop."""

from __future__ import annotations

from unittest.mock import patch

from quickhatch.bridge.server import BridgeState
from quickhatch.config import AIConfig, Preferences, UserProfile
from quickhatch.gui.server import (
    GUIState,
    _call_model_analysis,
    _call_openai_with_tools,
    _run_agent_background,
)


class _FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload or {}
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _make_state() -> GUIState:
    profile = UserProfile()
    profile.ai_config = AIConfig(
        provider="local_lmstudio",
        endpoint="http://127.0.0.1:1234/v1",
        model_name="test-model",
    )
    profile.preferences = Preferences(
        migration_type="fresh_install",
        use_cases=["development"],
    )
    bridge = BridgeState(profile, persist_path=None)
    return GUIState(profile, bridge)


class TestAnalysisAgentStructuredSections:
    def test_structured_distro_response_drives_downstream_primer_and_ui_markdown(self):
        gui_state = _make_state()
        suggested: list[dict] = []

        def fake_model(
            endpoint, model, prompt, api_key, bridge, provider=None, use_native_search=False
        ):
            if "SECTION: DISTRO" in prompt:
                return (
                    '{"title":"Recommended Distribution",'
                    '"markdown":"### Recommended Distribution\\n\\n**Fedora Workstation**",'
                    '"metadata":{"recommended_distro":"Fedora Workstation"}}'
                )
            if "SECTION: PLAN" in prompt:
                assert "DISTRO INSTALL PRIMER: Fedora Workstation" in prompt
                return '{"title":"Migration Plan","markdown":"1. Install Fedora Workstation"}'
            if "SECTION: SUMMARY" in prompt:
                return '{"title":"Your Migration at a Glance","markdown":"Done."}'
            return '{"title":"Generic","markdown":"Section body"}'

        def fake_post(url, json=None, timeout=5, **kwargs):
            if url.endswith("/api/suggest"):
                suggested.append(json)
            return _FakeResponse({"status": "ok"})

        with patch("quickhatch.gui.server._call_model_analysis", side_effect=fake_model):
            with patch("requests.post", side_effect=fake_post):
                _run_agent_background(gui_state, 9999)

        distro_post = next(item for item in suggested if item["category"] == "distro")
        assert distro_post["content"] == "### Recommended Distribution\n\n**Fedora Workstation**"
        assert any(
            "Distro resolved: Fedora Workstation" in m["content"] for m in gui_state.bridge.messages
        )

    def test_prefetch_search_context_no_longer_runs_harness_searches(self):
        from quickhatch.gui.server import _prefetch_search_context

        gui_state = _make_state()
        assert _prefetch_search_context(gui_state.profile, gui_state.bridge) == ("", [])

    def test_analysis_native_search_uses_provider_search_path(self):
        gui_state = _make_state()

        with patch(
            "quickhatch.gui.server._call_model_with_native_search",
            return_value="native result",
        ) as native_call:
            with patch("quickhatch.gui.server._call_openai_with_tools") as openai_call:
                result = _call_model_analysis(
                    gui_state.profile.ai_config.endpoint or "",
                    gui_state.profile.ai_config.model_name or "",
                    "SECTION: DISTRO",
                    gui_state.profile.ai_config.api_key,
                    gui_state.bridge,
                    provider="api_openai",
                    use_native_search=True,
                )

        assert result == "native result"
        native_call.assert_called_once()
        openai_call.assert_not_called()

    def test_analysis_prefixes_gemini_gemma_for_openai_compat_chat(self):
        profile = UserProfile()
        bridge = BridgeState(profile, persist_path=None)
        seen: dict[str, object] = {}

        def fake_post(url, json=None, **kwargs):
            seen["url"] = url
            seen["body"] = json
            return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})

        with patch("requests.post", side_effect=fake_post):
            result = _call_openai_with_tools(
                "https://generativelanguage.googleapis.com/v1beta/openai",
                "gemma-4-31b-it",
                [{"role": "user", "content": "Plan"}],
                "secret",
                bridge,
                provider="api_gemini",
            )

        assert result == "ok"
        assert seen["body"]["model"] == "models/gemma-4-31b-it"
