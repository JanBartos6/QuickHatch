from __future__ import annotations

from quickhatch.setup.audit import audit_stage
from quickhatch.setup.types import (
    DesktopDesignState,
    DesktopRuntimeState,
    SetupFacts,
    SetupStage,
)


def test_desktop_audit_blocks_design_attempt_without_observed_design_state():
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
    )

    result = audit_stage(
        SetupStage.DESKTOP,
        facts,
        stage_state={
            "persisted_config_written": True,
            "desktop_design_attempted": True,
        },
    )

    assert not result.passed
    assert any("no configured theme/icon/wallpaper state" in item for item in result.blockers)


def test_desktop_audit_accepts_stock_desktop_with_enabled_display_manager():
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="linus",
        display_manager="gdm",
        display_manager_enabled=True,
        display_manager_active=True,
        desktop_runtime_state=DesktopRuntimeState.PENDING_RELOGIN,
        desktop_design=DesktopDesignState(
            environment="GNOME",
            gnome_gtk_theme="Adwaita",
            gnome_icon_theme="Adwaita",
            gnome_cursor_theme="Adwaita",
            gnome_wallpaper="file:///usr/share/backgrounds/gnome/adwaita-l.jxl",
        ),
    )

    result = audit_stage(SetupStage.DESKTOP, facts, stage_state={})

    assert result.passed
    assert result.details["desktop_runtime_state"] == "pending_relogin"
    assert result.details["display_manager_active"] is True


def test_desktop_audit_blocks_enabled_but_inactive_display_manager():
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="linus",
        display_manager="gdm",
        display_manager_enabled=True,
        display_manager_active=False,
        desktop_runtime_state=DesktopRuntimeState.BLOCKED_NO_GUI_SESSION,
        desktop_design=DesktopDesignState(environment="GNOME"),
    )

    result = audit_stage(SetupStage.DESKTOP, facts, stage_state={})

    assert not result.passed
    assert any("enabled but not active" in item for item in result.blockers)


def test_desktop_audit_accepts_observed_design_state():
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
        desktop_design=DesktopDesignState(
            environment="KDE",
            kde_color_scheme="SweetMars",
            kde_icon_theme="candy-icons",
            kde_wallpaper="file:///home/jan/Pictures/masterpiece.png",
        ),
    )

    result = audit_stage(
        SetupStage.DESKTOP,
        facts,
        stage_state={
            "persisted_config_written": True,
            "desktop_design_attempted": True,
            "wallpaper_attempted": True,
        },
    )

    assert result.passed
    assert result.details["desktop_design"]["kde_color_scheme"] == "SweetMars"


def test_desktop_audit_accepts_observed_xfce_design_state():
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="ada",
        desktop_runtime_state=DesktopRuntimeState.PENDING_RELOGIN,
        desktop_design=DesktopDesignState(
            environment="XFCE",
            xfce_gtk_theme="Arc-Dark",
            xfce_icon_theme="Papirus",
            xfce_window_theme="Arc-Dark",
        ),
    )

    result = audit_stage(
        SetupStage.DESKTOP,
        facts,
        stage_state={
            "persisted_config_written": True,
            "desktop_design_attempted": True,
            "display_manual_followup": True,
        },
    )

    assert result.passed
    assert result.details["desktop_design"]["xfce_icon_theme"] == "Papirus"


def test_desktop_audit_blocks_wallpaper_attempt_without_wallpaper_state():
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
        desktop_design=DesktopDesignState(environment="KDE", kde_icon_theme="candy-icons"),
    )

    result = audit_stage(
        SetupStage.DESKTOP,
        facts,
        stage_state={
            "persisted_config_written": True,
            "desktop_design_attempted": True,
            "wallpaper_attempted": True,
        },
    )

    assert not result.passed
    assert any("Wallpaper apply/configuration was attempted" in item for item in result.blockers)
