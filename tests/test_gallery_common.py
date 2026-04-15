"""Tests for shared gallery metadata detectors."""

from __future__ import annotations

from quickhatch.gallery.common import detect_de, detect_font, detect_theme


def test_detect_de_avoids_generic_adjectives():
    assert detect_de("This is an awesome setup guide.") is None
    assert detect_de("Configured for AwesomeWM with rofi and picom.") == "awesome"


def test_detect_font_requires_a_real_token_boundary():
    assert detect_font("The printer interface stayed responsive.") is None
    assert detect_font("UI font: Inter") == "inter"


def test_detect_theme_rejects_html_fragment_noise():
    assert detect_theme('theme: dark" />') is None
