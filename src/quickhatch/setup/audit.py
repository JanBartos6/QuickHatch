"""Stage-specific audits for remote setup."""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from .types import (
    AuditResult,
    DesktopRuntimeState,
    PlanItemStatus,
    ResearchPlan,
    SetupFacts,
    SetupStage,
)


def audit_stage(
    stage: SetupStage,
    facts: SetupFacts,
    *,
    plan: ResearchPlan | None = None,
    stage_state: dict[str, Any] | None = None,
) -> AuditResult:
    state = stage_state or {}
    blockers: list[str] = []
    details: dict[str, Any] = {}

    if stage == SetupStage.CONNECT_AND_PROBE:
        if facts.package_manager == "unknown":
            blockers.append("Could not detect the remote package manager.")
        if not facts.current_user:
            blockers.append("Could not determine the current SSH user.")
        details["package_manager"] = facts.package_manager
        details["distro_family"] = facts.distro_family

    elif stage == SetupStage.RESEARCH_AND_PLAN:
        if plan is None:
            blockers.append("No research plan was produced.")
        elif any(
            bool(q.get("required", True)) and not str(q.get("answer", "") or "").strip()
            for q in plan.unresolved_questions
        ):
            blockers.append("Research plan still has unanswered required questions.")
        details["planner_revision"] = getattr(plan, "planner_revision", 0)

    elif stage == SetupStage.INSTALL_OR_SKIP:
        if facts.is_live:
            if not state.get("pre_reboot_checks_passed"):
                blockers.append("Live install stage has not satisfied bootability/recovery checks.")
            if not state.get("reconnected_after_install"):
                blockers.append("Target has not reconnected after the install reboot.")
        details["is_live"] = facts.is_live

    elif stage == SetupStage.POSTBOOT_DRIVER:
        expected = (plan.driver_strategy.packages if plan else []) or []
        details["expected_driver_packages"] = expected
        details["nvidia_smi_state"] = facts.nvidia_smi_state
        details["nvidia_kernel_driver"] = facts.nvidia_kernel_driver
        details["nvidia_nouveau_bound"] = facts.nvidia_nouveau_bound
        details["secure_boot_state"] = facts.secure_boot_state
        details["amd_kernel_driver"] = facts.amd_kernel_driver
        details["intel_kernel_driver"] = facts.intel_kernel_driver
        details["vulkan_state"] = facts.vulkan_state
        details["vulkan_devices"] = list(facts.vulkan_devices)
        details["vaapi_state"] = facts.vaapi_state
        details["rocm_state"] = facts.rocm_state
        if (
            "nvidia" in facts.gpu_vendors
            and plan is not None
            and (
                plan.driver_strategy.confidence == "manual_followup_required"
                or plan.driver_strategy.source == "manual_followup"
            )
        ):
            blockers.append(
                "NVIDIA driver strategy still requires manual follow-up; do not declare "
                "driver setup complete until it is explicitly installed, skipped, or verified."
            )
        if (
            "nvidia" in facts.gpu_vendors
            and plan is not None
            and plan.driver_strategy.confidence != "manual_followup_required"
            and plan.driver_strategy.source != "manual_followup"
            and not expected
        ):
            blockers.append(
                "NVIDIA driver strategy is marked resolved but contains no driver packages; "
                "re-research the target driver path before continuing."
            )
        if "nvidia" in facts.gpu_vendors and expected and facts.nvidia_smi_state != "ok":
            blockers.append("NVIDIA verification is incomplete: nvidia-smi is not healthy yet.")
            if facts.secure_boot_state == "enabled":
                blockers.append(
                    "Secure Boot is enabled; NVIDIA kernel modules may need MOK signing, "
                    "an enrolled key, or Secure Boot disabled before nvidia-smi can pass."
                )
        if "nvidia" in facts.gpu_vendors and facts.nvidia_nouveau_bound:
            blockers.append(
                "NVIDIA GPU is still bound to nouveau; blacklist nouveau, regenerate initramfs, "
                "reboot, then verify nvidia-smi."
            )
        if "amd" in facts.gpu_vendors:
            if _kernel_driver_missing(facts.amd_kernel_driver):
                blockers.append(
                    "AMD GPU has no bound kernel driver; verify linux-firmware/Mesa stack "
                    "and that the card is not reserved for passthrough."
                )
            if _modern_amd_gpu_should_use_amdgpu(facts) and _driver_names(
                facts.amd_kernel_driver
            ) == {"radeon"}:
                blockers.append(
                    "Modern Radeon hardware is bound to the legacy radeon driver; it should "
                    "normally use amdgpu before desktop/display success is claimed."
                )
            if facts.vulkan_state == "present_but_failing":
                blockers.append(
                    "Vulkan tools are installed but the Vulkan probe fails; graphics/gaming "
                    "acceleration is not verified."
                )
        if "intel" in facts.gpu_vendors:
            if _kernel_driver_missing(facts.intel_kernel_driver):
                blockers.append(
                    "Intel GPU has no bound kernel driver; verify i915/xe driver state and "
                    "firmware before claiming graphics setup success."
                )
            if facts.vulkan_state == "present_but_failing":
                blockers.append(
                    "Vulkan tools are installed but the Vulkan probe fails; Intel graphics "
                    "acceleration is not verified."
                )
        if state.get("failed_verifications"):
            blockers.extend(list(state["failed_verifications"]))

    elif stage == SetupStage.DESKTOP:
        details["desktop_runtime_state"] = facts.desktop_runtime_state.value
        details["desktop_design"] = asdict(facts.desktop_design)
        details["display_manager_active"] = facts.display_manager_active
        details["display_manual_followup"] = bool(state.get("display_manual_followup"))
        if state.get("display_manual_followup_reason"):
            details["display_manual_followup_reason"] = state.get("display_manual_followup_reason")
        details["display_preferences"] = list(state.get("display_preferences", []) or [])
        details["remote_displays"] = [
            {
                "name": display.name,
                "current_mode": display.current_mode,
                "refresh_hz": display.refresh_hz,
                "scale_percent": display.scale_percent,
                "primary": display.primary,
            }
            for display in facts.displays
        ]
        if facts.desktop_runtime_state == DesktopRuntimeState.UNKNOWN:
            blockers.append("Desktop runtime state is still unknown.")
        if facts.display_manager_enabled and not facts.display_manager_active:
            blockers.append(
                "Display manager is enabled but not active; start it or reboot before declaring desktop success."
            )
        if not _desktop_persistence_satisfied(facts, state):
            blockers.append("Desktop configuration was not persisted yet.")
        if state.get("desktop_design_attempted") and not _has_desktop_design_signal(facts):
            blockers.append(
                "Desktop design config was attempted, but no configured theme/icon/"
                "wallpaper state was detected in the target user session."
            )
        if state.get("wallpaper_attempted") and not _has_wallpaper_signal(facts):
            blockers.append(
                "Wallpaper apply/configuration was attempted, but no target wallpaper "
                "state was detected."
            )
        if plan and _plan_mentions_wallpaper(plan) and not _has_wallpaper_signal(facts):
            blockers.append(
                "Desktop plan mentions wallpaper, but the target desktop wallpaper was "
                "not verified in the active desktop environment."
            )
        blockers.extend(_desktop_design_contract_blockers(facts, state))
        if facts.desktop_runtime_state == DesktopRuntimeState.BLOCKED_DRIVER_OR_DISPLAY_STACK:
            blockers.append("Desktop apply is blocked by the current driver or display stack.")
        blockers.extend(str(item) for item in (state.get("display_preference_blockers") or []))

    elif stage == SetupStage.APPS:
        pending_slow = [
            app.name
            for app in (plan.apps if plan else [])
            if app.status == PlanItemStatus.OPTIONAL_SLOW_PENDING_CONFIRMATION
        ]
        unresolved = [
            app.name
            for app in (plan.apps if plan else [])
            if app.required and app.status in {PlanItemStatus.PENDING, PlanItemStatus.IN_PROGRESS}
        ]
        if pending_slow:
            blockers.append(
                "Optional slow apps still need user confirmation: "
                + ", ".join(sorted(pending_slow))
            )
        if unresolved:
            blockers.append("Required apps are still unresolved: " + ", ".join(sorted(unresolved)))
        details["apps"] = {app.name: app.status.value for app in (plan.apps if plan else [])}

    elif stage == SetupStage.FINAL_AUDIT:
        prior_results = list(state.get("stage_results", []) or [])
        blockers.extend(
            result.blockers
            for result in prior_results
            if not getattr(result, "passed", False) and getattr(result, "blockers", None)
        )
        if facts.waiting_for_input:
            blockers.append("A blocking user question is still unresolved.")
        flattened: list[str] = []
        for blocker in blockers:
            if isinstance(blocker, list):
                flattened.extend(str(item) for item in blocker)
            else:
                flattened.append(str(blocker))
        blockers = flattened

    return AuditResult(stage=stage, passed=not blockers, blockers=blockers, details=details)


def _has_desktop_design_signal(facts: SetupFacts) -> bool:
    design = facts.desktop_design
    env = _desktop_environment(design)
    if env == "kde":
        values = (
            design.kde_color_scheme,
            design.kde_icon_theme,
            design.kde_cursor_theme,
            design.kde_wallpaper,
            design.kde_wobbly_windows,
            design.kde_magic_lamp,
        )
    elif env == "gnome":
        values = (
            design.gnome_gtk_theme,
            design.gnome_icon_theme,
            design.gnome_cursor_theme,
            design.gnome_wallpaper,
        )
    elif env == "xfce":
        values = (
            design.xfce_gtk_theme,
            design.xfce_icon_theme,
            design.xfce_window_theme,
            design.xfce_wallpaper,
        )
    else:
        values = (
            design.kde_color_scheme,
            design.kde_icon_theme,
            design.kde_cursor_theme,
            design.kde_wallpaper,
            design.kde_wobbly_windows,
            design.kde_magic_lamp,
            design.gnome_gtk_theme,
            design.gnome_icon_theme,
            design.gnome_cursor_theme,
            design.gnome_wallpaper,
            design.xfce_gtk_theme,
            design.xfce_icon_theme,
            design.xfce_window_theme,
            design.xfce_wallpaper,
        )
    return any(str(value or "").strip() for value in values)


def _desktop_persistence_satisfied(facts: SetupFacts, state: dict[str, Any]) -> bool:
    if state.get("persisted_config_written"):
        return True
    if state.get("desktop_design_attempted") or state.get("wallpaper_attempted"):
        return False
    # A stock desktop plan may intentionally keep DE defaults. In that case,
    # enabling the display manager is the persistent configuration boundary.
    return bool(facts.display_manager_enabled and facts.display_manager)


def _has_wallpaper_signal(facts: SetupFacts) -> bool:
    design = facts.desktop_design
    env = _desktop_environment(design)
    if env == "kde":
        return bool(str(design.kde_wallpaper or "").strip())
    if env == "gnome":
        return bool(str(design.gnome_wallpaper or "").strip())
    if env == "xfce":
        return bool(str(design.xfce_wallpaper or "").strip())
    return bool(
        str(design.kde_wallpaper or design.gnome_wallpaper or design.xfce_wallpaper or "").strip()
    )


def _desktop_environment(design: Any) -> str:
    env = str(getattr(design, "environment", "") or "").strip().lower()
    if "plasma" in env or "kde" in env:
        return "kde"
    if "gnome" in env:
        return "gnome"
    if "xfce" in env:
        return "xfce"
    return ""


def _plan_mentions_wallpaper(plan: ResearchPlan) -> bool:
    text = " ".join(
        str(value or "")
        for value in (
            plan.target_summary,
            plan.desktop_strategy.strategy,
            plan.desktop_strategy.notes,
        )
    )
    return bool(re.search(r"\bwall[-\s]?paper\b|background image", text, re.IGNORECASE))


def _desktop_design_contract_blockers(facts: SetupFacts, state: dict[str, Any]) -> list[str]:
    """Check intended persisted design values against refreshed target facts."""

    design = facts.desktop_design
    checks = (
        (
            "KDE icon theme",
            state.get("expected_kde_icon_theme"),
            design.kde_icon_theme,
            _matches_exactish,
        ),
        (
            "KDE cursor theme",
            state.get("expected_kde_cursor_theme"),
            design.kde_cursor_theme,
            _matches_exactish,
        ),
        (
            "KDE color scheme",
            state.get("expected_kde_color_scheme"),
            design.kde_color_scheme,
            _matches_exactish,
        ),
        (
            "KDE wallpaper",
            state.get("expected_kde_wallpaper"),
            design.kde_wallpaper,
            _matches_pathish,
        ),
        (
            "KWin wobbly windows",
            state.get("expected_kde_wobbly_windows"),
            design.kde_wobbly_windows,
            _matches_boolish,
        ),
        (
            "KWin magic lamp",
            state.get("expected_kde_magic_lamp"),
            design.kde_magic_lamp,
            _matches_boolish,
        ),
    )
    blockers: list[str] = []
    for label, expected, observed, matcher in checks:
        expected_text = str(expected or "").strip()
        if not expected_text:
            continue
        if _is_dynamic_shell_expectation(expected_text):
            continue
        observed_text = str(observed or "").strip()
        if not observed_text:
            continue
        if matcher(expected_text, observed_text):
            continue
        blockers.append(
            f"{label} mismatch: expected {expected_text!r}, target reports "
            f"{observed_text!r}. Re-apply or persist the desktop design before declaring success."
        )
    return blockers


def _is_dynamic_shell_expectation(value: str) -> bool:
    text = str(value or "")
    return "$" in text or "`" in text


def _normalize_design_value(value: str) -> str:
    return re.sub(r"[\s_-]+", "-", str(value or "").strip().strip("\"'").lower())


def _matches_exactish(expected: str, observed: str) -> bool:
    if not expected:
        return True
    if not observed:
        return False
    expected_norm = _normalize_design_value(expected)
    observed_norm = _normalize_design_value(observed)
    return expected_norm == observed_norm or expected_norm in observed_norm


def _matches_boolish(expected: str, observed: str) -> bool:
    expected_norm = str(expected or "").strip().lower()
    observed_norm = str(observed or "").strip().lower()
    if expected_norm in {"true", "1", "yes", "on", "enabled"}:
        return observed_norm in {"true", "1", "yes", "on", "enabled"}
    if expected_norm in {"false", "0", "no", "off", "disabled"}:
        return observed_norm in {"false", "0", "no", "off", "disabled", ""}
    return _matches_exactish(expected, observed)


def _matches_pathish(expected: str, observed: str) -> bool:
    if not expected:
        return True
    if not observed:
        return False
    expected_norm = str(expected).strip().strip("\"'").replace("\\", "/").lower()
    observed_norm = str(observed).strip().strip("\"'").replace("\\", "/").lower()
    if expected_norm == observed_norm or expected_norm in observed_norm:
        return True
    expected_name = expected_norm.rsplit("/", 1)[-1]
    return bool(expected_name and expected_name in observed_norm)


def _driver_names(driver: str) -> set[str]:
    return {item.strip().lower() for item in (driver or "").split(",") if item.strip()}


def _kernel_driver_missing(driver: str) -> bool:
    drivers = _driver_names(driver)
    return not drivers or drivers <= {"none", "unknown"}


def _modern_amd_gpu_should_use_amdgpu(facts: SetupFacts) -> bool:
    inventory = " ".join(facts.gpu_inventory or [])
    return bool(
        re.search(
            r"\b(?:radeon\s+)?rx\s*\d{3,4}\b|\bvega\b|\bnavi\b|\brdna\b|\bmi\d{2,3}\b",
            inventory,
            re.IGNORECASE,
        )
    )


def final_audit(
    facts: SetupFacts, plan: ResearchPlan, stage_results: list[AuditResult]
) -> AuditResult:
    return audit_stage(
        SetupStage.FINAL_AUDIT,
        facts,
        plan=plan,
        stage_state={"stage_results": stage_results},
    )
