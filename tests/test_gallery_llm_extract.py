"""Tests for LLM-based gallery metadata extraction helpers."""

from __future__ import annotations

import pytest

from quickhatch.gallery.llm_extract import (
    _sanitize_field,
    _strip_images,
    sanitize_llm_result,
)


class TestStripImages:
    def test_markdown_image_becomes_alt_text(self):
        text = "before ![my screenshot](https://example.com/x.png) after"
        assert _strip_images(text) == "before [image: my screenshot] after"

    def test_markdown_image_without_alt(self):
        assert _strip_images("![](a.png)") == "[image: ]"

    def test_html_image_with_alt(self):
        text = '<img src="x.png" alt="preview" width="100">'
        assert _strip_images(text) == "[image: preview]"

    def test_html_image_without_alt(self):
        text = '<img src="x.png" />'
        assert _strip_images(text) == "[image]"

    def test_plain_text_passes_through(self):
        assert _strip_images("no images here") == "no images here"


class TestSanitizeField:
    @pytest.mark.parametrize(
        "good",
        [
            "Inter",
            "JetBrainsMono Nerd Font",
            "catppuccin-mocha",
            "Papirus-Dark",
            "SF Pro",
            "Bibata-Modern-Ice",
        ],
    )
    def test_real_tokens_pass(self, good):
        assert _sanitize_field(good) == good

    @pytest.mark.parametrize(
        "bad",
        [
            ":{",
            "} , {",
            "__proto__",
            "null",
            "None",
            "",
            "N/A",
            "normal",
            "default",
            "system-ui",
            "sans-serif",
            "monospace",
            "true",
            ",,,,",
            "{foo",
            "</script>",
        ],
    )
    def test_hallucinations_become_none(self, bad):
        assert _sanitize_field(bad) is None

    def test_whitespace_and_quotes_stripped(self):
        assert _sanitize_field('  "Catppuccin-Mocha"  ') == "Catppuccin-Mocha"

    def test_none_passes_through(self):
        assert _sanitize_field(None) is None

    def test_non_string_becomes_none(self):
        assert _sanitize_field(42) is None
        assert _sanitize_field([]) is None
        assert _sanitize_field({}) is None

    def test_overlong_strings_dropped(self):
        assert _sanitize_field("a" * 61) is None

    def test_digits_only_dropped(self):
        # No letters, no identity.
        assert _sanitize_field("1234") is None


class TestSanitizeLlmResult:
    def test_none_input_returns_none(self):
        assert sanitize_llm_result(None) is None

    def test_preserves_is_dotfiles_flag(self):
        result = sanitize_llm_result(
            {
                "is_dotfiles": True,
                "rejection_reason": None,
                "de": "hyprland",
                "theme": "catppuccin-mocha",
                "icons": "papirus",
                "cursor": "Bibata",
                "font_ui": "Inter",
                "font_mono": "JetBrainsMono Nerd Font",
                "wallpaper_hint": "aurora",
                "mood": "dark",
                "signature_features": ["waybar", "blur"],
                "confidence": "high",
            }
        )
        assert result is not None
        assert result["is_dotfiles"] is True
        assert result["de"] == "hyprland"
        assert result["font_ui"] == "Inter"
        assert result["font_mono"] == "JetBrainsMono Nerd Font"
        assert result["signature_features"] == ["waybar", "blur"]

    def test_hallucinated_fields_become_none(self):
        result = sanitize_llm_result(
            {
                "is_dotfiles": True,
                "rejection_reason": None,
                "de": "hyprland",
                "theme": ":{",
                "icons": "null",
                "cursor": "__proto__",
                "font_ui": "} , {",
                "font_mono": "settt_mono_font_font-fa" + "a" * 60,
                "wallpaper_hint": "system-ui",
                "mood": "dark",
                "signature_features": [],
                "confidence": "low",
            }
        )
        assert result is not None
        assert result["theme"] is None
        assert result["icons"] is None
        assert result["cursor"] is None
        assert result["font_ui"] is None
        assert result["font_mono"] is None
        assert result["wallpaper_hint"] is None

    def test_signature_features_cleans_garbage_and_caps_at_five(self):
        result = sanitize_llm_result(
            {
                "is_dotfiles": True,
                "rejection_reason": None,
                "de": "hyprland",
                "theme": None,
                "icons": None,
                "cursor": None,
                "font_ui": None,
                "font_mono": None,
                "wallpaper_hint": None,
                "mood": None,
                "signature_features": [
                    "waybar",
                    ":{",  # dropped
                    "blur",
                    "",  # dropped
                    "rounded corners",
                    "null",  # dropped
                    "centered dock",
                    "quickshell",
                    "extra feature that gets dropped due to cap",
                ],
                "confidence": "high",
            }
        )
        assert result is not None
        assert len(result["signature_features"]) == 5
        assert ":{" not in result["signature_features"]
        assert "null" not in result["signature_features"]
        assert "waybar" in result["signature_features"]

    def test_non_list_signature_features_becomes_empty(self):
        result = sanitize_llm_result(
            {
                "is_dotfiles": True,
                "rejection_reason": None,
                "de": "hyprland",
                "theme": None,
                "icons": None,
                "cursor": None,
                "font_ui": None,
                "font_mono": None,
                "wallpaper_hint": None,
                "mood": None,
                "signature_features": "not a list",
                "confidence": "low",
            }
        )
        assert result is not None
        assert result["signature_features"] == []
