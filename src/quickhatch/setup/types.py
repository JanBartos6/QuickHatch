"""Typed models for staged remote setup."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SetupStage(str, Enum):
    CONNECT_AND_PROBE = "connect_and_probe"
    RESEARCH_AND_PLAN = "research_and_plan"
    INSTALL_OR_SKIP = "install_or_skip"
    POSTBOOT_DRIVER = "postboot_driver"
    DESKTOP = "desktop"
    APPS = "apps"
    FINAL_AUDIT = "final_audit"


class PlanItemStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    INSTALLED = "installed"
    MANUAL_FOLLOWUP = "manual_followup"
    EXPLICITLY_SKIPPED = "explicitly_skipped"
    OPTIONAL_SLOW_PENDING_CONFIRMATION = "optional_slow_pending_confirmation"


class DesktopRuntimeState(str, Enum):
    PERSISTED_CONFIG_WRITTEN = "persisted_config_written"
    LIVE_APPLY_SUCCEEDED = "live_apply_succeeded"
    PENDING_RELOGIN = "pending_relogin"
    BLOCKED_NO_GUI_SESSION = "blocked_no_gui_session"
    BLOCKED_DRIVER_OR_DISPLAY_STACK = "blocked_driver_or_display_stack"
    UNKNOWN = "unknown"


@dataclass
class DisplayRuntimeInfo:
    name: str
    current_mode: str = ""
    advertised_modes: list[str] = field(default_factory=list)
    refresh_hz: float | None = None
    scale_percent: int | None = None
    primary: bool = False


@dataclass
class StrategyDecision:
    strategy: str
    confidence: str = "manual_followup_required"
    source: str = "manual_followup"
    packages: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class AppPlanItem:
    name: str
    package_name: str = ""
    source: str = "manual_followup"
    status: PlanItemStatus = PlanItemStatus.PENDING
    required: bool = False
    optional_slow: bool = False
    reason: str = ""


@dataclass
class CommandGroup:
    id: str
    stage: SetupStage
    title: str
    commands: list[str]
    verification: list[str] = field(default_factory=list)
    rationale: str = ""
    source: str = "repo"
    packages: list[str] = field(default_factory=list)
    optional: bool = False
    optional_slow: bool = False
    status: PlanItemStatus = PlanItemStatus.PENDING


@dataclass
class SetupFacts:
    distro_family: str = "unknown"
    package_manager: str = "unknown"
    community_helper: str = "none"
    init_system: str = "unknown"
    os_id: str = ""
    os_like: str = ""
    is_live: bool = False
    live_marker: str = ""
    boot_mode: str = "unknown"
    target_root_mounted: bool = False
    current_user: str = ""
    login_user: str = ""
    is_root: bool = False
    sudo_nopasswd: bool = False
    gpu_vendors: list[str] = field(default_factory=list)
    gpu_inventory: list[str] = field(default_factory=list)
    nvidia_smi_state: str = "unknown"
    driver_modules: list[str] = field(default_factory=list)
    session_type: str = ""
    display_manager: str = ""
    display_manager_enabled: bool = False
    desktop_runtime_state: DesktopRuntimeState = DesktopRuntimeState.UNKNOWN
    displays: list[DisplayRuntimeInfo] = field(default_factory=list)
    requested_app_states: dict[str, str] = field(default_factory=dict)
    pending_question_id: str = ""
    pending_question_blocking: bool = False
    waiting_for_input: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResearchPlan:
    target_summary: str
    distro_family: str
    package_manager: str
    driver_strategy: StrategyDecision
    desktop_strategy: StrategyDecision
    command_groups: list[CommandGroup] = field(default_factory=list)
    apps: list[AppPlanItem] = field(default_factory=list)
    unresolved_questions: list[dict[str, Any]] = field(default_factory=list)
    verification_commands: dict[str, list[str]] = field(default_factory=dict)
    required_items: list[str] = field(default_factory=list)
    optional_items: list[str] = field(default_factory=list)
    planner_revision: int = 1


@dataclass
class AuditResult:
    stage: SetupStage
    passed: bool
    blockers: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    partial: bool = False
