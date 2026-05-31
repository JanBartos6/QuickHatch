from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from quickhatch.bridge.server import BridgeState
from quickhatch.config import AIConfig, UserProfile
from quickhatch.setup.attempts import build_attempt_signature
from quickhatch.setup.orchestrator import GroupFailure, SetupOrchestrator, SetupRuntime
from quickhatch.setup.types import (
    AppPlanItem,
    AuditFinding,
    AuditFindingSeverity,
    AuditResult,
    CommandGroup,
    FinalSetupStatus,
    PlanItemStatus,
    ResearchPlan,
    SetupFacts,
    SetupStage,
    StrategyDecision,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "repair_loop_scenarios.yaml"


@dataclass
class MemoryRunLog:
    events: list[dict[str, Any]] = field(default_factory=list)

    def record(self, event_type: str, **payload: Any) -> None:
        self.events.append({"event_type": event_type, **payload})


class FakeExecutor:
    """Replay stage group outcomes without executing shell commands."""

    def __init__(self, outcomes: list[GroupFailure | None] | dict[str, list[GroupFailure | None]]):
        if isinstance(outcomes, dict):
            self.outcomes_by_group = {key: list(value) for key, value in outcomes.items()}
            self.outcomes: list[GroupFailure | None] = []
        else:
            self.outcomes_by_group = {}
            self.outcomes = list(outcomes)
        self.calls: list[dict[str, str]] = []

    def execute(
        self,
        stage: SetupStage,
        group: CommandGroup,
        _stage_state: dict[str, Any],
    ) -> GroupFailure | None:
        self.calls.append(
            {
                "stage": stage.value,
                "group_id": group.id,
                "command": group.commands[0] if group.commands else "",
            }
        )
        if self.outcomes_by_group:
            queue = self.outcomes_by_group[group.id]
        else:
            queue = self.outcomes
        assert queue, f"FakeExecutor exhausted for group {group.id}"
        return queue.pop(0)


class FakeRepairProvider:
    """Replay repair/model choices without calling an external provider."""

    def __init__(self, outcomes: list[dict[str, Any]]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def repair(
        self,
        stage: SetupStage,
        failed_group: CommandGroup,
        failure: GroupFailure,
        *,
        repair_attempt: int,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "stage": stage.value,
                "group_id": failed_group.id,
                "command": failure.command,
                "repair_attempt": repair_attempt,
            }
        )
        assert self.outcomes, f"FakeRepairProvider exhausted for {failed_group.id}"
        return self.outcomes.pop(0)


def _load_fixture() -> dict[str, Any]:
    payload = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert isinstance(payload.get("scenarios"), list)
    return payload


REPAIR_LOOP_FIXTURE = _load_fixture()


def _facts(
    *,
    family: str = "ubuntu",
    package_manager: str = "apt",
    is_live: bool = False,
) -> SetupFacts:
    return SetupFacts(
        distro_family=family,
        os_id=family,
        package_manager=package_manager,
        community_helper="yay" if package_manager == "pacman" else "none",
        current_user="jan" if not is_live else "root",
        login_user="jan" if not is_live else "root",
        is_root=is_live,
        sudo_nopasswd=True,
        is_live=is_live,
        live_marker="LIVE" if is_live else "INSTALLED",
        session_type="" if is_live else "wayland",
        raw={
            "install_profile": "configure_existing" if not is_live else "full_disk",
            "remote_setup_mode": "configure_only" if not is_live else "fresh_install",
        },
    )


def _profile(*, is_live: bool = False) -> UserProfile:
    profile = UserProfile()
    profile.install_profile = "full_disk" if is_live else "configure_existing"
    profile.remote_setup_mode = "fresh_install" if is_live else "configure_only"
    profile.install_username = "jan"
    return profile


def _group(
    stage: SetupStage,
    command: str,
    *,
    group_id: str = "group",
    title: str = "Test group",
    packages: list[str] | None = None,
    source: str = "repo",
    optional: bool = False,
) -> CommandGroup:
    return CommandGroup(
        id=group_id,
        stage=stage,
        title=title,
        commands=[command],
        verification=[],
        packages=list(packages or []),
        source=source,
        optional=optional,
    )


def _failure(
    command: str,
    *,
    failure_type: str = "command",
    stdout: str = "",
    stderr: str = "E: Unable to locate package",
    detail: str = "scripted failure",
) -> GroupFailure:
    return GroupFailure(failure_type, command, stdout, stderr, detail)


def _plan(
    groups: list[CommandGroup],
    *,
    apps: list[AppPlanItem] | None = None,
    family: str = "ubuntu",
    package_manager: str = "apt",
) -> ResearchPlan:
    return ResearchPlan(
        target_summary="repair-loop test setup",
        distro_family=family,
        package_manager=package_manager,
        driver_strategy=StrategyDecision("none", confidence="resolved_official"),
        desktop_strategy=StrategyDecision("none", confidence="resolved_official"),
        command_groups=groups,
        apps=list(apps or []),
    )


def _orchestrator(
    *,
    groups: list[CommandGroup] | None = None,
    apps: list[AppPlanItem] | None = None,
    facts: SetupFacts | None = None,
    call_model_text=None,
) -> tuple[SetupOrchestrator, MemoryRunLog]:
    facts = facts or _facts()
    profile = _profile(is_live=facts.is_live)
    bridge = BridgeState(profile, persist_path=None)
    runtime = SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(provider="api_openai", endpoint="http://example", model_name="test"),
        host="127.0.0.1",
        user=facts.current_user or "jan",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=call_model_text or (lambda _cfg, _messages, *_args: "{}"),
        probe_facts=lambda **_kwargs: facts,
        detect_live_env=lambda: (facts.is_live, facts.live_marker),
        ssh_exec=lambda _command, _timeout: (0, "", ""),
        repo_search=lambda _query: "",
        community_search=lambda _query: "",
        web_search=lambda _query, _max_results: "",
        wait_for_reconnect=lambda reconnect_user: (True, "fresh info", reconnect_user),
        run_pre_reboot_checks=lambda: (True, [], "ok"),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda _current, _is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=False,
    )
    orchestrator = SetupOrchestrator(runtime)
    runlog = MemoryRunLog()
    orchestrator.runlog = runlog  # type: ignore[assignment]
    orchestrator.ctx.facts = facts
    orchestrator.ctx.plan = _plan(
        groups or [],
        apps=apps,
        family=facts.distro_family,
        package_manager=facts.package_manager,
    )
    orchestrator._refresh_facts = lambda *, stage: None  # type: ignore[method-assign]
    return orchestrator, runlog


def _signature_events(runlog: MemoryRunLog) -> list[dict[str, Any]]:
    return [event for event in runlog.events if event["event_type"] == "repair_attempt_signature"]


def test_repair_loop_fixture_has_expected_policy_coverage() -> None:
    summary = REPAIR_LOOP_FIXTURE["merge_summary"]
    categories = set(summary["scenarios_by_category"])
    assert summary["total_scenarios"] == len(REPAIR_LOOP_FIXTURE["scenarios"])
    assert categories == {
        "diagnostics_budget",
        "group_scope",
        "identical_loop",
        "provider_error",
        "required_optional",
        "strategy_change",
    }
    assert summary["rejected_ambiguous_cases"] == []
    assert "install_spine_failures_remain_blocking" in summary["invariants_covered"]
    assert (
        "final_audit_repairs_are_budget_limited_and_status_honest" in summary["invariants_covered"]
    )


def test_identical_repair_attempts_are_detected_and_stop() -> None:
    command = "sudo apt-get install -y obsidian"
    group = _group(
        SetupStage.APPS,
        command,
        group_id="obsidian",
        title="Install Obsidian",
        packages=["obsidian"],
        optional=True,
    )
    app = AppPlanItem(
        "Obsidian",
        package_name="obsidian",
        source="repo",
        required=False,
    )
    failure = _failure(command, stderr="E: Unable to locate package obsidian")
    orchestrator, runlog = _orchestrator(groups=[group], apps=[app])
    executor = FakeExecutor([failure, failure, failure])
    repair_provider = FakeRepairProvider(
        [
            {"action": "retry_group", "group": group},
            {"action": "retry_group", "group": group},
        ]
    )
    orchestrator._execute_group = executor.execute  # type: ignore[method-assign]
    orchestrator._repair_failed_group = repair_provider.repair  # type: ignore[method-assign]

    orchestrator._run_stage_groups(SetupStage.APPS, audit_after=False)

    signature_events = _signature_events(runlog)
    assert [event["same_loop_count"] for event in signature_events] == [1, 2, 3]
    assert signature_events[-1]["loop_exhausted"] is True
    assert len(repair_provider.calls) == 2
    assert group.status == PlanItemStatus.MANUAL_FOLLOWUP
    assert app.status == PlanItemStatus.MANUAL_FOLLOWUP


def test_semantically_same_package_attempts_share_loop_signature() -> None:
    group = _group(
        SetupStage.APPS,
        "sudo apt-get install -y obsidian",
        group_id="obsidian",
        title="Install Obsidian",
        packages=["obsidian"],
    )
    orchestrator, _runlog = _orchestrator(groups=[group])
    stage_state: dict[str, Any] = {}
    commands = [
        "sudo apt-get install -y obsidian",
        "apt-get install --yes obsidian",
        "sudo apt-get install obsidian",
    ]
    decisions = [
        orchestrator._register_repair_attempt_signature(
            SetupStage.APPS,
            group,
            _failure(command, stderr="E: Unable to locate package obsidian"),
            stage_state,
        )
        for command in commands
    ]

    assert [decision["same_loop_count"] for decision in decisions] == [1, 2, 3]
    assert decisions[-1]["loop_exhausted"] is True


def test_repo_to_aur_flatpak_or_snap_strategy_changes_continue() -> None:
    repo_group = _group(
        SetupStage.APPS,
        "sudo pacman -S --noconfirm visual-studio-code",
        group_id="code",
        title="Install Visual Studio Code",
        packages=["visual-studio-code"],
        source="repo",
    )
    aur_group = _group(
        SetupStage.APPS,
        "yay -S --noconfirm visual-studio-code-bin",
        group_id="code-aur",
        title="Install Visual Studio Code from AUR",
        packages=["visual-studio-code-bin"],
        source="aur",
    )
    flatpak_group = _group(
        SetupStage.APPS,
        "flatpak install -y flathub com.visualstudio.code",
        group_id="code-flatpak",
        title="Install Visual Studio Code from Flatpak",
        packages=["com.visualstudio.code"],
        source="flatpak",
    )
    failure_repo = _failure(
        repo_group.commands[0],
        stderr="repo package unavailable after metadata refresh",
    )
    failure_aur = _failure(
        aur_group.commands[0],
        stderr="AUR package unavailable after search evidence",
    )
    orchestrator, runlog = _orchestrator(
        groups=[repo_group],
        facts=_facts(family="arch", package_manager="pacman"),
    )
    executor = FakeExecutor([failure_repo, failure_aur, None])
    repair_provider = FakeRepairProvider(
        [
            {"action": "retry_group", "group": aur_group},
            {"action": "retry_group", "group": flatpak_group},
        ]
    )
    orchestrator._execute_group = executor.execute  # type: ignore[method-assign]
    orchestrator._repair_failed_group = repair_provider.repair  # type: ignore[method-assign]

    orchestrator._run_stage_groups(SetupStage.APPS, audit_after=False)

    assert [call["command"] for call in executor.calls] == [
        repo_group.commands[0],
        aur_group.commands[0],
        flatpak_group.commands[0],
    ]
    assert all(event["loop_exhausted"] is False for event in _signature_events(runlog))
    assert flatpak_group.status == PlanItemStatus.COMPLETED


def test_diagnostics_and_search_are_budget_limited_without_identical_looping() -> None:
    group = _group(
        SetupStage.APPS,
        "apt-cache search definitely-missing-editor",
        group_id="diagnostic",
        title="Search package candidates",
    )
    orchestrator, _runlog = _orchestrator(groups=[group])
    stage_state: dict[str, Any] = {}
    decisions = [
        orchestrator._register_repair_attempt_signature(
            SetupStage.APPS,
            group,
            _failure(
                "apt-cache search definitely-missing-editor",
                stdout="",
                stderr="",
            ),
            stage_state,
        )
        for _ in range(4)
    ]

    assert [decision["loop_exhausted"] for decision in decisions] == [False] * 4
    assert [decision["consumes_budget"] for decision in decisions] == [
        False,
        False,
        True,
        True,
    ]


@pytest.mark.parametrize("error", ["429 rate limit", "provider timeout"])
def test_provider_errors_do_not_create_endless_repair_calls(error: str) -> None:
    calls = {"count": 0}

    def call_model_text(_config: AIConfig, _messages: list[dict[str, str]], *_args: object) -> str:
        calls["count"] += 1
        raise RuntimeError(error)

    group = _group(
        SetupStage.APPS,
        "sudo apt-get install -y obsidian",
        group_id="obsidian",
        title="Install Obsidian",
        packages=["obsidian"],
    )
    orchestrator, runlog = _orchestrator(groups=[group], call_model_text=call_model_text)

    outcome = orchestrator._repair_failed_group(
        SetupStage.APPS,
        group,
        _failure(group.commands[0]),
        repair_attempt=1,
    )

    assert calls["count"] == 3
    assert outcome["action"] == "manual_followup"
    assert "failed after 3 attempts" in outcome["reason"]
    assert "Repair LLM unavailable" in outcome["reason"]
    assert any(
        event["event_type"] == "repair_model_unavailable_manual_followup" for event in runlog.events
    )


def test_optional_app_and_theme_failures_become_manual_followup() -> None:
    app_group = _group(
        SetupStage.APPS,
        "sudo apt-get install -y obsidian",
        group_id="obsidian",
        title="Install Obsidian",
        packages=["obsidian"],
        optional=True,
    )
    app = AppPlanItem(
        "Obsidian",
        package_name="obsidian",
        source="repo",
        required=False,
    )
    orchestrator, _runlog = _orchestrator(groups=[app_group], apps=[app])
    executor = FakeExecutor([_failure(app_group.commands[0])])
    repair_provider = FakeRepairProvider(
        [{"action": "manual_followup", "reason": "Optional package unavailable."}]
    )
    orchestrator._execute_group = executor.execute  # type: ignore[method-assign]
    orchestrator._repair_failed_group = repair_provider.repair  # type: ignore[method-assign]

    orchestrator._run_stage_groups(SetupStage.APPS, audit_after=False)

    assert app_group.status == PlanItemStatus.MANUAL_FOLLOWUP
    assert app.status == PlanItemStatus.MANUAL_FOLLOWUP
    assert app.reason == "Optional package unavailable."


def test_core_install_reconnect_and_boot_failures_block_not_silent_followup() -> None:
    group = _group(
        SetupStage.INSTALL_OR_SKIP,
        "pacstrap -K /mnt base linux linux-firmware",
        group_id="bootstrap",
        title="Bootstrap target",
    )
    facts = _facts(family="arch", package_manager="pacman", is_live=True)
    orchestrator, _runlog = _orchestrator(groups=[group], facts=facts)
    executor = FakeExecutor([_failure(group.commands[0], detail="bootstrap failed")])
    repair_provider = FakeRepairProvider(
        [{"action": "manual_followup", "reason": "defer bootstrap manually"}]
    )
    orchestrator._execute_group = executor.execute  # type: ignore[method-assign]
    orchestrator._repair_failed_group = repair_provider.repair  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="cannot be marked manual follow-up"):
        orchestrator._run_stage_groups(SetupStage.INSTALL_OR_SKIP, audit_after=False)

    reconnect_failure = _failure(
        "systemctl reboot",
        failure_type="live_reboot_required",
        detail="controlled reboot/reconnect boundary failed",
    )
    orchestrator, _runlog = _orchestrator(groups=[group], facts=facts)
    executor = FakeExecutor([reconnect_failure])
    repair_provider = FakeRepairProvider([])
    orchestrator._execute_group = executor.execute  # type: ignore[method-assign]
    orchestrator._repair_failed_group = repair_provider.repair  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="controlled reboot/reconnect"):
        orchestrator._run_stage_groups(SetupStage.INSTALL_OR_SKIP, audit_after=False)
    assert repair_provider.calls == []


def test_group_level_repair_budget_is_scoped_per_group() -> None:
    first = _group(
        SetupStage.APPS,
        "sudo apt-get install -y app-one",
        group_id="one",
        title="Install app one",
        packages=["app-one"],
    )
    second = _group(
        SetupStage.APPS,
        "sudo apt-get install -y app-two",
        group_id="two",
        title="Install app two",
        packages=["app-two"],
    )
    orchestrator, _runlog = _orchestrator(groups=[first, second])
    executor = FakeExecutor(
        {
            "one": [_failure(first.commands[0]), _failure(first.commands[0]), None],
            "two": [_failure(second.commands[0]), None],
        }
    )
    repair_provider = FakeRepairProvider(
        [
            {"action": "retry_group", "group": first},
            {"action": "retry_group", "group": first},
            {"action": "retry_group", "group": second},
        ]
    )
    orchestrator._execute_group = executor.execute  # type: ignore[method-assign]
    orchestrator._repair_failed_group = repair_provider.repair  # type: ignore[method-assign]

    orchestrator._run_stage_groups(SetupStage.APPS, audit_after=False)

    assert [(call["group_id"], call["repair_attempt"]) for call in repair_provider.calls] == [
        ("one", 1),
        ("one", 2),
        ("two", 1),
    ]
    assert first.status == PlanItemStatus.COMPLETED
    assert second.status == PlanItemStatus.COMPLETED


def test_failed_app_group_does_not_poison_unrelated_successful_group() -> None:
    failing = _group(
        SetupStage.APPS,
        "sudo apt-get install -y missing-chat",
        group_id="chat",
        title="Install Missing Chat",
        packages=["missing-chat"],
        optional=True,
    )
    succeeding = _group(
        SetupStage.APPS,
        "sudo apt-get install -y vlc",
        group_id="vlc",
        title="Install VLC",
        packages=["vlc"],
    )
    missing_app = AppPlanItem(
        "Missing Chat",
        package_name="missing-chat",
        source="repo",
        required=False,
    )
    vlc = AppPlanItem("VLC", package_name="vlc", source="repo", required=True)
    orchestrator, _runlog = _orchestrator(groups=[failing, succeeding], apps=[missing_app, vlc])
    executor = FakeExecutor({"chat": [_failure(failing.commands[0])], "vlc": [None]})
    repair_provider = FakeRepairProvider(
        [{"action": "manual_followup", "reason": "Missing Chat package unavailable."}]
    )
    orchestrator._execute_group = executor.execute  # type: ignore[method-assign]
    orchestrator._repair_failed_group = repair_provider.repair  # type: ignore[method-assign]

    orchestrator._run_stage_groups(SetupStage.APPS, audit_after=False)

    assert failing.status == PlanItemStatus.MANUAL_FOLLOWUP
    assert succeeding.status == PlanItemStatus.COMPLETED
    assert missing_app.status == PlanItemStatus.MANUAL_FOLLOWUP
    assert vlc.status == PlanItemStatus.INSTALLED


def test_final_status_is_honest_when_repair_ends_in_manual_followup() -> None:
    group = _group(
        SetupStage.APPS,
        "sudo apt-get install -y code",
        group_id="code",
        title="Install Code",
        packages=["code"],
    )
    finding = AuditFinding(
        id="final_audit:code_missing",
        severity=AuditFindingSeverity.REPAIRABLE,
        category="apps",
        summary="Visual Studio Code is missing",
        suggested_repair_scope="install code",
    )
    orchestrator, _runlog = _orchestrator(groups=[group])

    def compute_final_audit_result() -> AuditResult:
        return AuditResult(
            stage=SetupStage.FINAL_AUDIT,
            passed=False,
            blockers=["Visual Studio Code is missing"],
            findings=[finding],
        )

    orchestrator._compute_final_audit_result = compute_final_audit_result  # type: ignore[method-assign]
    orchestrator._repairable_final_findings = lambda _result: [finding]  # type: ignore[method-assign]
    orchestrator._repair_final_audit_findings = (  # type: ignore[method-assign]
        lambda _findings, *, repair_attempt, final_state: {
            "action": "manual_followup",
            "executed": False,
            "reason": "Code install needs manual follow-up.",
        }
    )

    result = orchestrator._run_final_audit()

    assert result.final_status == FinalSetupStatus.MANUAL_FOLLOWUP_REQUIRED
    assert orchestrator.bridge.snapshot_setup_run()["final_status"] == (
        FinalSetupStatus.MANUAL_FOLLOWUP_REQUIRED.value
    )


def test_signature_builder_keeps_strategy_changes_distinct() -> None:
    repo = build_attempt_signature(
        stage=SetupStage.APPS.value,
        group_id="app",
        group_title="Install app",
        command="sudo apt-get install -y obsidian",
        stderr="E: Unable to locate package obsidian",
    )
    flatpak = build_attempt_signature(
        stage=SetupStage.APPS.value,
        group_id="app",
        group_title="Install app",
        command="flatpak install -y flathub md.obsidian.Obsidian",
        stderr="No such ref",
    )
    snap = build_attempt_signature(
        stage=SetupStage.APPS.value,
        group_id="app",
        group_title="Install app",
        command="sudo snap install obsidian",
        stderr="snap unavailable",
    )

    assert repo.loop_key != flatpak.loop_key
    assert repo.loop_key != snap.loop_key
