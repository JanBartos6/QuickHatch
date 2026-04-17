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

    def test_distro_section_hard_respects_user_preference(self, profile):
        """When the user says Arch, the LLM must not suggest EndeavourOS/Manjaro/etc."""
        prompt = _build_section_prompt(profile, "distro", "")
        lower = prompt.lower()
        # The section instruction must explicitly forbid substitution.
        assert "hard rule" in lower
        assert "endeavouros" in lower  # example forbidden redirect is called out
        assert "distro_preference" in lower
        assert "explicit choice is final" in lower

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

    def test_desktop_prompt_mentions_gallery_recipe_when_pinned(self, profile):
        profile.preferences.gallery_picks = ["unixporn-zyx123"]
        prompt = _build_section_prompt(profile, "desktop", "")
        assert "PINNED GALLERY RECIPES" in prompt
        assert "unixporn-zyx123" in prompt
        assert "Recipe `unixporn-zyx123` will be applied" in prompt

    def test_unknown_section_doesnt_crash(self, profile):
        """An unrecognized section name shouldn't raise."""
        prompt = _build_section_prompt(profile, "notreal", "")
        assert isinstance(prompt, str) and len(prompt) > 100


class TestOfflineInstallScript:
    """When another_machine=='no', the install_script section prompt should be
    generated, instructing the LLM to output a self-contained bash script.
    """

    def test_install_script_section_has_bash_output_mandate(self, profile):
        # Not using the section loop (that's in server.py), but the section's
        # prompt text should exist and demand raw bash output.
        prompt = _build_section_prompt(profile, "install_script", "")
        assert "bash script" in prompt.lower()
        # Must instruct the LLM to skip Markdown fences / prose
        assert "only a bash script" in prompt.lower() or "only bash" in prompt.lower()
        # Must reference the disk-confirm safety step
        assert "lsblk" in prompt.lower() or "disk" in prompt.lower()
        # Must reference the first-boot staging for post-install config
        assert "first-boot" in prompt.lower() or "firstboot" in prompt.lower()


class TestInstallPrimerInjection:
    """The distro install primer should be injected into plan/desktop/summary
    prompts, using chosen_distro first then falling back to user preference.
    """

    def test_plan_section_injects_primer_from_user_preference(self, profile):
        profile.preferences.distro_preference = "Arch Linux"
        prompt = _build_section_prompt(profile, "plan", "")
        assert "DISTRO INSTALL PRIMER: Arch Linux" in prompt
        assert "pacstrap" in prompt  # from the family primer body

    def test_desktop_section_injects_primer(self, profile):
        profile.preferences.distro_preference = "fedora"
        prompt = _build_section_prompt(profile, "desktop", "")
        assert "DISTRO INSTALL PRIMER: Fedora Workstation" in prompt
        assert "kickstart" in prompt.lower()

    def test_summary_section_injects_primer(self, profile):
        profile.preferences.distro_preference = "ubuntu"
        prompt = _build_section_prompt(profile, "summary", "")
        assert "DISTRO INSTALL PRIMER: Ubuntu LTS" in prompt

    def test_non_install_sections_skip_primer(self, profile):
        """Distro, apps, and drivers sections don't need install primers."""
        profile.preferences.distro_preference = "Arch Linux"
        for section in ("distro", "apps", "drivers"):
            prompt = _build_section_prompt(profile, section, "")
            assert "DISTRO INSTALL PRIMER" not in prompt, (
                f"Section {section} should not include the install primer"
            )

    def test_chosen_distro_arg_overrides_user_preference(self, profile):
        """If chosen_distro is passed, it wins over the user's stated preference."""
        profile.preferences.distro_preference = "Arch Linux"
        prompt = _build_section_prompt(profile, "plan", "", chosen_distro="Fedora Workstation")
        # Fedora primer present, Arch primer NOT present
        assert "DISTRO INSTALL PRIMER: Fedora Workstation" in prompt
        assert "DISTRO INSTALL PRIMER: Arch Linux" not in prompt

    def test_chosen_distro_none_falls_back_to_user_preference(self, profile):
        profile.preferences.distro_preference = "nixos"
        prompt = _build_section_prompt(profile, "plan", "", chosen_distro=None)
        assert "DISTRO INSTALL PRIMER: NixOS" in prompt

    def test_no_distro_preference_and_no_chosen_distro_skips_primer(self, profile):
        """If neither is set, no primer is injected (graceful degradation)."""
        profile.preferences.distro_preference = None
        prompt = _build_section_prompt(profile, "plan", "", chosen_distro=None)
        assert "DISTRO INSTALL PRIMER" not in prompt

    def test_unknown_distro_skips_primer(self, profile):
        """If the distro name doesn't resolve to a known primer, no block is emitted."""
        prompt = _build_section_prompt(profile, "plan", "", chosen_distro="Banana OS")
        assert "DISTRO INSTALL PRIMER" not in prompt


class TestCuratedLibraryInjection:
    """The curated gallery library should be injected into design-section prompts.

    The harness LLM needs access to the full 100+ recipe reference library
    when recommending a desktop, so it can either pick a known-good design
    or borrow components when the user's ask isn't an exact match.
    """

    def test_desktop_section_includes_library_block(self, profile):
        prompt = _build_section_prompt(profile, "desktop", "")
        assert "CURATED DESIGN LIBRARY" in prompt

    def test_library_block_has_inspiration_framing(self, profile):
        prompt = _build_section_prompt(profile, "desktop", "")
        # The block must frame the library as authoritative reference data
        # AND as inspiration for off-library requests — both matter.
        assert "highly curated" in prompt.lower()
        assert "inspiration" in prompt.lower() or "borrow" in prompt.lower()

    def test_library_includes_real_entries(self, profile):
        prompt = _build_section_prompt(profile, "desktop", "")
        # At least one concrete recipe id should appear in the block.
        assert "`github-" in prompt or "`unixporn-" in prompt or "`hyprland-" in prompt

    def test_summary_section_also_gets_library(self, profile):
        """Summary section may reference the recommended design — it gets the library too."""
        prompt = _build_section_prompt(profile, "summary", "")
        assert "CURATED DESIGN LIBRARY" in prompt

    def test_non_design_sections_skip_library(self, profile):
        """Apps/drivers/plan/distro don't need the library — it would bloat the prompt."""
        for section in ("apps", "drivers", "plan", "distro"):
            prompt = _build_section_prompt(profile, section, "")
            assert "CURATED DESIGN LIBRARY" not in prompt, (
                f"Section {section} should NOT include the library block"
            )


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
