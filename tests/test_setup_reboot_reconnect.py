from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml

from quickhatch.bridge.server import BridgeState
from quickhatch.config import AIConfig, UserProfile
from quickhatch.setup.audit import final_audit
from quickhatch.setup.orchestrator import SetupOrchestrator, SetupRuntime
from quickhatch.setup.types import (
    AuditResult,
    CommandGroup,
    FinalSetupStatus,
    ResearchPlan,
    SetupFacts,
    SetupStage,
    StrategyDecision,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "reboot_reconnect_scenarios.yaml"


def _load_scenarios() -> list[dict[str, Any]]:
    payload = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    return list(payload["scenarios"])


SCENARIOS = _load_scenarios()
SCENARIO_BY_ID = {scenario["id"]: scenario for scenario in SCENARIOS}


class MemoryRunLog:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, event_type: str, **payload: Any) -> None:
        self.events.append({"event_type": event_type, **payload})


class FakeReconnectController:
    def __init__(self, states: list[str], fact_state: dict[str, SetupFacts], after: SetupFacts):
        self.states = list(states)
        self.fact_state = fact_state
        self.after = after
        self.calls: list[str] = []

    def __call__(self, reconnect_user: str) -> tuple[bool, str, str]:
        self.calls.append(reconnect_user)
        seen: list[str] = []
        for state in self.states:
            seen.append(state)
            if state == "connected":
                self.fact_state["facts"] = self.after
                return True, "fresh installed system", reconnect_user
        return False, "SSH did not reconnect after reboot: " + ",".join(seen), reconnect_user


def _profile(*, mode: str = "fresh_install") -> UserProfile:
    profile = UserProfile()
    profile.install_username = "jan"
    profile.linux_user = "root" if mode == "fresh_install" else "jan"
    if mode == "configure_existing":
        profile.install_profile = "configure_existing"
        profile.remote_setup_mode = "configure_only"
    else:
        profile.install_profile = "full_disk_wipe"
        profile.remote_setup_mode = "wipe_and_reinstall"
    return profile


def _facts(
    *,
    is_live: bool,
    ssh_ready: bool = True,
    target_root_mounted: bool = False,
    same_target: bool = True,
    fact_probe_ok: bool = True,
) -> SetupFacts:
    return SetupFacts(
        distro_family="arch",
        os_id="arch",
        package_manager="pacman",
        community_helper="none",
        current_user="root" if is_live else "jan",
        login_user="root" if is_live else "jan",
        is_root=is_live,
        sudo_nopasswd=ssh_ready,
        is_live=is_live,
        live_marker="LIVE" if is_live else "INSTALLED",
        target_root_mounted=target_root_mounted,
        fact_probe_ok=fact_probe_ok,
        raw={"same_target": same_target},
    )


def _facts_from_fixture(
    scenario: dict[str, Any], *, key: str, target_root_mounted: bool = False
) -> SetupFacts:
    raw = scenario[key]
    is_live = raw["is_live"] is True
    ssh_ready = raw["ssh_ready"] is not False
    same_target = raw.get("same_target", True) is not False
    fact_probe_ok = raw["is_live"] != "unknown"
    return _facts(
        is_live=is_live,
        ssh_ready=ssh_ready,
        target_root_mounted=target_root_mounted,
        same_target=same_target,
        fact_probe_ok=fact_probe_ok,
    )


def _plan(*groups: CommandGroup) -> ResearchPlan:
    return ResearchPlan(
        target_summary="reboot/reconnect test plan",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision("none", confidence="resolved_official", source="repo"),
        desktop_strategy=StrategyDecision("none", confidence="resolved_official", source="repo"),
        command_groups=list(groups),
    )


def _runtime(
    *,
    profile: UserProfile,
    bridge: BridgeState,
    fact_state: dict[str, SetupFacts],
    executed: list[str],
    reconnect: FakeReconnectController | None = None,
    precheck: tuple[bool, list[str], str] = (True, [], "ok"),
) -> SetupRuntime:
    def fake_ssh_exec(command: str, _timeout: int) -> tuple[int, str, str]:
        executed.append(command)
        if "for qh_root in" in command and "/etc/passwd" in command:
            return 0, "/mnt\n", ""
        return 0, "", ""

    return SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(provider="api_openai", endpoint="http://example", model_name="test"),
        host="127.0.0.1",
        user=fact_state["facts"].current_user or "root",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=lambda _cfg, _messages, **_kwargs: "{}",
        probe_facts=lambda **_kwargs: fact_state["facts"],
        detect_live_env=lambda: (
            fact_state["facts"].is_live,
            fact_state["facts"].live_marker or "",
        ),
        ssh_exec=fake_ssh_exec,
        repo_search=lambda _query: "",
        community_search=lambda _query: "",
        web_search=lambda _query, _max_results: "",
        wait_for_reconnect=reconnect
        or (lambda reconnect_user: (True, "fresh installed system", reconnect_user)),
        run_pre_reboot_checks=lambda: precheck,
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda _current, _is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=False,
    )


def _stub_reconnect_key_check(orchestrator: SetupOrchestrator) -> None:
    def ensure() -> None:
        state = orchestrator.ctx.stage_state.setdefault(SetupStage.INSTALL_OR_SKIP.value, {})
        state["ssh_continuity_key_installed"] = True
        orchestrator.runlog.record(
            "ssh_continuity_key",
            stage=SetupStage.INSTALL_OR_SKIP.value,
            user=orchestrator.ctx.created_user or orchestrator.runtime.user,
            stubbed=True,
        )

    orchestrator._ensure_reconnect_ssh_key = ensure  # type: ignore[method-assign]


def _orchestrator_for_scenario(
    scenario: dict[str, Any],
    *,
    precheck: tuple[bool, list[str], str] = (True, [], "ok"),
    plan: ResearchPlan | None = None,
) -> tuple[SetupOrchestrator, list[str], MemoryRunLog, FakeReconnectController]:
    before = _facts_from_fixture(scenario, key="facts_before", target_root_mounted=True)
    after = _facts_from_fixture(scenario, key="facts_after")
    fact_state = {"facts": before}
    executed: list[str] = []
    bridge = BridgeState(_profile(mode=scenario["mode"]), persist_path=None)
    reconnect = FakeReconnectController(
        [step["state"] for step in scenario.get("reconnect_sequence", [])],
        fact_state,
        after,
    )
    runtime = _runtime(
        profile=bridge.profile,
        bridge=bridge,
        fact_state=fact_state,
        executed=executed,
        reconnect=reconnect,
        precheck=precheck,
    )
    orchestrator = SetupOrchestrator(runtime)
    orchestrator.runlog = MemoryRunLog()  # type: ignore[assignment]
    orchestrator.ctx.facts = before
    orchestrator.ctx.plan = plan or _plan()
    _stub_reconnect_key_check(orchestrator)
    return orchestrator, executed, orchestrator.runlog, reconnect  # type: ignore[return-value]


def _precheck_from_scenario(scenario: dict[str, Any]) -> tuple[bool, list[str], str]:
    before = scenario["facts_before"]
    pending = scenario["stage_state_before"].get("pending_questions", [])
    failures: list[str] = []
    checks = {
        "fstab_ok": "fstab is not verified",
        "bootloader_ok": "bootloader is not verified",
        "kernel_ok": "kernel artifact is not verified",
        "initramfs_ok": "initramfs is not verified",
        "ssh_ready": "SSH continuity is not verified",
        "package_db_healthy": "package DB health is not verified",
    }
    for key, message in checks.items():
        if before.get(key) is not True:
            failures.append(message)
    if pending:
        failures.append("pending question remains: " + ", ".join(str(item) for item in pending))
    if failures:
        return False, failures, "; ".join(failures)
    return True, [], "ok"


def _event_names(runlog: MemoryRunLog) -> list[str]:
    return [event["event_type"] for event in runlog.events]


def test_fixture_contains_required_reboot_policy_coverage() -> None:
    categories = {scenario["category"] for scenario in SCENARIOS}
    modes = {scenario["mode"] for scenario in SCENARIOS}
    effects = {scenario["expected"]["final_status_effect"] for scenario in SCENARIOS}

    assert {
        "pre_reboot_gate",
        "checkpoint",
        "reconnect_success",
        "reconnect_failure",
        "postboot_facts",
        "live_iso",
        "configure_existing",
        "driver_reboot",
        "final_audit",
    } <= categories
    assert {"fresh_install", "configure_existing", "postboot_setup"} <= modes
    assert {"none", "success_with_warnings", "manual_followup_required", "blocked"} <= effects


def test_raw_install_reboot_command_is_deferred_until_harness_reboot() -> None:
    scenario = SCENARIO_BY_ID["preflight-raw-reboot-deferred-then-guarded"]
    before = _facts_from_fixture(scenario, key="facts_before", target_root_mounted=False)
    after = _facts_from_fixture(scenario, key="facts_after")
    fact_state = {"facts": before}
    executed: list[str] = []
    bridge = BridgeState(_profile(), persist_path=None)
    reconnect = FakeReconnectController(["disconnected", "connected"], fact_state, after)
    group = CommandGroup(
        id="install",
        stage=SetupStage.INSTALL_OR_SKIP,
        title="Install base",
        commands=["echo install base", scenario["reboot_request"]["command"]],
    )
    runtime = _runtime(
        profile=bridge.profile,
        bridge=bridge,
        fact_state=fact_state,
        executed=executed,
        reconnect=reconnect,
    )
    orchestrator = SetupOrchestrator(runtime)
    orchestrator.runlog = MemoryRunLog()  # type: ignore[assignment]
    orchestrator.ctx.facts = before
    orchestrator.ctx.plan = _plan(group)
    orchestrator._ensure_live_install_target_user = lambda: None  # type: ignore[method-assign]
    orchestrator.ctx.created_user = "root"
    orchestrator._enforce_live_family_install_spine = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    _stub_reconnect_key_check(orchestrator)

    orchestrator._run_install_or_skip()

    assert not any(command.strip() == "reboot" for command in executed)
    assert any("quickhatch-reboot.log" in command for command in executed)
    assert "command_deferred_until_guarded_reboot" in _event_names(orchestrator.runlog)  # type: ignore[arg-type]
    assert reconnect.calls == ["root"]


@pytest.mark.parametrize(
    "scenario",
    [item for item in SCENARIOS if item["category"] == "pre_reboot_gate"],
    ids=lambda item: item["id"],
)
def test_pre_reboot_gate_scenarios_control_install_reboot(scenario: dict[str, Any]) -> None:
    precheck = _precheck_from_scenario(scenario)
    orchestrator, executed, _runlog, reconnect = _orchestrator_for_scenario(
        scenario,
        precheck=precheck,
    )

    if scenario["expected"]["should_reboot"]:
        orchestrator._run_install_or_skip()
        assert any("quickhatch-reboot.log" in command for command in executed)
        assert reconnect.calls
        assert orchestrator.ctx.facts is not None
        assert orchestrator.ctx.facts.is_live is False
    else:
        with pytest.raises(RuntimeError):
            orchestrator._run_install_or_skip()
        assert not any("quickhatch-reboot.log" in command for command in executed)
        assert reconnect.calls == []


def test_checkpoint_state_is_persisted_before_controlled_install_reboot() -> None:
    scenario = SCENARIO_BY_ID["checkpoint-before-controlled-install-reboot"]
    orchestrator, _executed, runlog, _reconnect = _orchestrator_for_scenario(scenario)
    install_state = orchestrator.ctx.stage_state.setdefault(SetupStage.INSTALL_OR_SKIP.value, {})
    install_state.update(deepcopy(scenario["stage_state_before"]))

    orchestrator._run_install_or_skip()

    event_names = _event_names(runlog)
    reboot_index = event_names.index("install_reboot")
    checkpoint_events = [
        event
        for event in runlog.events[:reboot_index]
        if event["event_type"]
        in {"reboot_checkpoint", "checkpoint_saved", "setup_checkpoint_saved"}
    ]
    assert checkpoint_events, (
        "controlled install reboot should persist run id, plan, stage state, completed groups, "
        "manual follow-ups, repair attempts, and pending questions before issuing reboot"
    )
    checkpoint = orchestrator.bridge.snapshot_setup_run()["reboot_checkpoint"]
    assert checkpoint["run_id"] == orchestrator.ctx.run_id
    assert checkpoint["stage"] == SetupStage.INSTALL_OR_SKIP.value
    assert (
        checkpoint["stage_state"][SetupStage.INSTALL_OR_SKIP.value]["completed_groups"]
        == scenario["stage_state_before"]["completed_groups"]
    )
    assert checkpoint["manual_followups"] == scenario["stage_state_before"]["manual_followups"]
    assert (
        checkpoint["repair_attempts"][SetupStage.INSTALL_OR_SKIP.value]
        == scenario["stage_state_before"]["repair_attempts"]
    )


@pytest.mark.parametrize(
    "scenario",
    [
        SCENARIO_BY_ID["reconnect-refused-then-success"],
        SCENARIO_BY_ID["reconnect-timeouts-then-success"],
        SCENARIO_BY_ID["reconnect-never-returns-blocks"],
    ],
    ids=lambda item: item["id"],
)
def test_reconnect_loop_success_and_failure_are_handled(scenario: dict[str, Any]) -> None:
    orchestrator, executed, runlog, reconnect = _orchestrator_for_scenario(scenario)

    if scenario["expected"]["should_resume"]:
        orchestrator._run_install_or_skip()
        assert reconnect.calls == ["root"]
        assert any("quickhatch-reboot.log" in command for command in executed)
        assert "install_reboot_result" in _event_names(runlog)
    else:
        with pytest.raises(RuntimeError, match="SSH did not reconnect|Target did not reconnect"):
            orchestrator._run_install_or_skip()
        assert reconnect.calls == ["root"]
        assert "install_reboot_result" in _event_names(runlog)


def test_post_reboot_facts_refresh_blocks_still_live_iso() -> None:
    scenario = SCENARIO_BY_ID["postboot-still-live-blocks-all-later-stages"]
    orchestrator, _executed, runlog, reconnect = _orchestrator_for_scenario(scenario)

    with pytest.raises(RuntimeError, match="live environment"):
        orchestrator._run_install_or_skip()

    assert reconnect.calls == ["root"]
    assert "facts_refresh" in _event_names(runlog)
    assert orchestrator.ctx.facts is not None
    assert orchestrator.ctx.facts.is_live is True


def test_postboot_stages_do_not_use_stale_live_facts_after_reconnect() -> None:
    scenario = SCENARIO_BY_ID["postboot-apps-deferred-while-live"]
    live = _facts_from_fixture(scenario, key="facts_after")
    bridge = BridgeState(_profile(), persist_path=None)
    executed: list[str] = []
    fact_state = {"facts": live}
    runtime = _runtime(
        profile=bridge.profile,
        bridge=bridge,
        fact_state=fact_state,
        executed=executed,
    )
    group = CommandGroup(
        id="apps",
        stage=SetupStage.APPS,
        title="Install app",
        commands=[scenario["reboot_request"]["command"]],
    )
    orchestrator = SetupOrchestrator(runtime)
    orchestrator.runlog = MemoryRunLog()  # type: ignore[assignment]
    orchestrator.ctx.facts = live
    orchestrator.ctx.plan = _plan(group)

    with pytest.raises(RuntimeError, match="live environment"):
        orchestrator._run_stage_groups(SetupStage.APPS)

    assert executed == []
    assert "postboot_stage_blocked_live_environment" in _event_names(orchestrator.runlog)  # type: ignore[arg-type]


def test_resume_after_reconnect_skips_completed_destructive_groups_and_preserves_followups() -> (
    None
):
    scenario = SCENARIO_BY_ID["checkpoint-repair-state-does-not-rerun-disk"]
    installed = _facts_from_fixture(scenario, key="facts_after")
    bridge = BridgeState(_profile(), persist_path=None)
    executed: list[str] = []
    fact_state = {"facts": installed}
    runtime = _runtime(
        profile=bridge.profile,
        bridge=bridge,
        fact_state=fact_state,
        executed=executed,
    )
    destructive_group = CommandGroup(
        id="disk",
        stage=SetupStage.INSTALL_OR_SKIP,
        title="Disk prep",
        commands=["parted -s /dev/sda mklabel gpt"],
    )
    orchestrator = SetupOrchestrator(runtime)
    orchestrator.ctx.facts = installed
    orchestrator.ctx.plan = _plan(destructive_group)
    orchestrator.ctx.stage_state[SetupStage.APPS.value] = {
        "manual_followups": ["optional app unavailable"]
    }

    orchestrator._run_install_or_skip()

    assert not executed
    assert orchestrator.ctx.stage_state[SetupStage.INSTALL_OR_SKIP.value]["skipped"] is True
    assert orchestrator.ctx.stage_state[SetupStage.APPS.value]["manual_followups"] == [
        "optional app unavailable"
    ]


def test_configure_existing_raw_reboot_is_blocked_not_executed() -> None:
    scenario = SCENARIO_BY_ID["configure-existing-raw-reboot-blocks"]
    facts = _facts_from_fixture(scenario, key="facts_before")
    bridge = BridgeState(_profile(mode="configure_existing"), persist_path=None)
    executed: list[str] = []
    fact_state = {"facts": facts}
    runtime = _runtime(
        profile=bridge.profile,
        bridge=bridge,
        fact_state=fact_state,
        executed=executed,
    )
    group = CommandGroup(
        id="apps",
        stage=SetupStage.APPS,
        title="Bad reboot",
        commands=[scenario["reboot_request"]["command"]],
    )
    orchestrator = SetupOrchestrator(runtime)
    orchestrator.runlog = MemoryRunLog()  # type: ignore[assignment]
    orchestrator.ctx.facts = facts
    orchestrator.ctx.plan = _plan(group)

    orchestrator._run_stage_groups(SetupStage.APPS)

    assert executed == []
    assert "command_preflight_blocked" in _event_names(orchestrator.runlog)  # type: ignore[arg-type]


def test_configure_existing_driver_reboot_can_follow_controlled_path() -> None:
    scenario = SCENARIO_BY_ID["configure-existing-driver-reboot-controlled"]
    before = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        gpu_vendors=["nvidia"],
        current_user="jan",
        login_user="jan",
        sudo_nopasswd=True,
        nvidia_smi_state="present_but_failing",
        nvidia_kernel_driver="nouveau",
        nvidia_nouveau_bound=True,
    )
    after = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        gpu_vendors=["nvidia"],
        current_user="jan",
        login_user="jan",
        sudo_nopasswd=True,
        nvidia_smi_state="ok",
        nvidia_kernel_driver="nvidia",
        nvidia_nouveau_bound=False,
        driver_modules=["nvidia"],
    )
    fact_state = {"facts": before}
    bridge = BridgeState(_profile(mode="configure_existing"), persist_path=None)
    executed: list[str] = []
    reconnect = FakeReconnectController(
        [step["state"] for step in scenario["reconnect_sequence"]],
        fact_state,
        after,
    )
    runtime = _runtime(
        profile=bridge.profile,
        bridge=bridge,
        fact_state=fact_state,
        executed=executed,
        reconnect=reconnect,
    )
    orchestrator = SetupOrchestrator(runtime)
    orchestrator.runlog = MemoryRunLog()  # type: ignore[assignment]
    orchestrator.ctx.facts = before
    group = CommandGroup(
        id="driver",
        stage=SetupStage.POSTBOOT_DRIVER,
        title="NVIDIA Driver Setup",
        commands=["sudo pacman -S --noconfirm nvidia-open nvidia-utils"],
        verification=["nvidia-smi"],
        packages=["nvidia-open"],
    )

    orchestrator._maybe_reboot_for_nvidia_driver(SetupStage.POSTBOOT_DRIVER, group, {})

    assert any("sudo reboot" in command for command in executed)
    assert reconnect.calls == ["jan"]
    assert "nvidia_driver_reconnect" in _event_names(orchestrator.runlog)  # type: ignore[arg-type]
    assert orchestrator.ctx.facts is after


def test_final_status_blocks_ssh_reconnect_and_live_environment_failures() -> None:
    plan = _plan()
    result = final_audit(
        _facts(is_live=False),
        plan,
        [
            AuditResult(
                stage=SetupStage.INSTALL_OR_SKIP,
                passed=False,
                blockers=["SSH did not reconnect after reboot"],
            )
        ],
    )
    assert result.final_status == FinalSetupStatus.BLOCKED

    live_result = final_audit(
        _facts(is_live=True),
        plan,
        [AuditResult(stage=SetupStage.INSTALL_OR_SKIP, passed=True)],
    )
    assert live_result.final_status == FinalSetupStatus.BLOCKED
    assert any("live environment" in blocker for blocker in live_result.blockers)


def test_reboot_required_but_unverified_is_not_plain_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = SCENARIO_BY_ID["postboot-reboot-required-marker-warning"]
    facts = _facts_from_fixture(scenario, key="facts_before")
    bridge = BridgeState(_profile(), persist_path=None)
    executed: list[str] = []
    fact_state = {"facts": facts}
    runtime = _runtime(
        profile=bridge.profile,
        bridge=bridge,
        fact_state=fact_state,
        executed=executed,
    )
    orchestrator = SetupOrchestrator(runtime)
    orchestrator.ctx.facts = facts
    orchestrator.ctx.plan = _plan()
    orchestrator.ctx.stage_results = [
        AuditResult(stage=SetupStage.INSTALL_OR_SKIP, passed=True),
        AuditResult(stage=SetupStage.POSTBOOT_DRIVER, passed=True),
        AuditResult(stage=SetupStage.DESKTOP, passed=True),
        AuditResult(stage=SetupStage.APPS, passed=True),
    ]
    monkeypatch.setattr(
        orchestrator,
        "_remote_final_verification",
        lambda: ["A reboot is still required before setup can be declared complete."],
    )

    result = orchestrator._compute_final_audit_result()

    assert result.final_status != FinalSetupStatus.SUCCESS
    assert result.findings
