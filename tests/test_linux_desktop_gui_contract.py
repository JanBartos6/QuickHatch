from __future__ import annotations

import pytest

from quickhatch.setup.audit import audit_stage
from quickhatch.setup.types import (
    DesktopDesignState,
    DesktopRuntimeState,
    SetupFacts,
    SetupStage,
)


def _facts(design: DesktopDesignState) -> SetupFacts:
    return SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="quickhatch",
        login_user="quickhatch",
        display_manager="sddm",
        display_manager_enabled=True,
        display_manager_active=True,
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
        desktop_design=design,
    )


def _strict_state(*elements: str, **expected: str) -> dict[str, object]:
    return {
        "persisted_config_written": True,
        "desktop_design_attempted": True,
        "strict_desktop_design_contract": True,
        "required_desktop_design_elements": list(elements),
        **expected,
    }


@pytest.mark.parametrize("element", ["theme", "icons", "cursor", "wallpaper", "effects"])
def test_strict_linux_desktop_gui_contract_requires_expected_value_per_element(
    element: str,
) -> None:
    facts = _facts(
        DesktopDesignState(
            environment="KDE",
            kde_color_scheme="BreezeDark",
            kde_icon_theme="Papirus",
            kde_cursor_theme="Bibata-Modern-Ice",
            kde_wallpaper="file:///home/quickhatch/Pictures/wallpaper.jpg",
            kde_wobbly_windows="true",
        )
    )

    result = audit_stage(
        SetupStage.DESKTOP,
        facts,
        stage_state=_strict_state(element),
    )

    assert not result.passed
    assert any("no concrete expected target value" in blocker for blocker in result.blockers)
    assert result.details["required_desktop_design_elements"] == [element]


def test_strict_linux_desktop_gui_contract_accepts_matching_kde_elements() -> None:
    facts = _facts(
        DesktopDesignState(
            environment="KDE",
            kde_color_scheme="BreezeDark",
            kde_icon_theme="Papirus-Dark",
            kde_cursor_theme="Bibata-Modern-Ice",
            kde_wallpaper="file:///home/quickhatch/Pictures/wallpaper.jpg",
            kde_wobbly_windows="true",
            kde_magic_lamp="true",
        )
    )

    result = audit_stage(
        SetupStage.DESKTOP,
        facts,
        stage_state=_strict_state(
            "theme",
            "icons",
            "cursor",
            "wallpaper",
            "effects",
            expected_kde_color_scheme="Breeze Dark",
            expected_kde_icon_theme="Papirus-Dark",
            expected_kde_cursor_theme="Bibata-Modern-Ice",
            expected_kde_wallpaper="/home/quickhatch/Pictures/wallpaper.jpg",
            expected_kde_wobbly_windows="true",
            expected_kde_magic_lamp="true",
            wallpaper_attempted=True,
        ),
    )

    assert result.passed
    assert result.blockers == []


@pytest.mark.parametrize(
    ("element", "expected_key", "expected_value", "design"),
    [
        (
            "theme",
            "expected_kde_color_scheme",
            "SweetMars",
            DesktopDesignState(environment="KDE", kde_color_scheme="BreezeDark"),
        ),
        (
            "icons",
            "expected_kde_icon_theme",
            "candy-icons",
            DesktopDesignState(environment="KDE", kde_icon_theme="Breeze"),
        ),
        (
            "cursor",
            "expected_kde_cursor_theme",
            "Bibata-Modern-Ice",
            DesktopDesignState(environment="KDE", kde_cursor_theme="Breeze"),
        ),
        (
            "wallpaper",
            "expected_kde_wallpaper",
            "/home/quickhatch/Pictures/wallpaper.jpg",
            DesktopDesignState(
                environment="KDE",
                kde_wallpaper="file:///usr/share/wallpapers/default.jpg",
            ),
        ),
        (
            "effects",
            "expected_kde_wobbly_windows",
            "true",
            DesktopDesignState(environment="KDE", kde_wobbly_windows="false"),
        ),
    ],
)
def test_strict_linux_desktop_gui_contract_blocks_element_mismatch(
    element: str,
    expected_key: str,
    expected_value: str,
    design: DesktopDesignState,
) -> None:
    result = audit_stage(
        SetupStage.DESKTOP,
        _facts(design),
        stage_state=_strict_state(element, **{expected_key: expected_value}),
    )

    assert not result.passed
    assert any("mismatch" in blocker or "no target value" in blocker for blocker in result.blockers)


def test_strict_linux_desktop_gui_contract_rejects_wrong_desktop_environment() -> None:
    facts = _facts(
        DesktopDesignState(
            environment="GNOME",
            gnome_gtk_theme="Adwaita-dark",
            gnome_icon_theme="Papirus-Dark",
        )
    )

    result = audit_stage(
        SetupStage.DESKTOP,
        facts,
        stage_state=_strict_state(
            "theme",
            "icons",
            expected_kde_color_scheme="BreezeDark",
            expected_kde_icon_theme="Papirus-Dark",
        ),
    )

    assert not result.passed
    assert any("KDE color scheme was expected" in blocker for blocker in result.blockers)
    assert any("KDE icon theme was expected" in blocker for blocker in result.blockers)


def test_strict_linux_desktop_gui_contract_requires_dynamic_wallpaper_to_be_observed() -> None:
    missing = audit_stage(
        SetupStage.DESKTOP,
        _facts(DesktopDesignState(environment="KDE")),
        stage_state=_strict_state(
            "wallpaper",
            expected_kde_wallpaper="file://$WALLPAPER",
        ),
    )
    observed = audit_stage(
        SetupStage.DESKTOP,
        _facts(
            DesktopDesignState(
                environment="KDE",
                kde_wallpaper="file:///home/quickhatch/Pictures/generated-wallpaper.jpg",
            )
        ),
        stage_state=_strict_state(
            "wallpaper",
            expected_kde_wallpaper="file://$WALLPAPER",
        ),
    )

    assert not missing.passed
    assert any("shell-dynamic expected value" in blocker for blocker in missing.blockers)
    assert observed.passed


def test_strict_gui_contract_uses_explicit_manual_followup_for_missing_value() -> None:
    result = audit_stage(
        SetupStage.DESKTOP,
        _facts(DesktopDesignState(environment="KDE")),
        stage_state={
            **_strict_state("icons"),
            "manual_followups": [
                {
                    "reason": (
                        "Icon pack candy-icons is unavailable in configured repositories; "
                        "manual icon theme selection is required."
                    )
                }
            ],
        },
    )

    assert not result.passed
    assert any("Icon pack candy-icons is unavailable" in blocker for blocker in result.blockers)
    assert not any("no concrete expected target value" in blocker for blocker in result.blockers)
