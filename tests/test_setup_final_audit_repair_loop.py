from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from quickhatch.bridge.server import BridgeState
from quickhatch.config import AIConfig, UserProfile
from quickhatch.setup.capabilities import CapabilityStore, dangerous_command_requirements
from quickhatch.setup.orchestrator import GroupFailure, SetupOrchestrator, SetupRuntime
from quickhatch.setup.preflight import preflight_setup_command
from quickhatch.setup.types import (
    AuditFinding,
    AuditFindingSeverity,
    AuditResult,
    CommandGroup,
    FinalSetupStatus,
    ResearchPlan,
    SetupFacts,
    SetupStage,
    StrategyDecision,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "final_audit_repair_scenarios.yaml"
RUN_ID = "final-audit-repair-test"


@dataclass
class MemoryRunLog:
    events: list[dict[str, Any]] = field(default_factory=list)

    def record(self, event_type: str, **payload: Any) -> None:
        self.events.append({"event_type": event_type, **payload})


@dataclass
class RepairCall:
    stage: SetupStage
    request_group: CommandGroup
    failure: GroupFailure
    repair_attempt: int


@dataclass
class ExecuteCall:
    stage: SetupStage
    group: CommandGroup


class ScriptedFinalAuditRepairHarness:
    """Run the real final-audit loop without executing shell commands."""

    def __init__(self, scenario: dict[str, Any]) -> None:
        self.scenario = scenario
        self.compute_calls = 0
        self.repair_calls: list[RepairCall] = []
        self.execute_calls: list[ExecuteCall] = []
        self.runlog = MemoryRunLog()
        self.orchestrator = _orchestrator()
        self.orchestrator.runlog = self.runlog  # type: ignore[assignment]
        self.orchestrator._compute_final_audit_result = self._compute_result  # type: ignore[method-assign]
        self.orchestrator._repair_failed_group = self._repair_failed_group  # type: ignore[method-assign]
        self.orchestrator._execute_group = self._execute_group  # type: ignore[method-assign]
        self.orchestrator._refresh_facts = lambda *, stage: None  # type: ignore[method-assign]

    def run(self) -> AuditResult:
        return self.orchestrator._run_final_audit()

    def _compute_result(self) -> AuditResult:
        self.compute_calls += 1
        if self.compute_calls == 1:
            return _result_from_raw_findings(self.scenario["initial_findings"])
        return _result_from_raw_findings(self.scenario["repair_result"]["remaining_findings"])

    def _repair_failed_group(
        self,
        stage: SetupStage,
        failed_group: CommandGroup,
        failure: GroupFailure,
        *,
        repair_attempt: int,
    ) -> dict[str, Any]:
        self.repair_calls.append(
            RepairCall(
                stage=stage,
                request_group=failed_group,
                failure=failure,
                repair_attempt=repair_attempt,
            )
        )
        outcome = self.scenario["repair_result"]["outcome"]
        if outcome == "provider_error":
            raise RuntimeError(_remaining_summary(self.scenario) or "provider error")
        if outcome == "not_attempted":
            return {
                "action": "manual_followup",
                "reason": _remaining_summary(self.scenario) or "repair not attempted",
            }
        return {"action": "retry_group", "group": _repair_group_for_scenario(self.scenario, stage)}

    def _execute_group(
        self,
        stage: SetupStage,
        group: CommandGroup,
        _stage_state: dict[str, Any],
    ) -> GroupFailure | None:
        self.execute_calls.append(ExecuteCall(stage=stage, group=group))
        outcome = self.scenario["repair_result"]["outcome"]
        if outcome == "success":
            return None
        command = group.commands[0] if group.commands else "final audit repair"
        detail = _remaining_summary(self.scenario) or f"repair ended with {outcome}"
        return GroupFailure(outcome, command, "", "", detail)


def _load_fixture() -> dict[str, Any]:
    payload = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert isinstance(payload.get("scenarios"), list)
    return payload


FIXTURE = _load_fixture()
SCENARIOS: list[dict[str, Any]] = list(FIXTURE["scenarios"])
SCENARIO_BY_ID = {scenario["id"]: scenario for scenario in SCENARIOS}


def _fixture_id(scenario: dict[str, Any]) -> str:
    return str(scenario["id"])


def _expected_status(scenario: dict[str, Any]) -> FinalSetupStatus:
    return FinalSetupStatus(str(scenario["expected"]["final_status"]))


def _severity(value: str) -> AuditFindingSeverity:
    return AuditFindingSeverity(str(value))


def _finding(raw: dict[str, Any], index: int) -> AuditFinding:
    summary = str(raw.get("summary") or "")
    category = str(raw.get("category") or "setup")
    return AuditFinding(
        id=str(raw.get("id") or f"{category}:{index}"),
        severity=_severity(str(raw["severity"])),
        category=category,
        summary=summary,
        required_capability_kinds=[str(kind) for kind in raw.get("required_capability_kinds", [])],
        suggested_repair_scope=f"final-audit fixture case for {category}",
    )


def _result_from_raw_findings(raw_findings: list[dict[str, Any]]) -> AuditResult:
    findings = [_finding(raw, index) for index, raw in enumerate(raw_findings)]
    blockers = [
        finding.summary for finding in findings if finding.severity == AuditFindingSeverity.FATAL
    ]
    result = AuditResult(
        stage=SetupStage.FINAL_AUDIT,
        passed=not blockers,
        blockers=blockers,
        findings=findings,
    )
    result.final_status = SetupOrchestrator._final_status_from_findings(result)
    return result


def _has_initial_repairable_finding(scenario: dict[str, Any]) -> bool:
    return any(
        str(finding["severity"]) == AuditFindingSeverity.REPAIRABLE.value
        for finding in scenario["initial_findings"]
    )


def _initial_categories(scenario: dict[str, Any]) -> set[str]:
    return {str(finding.get("category") or "") for finding in scenario["initial_findings"]}


def _remaining_summary(scenario: dict[str, Any]) -> str:
    summaries = [
        str(finding.get("summary") or "")
        for finding in scenario["repair_result"].get("remaining_findings", [])
        if finding.get("summary")
    ]
    return "; ".join(summaries)


def _repair_group_for_scenario(scenario: dict[str, Any], stage: SetupStage) -> CommandGroup:
    commands: list[str] = []
    title_parts: list[str] = []
    for group in scenario["repair_plan"].get("command_groups", []):
        title_parts.append(str(group.get("title") or "repair group"))
        commands.extend(str(command) for command in group.get("commands", []))
    return CommandGroup(
        id=f"{scenario['id']}-scripted-final-repair",
        stage=stage,
        title="; ".join(title_parts) or "Scripted final audit repair",
        commands=commands,
        verification=[],
        rationale=str(scenario.get("rationale") or ""),
    )


def _orchestrator() -> SetupOrchestrator:
    profile = UserProfile()
    profile.install_username = "jan"
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        os_id="arch",
        package_manager="pacman",
        current_user="jan",
        login_user="jan",
        sudo_nopasswd=True,
        is_live=False,
    )
    runtime = SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(provider="api_openai", endpoint="http://example", model_name="test"),
        host="127.0.0.1",
        user="jan",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=lambda _cfg, _messages, **_kwargs: json.dumps({}),
        probe_facts=lambda **_kwargs: facts,
        detect_live_env=lambda: (False, "INSTALLED"),
        ssh_exec=lambda _command, _timeout: (0, "", ""),
        repo_search=lambda _query: "",
        community_search=lambda _query: "",
        web_search=lambda _query, _max_results: "",
        wait_for_reconnect=lambda reconnect_user: (True, "fresh system info", reconnect_user),
        run_pre_reboot_checks=lambda: (True, [], "ok"),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda _current, _is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=False,
    )
    orchestrator = SetupOrchestrator(runtime)
    orchestrator.ctx.facts = facts
    orchestrator.ctx.plan = ResearchPlan(
        target_summary="final audit repair loop test",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision("none", confidence="resolved_official", source="repo"),
        desktop_strategy=StrategyDecision("none", confidence="resolved_official", source="repo"),
        command_groups=[],
        apps=[],
    )
    return orchestrator


def _summary_text(result: AuditResult) -> str:
    pieces: list[str] = []
    pieces.extend(result.blockers)
    pieces.extend(f"{finding.category} {finding.summary}" for finding in result.findings)
    pieces.extend(str(item) for item in result.details.get("final_audit_repairs", []))
    return "\n".join(pieces).lower()


def _facts_for_dangerous_repair() -> SetupFacts:
    return SetupFacts(
        distro_family="arch",
        os_id="arch",
        package_manager="pacman",
        current_user="root",
        login_user="root",
        is_root=True,
        is_live=True,
        target_root_mounted=True,
        sudo_nopasswd=True,
    )


def test_final_audit_repair_fixture_has_expected_policy_coverage() -> None:
    summary = FIXTURE["merge_summary"]
    assert summary["total_scenarios"] == len(SCENARIOS)
    assert summary["rejected_ambiguous_cases"] == []
    assert set(summary["scenarios_by_category"]) == {
        "app_followup",
        "desktop_followup",
        "failed_repair",
        "fatal_core",
        "repairable",
        "summary_status",
        "unsupported_safe",
    }
    assert set(summary["final_statuses"]) == {
        "blocked",
        "manual_followup_required",
        "success",
        "success_with_warnings",
        "unsupported_safe",
    }
    assert set(summary["repair_outcomes"]) == {
        "capability_required",
        "failure",
        "not_attempted",
        "preflight_block",
        "provider_error",
        "success",
    }


REMAINING_FINDING_STATUS_SCENARIOS = [
    scenario
    for scenario in SCENARIOS
    if not (
        scenario["repair_result"]["outcome"]
        in {"capability_required", "failure", "preflight_block", "provider_error"}
        and any(
            finding["severity"] == AuditFindingSeverity.REPAIRABLE.value
            for finding in scenario["repair_result"]["remaining_findings"]
        )
    )
]


@pytest.mark.parametrize("scenario", REMAINING_FINDING_STATUS_SCENARIOS, ids=_fixture_id)
def test_final_status_from_remaining_findings_matches_fixture(scenario: dict[str, Any]) -> None:
    result = _result_from_raw_findings(scenario["repair_result"]["remaining_findings"])

    assert result.final_status == _expected_status(scenario)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=_fixture_id)
def test_final_audit_repair_loop_matches_fixture_status(scenario: dict[str, Any]) -> None:
    harness = ScriptedFinalAuditRepairHarness(scenario)

    result = harness.run()

    assert result.final_status == _expected_status(scenario)
    assert len(harness.repair_calls) <= SetupOrchestrator.FINAL_AUDIT_REPAIRS
    assert bool(harness.repair_calls) is _has_initial_repairable_finding(scenario)


@pytest.mark.parametrize(
    "scenario",
    [scenario for scenario in SCENARIOS if _has_initial_repairable_finding(scenario)],
    ids=_fixture_id,
)
def test_repairable_findings_request_focused_bounded_repair(scenario: dict[str, Any]) -> None:
    harness = ScriptedFinalAuditRepairHarness(scenario)

    harness.run()

    assert 1 <= len(harness.repair_calls) <= SetupOrchestrator.FINAL_AUDIT_REPAIRS
    call = harness.repair_calls[0]
    assert call.request_group.id == "final-audit-repair-1"
    assert call.request_group.title == "Focused final audit repair"
    assert call.request_group.commands == []
    assert call.failure.command == "final audit"
    for finding in scenario["initial_findings"]:
        if finding["severity"] == AuditFindingSeverity.REPAIRABLE.value:
            assert str(finding["summary"]) in call.failure.detail
            assert {"finding": str(finding["summary"])} in call.request_group.evidence
    categories = _initial_categories(scenario)
    if categories & {"fstab", "bootability", "ssh_reconnect"}:
        assert call.stage == SetupStage.INSTALL_OR_SKIP
    elif "driver" in categories:
        assert call.stage == SetupStage.POSTBOOT_DRIVER
    elif "desktop" in categories:
        assert call.stage == SetupStage.DESKTOP
    else:
        assert call.stage == SetupStage.APPS


@pytest.mark.parametrize(
    "scenario",
    [
        scenario
        for scenario in SCENARIOS
        if scenario["repair_result"]["outcome"]
        in {"capability_required", "failure", "preflight_block", "provider_error"}
    ],
    ids=_fixture_id,
)
def test_failed_or_blocked_repair_is_not_plain_success(scenario: dict[str, Any]) -> None:
    result = ScriptedFinalAuditRepairHarness(scenario).run()

    assert result.final_status != FinalSetupStatus.SUCCESS
    if result.final_status == FinalSetupStatus.BLOCKED:
        assert any(finding.severity == AuditFindingSeverity.FATAL for finding in result.findings)
    else:
        assert result.final_status in {
            FinalSetupStatus.MANUAL_FOLLOWUP_REQUIRED,
            FinalSetupStatus.SUCCESS_WITH_WARNINGS,
            FinalSetupStatus.UNSUPPORTED_SAFE,
        }


@pytest.mark.parametrize(
    "scenario",
    [
        scenario
        for scenario in SCENARIOS
        if scenario["category"] in {"app_followup", "desktop_followup"}
    ],
    ids=_fixture_id,
)
def test_app_and_desktop_findings_are_nonfatal(scenario: dict[str, Any]) -> None:
    result = ScriptedFinalAuditRepairHarness(scenario).run()

    assert result.final_status != FinalSetupStatus.BLOCKED
    assert not any(finding.severity == AuditFindingSeverity.FATAL for finding in result.findings)
    text = _summary_text(result)
    assert any(
        keyword in text
        for keyword in ("app", "application", "theme", "desktop", "gui", "display", "font")
    )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=_fixture_id)
def test_final_status_and_summary_agree(scenario: dict[str, Any]) -> None:
    result = ScriptedFinalAuditRepairHarness(scenario).run()
    status = result.final_status
    severities = {finding.severity for finding in result.findings}
    text = _summary_text(result)

    assert status is not None
    for forbidden in scenario["expected"].get("summary_must_not_claim", []):
        assert str(forbidden).lower() not in text
    if status == FinalSetupStatus.SUCCESS:
        assert result.findings == []
        assert result.blockers == []
    elif status == FinalSetupStatus.SUCCESS_WITH_WARNINGS:
        assert result.findings
        assert AuditFindingSeverity.FATAL not in severities
        assert AuditFindingSeverity.MANUAL_FOLLOWUP not in severities
        assert AuditFindingSeverity.UNSUPPORTED_SAFE not in severities
    elif status == FinalSetupStatus.MANUAL_FOLLOWUP_REQUIRED:
        assert AuditFindingSeverity.MANUAL_FOLLOWUP in severities
    elif status == FinalSetupStatus.UNSUPPORTED_SAFE:
        assert AuditFindingSeverity.UNSUPPORTED_SAFE in severities
        assert AuditFindingSeverity.FATAL not in severities
    elif status == FinalSetupStatus.BLOCKED:
        assert result.blockers
        assert AuditFindingSeverity.FATAL in severities


@pytest.mark.parametrize(
    ("scenario_id", "expected_kind"),
    [
        ("dangerous-fstab-reject", "write_fstab"),
        ("json-exhaust-boot", "bootloader_install"),
        ("cap-required-chroot", "initramfs_rebuild"),
        ("pacman-lock-fail", "package_db_repair"),
    ],
)
def test_dangerous_final_repair_commands_require_capabilities(
    scenario_id: str,
    expected_kind: str,
) -> None:
    scenario = SCENARIO_BY_ID[scenario_id]
    commands = [
        command
        for group in scenario["repair_plan"]["command_groups"]
        for command in group["commands"]
    ]
    command = next(
        command
        for command in commands
        if any(req["kind"] == expected_kind for req in dangerous_command_requirements(command))
    )
    requirements = dangerous_command_requirements(command)
    assert expected_kind in {req["kind"] for req in requirements}

    facts = _facts_for_dangerous_repair()
    reason = preflight_setup_command(command, facts)
    assert reason is not None
    assert expected_kind in reason

    store = CapabilityStore(run_id=RUN_ID, install_profile="full_disk_wipe")
    for index, requirement in enumerate(requirements):
        store.grant(
            kind=requirement["kind"],
            target=requirement.get("target", ""),
            confirmation_text=f"CONFIRM {requirement['kind']}",
            confirmation_id=f"{scenario_id}-{index}",
            reason=f"final audit repair test grant for {scenario_id}",
            source="harness_generated",
            one_time_use=False,
            preview_id=f"{scenario_id}-{index}",
        )
    assert preflight_setup_command(command, facts, capability_store=store) is None
