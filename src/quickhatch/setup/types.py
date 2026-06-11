"""Typed models for staged remote setup."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .operations import SetupOperation


class SetupStage(str, Enum):
    CONNECT_AND_PROBE = "connect_and_probe"
    RESEARCH_AND_PLAN = "research_and_plan"
    INSTALL_OR_SKIP = "install_or_skip"
    POSTBOOT_DRIVER = "postboot_driver"
    DESKTOP = "desktop"
    APPS = "apps"
    DATA_TRANSFER = "data_transfer"
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


class PreflightDecisionKind(str, Enum):
    BLOCK = "block"
    ADVISORY = "advisory"
    SHADOW = "shadow"


class FailureCategory(str, Enum):
    NETWORK = "network"
    PACKAGE_MISSING = "package_missing"
    PACKAGE_CONFLICT = "package_conflict"
    SUDO_PASSWORD = "sudo_password"
    BOOTLOADER = "bootloader"
    FSTAB = "fstab"
    SSH_RECONNECT = "ssh_reconnect"
    DISPLAY_SESSION = "display_session"
    DRIVER_PENDING_REBOOT = "driver_pending_reboot"
    TIMEOUT = "timeout"
    MODEL_JSON = "model_json"
    HARNESS_FALSE_POSITIVE = "harness_false_positive"
    UNKNOWN = "unknown"


class DriverAuditState(str, Enum):
    HEALTHY = "healthy"
    NEEDS_REBOOT = "needs_reboot"
    MANUAL_FOLLOWUP_SECURE_BOOT = "manual_followup_secure_boot"
    UNSUPPORTED_HARDWARE = "unsupported_hardware"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class FinalSetupStatus(str, Enum):
    SUCCESS = "success"
    SUCCESS_WITH_WARNINGS = "success_with_warnings"
    MANUAL_FOLLOWUP_REQUIRED = "manual_followup_required"
    UNSUPPORTED_SAFE = "unsupported_safe"
    BLOCKED = "blocked"


class AuditFindingSeverity(str, Enum):
    FATAL = "fatal"
    REPAIRABLE = "repairable"
    MANUAL_FOLLOWUP = "manual_followup"
    WARNING = "warning"
    UNSUPPORTED_SAFE = "unsupported_safe"


@dataclass
class PreflightDecision:
    kind: PreflightDecisionKind
    category: str
    reason: str
    suggested_next: str = ""
    command: str = ""


@dataclass
class FailureClassification:
    category: FailureCategory
    detail: str = ""
    confidence: str = "medium"


@dataclass
class AuditFinding:
    id: str
    severity: AuditFindingSeverity
    category: str
    summary: str
    evidence: list[str] = field(default_factory=list)
    suggested_repair_scope: str = ""
    required_capability_kinds: list[str] = field(default_factory=list)


@dataclass
class CertificationResult:
    success: bool
    family: str
    status: str
    artifact_dir: str = ""
    blockers: list[str] = field(default_factory=list)
    summary: str = ""
    snapshots: list[str] = field(default_factory=list)


@dataclass
class DisplayRuntimeInfo:
    name: str
    current_mode: str = ""
    advertised_modes: list[str] = field(default_factory=list)
    refresh_hz: float | None = None
    scale_percent: int | None = None
    primary: bool = False


@dataclass
class DesktopDesignState:
    environment: str = ""
    kde_color_scheme: str = ""
    kde_icon_theme: str = ""
    kde_cursor_theme: str = ""
    kde_wallpaper: str = ""
    kde_wobbly_windows: str = ""
    kde_magic_lamp: str = ""
    gnome_gtk_theme: str = ""
    gnome_icon_theme: str = ""
    gnome_cursor_theme: str = ""
    gnome_wallpaper: str = ""
    xfce_gtk_theme: str = ""
    xfce_icon_theme: str = ""
    xfce_cursor_theme: str = ""
    xfce_window_theme: str = ""
    xfce_wallpaper: str = ""


@dataclass
class PlanEvidence:
    source_type: str
    summary: str
    query: str = ""
    url: str = ""
    title: str = ""
    confidence: str = "medium"


@dataclass
class StrategyDecision:
    strategy: str
    confidence: str = "manual_followup_required"
    source: str = "manual_followup"
    packages: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    notes: str = ""
    evidence: list[PlanEvidence] = field(default_factory=list)


@dataclass
class AppPlanItem:
    name: str
    package_name: str = ""
    source: str = "manual_followup"
    status: PlanItemStatus = PlanItemStatus.PENDING
    required: bool = False
    optional_slow: bool = False
    reason: str = ""
    evidence: list[PlanEvidence] = field(default_factory=list)


@dataclass
class CommandGroup:
    id: str
    stage: SetupStage
    title: str
    commands: list[str] = field(default_factory=list)
    operations: list[SetupOperation] = field(default_factory=list)
    verification: list[str] = field(default_factory=list)
    rationale: str = ""
    source: str = "repo"
    packages: list[str] = field(default_factory=list)
    optional: bool = False
    optional_slow: bool = False
    status: PlanItemStatus = PlanItemStatus.PENDING
    evidence: list[PlanEvidence] = field(default_factory=list)


@dataclass
class SetupFacts:
    distro_family: str = "unknown"
    package_manager: str = "unknown"
    community_helper: str = "none"
    init_system: str = "unknown"
    os_id: str = ""
    os_like: str = ""
    kernel_release: str = ""
    virtualization: str = ""
    system_vendor: str = ""
    product_name: str = ""
    is_live: bool = False
    live_marker: str = ""
    boot_mode: str = "unknown"
    secure_boot_state: str = "unknown"
    target_root_mounted: bool = False
    current_user: str = ""
    login_user: str = ""
    is_root: bool = False
    sudo_nopasswd: bool = False
    gpu_vendors: list[str] = field(default_factory=list)
    gpu_inventory: list[str] = field(default_factory=list)
    nvidia_smi_state: str = "unknown"
    nvidia_kernel_driver: str = ""
    nvidia_nouveau_bound: bool = False
    amd_kernel_driver: str = ""
    intel_kernel_driver: str = ""
    vulkan_state: str = "unknown"
    vulkan_devices: list[str] = field(default_factory=list)
    vaapi_state: str = "unknown"
    rocm_state: str = "unknown"
    driver_modules: list[str] = field(default_factory=list)
    session_type: str = ""
    display_manager: str = ""
    display_manager_enabled: bool = False
    display_manager_active: bool = False
    desktop_runtime_state: DesktopRuntimeState = DesktopRuntimeState.UNKNOWN
    displays: list[DisplayRuntimeInfo] = field(default_factory=list)
    desktop_design: DesktopDesignState = field(default_factory=DesktopDesignState)
    requested_app_states: dict[str, str] = field(default_factory=dict)
    pending_question_id: str = ""
    pending_question_blocking: bool = False
    waiting_for_input: bool = False
    fact_probe_ok: bool = True
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
    findings: list[AuditFinding] = field(default_factory=list)
    final_status: FinalSetupStatus | None = None
