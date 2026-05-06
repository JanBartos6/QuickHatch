"""Tests for recipe_apply_block — the concrete apply instructions
injected into prompts when the user pins a gallery recipe.
"""

from __future__ import annotations

import pytest

from quickhatch.ai.recipe_apply import recipe_apply_block


class TestRecipeApplyBlock:
    def test_empty_picks_returns_empty(self):
        assert recipe_apply_block([]) == ""
        assert recipe_apply_block(None) == ""

    def test_unknown_id_returns_empty(self):
        assert recipe_apply_block(["nope-not-a-real-id"]) == ""

    def test_known_id_produces_actionable_block(self):
        # Pick any real permissive entry from gallery.json; unknown-license
        # recipes are intentionally not auto-applied.
        block = recipe_apply_block(["unixporn-1kcebee"])
        assert block  # non-empty
        assert "APPLY THIS GALLERY RECIPE" in block
        # Concrete DE-commands must be present so the agent knows how
        # to apply regardless of which DE the user picked.
        assert "kwriteconfig6" in block
        assert "gsettings" in block
        # The recipe's theme + DE should be visible to the agent.
        assert "plasma" in block.lower() or "kde" in block.lower()

    def test_unknown_license_recipe_returns_empty(self):
        assert recipe_apply_block(["unixporn-r5ot7x"]) == ""

    def test_first_pick_wins(self):
        """When multiple picks are supplied, only the first is applied
        (the prompt claims 'the first pinned recipe will be applied')."""
        only_first = recipe_apply_block(["unixporn-1kcebee"])
        combined = recipe_apply_block(["unixporn-1kcebee", "hyprland-1b4izs2"])
        assert only_first == combined


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
