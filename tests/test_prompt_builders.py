"""Tests for the prompt-building functions in gui.server."""

from __future__ import annotations

import pytest

from quickhatch.config import Preferences, UserProfile
from quickhatch.gui.server import _build_section_prompt


@pytest.fixture
def profile():
    p = UserProfile()
    p.preferences = Preferences(
        migration_type="fresh_install",
        desktop_look="macos_like",
        icon_style="flat",
        wallpaper_style="dark",
        use_cases=["development", "gaming"],
    )
    return p


class TestBuildSectionPrompt:
    def test_distro_section_includes_section_marker(self, profile):
        prompt = _build_section_prompt(profile, "distro", "")
        assert "SECTION: DISTRO" in prompt.upper()

    def test_apps_section_mentions_table_format(self, profile):
        prompt = _build_section_prompt(profile, "apps", "")
        assert "table" in prompt.lower()

    def test_desktop_section_mentions_activation(self, profile):
        """Desktop must tell the agent to ACTIVATE themes, not just install."""
        prompt = _build_section_prompt(profile, "desktop", "")
        assert "activate" in prompt.lower() or "kdeglobals" in prompt.lower()

    def test_plan_section_includes_user_creation(self, profile):
        """Plans must create a user with wheel group."""
        prompt = _build_section_prompt(profile, "plan", "")
        assert "wheel" in prompt.lower() or "user" in prompt.lower()

    def test_summary_section_is_short(self, profile):
        prompt = _build_section_prompt(profile, "summary", "")
        # The instruction text itself should be compact
        assert "under 200 words" in prompt.lower() or "short" in prompt.lower()

    def test_search_context_is_included(self, profile):
        ctx = "### Search: GPU — 'nvidia rtx 3080 linux'\n- Some relevant link"
        prompt = _build_section_prompt(profile, "distro", ctx)
        assert "nvidia rtx 3080 linux" in prompt

    def test_no_user_feedback_by_default(self, profile):
        prompt = _build_section_prompt(profile, "distro", "")
        assert "USER FEEDBACK" not in prompt

    def test_user_feedback_injected_when_present(self, profile):
        feedback = "Actually I want Arch Linux instead of Pop!_OS"
        prompt = _build_section_prompt(profile, "distro", "", user_feedback=feedback)
        assert "USER FEEDBACK" in prompt
        assert "Arch Linux" in prompt
        # Should come with a strong instruction to follow the feedback
        assert "follow this" in prompt.lower() or "changed their mind" in prompt.lower()

    def test_end_state_principle_is_present(self, profile):
        """Every section prompt should emphasize the working-end-state principle."""
        prompt = _build_section_prompt(profile, "plan", "")
        assert "end-state" in prompt.lower() or "working" in prompt.lower()

    def test_profile_json_included(self, profile):
        prompt = _build_section_prompt(profile, "distro", "")
        assert "USER SYSTEM PROFILE" in prompt
        assert "development" in prompt  # from use_cases

    def test_unknown_section_doesnt_crash(self, profile):
        """An unrecognized section name shouldn't raise."""
        prompt = _build_section_prompt(profile, "notreal", "")
        assert isinstance(prompt, str) and len(prompt) > 100


class TestApiKeyRedaction:
    def test_section_prompt_redacts_api_key(self, profile):
        profile.ai_config.api_key = "sk-sensitive-xyz"
        prompt = _build_section_prompt(profile, "distro", "")
        assert "sensitive-xyz" not in prompt
        assert "REDACTED" in prompt

    def test_section_prompt_handles_no_api_key(self, profile):
        profile.ai_config.api_key = None
        prompt = _build_section_prompt(profile, "distro", "")
        # Should still include the config, just without an api_key value
        assert '"api_key"' in prompt


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
