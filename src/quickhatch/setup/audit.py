"""Stage-specific audits for remote setup."""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from .types import (
    AuditFinding,
    AuditFindingSeverity,
    AuditResult,
    DesktopRuntimeState,
    FinalSetupStatus,
    PlanItemStatus,
    ResearchPlan,
    SetupFacts,
    SetupStage,
)


def _coerce_plan_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "required"}:
        return True
    if text in {"0", "false", "no", "n", "off", "optional", "skip"}:
        return False
    return default


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
            _coerce_plan_bool(q.get("required", True), default=True)
            and not str(q.get("answer", "") or "").strip()
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
        details["fact_probe_ok"] = facts.fact_probe_ok
        details["secure_boot_state"] = facts.secure_boot_state
        details["amd_kernel_driver"] = facts.amd_kernel_driver
        details["intel_kernel_driver"] = facts.intel_kernel_driver
        details["vulkan_state"] = facts.vulkan_state
        details["vulkan_devices"] = list(facts.vulkan_devices)
        details["vaapi_state"] = facts.vaapi_state
        details["rocm_state"] = facts.rocm_state
        details["driver_manual_followup"] = False
        details["driver_research_needed"] = False
        if (
            "nvidia" in facts.gpu_vendors
            and plan is not None
            and (
                plan.driver_strategy.confidence == "manual_followup_required"
                or plan.driver_strategy.source == "manual_followup"
            )
        ):
            details["driver_manual_followup"] = True
            details["driver_manual_followup_reason"] = (
                "NVIDIA driver strategy requires explicit manual follow-up."
            )
        gpu_vendors = {vendor.lower() for vendor in facts.gpu_vendors}
        if gpu_vendors and not facts.fact_probe_ok:
            blockers.append(
                "GPU verification facts are stale because the latest remote fact probe failed; "
                "refresh facts successfully before declaring driver setup complete."
            )
        if (
            "nvidia" in facts.gpu_vendors
            and plan is not None
            and plan.driver_strategy.confidence != "manual_followup_required"
            and plan.driver_strategy.source != "manual_followup"
            and not expected
            and facts.nvidia_smi_state != "ok"
        ):
            details["driver_research_needed"] = True
            details["driver_research_reason"] = (
                "NVIDIA driver strategy is marked resolved but contains no driver packages."
            )
            blockers.append(
                "NVIDIA driver strategy is marked resolved but contains no driver packages "
                "and nvidia-smi is not already healthy. Re-research the driver plan or mark "
                "driver setup as explicit manual follow-up."
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
        details["desktop_manual_followups"] = list(state.get("manual_followups", []) or [])
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
        if not _desktop_persistence_satisfied(facts, state) and not _desktop_followup_recorded(
            state
        ):
            blockers.append("Desktop configuration was not persisted yet.")
        desktop_followups: list[str] = []
        if state.get("desktop_design_attempted") and not _has_desktop_design_signal(facts):
            desktop_followups.append(
                "Desktop design config was attempted, but no configured theme/icon/"
                "wallpaper state was detected in the target user session."
            )
        if state.get("wallpaper_attempted") and not _has_wallpaper_signal(facts):
            desktop_followups.append(
                "Wallpaper apply/configuration was attempted, but no target wallpaper "
                "state was detected."
            )
        if plan and _plan_mentions_wallpaper(plan) and not _has_wallpaper_signal(facts):
            desktop_followups.append(
                "Desktop plan mentions wallpaper, but the target desktop wallpaper was "
                "not verified in the active desktop environment."
            )
        if state.get("gnome_wallpaper_dark_uri_only"):
            desktop_followups.append(
                "GNOME wallpaper was applied through picture-uri-dark only; verify the "
                "light-mode wallpaper key before declaring wallpaper setup fully complete."
            )
        for element in _plan_requested_design_elements(plan):
            if element == "wallpaper":
                continue
            if not _has_design_element_signal(facts, element):
                desktop_followups.append(
                    f"Desktop plan mentions {element}, but the target desktop {element} "
                    "state was not verified in the active desktop environment."
                )
        if (
            facts.desktop_runtime_state == DesktopRuntimeState.PENDING_RELOGIN
            and _has_expected_desktop_design_state(state)
        ):
            desktop_followups.append(
                "Desktop design settings were persisted but not live-verified in an active "
                "target GUI session; verify theme/icons/cursor/wallpaper after login."
            )
        desktop_followups.extend(_desktop_design_contract_blockers(facts, state))
        display_followups = [str(item) for item in (state.get("display_preference_blockers") or [])]
        desktop_followups.extend(display_followups)
        details["desktop_design_manual_followups"] = desktop_followups
        details["display_preference_manual_followups"] = display_followups
        if state.get("strict_desktop_design_contract"):
            blockers.extend(desktop_followups)
        if facts.desktop_runtime_state == DesktopRuntimeState.BLOCKED_DRIVER_OR_DISPLAY_STACK:
            blockers.append("Desktop apply is blocked by the current driver or display stack.")

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
        required_followup = [
            app.name
            for app in (plan.apps if plan else [])
            if app.required
            and app.status
            in {
                PlanItemStatus.BLOCKED,
                PlanItemStatus.FAILED,
                PlanItemStatus.OPTIONAL_SLOW_PENDING_CONFIRMATION,
            }
        ]
        required_manual_without_reason = [
            app.name
            for app in (plan.apps if plan else [])
            if app.required
            and app.status == PlanItemStatus.MANUAL_FOLLOWUP
            and not str(app.reason or "").strip()
        ]
        if pending_slow:
            blockers.append(
                "Optional slow apps still need user confirmation: "
                + ", ".join(sorted(pending_slow))
            )
        if unresolved:
            blockers.append("Required apps are still unresolved: " + ", ".join(sorted(unresolved)))
        if required_followup:
            blockers.append(
                "Required apps are blocked, failed, or still waiting for confirmation: "
                + ", ".join(sorted(required_followup))
            )
        if required_manual_without_reason:
            blockers.append(
                "Required apps marked manual follow-up need an explicit reason: "
                + ", ".join(sorted(required_manual_without_reason))
            )
        details["apps"] = {app.name: app.status.value for app in (plan.apps if plan else [])}

    elif stage == SetupStage.FINAL_AUDIT:
        prior_results = list(state.get("stage_results", []) or [])
        blockers.extend(
            result.blockers
            for result in prior_results
            if not getattr(result, "passed", False) and getattr(result, "blockers", None)
        )
        desktop_followups = _desktop_followups_from_stage_results(prior_results)
        blockers.extend(
            f"Desktop manual follow-up remains: {reason}" for reason in desktop_followups
        )
        if facts.is_live:
            blockers.append(
                "Target is still reporting a live environment during final audit; "
                "the installed system has not been proven booted/reconnectable."
            )
        if plan:
            verified_alternatives = [
                item
                for item in state.get("verified_alternative_apps", []) or []
                if isinstance(item, dict)
            ]
            if verified_alternatives:
                blockers.append(
                    "Alternative installed for requested app(s): "
                    + ", ".join(
                        sorted(
                            f"{str(item.get('app') or '').strip()} -> "
                            f"{str(item.get('alternative') or item.get('target') or '').strip()}"
                            for item in verified_alternatives
                            if str(item.get("app") or "").strip()
                        )
                    )
                )
            followup_app_statuses = {
                PlanItemStatus.MANUAL_FOLLOWUP,
                PlanItemStatus.BLOCKED,
                PlanItemStatus.FAILED,
                PlanItemStatus.OPTIONAL_SLOW_PENDING_CONFIRMATION,
            }
            fatal_required_app_failures = [
                app.name
                for app in plan.apps
                if app.required and app.status in {PlanItemStatus.BLOCKED, PlanItemStatus.FAILED}
            ]
            fatal_required_app_names = set(fatal_required_app_failures)
            app_followups = [
                app.name
                for app in plan.apps
                if app.status in followup_app_statuses
                and app.name not in fatal_required_app_names
                and not app_manual_followup_satisfied_by_verified(
                    app.name,
                    app.package_name,
                    state.get("verified_installed_apps", []) or [],
                )
            ]
            if fatal_required_app_failures:
                blockers.append(
                    "Required app failed or blocked: "
                    + ", ".join(sorted(fatal_required_app_failures))
                )
            if app_followups:
                blockers.append(
                    "Manual follow-up app(s) remain: " + ", ".join(sorted(app_followups))
                )
            followup_groups = [
                group.title or group.id
                for group in plan.command_groups
                if group.status
                in {
                    PlanItemStatus.MANUAL_FOLLOWUP,
                    PlanItemStatus.OPTIONAL_SLOW_PENDING_CONFIRMATION,
                }
            ]
            if followup_groups:
                blockers.append(
                    "Manual follow-up setup item(s) remain: " + ", ".join(sorted(followup_groups))
                )
            driver_strategy = plan.driver_strategy
            driver_manual_followup = (
                "nvidia" in {vendor.lower() for vendor in facts.gpu_vendors}
                and (
                    driver_strategy.confidence == "manual_followup_required"
                    or driver_strategy.source == "manual_followup"
                )
                and not driver_strategy.packages
            )
            if driver_manual_followup:
                reason = driver_strategy.notes.strip()
                summary = "NVIDIA driver strategy requires explicit user follow-up."
                blockers.append(f"{summary} {reason}" if reason else summary)
        if facts.waiting_for_input:
            blockers.append("A blocking user question is still unresolved.")
        flattened: list[str] = []
        for blocker in blockers:
            if isinstance(blocker, list):
                flattened.extend(str(item) for item in blocker)
            else:
                flattened.append(str(blocker))
        blockers = flattened

    findings = [_finding_from_blocker(stage, blocker) for blocker in blockers]
    final_status = (
        _final_status_from_findings(findings) if stage == SetupStage.FINAL_AUDIT else None
    )
    if stage == SetupStage.FINAL_AUDIT:
        passed = not any(finding.severity == AuditFindingSeverity.FATAL for finding in findings)
        result_blockers = [
            finding.summary
            for finding in findings
            if finding.severity == AuditFindingSeverity.FATAL
        ]
    else:
        passed = not findings
        result_blockers = [finding.summary for finding in findings]
    return AuditResult(
        stage=stage,
        passed=passed,
        blockers=result_blockers,
        details=details,
        findings=findings,
        final_status=final_status,
    )


def _finding_from_blocker(stage: SetupStage, blocker: str) -> AuditFinding:
    text = str(blocker or "").strip()
    category = _finding_category(text)
    severity = _finding_severity(stage, category, text)
    return AuditFinding(
        id=f"{stage.value}:{category}:{abs(hash(text)) % 100000}",
        severity=severity,
        category=category,
        summary=text,
        evidence=[text] if text else [],
        suggested_repair_scope=_repair_scope_for_category(category),
        required_capability_kinds=_capabilities_for_category(category),
    )


def findings_from_blockers(stage: SetupStage, blockers: list[str]) -> list[AuditFinding]:
    return [_finding_from_blocker(stage, blocker) for blocker in blockers]


def app_manual_followup_satisfied_by_verified(
    app_name: str, package_name: str, verified_names: list[str] | set[str] | tuple[str, ...]
) -> bool:
    """Return true when a manual app row is just an alias of a verified app.

    The planner can carry both a Windows display name and the resolved Linux app
    name. For example, `Spotify - Music and Podcasts` may remain as a manual
    row while the resolved `Spotify` package is verified. Do not let those
    display-name aliases create a false manual-follow-up finding.
    """

    aliases = _manual_followup_app_aliases(app_name, package_name)
    verified_aliases: set[str] = set()
    for name in verified_names:
        verified_aliases.update(_manual_followup_app_aliases(str(name), ""))
    return bool(aliases and verified_aliases and aliases.intersection(verified_aliases))


def _manual_followup_app_aliases(app_name: str, package_name: str) -> set[str]:
    aliases: set[str] = set()
    for value in (app_name, package_name):
        normalized = _normalize_app_alias(value)
        if not normalized:
            continue
        aliases.add(normalized)
        if normalized.endswith(" media player"):
            aliases.add(normalized.removesuffix(" media player").strip())
        if normalized.endswith(" music and podcasts"):
            aliases.add(normalized.removesuffix(" music and podcasts").strip())
    return {alias for alias in aliases if alias}


def _normalize_app_alias(value: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _finding_category(text: str) -> str:
    lower = text.lower()
    if "live environment" in lower or "live installer" in lower:
        return "bootability"
    if "fstab" in lower:
        return "fstab"
    if "nvidia" in lower or "secure boot" in lower or "nouveau" in lower:
        return "driver"
    if "bootloader" in lower or "grub" in lower or "initramfs" in lower or "kernel" in lower:
        return "bootability"
    if "ssh" in lower or "reconnect" in lower:
        return "ssh_reconnect"
    if (
        "wallpaper" in lower
        or "theme" in lower
        or "desktop design" in lower
        or "desktop config" in lower
        or "display" in lower
        or "icon" in lower
        or "cursor" in lower
        or "effect" in lower
        or "gnome" in lower
        or "kde" in lower
        or "xfce" in lower
        or "plasma" in lower
        or "kwin" in lower
    ):
        return "desktop"
    if "app" in lower or "package" in lower:
        return "apps"
    if "question" in lower:
        return "blocking_question"
    return "setup"


def _finding_severity(stage: SetupStage, category: str, text: str) -> AuditFindingSeverity:
    lower = text.lower()
    if category in {"fstab", "bootability", "ssh_reconnect", "blocking_question"}:
        return AuditFindingSeverity.FATAL
    if category == "apps":
        if "alternative installed" in lower:
            return AuditFindingSeverity.WARNING
        return AuditFindingSeverity.MANUAL_FOLLOWUP
    if category == "driver" and "secure boot" in lower:
        return AuditFindingSeverity.UNSUPPORTED_SAFE
    if category in {"desktop", "apps", "driver"}:
        return AuditFindingSeverity.MANUAL_FOLLOWUP
    if stage == SetupStage.FINAL_AUDIT:
        return AuditFindingSeverity.REPAIRABLE
    return AuditFindingSeverity.FATAL


def _repair_scope_for_category(category: str) -> str:
    return {
        "fstab": "repair target fstab only",
        "bootability": "repair kernel/initramfs/bootloader only",
        "ssh_reconnect": "repair SSH or sudo continuity only",
        "driver": "repair or mark driver manual follow-up",
        "desktop": "repair persisted desktop config or mark cosmetic manual follow-up",
        "apps": "repair package identifiers or mark app manual follow-up",
    }.get(category, "repair this finding only")


def _capabilities_for_category(category: str) -> list[str]:
    if category == "fstab":
        return ["write_fstab"]
    if category == "bootability":
        return ["bootloader_install"]
    return []


def _final_status_from_findings(findings: list[AuditFinding]) -> FinalSetupStatus:
    if any(finding.severity == AuditFindingSeverity.FATAL for finding in findings):
        return FinalSetupStatus.BLOCKED
    if any(finding.severity == AuditFindingSeverity.MANUAL_FOLLOWUP for finding in findings):
        return FinalSetupStatus.MANUAL_FOLLOWUP_REQUIRED
    if any(finding.severity == AuditFindingSeverity.UNSUPPORTED_SAFE for finding in findings):
        return FinalSetupStatus.UNSUPPORTED_SAFE
    if findings:
        return FinalSetupStatus.SUCCESS_WITH_WARNINGS
    return FinalSetupStatus.SUCCESS


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
            design.xfce_cursor_theme,
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
            design.xfce_cursor_theme,
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


def _desktop_followup_recorded(state: dict[str, Any]) -> bool:
    return bool(
        state.get("display_manual_followup")
        or state.get("display_manual_followup_reason")
        or state.get("manual_followups")
        or state.get("display_preference_blockers")
    )


def _desktop_followups_from_stage_results(prior_results: list[AuditResult]) -> list[str]:
    reasons: list[str] = []
    seen: set[str] = set()
    for result in prior_results:
        if getattr(result, "stage", None) != SetupStage.DESKTOP:
            continue
        details = getattr(result, "details", {}) or {}
        candidates: list[Any] = []
        if details.get("display_manual_followup_reason"):
            candidates.append(details["display_manual_followup_reason"])
        candidates.extend(details.get("desktop_manual_followups") or [])
        candidates.extend(details.get("desktop_design_manual_followups") or [])
        candidates.extend(details.get("display_preference_manual_followups") or [])
        for candidate in candidates:
            if isinstance(candidate, dict):
                reason = str(candidate.get("reason") or "").strip()
            else:
                reason = str(candidate or "").strip()
            if not reason:
                continue
            if any(reason in existing or existing in reason for existing in seen):
                continue
            seen.add(reason)
            reasons.append(reason)
    return reasons


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


def _plan_requested_design_elements(plan: ResearchPlan | None) -> set[str]:
    if plan is None:
        return set()
    text = " ".join(
        str(value or "")
        for value in (
            plan.target_summary,
            plan.desktop_strategy.strategy,
            plan.desktop_strategy.notes,
        )
    ).lower()
    elements: set[str] = set()
    if re.search(r"\btheme\b|color[-\s]?scheme|look[-\s]?and[-\s]?feel", text):
        elements.add("theme")
    if re.search(r"\bicons?\b|icon[-\s]?theme", text):
        elements.add("icons")
    if re.search(r"\bcursor\b|cursor[-\s]?theme", text):
        elements.add("cursor")
    if re.search(r"\bwall[-\s]?paper\b|background image", text):
        elements.add("wallpaper")
    if re.search(r"\beffects?\b|animation|blur|composit", text):
        elements.add("effects")
    return elements


def _has_design_element_signal(facts: SetupFacts, element: str) -> bool:
    design = facts.desktop_design
    if element == "theme":
        return bool(
            str(
                design.kde_color_scheme
                or design.gnome_gtk_theme
                or design.xfce_gtk_theme
                or design.xfce_window_theme
                or ""
            ).strip()
        )
    if element == "icons":
        return bool(
            str(
                design.kde_icon_theme or design.gnome_icon_theme or design.xfce_icon_theme or ""
            ).strip()
        )
    if element == "cursor":
        return bool(
            str(
                design.kde_cursor_theme
                or design.gnome_cursor_theme
                or design.xfce_cursor_theme
                or ""
            ).strip()
        )
    if element == "wallpaper":
        return _has_wallpaper_signal(facts)
    if element == "effects":
        return bool(str(design.kde_wobbly_windows or design.kde_magic_lamp or "").strip())
    return False


def _has_expected_desktop_design_state(state: dict[str, Any]) -> bool:
    return any(str(key).startswith("expected_") for key in state)


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
            _matches_kde_color_scheme,
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
        (
            "GNOME GTK theme",
            state.get("expected_gnome_gtk_theme"),
            design.gnome_gtk_theme,
            _matches_exactish,
        ),
        (
            "GNOME icon theme",
            state.get("expected_gnome_icon_theme"),
            design.gnome_icon_theme,
            _matches_exactish,
        ),
        (
            "GNOME cursor theme",
            state.get("expected_gnome_cursor_theme"),
            design.gnome_cursor_theme,
            _matches_exactish,
        ),
        (
            "GNOME wallpaper",
            state.get("expected_gnome_wallpaper"),
            design.gnome_wallpaper,
            _matches_pathish,
        ),
        (
            "XFCE GTK theme",
            state.get("expected_xfce_gtk_theme"),
            design.xfce_gtk_theme,
            _matches_exactish,
        ),
        (
            "XFCE icon theme",
            state.get("expected_xfce_icon_theme"),
            design.xfce_icon_theme,
            _matches_exactish,
        ),
        (
            "XFCE cursor theme",
            state.get("expected_xfce_cursor_theme"),
            design.xfce_cursor_theme,
            _matches_exactish,
        ),
        (
            "XFCE wallpaper",
            state.get("expected_xfce_wallpaper"),
            design.xfce_wallpaper,
            _matches_pathish,
        ),
        (
            "XFCE compositing",
            state.get("expected_xfce_compositing"),
            getattr(design, "xfce_compositing", ""),
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
            blockers.append(
                f"{label} was expected as {expected_text!r}, but no target value was detected. "
                "Re-apply or persist the desktop design before declaring success."
            )
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


def _matches_kde_color_scheme(expected: str, observed: str) -> bool:
    if _matches_exactish(expected, observed):
        return True
    expected_norm = _normalize_design_value(expected)
    observed_norm = _normalize_design_value(observed)
    builtin_dark_names = {
        "breezedark",
        "breeze-dark",
        "org.kde.breezedark.desktop",
        "org-kde-breezedark-desktop",
    }
    if expected_norm in builtin_dark_names and "custom-dark-palette" in observed_norm:
        return True
    return False


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
