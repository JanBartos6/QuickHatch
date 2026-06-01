from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from quickhatch.setup.audit import audit_stage, final_audit
from quickhatch.setup.preflight import preflight_setup_decisions
from quickhatch.setup.types import (
    AuditResult,
    FinalSetupStatus,
    PreflightDecisionKind,
    ResearchPlan,
    SetupFacts,
    SetupStage,
    StrategyDecision,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "driver_uncertainty_scenarios.yaml"


FINAL_STATUS_EFFECTS = {
    FinalSetupStatus.SUCCESS: "none",
    FinalSetupStatus.SUCCESS_WITH_WARNINGS: "success_with_warnings",
    FinalSetupStatus.MANUAL_FOLLOWUP_REQUIRED: "manual_followup_required",
    FinalSetupStatus.UNSUPPORTED_SAFE: "unsupported_safe",
    FinalSetupStatus.BLOCKED: "blocked",
}
EXPECTED_FINAL_STATUSES = {
    "none": FinalSetupStatus.SUCCESS,
    "success_with_warnings": FinalSetupStatus.SUCCESS_WITH_WARNINGS,
    "manual_followup_required": FinalSetupStatus.MANUAL_FOLLOWUP_REQUIRED,
    "unsupported_safe": FinalSetupStatus.UNSUPPORTED_SAFE,
    "blocked": FinalSetupStatus.BLOCKED,
}
PHYSICAL_GPU_VENDORS = {"amd", "intel", "nvidia"}
RISKY_DRIVER_BOOT_COMMANDS = (
    ("sudo update-initramfs -u -k all", "initramfs_rebuild"),
    ("sudo mkinitcpio -P", "initramfs_rebuild"),
    ("sudo dracut --force --regenerate-all", "initramfs_rebuild"),
    ("sudo kernel-install add 6.1.0 /boot/vmlinuz-linux", "boot_artifact_update"),
)


@dataclass(frozen=True)
class FakeCommandResult:
    event_type: str
    detail: str
    rc: int
    stdout: str
    stderr: str


class FakeExecutor:
    """Capture scripted driver-stage events without running shell commands."""

    def __init__(self) -> None:
        self.results: list[FakeCommandResult] = []

    def record(self, event: dict[str, Any]) -> FakeCommandResult:
        result = FakeCommandResult(
            event_type=str(event.get("type") or ""),
            detail=str(event.get("detail") or ""),
            rc=int(event.get("rc") or 0),
            stdout=str(event.get("stdout") or ""),
            stderr=str(event.get("stderr") or ""),
        )
        self.results.append(result)
        return result


class FakeDriverStrategyProvider:
    """Return the fixture's driver strategy as a normal ResearchPlan strategy."""

    def __init__(self, scenario: dict[str, Any]) -> None:
        self.scenario = scenario

    def strategy(self) -> StrategyDecision:
        raw = self.scenario["driver_strategy"]
        source = str(raw.get("source") or "manual_followup")
        return StrategyDecision(
            strategy=source,
            confidence=str(raw.get("confidence") or "manual_followup_required"),
            source=source,
            packages=list(raw.get("packages") or []),
            notes=str(raw.get("notes") or ""),
        )


@dataclass
class FakeRunLog:
    events: list[dict[str, Any]] = field(default_factory=list)

    def record(self, event_type: str, **payload: Any) -> None:
        self.events.append({"event_type": event_type, **payload})


@dataclass
class DriverScenarioOutcome:
    scenario_id: str
    facts: SetupFacts
    stage_state: dict[str, Any]
    postboot_result: AuditResult
    final_result: AuditResult
    runlog: FakeRunLog
    executed: FakeExecutor

    @property
    def final_status_effect(self) -> str:
        assert self.final_result.final_status is not None
        return FINAL_STATUS_EFFECTS[self.final_result.final_status]


def _load_fixture() -> dict[str, Any]:
    payload = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert isinstance(payload.get("scenarios"), list)
    return payload


FIXTURE = _load_fixture()
SCENARIOS: list[dict[str, Any]] = list(FIXTURE["scenarios"])


def _fixture_id(scenario: dict[str, Any]) -> str:
    return str(scenario["id"])


def _facts_from_profile(profile: dict[str, Any]) -> SetupFacts:
    vendors = [str(vendor).lower() for vendor in profile.get("gpu_vendors") or []]
    kernel_module_loaded = profile.get("kernel_module_loaded")
    nvidia_driver_installed = profile.get("nvidia_driver_installed")
    nouveau_loaded = profile.get("nouveau_loaded") is True
    nvidia_smi_state = "unknown"
    nvidia_kernel_driver = ""
    driver_modules: list[str] = []
    amd_kernel_driver = ""
    intel_kernel_driver = ""

    if "nvidia" in vendors:
        if kernel_module_loaded is True:
            nvidia_smi_state = "ok"
            nvidia_kernel_driver = "nvidia"
            driver_modules.extend(["nvidia", "nvidia_drm"])
        elif nouveau_loaded:
            nvidia_smi_state = "present_but_failing" if nvidia_driver_installed else "missing"
            nvidia_kernel_driver = "nouveau"
            driver_modules.append("nouveau")
        elif nvidia_driver_installed is True:
            nvidia_smi_state = "present_but_failing"
            nvidia_kernel_driver = "none"
        elif kernel_module_loaded is False:
            nvidia_smi_state = "missing"
            nvidia_kernel_driver = "none"

    if "amd" in vendors:
        if kernel_module_loaded is True:
            amd_kernel_driver = "amdgpu"
        elif kernel_module_loaded is False:
            amd_kernel_driver = "none"
        else:
            amd_kernel_driver = "unknown"

    if "intel" in vendors:
        if kernel_module_loaded is True:
            intel_kernel_driver = "i915"
        elif kernel_module_loaded is False:
            intel_kernel_driver = "none"
        else:
            intel_kernel_driver = "unknown"

    package_manager = str(profile.get("package_manager") or "unknown")
    family = {
        "apt": "ubuntu",
        "dnf": "fedora",
        "generic": "generic",
        "pacman": "arch",
    }.get(package_manager, "generic")
    secure_boot = str(profile.get("secure_boot") or "unknown")
    is_vm = profile.get("is_vm") is True

    return SetupFacts(
        distro_family=family,
        os_id=family,
        package_manager=package_manager,
        current_user="quickhatch",
        login_user="quickhatch",
        sudo_nopasswd=True,
        secure_boot_state=secure_boot,
        virtualization="kvm" if is_vm else "",
        gpu_vendors=vendors,
        gpu_inventory=[str(model) for model in profile.get("gpu_models") or []],
        nvidia_smi_state=nvidia_smi_state,
        nvidia_kernel_driver=nvidia_kernel_driver,
        nvidia_nouveau_bound=nouveau_loaded,
        amd_kernel_driver=amd_kernel_driver,
        intel_kernel_driver=intel_kernel_driver,
        vulkan_state="unknown",
        driver_modules=driver_modules,
        raw={
            "QH_HAS_GUI_SESSION": str(profile.get("has_gui_session")),
            "QH_IS_VM": "yes" if is_vm else "no",
            "QH_DRIVER_FIXTURE": "driver_uncertainty",
        },
    )


def _plan_for_scenario(scenario: dict[str, Any], facts: SetupFacts) -> ResearchPlan:
    strategy = FakeDriverStrategyProvider(scenario).strategy()
    return ResearchPlan(
        target_summary=f"Driver uncertainty scenario {scenario['id']}",
        distro_family=facts.distro_family,
        package_manager=facts.package_manager,
        driver_strategy=strategy,
        desktop_strategy=StrategyDecision("none", confidence="resolved_official"),
    )


def _apply_scripted_events(
    scenario: dict[str, Any],
    facts: SetupFacts,
    executor: FakeExecutor,
    runlog: FakeRunLog,
) -> dict[str, Any]:
    failed_verifications: list[str] = []
    for event in scenario.get("simulated_events") or []:
        result = executor.record(event)
        runlog.record(
            result.event_type,
            scenario_id=scenario["id"],
            detail=result.detail,
            rc=result.rc,
            stdout_tail=result.stdout[-240:],
            stderr_tail=result.stderr[-240:],
        )
        event_text = " ".join((result.detail, result.stdout, result.stderr)).lower()
        _apply_driver_fact_refresh(facts, event_text)
        if (
            result.event_type
            in {
                "command_failure",
                "module_probe",
                "provider_error",
            }
            and result.rc != 0
        ):
            reason = result.detail or result.stderr or "driver operation failed"
            failed_verifications.append(f"Driver follow-up: {reason}")
        if result.event_type == "facts_refresh" and result.rc != 0:
            facts.fact_probe_ok = False
            reason = result.detail or result.stderr or "driver fact refresh failed"
            failed_verifications.append(f"Driver follow-up: {reason}")
        if "blocked_without_capability" in result.stderr or "denied by safety" in event_text:
            failed_verifications.append(f"Driver safety block: {result.stderr or result.detail}")
        if "vulkan" in event_text and ("fails" in event_text or "failure" in event_text):
            failed_verifications.append("Driver follow-up: Vulkan graphics probe failed.")

    if not failed_verifications:
        return {}
    return {"failed_verifications": failed_verifications}


def _apply_driver_fact_refresh(facts: SetupFacts, event_text: str) -> None:
    negative_load = any(
        phrase in event_text
        for phrase in (
            "do not show nvidia module loaded",
            "does not show nvidia module loaded",
            "not show nvidia module loaded",
            "no nvidia module loaded",
            "module not loaded",
            "kernel_driver=missing",
        )
    )
    if (
        "nvidia_smi_state=ok" in event_text
        or "nvidia_smi=ok" in event_text
        or "nvidia-smi ok" in event_text
        or ("nvidia module loaded" in event_text and not negative_load)
        or ("nvidia kernel module" in event_text and not negative_load)
        or "nvidia-smi work" in event_text
        or "nvidia-smi and kernel module checks pass" in event_text
    ) and "nvidia" in facts.gpu_vendors:
        facts.nvidia_smi_state = "ok"
        facts.nvidia_kernel_driver = "nvidia"
        facts.nvidia_nouveau_bound = False
        facts.driver_modules = sorted({*facts.driver_modules, "nvidia", "nvidia_drm"})
    if (
        "nouveau=false" in event_text
        or "nouveau is gone" in event_text
        or "nouveau absent" in event_text
    ):
        facts.nvidia_nouveau_bound = False
        if facts.nvidia_kernel_driver == "nouveau":
            facts.nvidia_kernel_driver = "nvidia" if facts.nvidia_smi_state == "ok" else "none"
    if (
        "nouveau persists" in event_text
        or "nouveau remains" in event_text
        or "still bound to nouveau" in event_text
    ):
        facts.nvidia_nouveau_bound = True
        facts.nvidia_kernel_driver = "nouveau"
        facts.nvidia_smi_state = "present_but_failing"
    if "driver=radeon" in event_text or "legacy radeon module" in event_text:
        facts.amd_kernel_driver = "radeon"
    elif "amdgpu" in event_text and "instead of amdgpu" not in event_text:
        facts.amd_kernel_driver = "amdgpu"
    if "kernel_driver=missing" in event_text:
        if "intel" in facts.gpu_vendors:
            facts.intel_kernel_driver = "none"
        if "amd" in facts.gpu_vendors:
            facts.amd_kernel_driver = "none"
    elif re.search(r"\bi915\b|\bxe\b", event_text) and "do not prove i915" not in event_text:
        facts.intel_kernel_driver = "i915"
    if "vulkan=ok" in event_text:
        facts.vulkan_state = "ok"
    if "vulkan_state=unknown" in event_text:
        facts.vulkan_state = "unknown"
    if "vulkan_state=present_but_failing" in event_text or (
        "vulkan" in event_text and any(term in event_text for term in ("failed", "fails"))
    ):
        facts.vulkan_state = "present_but_failing"


def _run_current_driver_audit(scenario: dict[str, Any]) -> DriverScenarioOutcome:
    facts = _facts_from_profile(scenario["facts_profile"])
    plan = _plan_for_scenario(scenario, facts)
    executor = FakeExecutor()
    runlog = FakeRunLog()
    stage_state = _apply_scripted_events(scenario, facts, executor, runlog)
    postboot_result = audit_stage(
        SetupStage.POSTBOOT_DRIVER,
        facts,
        plan=plan,
        stage_state=stage_state,
    )
    unrelated_successes = [
        AuditResult(
            stage=SetupStage.APPS,
            passed=True,
            details={"apps": {"Firefox": "installed"}},
        ),
        AuditResult(
            stage=SetupStage.DESKTOP,
            passed=True,
            details={"desktop_manual_followups": []},
        ),
    ]
    final_result = final_audit(facts, plan, [*unrelated_successes, postboot_result])
    return DriverScenarioOutcome(
        scenario_id=str(scenario["id"]),
        facts=facts,
        stage_state=stage_state,
        postboot_result=postboot_result,
        final_result=final_result,
        runlog=runlog,
        executed=executor,
    )


def _expected_final_status(scenario: dict[str, Any]) -> FinalSetupStatus:
    return EXPECTED_FINAL_STATUSES[scenario["expected"]["final_status_effect"]]


def _finding_text(result: AuditResult) -> str:
    return " ".join(
        str(value) for finding in result.findings for value in [finding.summary, *finding.evidence]
    ).lower()


def _physical_gpu_vendors(scenario: dict[str, Any]) -> set[str]:
    return {
        str(vendor).lower()
        for vendor in scenario["facts_profile"].get("gpu_vendors") or []
        if str(vendor).lower() in PHYSICAL_GPU_VENDORS
    }


def _is_nvidia_scenario(scenario: dict[str, Any]) -> bool:
    return "nvidia" in _physical_gpu_vendors(scenario)


def _expected_driver_status(scenario: dict[str, Any]) -> str:
    return str(scenario["expected"]["driver_status"])


def _format_policy_mismatch(scenario: dict[str, Any], outcome: DriverScenarioOutcome) -> str:
    final_status = outcome.final_result.final_status
    assert final_status is not None
    findings = [
        f"{finding.severity.value}/{finding.category}: {finding.summary}"
        for finding in outcome.final_result.findings
    ]
    return (
        f"{scenario['id']} expected final_status_effect="
        f"{scenario['expected']['final_status_effect']} but current audit produced "
        f"{FINAL_STATUS_EFFECTS[final_status]}; postboot_blockers="
        f"{outcome.postboot_result.blockers}; findings={findings}"
    )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=_fixture_id)
def test_driver_uncertainty_scenarios_match_current_final_status_policy(
    scenario: dict[str, Any],
) -> None:
    outcome = _run_current_driver_audit(scenario)

    assert outcome.final_result.final_status == _expected_final_status(scenario), (
        _format_policy_mismatch(scenario, outcome)
    )


@pytest.mark.parametrize(
    "scenario",
    [
        scenario
        for scenario in SCENARIOS
        if _is_nvidia_scenario(scenario) and _expected_driver_status(scenario) != "verified"
    ],
    ids=_fixture_id,
)
def test_nvidia_uncertainty_is_honest(scenario: dict[str, Any]) -> None:
    outcome = _run_current_driver_audit(scenario)

    assert outcome.final_result.final_status != FinalSetupStatus.SUCCESS, (
        "unknown, unsupported, legacy, conflicting, or unverified NVIDIA work must not "
        f"be plain success: {_format_policy_mismatch(scenario, outcome)}"
    )


@pytest.mark.parametrize(
    "scenario",
    [
        scenario
        for scenario in SCENARIOS
        if scenario["category"] == "secure_boot" and _expected_driver_status(scenario) != "verified"
    ],
    ids=_fixture_id,
)
def test_secure_boot_uncertainty_is_honest(scenario: dict[str, Any]) -> None:
    outcome = _run_current_driver_audit(scenario)

    assert outcome.final_result.final_status != FinalSetupStatus.SUCCESS, (
        "Secure Boot with unverified module load, pending MOK, or signing uncertainty "
        f"must not be plain success: {_format_policy_mismatch(scenario, outcome)}"
    )
    evidence = _finding_text(outcome.final_result)
    assert any(term in evidence for term in ("secure boot", "mok", "sign", "module", "nvidia"))


@pytest.mark.parametrize(
    "scenario",
    [scenario for scenario in SCENARIOS if scenario["category"] == "vm_gpu"],
    ids=_fixture_id,
)
def test_vm_graphics_are_not_real_hardware_certification(scenario: dict[str, Any]) -> None:
    outcome = _run_current_driver_audit(scenario)

    assert scenario["facts_profile"]["is_vm"] is True
    assert _expected_driver_status(scenario) in {
        "manual_followup_required",
        "skipped",
        "unsupported_safe",
    }
    if scenario["expected"]["final_status_effect"] != "none":
        assert outcome.final_result.final_status != FinalSetupStatus.SUCCESS, (
            "virtual GPU facts must not become plain physical GPU certification: "
            f"{_format_policy_mismatch(scenario, outcome)}"
        )


@pytest.mark.parametrize(
    "scenario",
    [
        scenario
        for scenario in SCENARIOS
        if scenario["category"] in {"amd", "intel"}
        and scenario["expected"]["final_status_effect"] != "blocked"
    ],
    ids=_fixture_id,
)
def test_amd_intel_cases_are_not_overblocked(scenario: dict[str, Any]) -> None:
    outcome = _run_current_driver_audit(scenario)

    assert outcome.final_result.final_status != FinalSetupStatus.BLOCKED, (
        "normal AMD/Intel Mesa/firmware uncertainty should become warning/manual "
        f"follow-up, not a fatal setup block: {_format_policy_mismatch(scenario, outcome)}"
    )


@pytest.mark.parametrize(
    "scenario",
    [
        scenario
        for scenario in SCENARIOS
        if "firmware"
        in (scenario.get("rationale", "") + scenario["driver_strategy"]["notes"]).lower()
        and scenario["expected"]["final_status_effect"] == "manual_followup_required"
    ],
    ids=_fixture_id,
)
def test_missing_firmware_has_specific_nonfatal_followup(scenario: dict[str, Any]) -> None:
    outcome = _run_current_driver_audit(scenario)

    assert outcome.final_result.final_status == FinalSetupStatus.MANUAL_FOLLOWUP_REQUIRED, (
        "missing GPU firmware should be specific manual follow-up, not success or fatal block: "
        f"{_format_policy_mismatch(scenario, outcome)}"
    )
    assert "firmware" in _finding_text(outcome.final_result)


@pytest.mark.parametrize(
    "scenario",
    [
        scenario
        for scenario in SCENARIOS
        if scenario["expected"]["should_reboot_verify"] is True
        and _expected_driver_status(scenario) == "installed_unverified"
    ],
    ids=_fixture_id,
)
def test_reboot_required_install_success_is_not_verified_success(
    scenario: dict[str, Any],
) -> None:
    outcome = _run_current_driver_audit(scenario)

    assert outcome.final_result.final_status != FinalSetupStatus.SUCCESS, (
        "driver install success before reboot/module facts must not be verified success: "
        f"{_format_policy_mismatch(scenario, outcome)}"
    )


def test_risky_driver_boot_artifact_commands_require_capability() -> None:
    facts = SetupFacts(
        distro_family="ubuntu",
        package_manager="apt",
        current_user="quickhatch",
        login_user="quickhatch",
        sudo_nopasswd=True,
    )

    for command, capability_kind in RISKY_DRIVER_BOOT_COMMANDS:
        decisions = preflight_setup_decisions(command, facts)
        blocks = [
            decision for decision in decisions if decision.kind == PreflightDecisionKind.BLOCK
        ]
        assert blocks, f"{command} did not hard-block without capability"
        assert any(capability_kind in block.reason for block in blocks), blocks


@pytest.mark.parametrize("scenario", SCENARIOS, ids=_fixture_id)
def test_driver_uncertainty_preserves_unrelated_setup_progress(
    scenario: dict[str, Any],
) -> None:
    outcome = _run_current_driver_audit(scenario)

    if scenario["expected"]["should_continue_unrelated_setup"] is True:
        assert outcome.final_result.final_status != FinalSetupStatus.BLOCKED, (
            "driver uncertainty should preserve unrelated app/theme progress unless core "
            f"boot or reconnect safety is at risk: {_format_policy_mismatch(scenario, outcome)}"
        )
    else:
        assert outcome.final_result.final_status == FinalSetupStatus.BLOCKED


@pytest.mark.parametrize(
    "scenario",
    [
        scenario
        for scenario in SCENARIOS
        if scenario["expected"]["final_status_effect"]
        in {"manual_followup_required", "unsupported_safe"}
    ],
    ids=_fixture_id,
)
def test_manual_followup_driver_evidence_mentions_reason(scenario: dict[str, Any]) -> None:
    outcome = _run_current_driver_audit(scenario)

    assert outcome.final_result.findings, f"{scenario['id']} produced no follow-up evidence"
    evidence = _finding_text(outcome.final_result)
    assert any(
        term in evidence
        for term in (
            "amd",
            "driver",
            "firmware",
            "gpu",
            "intel",
            "mok",
            "nouveau",
            "nvidia",
            "secure boot",
            "vulkan",
        )
    ), f"{scenario['id']} evidence is not driver-specific: {evidence}"


def test_driver_uncertainty_fixture_schema_and_coverage() -> None:
    summary = FIXTURE["merge_summary"]
    scenario_ids = [scenario["id"] for scenario in SCENARIOS]
    categories = Counter(scenario["category"] for scenario in SCENARIOS)
    driver_statuses = Counter(scenario["expected"]["driver_status"] for scenario in SCENARIOS)
    final_effects = Counter(scenario["expected"]["final_status_effect"] for scenario in SCENARIOS)

    assert len(scenario_ids) == len(set(scenario_ids))
    assert summary["total_scenarios"] == len(SCENARIOS)
    assert summary["scenarios_by_category"] == dict(sorted(categories.items()))
    assert summary["driver_statuses"] == dict(sorted(driver_statuses.items()))
    assert summary["final_status_effects"] == dict(sorted(final_effects.items()))
    assert summary["rejected_ambiguous_cases"] == []
    assert 60 <= len(SCENARIOS) <= 120
    assert {"nvidia", "secure_boot", "amd", "intel", "vm_gpu"} <= set(categories)
    assert {
        "blocked",
        "installed_unverified",
        "manual_followup_required",
        "skipped",
        "unsupported_safe",
        "verified",
    } <= set(driver_statuses)
    assert {
        "blocked",
        "manual_followup_required",
        "none",
        "success_with_warnings",
        "unsupported_safe",
    } <= set(final_effects)
    fixture_text = FIXTURE_PATH.read_text(encoding="utf-8").lower()
    assert "no real hardware certification is claimed" in fixture_text


@pytest.mark.parametrize("scenario", SCENARIOS, ids=_fixture_id)
def test_driver_uncertainty_fixture_expected_outcomes_are_deterministic(
    scenario: dict[str, Any],
) -> None:
    expected = scenario["expected"]
    driver_status = expected["driver_status"]
    final_effect = expected["final_status_effect"]
    physical_vendors = _physical_gpu_vendors(scenario)

    assert driver_status in {
        "blocked",
        "installed_unverified",
        "manual_followup_required",
        "skipped",
        "unsupported_safe",
        "verified",
    }
    assert final_effect in EXPECTED_FINAL_STATUSES
    if driver_status in {"manual_followup_required", "unsupported_safe", "blocked"}:
        assert final_effect in {
            "manual_followup_required",
            "unsupported_safe",
            "blocked",
            "success_with_warnings",
        }
    if driver_status == "verified":
        assert physical_vendors
    if driver_status == "skipped":
        assert (
            scenario["facts_profile"]["is_vm"] is True
            or not physical_vendors
            or scenario["driver_strategy"]["requested"] is False
        )
    if expected["should_continue_unrelated_setup"] is False:
        assert final_effect == "blocked"
        assert driver_status == "blocked"
