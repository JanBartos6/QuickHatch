"""Stage-specific audits for remote setup."""

from __future__ import annotations

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
        if "nvidia" in facts.gpu_vendors and expected and facts.nvidia_smi_state != "ok":
            blockers.append("NVIDIA verification is incomplete: nvidia-smi is not healthy yet.")
        if state.get("failed_verifications"):
            blockers.extend(list(state["failed_verifications"]))

    elif stage == SetupStage.DESKTOP:
        details["desktop_runtime_state"] = facts.desktop_runtime_state.value
        if facts.desktop_runtime_state == DesktopRuntimeState.UNKNOWN:
            blockers.append("Desktop runtime state is still unknown.")
        if not state.get("persisted_config_written"):
            blockers.append("Desktop configuration was not persisted yet.")
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


def final_audit(
    facts: SetupFacts, plan: ResearchPlan, stage_results: list[AuditResult]
) -> AuditResult:
    return audit_stage(
        SetupStage.FINAL_AUDIT,
        facts,
        plan=plan,
        stage_state={"stage_results": stage_results},
    )
