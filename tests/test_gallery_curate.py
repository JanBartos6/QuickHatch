"""Tests for the gallery curation layer (normalize/dedupe/cap)."""

from __future__ import annotations

import pytest

from quickhatch.gallery.curate import (
    _FONT_MONO_ALIASES,
    _FONT_UI_ALIASES,
    _ICON_ALIASES,
    _THEME_ALIASES,
    _normalize_token,
    build_digest,
    cap_with_diversity,
    dedupe_by_repo,
    merge_descriptions,
    normalize_entry,
    quality_score,
)


class TestNormalizeToken:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Catppuccin Mocha", "catppuccin-mocha"),
            ("catppuccin-mocha", "catppuccin-mocha"),
            ("TokyoNight Moon", "tokyonight-moon"),
            ("Tokyo-Night", "tokyonight"),
            ("Rose Pine", "rose-pine"),
            ("Rosé Pine", "rose-pine"),
            ("Gruvbox Dark", "gruvbox-dark"),
            ("catppuccin-gtk-theme", "catppuccin"),
            ("catppuccin-mocha-lavender-dark", "catppuccin-mocha-lavender-dark"),
        ],
    )
    def test_theme_normalization(self, raw, expected):
        assert _normalize_token(raw, _THEME_ALIASES) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Papirus", "papirus"),
            ("Papirus-Dark", "papirus-dark"),
            ("papirus-icon-theme", "papirus"),
            ("Tela Circle", "tela-circle"),
            ("Bibata Modern Ice", "bibata-modern-ice"),
        ],
    )
    def test_icon_normalization(self, raw, expected):
        assert _normalize_token(raw, _ICON_ALIASES) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("JetBrainsMono", "JetBrainsMono"),
            ("jetbrains-mono", "JetBrainsMono"),
            ("JetBrainsMono Nerd Font", "JetBrainsMono Nerd Font"),
            ("Iosevka Nerd Font", "Iosevka Nerd Font"),
            ("FiraCode", "FiraCode"),
            ("Fira Code", "FiraCode"),
            ("CaskaydiaCove Nerd Font", "CaskaydiaCove Nerd Font"),
        ],
    )
    def test_font_mono_normalization(self, raw, expected):
        assert _normalize_token(raw, _FONT_MONO_ALIASES) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Inter", "Inter"),
            ("SF Pro", "SF Pro"),
            ("SF Pro Display", "SF Pro Display"),
            ("Adwaita Sans", "Adwaita Sans"),
        ],
    )
    def test_font_ui_normalization(self, raw, expected):
        assert _normalize_token(raw, _FONT_UI_ALIASES) == expected

    def test_empty_and_none(self):
        assert _normalize_token(None, _THEME_ALIASES) is None
        assert _normalize_token("", _THEME_ALIASES) is None
        assert _normalize_token("  ", _THEME_ALIASES) is None


class TestNormalizeEntry:
    def test_normalizes_all_token_fields(self):
        e = normalize_entry(
            {
                "theme": "Catppuccin Mocha",
                "icons": "Papirus-Dark",
                "font_ui": "Inter",
                "font_mono": "jetbrains-mono",
                "dotfiles_url": "https://github.com/foo/bar/tree/main",
            }
        )
        assert e["theme"] == "catppuccin-mocha"
        assert e["icons"] == "papirus-dark"
        assert e["font_ui"] == "Inter"
        assert e["font_mono"] == "JetBrainsMono"
        assert e["dotfiles_url"] == "https://github.com/foo/bar"


class TestDedupeByRepo:
    def test_merges_two_posts_for_same_repo(self):
        entries = [
            {
                "id": "unixporn-a",
                "dotfiles_url": "https://github.com/foo/dots",
                "source": "r/unixporn",
                "source_url": "https://reddit.com/r/unixporn/a",
                "author": "u/one",
                "upvotes": 1000,
            },
            {
                "id": "hyprland-b",
                "dotfiles_url": "https://github.com/foo/dots",
                "source": "r/hyprland",
                "source_url": "https://reddit.com/r/hyprland/b",
                "author": "u/two",
                "upvotes": 500,
            },
        ]
        result = dedupe_by_repo(entries)
        assert len(result) == 1
        assert result[0]["id"] == "unixporn-a"  # higher upvotes wins
        assert len(result[0]["additional_sources"]) == 1
        assert result[0]["additional_sources"][0]["source"] == "r/hyprland"

    def test_no_merge_when_unique(self):
        entries = [
            {"id": "a", "dotfiles_url": "https://github.com/x/a", "upvotes": 10},
            {"id": "b", "dotfiles_url": "https://github.com/x/b", "upvotes": 20},
        ]
        result = dedupe_by_repo(entries)
        assert len(result) == 2


class TestCapWithDiversity:
    def test_under_cap_returns_all(self):
        entries = [
            {"id": f"h{i}", "de": "hyprland", "upvotes": i, "extraction_confidence": "high"}
            for i in range(5)
        ]
        result = cap_with_diversity(entries, cap=10)
        assert len(result) == 5

    def test_caps_and_preserves_de_diversity(self):
        entries: list[dict] = []
        # 100 hyprland entries (would otherwise dominate) + 10 i3 + 5 gnome
        for i in range(100):
            entries.append(
                {
                    "id": f"h{i}",
                    "de": "hyprland",
                    "upvotes": i * 10,
                    "extraction_confidence": "high",
                }
            )
        for i in range(10):
            entries.append(
                {"id": f"i{i}", "de": "i3", "upvotes": 500, "extraction_confidence": "high"}
            )
        for i in range(5):
            entries.append(
                {"id": f"g{i}", "de": "gnome", "upvotes": 2000, "extraction_confidence": "high"}
            )
        result = cap_with_diversity(entries, cap=30)
        assert len(result) <= 30
        des = {e["de"] for e in result}
        assert "hyprland" in des
        assert "i3" in des
        assert "gnome" in des


class TestMergeDescriptions:
    def test_attaches_description_and_best_for(self):
        entries = [{"id": "a"}, {"id": "b"}]
        descriptions = {
            "a": {
                "description": "A balanced developer rice.",
                "best_for": ["coding", "developer tools"],
            }
        }
        result = merge_descriptions(entries, descriptions)
        assert result[0]["description"] == "A balanced developer rice."
        assert result[0]["best_for"] == ["coding", "developer tools"]
        assert "description" not in result[1]


class TestQualityScore:
    def test_high_confidence_beats_low(self):
        base = {"upvotes": 100, "staleness_score": 0.1}
        high = quality_score({**base, "extraction_confidence": "high"})
        low = quality_score({**base, "extraction_confidence": "low"})
        assert high > low

    def test_fresh_beats_stale(self):
        base = {"upvotes": 100, "extraction_confidence": "high"}
        fresh = quality_score({**base, "staleness_score": 0.1})
        stale = quality_score({**base, "staleness_score": 0.9})
        assert fresh > stale

    def test_richer_data_boosts_score(self):
        base = {
            "upvotes": 100,
            "extraction_confidence": "high",
            "staleness_score": 0.1,
        }
        bare = quality_score(base)
        rich = quality_score(
            {**base, "theme": "nord", "icons": "papirus", "font_mono": "JetBrainsMono"}
        )
        assert rich > bare


class TestBuildDigest:
    def test_digest_drops_heavy_fields(self):
        entry = {
            "id": "x",
            "de": "hyprland",
            "theme": "catppuccin-mocha",
            "description": "Nice",
            "packages": {"arch": ["a", "b"]},  # should be filtered out
            "palette": ["#1e1e2e", "#cba6f7"],
            "components": {"panel": "waybar"},
        }
        d = build_digest(entry)
        assert d["id"] == "x"
        assert d["de"] == "hyprland"
        assert d["components"] == {"panel": "waybar"}
        assert "packages" not in d  # not in keep list
