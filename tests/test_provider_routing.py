"""Regression tests for the provider-routing fix.

The historical bug: every API call used `"anthropic" in endpoint` to
decide whether to use Anthropic's body shape + auth header. A custom
endpoint whose hostname contained 'anthropic' (proxies, mirrors,
typo'd custom OpenAI-compat URLs) would silently route through
api.anthropic.com with the user's key in `x-api-key`. Both a credential
leak and a functional break.

Fix: the wizards now save the EXPLICIT provider label
(api_anthropic / api_openai / api_gemini / api_grok / api_custom /
local_*) end-to-end. The agent code dispatches on that label, not on
endpoint substrings.
"""

from __future__ import annotations

import pytest

from quickhatch.ai.model_list import (
    HOSTED_API_PROVIDERS,
    LOCAL_PROVIDERS,
    VALID_KINDS,
    chat_completions_url,
    is_anthropic,
)


class TestIsAnthropic:
    def test_only_api_anthropic_returns_true(self):
        assert is_anthropic("api_anthropic") is True

    def test_other_api_providers_return_false(self):
        for p in ("api_openai", "api_gemini", "api_grok", "api_custom"):
            assert is_anthropic(p) is False, f"{p} should not be Anthropic"

    def test_local_providers_return_false(self):
        for p in ("local_ollama", "local_lmstudio", "codex"):
            assert is_anthropic(p) is False

    def test_none_and_empty_return_false(self):
        assert is_anthropic(None) is False
        assert is_anthropic("") is False

    def test_endpoint_with_anthropic_substring_does_not_hijack(self):
        """The historical bug: a user-supplied endpoint containing
        'anthropic' caused the substring check to return True. With
        the new label-based check, the provider field is the only
        signal, so a custom endpoint can't hijack the call."""
        # Even if someone names their custom proxy 'anthropic-proxy',
        # is_anthropic only looks at the provider label.
        assert is_anthropic("api_custom") is False


class TestChatCompletionsUrl:
    def test_anthropic_uses_messages_endpoint(self):
        url = chat_completions_url("api_anthropic", "https://api.anthropic.com")
        assert url.endswith("/v1/messages")

    def test_openai_uses_chat_completions(self):
        url = chat_completions_url("api_openai", "https://api.openai.com/v1")
        assert url == "https://api.openai.com/v1/chat/completions"

    def test_gemini_uses_chat_completions(self):
        url = chat_completions_url(
            "api_gemini", "https://generativelanguage.googleapis.com/v1beta/openai"
        )
        assert url.endswith("/v1beta/openai/chat/completions")

    def test_grok_uses_chat_completions(self):
        url = chat_completions_url("api_grok", "https://api.x.ai/v1")
        assert url == "https://api.x.ai/v1/chat/completions"

    def test_lmstudio_uses_chat_completions(self):
        url = chat_completions_url("local_lmstudio", "http://localhost:1234/v1")
        assert url == "http://localhost:1234/v1/chat/completions"

    def test_ollama_appends_v1_when_missing(self):
        """The original Ollama bug: stored endpoint was
        http://localhost:11434 (no /v1), agent posted to
        /chat/completions which returned 404 because Ollama's
        OpenAI-compat is at /v1/chat/completions."""
        url = chat_completions_url("local_ollama", "http://localhost:11434")
        assert url == "http://localhost:11434/v1/chat/completions"

    def test_ollama_does_not_double_v1(self):
        """If a user / future code passes Ollama's endpoint already
        with /v1, we should not produce /v1/v1/chat/completions."""
        url = chat_completions_url("local_ollama", "http://localhost:11434/v1")
        assert url == "http://localhost:11434/v1/chat/completions"

    def test_trailing_slash_normalized(self):
        url = chat_completions_url("api_openai", "https://api.openai.com/v1/")
        assert url == "https://api.openai.com/v1/chat/completions"

    def test_custom_endpoint_with_anthropic_in_hostname_routes_to_custom(self):
        """The bug-fix's whole reason for existing: a custom endpoint
        whose URL contains 'anthropic' must NOT route through
        api.anthropic.com — even though the substring matches."""
        custom = "https://my-anthropic-proxy.internal/v1"
        url = chat_completions_url("api_custom", custom)
        # Goes to the custom endpoint, not api.anthropic.com.
        assert url == "https://my-anthropic-proxy.internal/v1/chat/completions"
        assert "api.anthropic.com" not in url


class TestProviderConstants:
    def test_valid_kinds_are_lowercase_strings(self):
        assert VALID_KINDS == {"anthropic", "openai-compat", "ollama"}

    def test_hosted_api_providers_complete_set(self):
        assert HOSTED_API_PROVIDERS == {
            "api_anthropic",
            "api_openai",
            "api_gemini",
            "api_grok",
            "api_custom",
        }

    def test_local_providers_complete_set(self):
        assert LOCAL_PROVIDERS == {"local_ollama", "local_lmstudio", "codex"}


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
