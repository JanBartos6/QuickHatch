from __future__ import annotations

from quickhatch.setup.audit import audit_stage
from quickhatch.setup.types import (
    DesktopDesignState,
    DesktopRuntimeState,
    ResearchPlan,
    SetupFacts,
    SetupStage,
    StrategyDecision,
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


def test_desktop_audit_blocks_expected_icon_reset_to_default():
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
        desktop_design=DesktopDesignState(
            environment="KDE",
            kde_icon_theme="breeze",
            kde_wallpaper="file:///home/jan/Pictures/masterpiece.png",
            kde_wobbly_windows="true",
        ),
    )

    result = audit_stage(
        SetupStage.DESKTOP,
        facts,
        stage_state={
            "persisted_config_written": True,
            "desktop_design_attempted": True,
            "wallpaper_attempted": True,
            "expected_kde_icon_theme": "candy-icons",
            "expected_kde_wallpaper": "/home/jan/Pictures/masterpiece.png",
            "expected_kde_wobbly_windows": "true",
        },
    )

    assert not result.passed
    assert any("KDE icon theme mismatch" in item for item in result.blockers)


def test_desktop_audit_blocks_expected_wallpaper_missing():
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
        desktop_design=DesktopDesignState(
            environment="KDE",
            kde_icon_theme="candy-icons",
            kde_wallpaper="file:///usr/share/wallpapers/default.jpg",
        ),
    )

    result = audit_stage(
        SetupStage.DESKTOP,
        facts,
        stage_state={
            "persisted_config_written": True,
            "desktop_design_attempted": True,
            "wallpaper_attempted": True,
            "expected_kde_icon_theme": "candy-icons",
            "expected_kde_wallpaper": "/home/jan/Pictures/masterpiece.png",
        },
    )

    assert not result.passed
    assert any("KDE wallpaper mismatch" in item for item in result.blockers)


def test_desktop_audit_blocks_missing_expected_icon_value():
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
        desktop_design=DesktopDesignState(
            environment="KDE",
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
            "expected_kde_icon_theme": "candy-icons",
            "expected_kde_wallpaper": "/home/jan/Pictures/masterpiece.png",
        },
    )

    assert not result.passed
    assert any("KDE icon theme was expected" in item for item in result.blockers)


def test_desktop_audit_ignores_dynamic_shell_wallpaper_expectation():
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="linus",
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
        desktop_design=DesktopDesignState(
            environment="KDE",
            kde_icon_theme="Papirus-Dark",
            kde_wallpaper="file:///usr/share/wallpapers/Next/contents/images/1440x2960.png",
        ),
    )

    result = audit_stage(
        SetupStage.DESKTOP,
        facts,
        stage_state={
            "persisted_config_written": True,
            "desktop_design_attempted": True,
            "wallpaper_attempted": True,
            "expected_kde_icon_theme": "Papirus-Dark",
            "expected_kde_wallpaper": "file://$W;",
        },
    )

    assert result.passed


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


def test_desktop_audit_does_not_treat_gnome_wallpaper_as_kde_wallpaper():
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="linus",
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
        desktop_design=DesktopDesignState(
            environment="KDE",
            kde_icon_theme="Papirus-Dark",
            kde_wallpaper="",
            gnome_wallpaper="file:///usr/share/backgrounds/gnome/adwaita-l.jxl",
        ),
    )
    plan = ResearchPlan(
        target_summary="KDE desktop with a bold geometric wallpaper.",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision("none"),
        desktop_strategy=StrategyDecision("plasma", notes="Apply wallpaper and icons."),
    )

    result = audit_stage(
        SetupStage.DESKTOP,
        facts,
        plan=plan,
        stage_state={
            "persisted_config_written": True,
            "desktop_design_attempted": True,
        },
    )

    assert not result.passed
    assert any("plan mentions wallpaper" in item for item in result.blockers)


def test_driver_audit_surfaces_secure_boot_for_failed_nvidia_stack():
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        gpu_vendors=["nvidia"],
        gpu_inventory=["NVIDIA GeForce RTX 3080"],
        nvidia_smi_state="present_but_failing",
        secure_boot_state="enabled",
    )
    plan = ResearchPlan(
        target_summary="Arch NVIDIA workstation",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision(
            strategy="official_nvidia_open",
            packages=["nvidia-open", "nvidia-utils"],
        ),
        desktop_strategy=StrategyDecision(strategy="none"),
    )

    result = audit_stage(SetupStage.POSTBOOT_DRIVER, facts, plan=plan)

    assert not result.passed
    assert any("nvidia-smi is not healthy" in item for item in result.blockers)
    assert any("Secure Boot is enabled" in item for item in result.blockers)
    assert result.details["secure_boot_state"] == "enabled"


def test_driver_audit_does_not_require_nvidia_for_amd_only_system():
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        gpu_vendors=["amd"],
        gpu_inventory=["AMD Radeon RX 7900 XTX"],
        amd_kernel_driver="amdgpu",
        nvidia_smi_state="missing",
    )
    plan = ResearchPlan(
        target_summary="Arch AMD workstation",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision(strategy="mesa_default", packages=[]),
        desktop_strategy=StrategyDecision(strategy="none"),
    )

    result = audit_stage(SetupStage.POSTBOOT_DRIVER, facts, plan=plan)

    assert result.passed


def test_driver_audit_blocks_unresolved_legacy_nvidia_strategy():
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        gpu_vendors=["nvidia"],
        gpu_inventory=["NVIDIA GeForce GTX 1080"],
        nvidia_smi_state="missing",
    )
    plan = ResearchPlan(
        target_summary="Arch legacy NVIDIA workstation",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision(
            strategy="nvidia_legacy_manual_followup",
            confidence="manual_followup_required",
            source="manual_followup",
            packages=[],
        ),
        desktop_strategy=StrategyDecision(strategy="none"),
    )

    result = audit_stage(SetupStage.POSTBOOT_DRIVER, facts, plan=plan)

    assert not result.passed
    assert any("manual follow-up" in item for item in result.blockers)


def test_driver_audit_blocks_stale_nvidia_probe_even_if_previous_state_was_healthy():
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        gpu_vendors=["nvidia"],
        gpu_inventory=["NVIDIA GeForce RTX 3080"],
        nvidia_smi_state="ok",
        nvidia_kernel_driver="nvidia",
        nvidia_nouveau_bound=False,
        fact_probe_ok=False,
    )
    plan = ResearchPlan(
        target_summary="Arch NVIDIA workstation",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision(
            strategy="official_nvidia_open",
            confidence="resolved_official",
            source="official",
            packages=["nvidia-open", "nvidia-utils"],
        ),
        desktop_strategy=StrategyDecision(strategy="none"),
    )

    result = audit_stage(SetupStage.POSTBOOT_DRIVER, facts, plan=plan)

    assert not result.passed
    assert any("stale" in item for item in result.blockers)


def test_driver_audit_blocks_resolved_nvidia_strategy_without_packages():
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        gpu_vendors=["nvidia"],
        gpu_inventory=["NVIDIA GeForce RTX 3080"],
        nvidia_smi_state="missing",
    )
    plan = ResearchPlan(
        target_summary="Arch NVIDIA workstation",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision(
            strategy="official_nvidia_open",
            confidence="resolved_official",
            source="official",
            packages=[],
        ),
        desktop_strategy=StrategyDecision(strategy="none"),
    )

    result = audit_stage(SetupStage.POSTBOOT_DRIVER, facts, plan=plan)

    assert not result.passed
    assert any("contains no driver packages" in item for item in result.blockers)


def test_driver_audit_blocks_modern_radeon_bound_to_legacy_driver():
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        gpu_vendors=["amd"],
        gpu_inventory=["AMD Radeon RX 6800 XT"],
        amd_kernel_driver="radeon",
        vulkan_state="missing",
    )
    plan = ResearchPlan(
        target_summary="Arch AMD workstation",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision(strategy="mesa_default", packages=[]),
        desktop_strategy=StrategyDecision(strategy="none"),
    )

    result = audit_stage(SetupStage.POSTBOOT_DRIVER, facts, plan=plan)

    assert not result.passed
    assert any("legacy radeon driver" in item for item in result.blockers)


def test_driver_audit_accepts_amdgpu_without_optional_vulkan_tooling():
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        gpu_vendors=["amd"],
        gpu_inventory=["AMD Radeon RX 7900 XTX"],
        amd_kernel_driver="amdgpu",
        vulkan_state="missing",
        vaapi_state="missing",
        rocm_state="missing",
    )
    plan = ResearchPlan(
        target_summary="Arch AMD workstation",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision(strategy="mesa_default", packages=[]),
        desktop_strategy=StrategyDecision(strategy="none"),
    )

    result = audit_stage(SetupStage.POSTBOOT_DRIVER, facts, plan=plan)

    assert result.passed


def test_driver_audit_blocks_amd_vulkan_probe_failure():
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        gpu_vendors=["amd"],
        gpu_inventory=["AMD Radeon RX 7900 XTX"],
        amd_kernel_driver="amdgpu",
        vulkan_state="present_but_failing",
    )
    plan = ResearchPlan(
        target_summary="Arch AMD workstation",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision(strategy="mesa_default", packages=[]),
        desktop_strategy=StrategyDecision(strategy="none"),
    )

    result = audit_stage(SetupStage.POSTBOOT_DRIVER, facts, plan=plan)

    assert not result.passed
    assert any("Vulkan probe fails" in item for item in result.blockers)


def test_driver_audit_blocks_intel_missing_kernel_driver():
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        gpu_vendors=["intel"],
        gpu_inventory=["Intel Arc A770"],
        intel_kernel_driver="none",
        vulkan_state="missing",
    )
    plan = ResearchPlan(
        target_summary="Arch Intel workstation",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision(strategy="intel_default", packages=[]),
        desktop_strategy=StrategyDecision(strategy="none"),
    )

    result = audit_stage(SetupStage.POSTBOOT_DRIVER, facts, plan=plan)

    assert not result.passed
    assert any("Intel GPU has no bound kernel driver" in item for item in result.blockers)


def test_driver_audit_blocks_intel_vulkan_probe_failure():
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        gpu_vendors=["intel"],
        gpu_inventory=["Intel Iris Xe"],
        intel_kernel_driver="i915",
        vulkan_state="present_but_failing",
    )
    plan = ResearchPlan(
        target_summary="Arch Intel workstation",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision(strategy="intel_default", packages=[]),
        desktop_strategy=StrategyDecision(strategy="none"),
    )

    result = audit_stage(SetupStage.POSTBOOT_DRIVER, facts, plan=plan)

    assert not result.passed
    assert any("Intel graphics acceleration" in item for item in result.blockers)
