"""Structured remote-setup orchestration helpers."""

from .audit import audit_stage, final_audit
from .facts import describe_fact_changes, probe_setup_facts
from .orchestrator import SetupOrchestrator, SetupRuntime
from .planner import (
    ResearchPlanValidationError,
    build_research_prompt,
    extract_research_plan,
)
from .resolver import (
    boot_repair_checks,
    build_install_command,
    build_service_enable_command,
    community_search_command,
    desktop_runtime_capabilities,
    detect_family,
    driver_strategy,
    repo_search_command,
)
from .runlog import SetupRunLogger
from .types import (
    AppPlanItem,
    AuditResult,
    CommandGroup,
    DesktopRuntimeState,
    DisplayRuntimeInfo,
    PlanItemStatus,
    ResearchPlan,
    SetupFacts,
    SetupStage,
    StrategyDecision,
)

__all__ = [
    "AppPlanItem",
    "AuditResult",
    "CommandGroup",
    "DesktopRuntimeState",
    "DisplayRuntimeInfo",
    "PlanItemStatus",
    "ResearchPlan",
    "ResearchPlanValidationError",
    "SetupFacts",
    "SetupOrchestrator",
    "SetupRunLogger",
    "SetupRuntime",
    "SetupStage",
    "StrategyDecision",
    "audit_stage",
    "boot_repair_checks",
    "build_install_command",
    "build_research_prompt",
    "build_service_enable_command",
    "community_search_command",
    "describe_fact_changes",
    "desktop_runtime_capabilities",
    "detect_family",
    "driver_strategy",
    "extract_research_plan",
    "final_audit",
    "probe_setup_facts",
    "repo_search_command",
]
