from __future__ import annotations

import json
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from quickhatch.bridge.server import BridgeState
from quickhatch.config import AIConfig, UserProfile
from quickhatch.setup.capabilities import CapabilityStore
from quickhatch.setup.orchestrator import SetupOrchestrator, SetupRuntime
from quickhatch.setup.support_bundle import export_support_bundle
from quickhatch.setup.types import (
    AppPlanItem,
    CommandGroup,
    ResearchPlan,
    SetupFacts,
    SetupStage,
    StrategyDecision,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "stop_abort_scenarios.yaml"


def _load_scenarios() -> list[dict[str, Any]]:
    payload = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    return list(payload["scenarios"])


SCENARIOS = _load_scenarios()
SCENARIO_BY_ID = {scenario["id"]: scenario for scenario in SCENARIOS}


class FileMemoryRunLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.events: list[dict[str, Any]] = []
        self._counter = 0

    def record(self, event_type: str, **payload: Any) -> None:
        self._counter += 1
        event = {
            "run_id": "stop-abort-test",
            "event_id": f"stop-abort-test:{self._counter:06d}",
            "event_type": event_type,
            **payload,
        }
        self.events.append(event)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, default=str) + "\n")


class FakeModelProvider:
    def __init__(self, response: str | None = None) -> None:
        self.calls: list[list[dict[str, str]]] = []
        self.response = response or json.dumps(
            {"action": "manual_followup", "reason": "manual follow-up after fake failure"}
        )

    def __call__(self, _config: AIConfig, messages: list[dict[str, str]], *_args: Any) -> str:
        self.calls.append(messages)
        return self.response


def _profile(*, mode: str = "configure_existing") -> UserProfile:
    profile = UserProfile()
    profile.install_username = "jan"
    profile.linux_user = "root" if mode == "fresh_install" else "jan"
    if mode == "fresh_install":
        profile.install_profile = "full_disk_wipe"
        profile.remote_setup_mode = "wipe_and_reinstall"
    else:
        profile.install_profile = "configure_existing"
        profile.remote_setup_mode = "configure_only"
    return profile


def _facts(
    *,
    mode: str = "configure_existing",
    is_live: bool = False,
    target_root_mounted: bool = False,
) -> SetupFacts:
    family = "arch"
    return SetupFacts(
        distro_family=family,
        os_id=family,
        package_manager="pacman",
        community_helper="none",
        current_user="root" if is_live else "jan",
        login_user="root" if is_live else "jan",
        is_root=is_live,
        sudo_nopasswd=True,
        is_live=is_live,
        live_marker="LIVE" if is_live else "INSTALLED",
        target_root_mounted=target_root_mounted,
        raw={
            "install_profile": "full_disk_wipe"
            if mode == "fresh_install"
            else "configure_existing",
            "remote_setup_mode": (
                "wipe_and_reinstall" if mode == "fresh_install" else "configure_only"
            ),
        },
    )


def _group(
    stage: SetupStage,
    command: str,
    *,
    group_id: str,
    title: str,
    packages: list[str] | None = None,
) -> CommandGroup:
    return CommandGroup(
        id=group_id,
        stage=stage,
        title=title,
        commands=[command],
        packages=list(packages or []),
    )


def _plan(*groups: CommandGroup, apps: list[AppPlanItem] | None = None) -> ResearchPlan:
    return ResearchPlan(
        target_summary="stop/abort test plan",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision("none", confidence="resolved_official", source="repo"),
        desktop_strategy=StrategyDecision("none", confidence="resolved_official", source="repo"),
        command_groups=list(groups),
        apps=list(apps or []),
    )


def _orchestrator(
    tmp_path: Path,
    *,
    mode: str = "configure_existing",
    facts: SetupFacts | None = None,
    plan: ResearchPlan | None = None,
    command_results: dict[str, tuple[int, str, str]] | None = None,
    stop_on_command: str = "",
    stop_during_precheck: bool = False,
    model: FakeModelProvider | None = None,
) -> tuple[SetupOrchestrator, list[str], FileMemoryRunLog, FakeModelProvider]:
    profile = _profile(mode=mode)
    bridge = BridgeState(profile, persist_path=None)
    facts = facts or _facts(mode=mode, is_live=(mode == "fresh_install"))
    fact_state = {"facts": facts}
    executed: list[str] = []
    command_results = dict(command_results or {})
    model = model or FakeModelProvider()

    def fake_ssh_exec(command: str, _timeout: int) -> tuple[int, str, str]:
        executed.append(command)
        if stop_on_command and stop_on_command in command:
            bridge.request_stop()
        for needle, result in command_results.items():
            if needle in command:
                return result
        return (0, "", "")

    def fake_precheck() -> tuple[bool, list[str], str]:
        if stop_during_precheck:
            bridge.request_stop()
        return (True, [], "ok")

    runtime = SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(provider="api_openai", endpoint="http://example", model_name="test"),
        host="127.0.0.1",
        user=facts.current_user or "jan",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=model,
        probe_facts=lambda **_kwargs: fact_state["facts"],
        detect_live_env=lambda: (
            fact_state["facts"].is_live,
            fact_state["facts"].live_marker or "",
        ),
        ssh_exec=fake_ssh_exec,
        repo_search=lambda _query: "",
        community_search=lambda _query: "",
        web_search=lambda _query, _max_results: "",
        wait_for_reconnect=lambda reconnect_user: (True, "fresh info", reconnect_user),
        run_pre_reboot_checks=fake_precheck,
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda _current, _is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=False,
    )
    orchestrator = SetupOrchestrator(runtime)
    runlog = FileMemoryRunLog(tmp_path / "runs" / "stop-abort-test.jsonl")
    orchestrator.runlog = runlog  # type: ignore[assignment]
    orchestrator.ctx.facts = facts
    orchestrator.ctx.plan = plan or _plan()
    return orchestrator, executed, runlog, model


def _event_names(runlog: FileMemoryRunLog) -> list[str]:
    return [str(event["event_type"]) for event in runlog.events]


def _bundle_texts(bundle: Path) -> dict[str, str]:
    with zipfile.ZipFile(bundle) as zf:
        return {
            name: zf.read(name).decode("utf-8")
            for name in zf.namelist()
            if name.endswith((".json", ".jsonl", ".sh", ".txt"))
        }


def test_stop_abort_fixture_schema_and_coverage() -> None:
    payload = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    summary = payload["merge_summary"]
    required = {
        "id",
        "category",
        "mode",
        "stage",
        "state_before",
        "event",
        "expected",
        "rationale",
    }
    categories = Counter(scenario["category"] for scenario in SCENARIOS)
    statuses = Counter(scenario["expected"]["final_status"] for scenario in SCENARIOS)

    assert len(SCENARIOS) == summary["total_scenarios"] == 74
    assert not summary["rejected_ambiguous_cases"]
    assert not [scenario["id"] for scenario in SCENARIOS if required - set(scenario)]
    assert {
        "user_stop",
        "repair_loop_stop",
        "pre_reboot_stop",
        "pending_question_stop",
        "capability_mismatch",
        "preflight_block",
        "safe_abort",
        "support_bundle",
    } <= set(categories)
    assert {"aborted", "blocked"} <= set(statuses)


def test_user_stop_during_app_install_preserves_progress_and_blocks_later_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = SCENARIO_BY_ID["user-stop-app-install-after-partial-success"]
    active = _group(
        SetupStage.APPS,
        "sudo pacman -S --noconfirm vlc",
        group_id="active-apps",
        title=scenario["state_before"]["active_group"],
        packages=["vlc"],
    )
    later = _group(
        SetupStage.APPS,
        "sudo pacman -S --noconfirm gimp",
        group_id="later-apps",
        title="Install later apps",
        packages=["gimp"],
    )
    apps = [
        AppPlanItem("Browser", package_name="firefox"),
        AppPlanItem("VLC", package_name="vlc"),
        AppPlanItem("GIMP", package_name="gimp"),
    ]
    orchestrator, executed, runlog, _model = _orchestrator(
        tmp_path,
        plan=_plan(active, later, apps=apps),
        stop_on_command="vlc",
    )
    orchestrator.ctx.stage_state[SetupStage.APPS.value] = {
        "completed_groups": list(scenario["state_before"]["completed_groups"])
    }
    monkeypatch.setattr(orchestrator, "_run_connect_and_probe", lambda: None)
    monkeypatch.setattr(orchestrator, "_run_research_and_plan", lambda: None)
    monkeypatch.setattr(orchestrator, "_run_install_or_skip", lambda: None)
    monkeypatch.setattr(orchestrator, "_run_postboot_driver", lambda: None)
    monkeypatch.setattr(orchestrator, "_run_desktop", lambda: None)
    monkeypatch.setattr(
        orchestrator,
        "_run_apps",
        lambda: orchestrator._run_stage_groups(SetupStage.APPS, audit_after=False),
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_final_audit",
        lambda: pytest.fail("final audit ran after user stop"),
    )

    summary = orchestrator.run()

    assert "stopped" in summary.lower()
    assert orchestrator.bridge.snapshot_setup_run()["status"] == "stopped"
    assert orchestrator.ctx.stage_state[SetupStage.APPS.value]["completed_groups"] == [
        "Install browser"
    ]
    assert any("vlc" in command for command in executed)
    assert not any("gimp" in command for command in executed)
    assert "run_started" in _event_names(runlog)
    assert "command_group_started" in _event_names(runlog)
    assert "success" not in summary.lower()


def test_stop_during_repair_loop_stops_model_and_executor_work(tmp_path: Path) -> None:
    scenario = SCENARIO_BY_ID["repair-stop-before-model-call"]
    failing = _group(
        SetupStage.APPS,
        "sudo pacman -S --noconfirm definitely-missing-app",
        group_id="missing-app",
        title=scenario["state_before"]["active_group"],
        packages=["definitely-missing-app"],
    )
    later = _group(
        SetupStage.APPS,
        "sudo pacman -S --noconfirm unrelated",
        group_id="later-app",
        title="Install unrelated app",
        packages=["unrelated"],
    )
    model = FakeModelProvider()
    orchestrator, executed, _runlog, model = _orchestrator(
        tmp_path,
        plan=_plan(failing, later),
        command_results={"definitely-missing-app": (1, "", "target not found")},
        stop_on_command="definitely-missing-app",
        model=model,
    )

    with pytest.raises(RuntimeError, match="Agent stopped"):
        orchestrator._run_stage_groups(SetupStage.APPS, audit_after=False)

    assert model.calls == []
    assert any(
        command == "sudo pacman -S --noconfirm definitely-missing-app" for command in executed
    )
    failed_command_index = executed.index("sudo pacman -S --noconfirm definitely-missing-app")
    assert executed[failed_command_index + 1 :] == []
    assert not any("unrelated" in command for command in executed)
    messages = "\n".join(message["content"] for message in orchestrator.bridge.messages)
    assert scenario["state_before"]["active_group"] in messages


def test_stop_before_reboot_prevents_controlled_reboot_dispatch(tmp_path: Path) -> None:
    scenario = SCENARIO_BY_ID["pre-reboot-stop-after-checks-pass"]
    facts = _facts(mode="fresh_install", is_live=True, target_root_mounted=True)
    orchestrator, executed, runlog, _model = _orchestrator(
        tmp_path,
        mode="fresh_install",
        facts=facts,
        stop_during_precheck=True,
    )
    orchestrator._ensure_reconnect_ssh_key = lambda: None  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Agent stopped"):
        orchestrator._run_install_or_skip()

    assert not any("quickhatch-reboot.log" in command for command in executed)
    assert "setup_checkpoint_saved" not in _event_names(runlog)
    assert scenario["expected"]["no_reboot_started"] is True


def test_stop_during_pending_question_marks_question_abandoned(tmp_path: Path) -> None:
    orchestrator, _executed, runlog, _model = _orchestrator(tmp_path)
    orchestrator.bridge.request_stop()

    with pytest.raises(RuntimeError, match="waiting for an answer"):
        orchestrator._ask_blocking_question(
            "Confirm target disk?",
            choices=["yes", "no"],
            stage=SetupStage.INSTALL_OR_SKIP,
            context={"question_kind": "target_disk_confirmation"},
            timeout=0,
        )

    assert orchestrator.bridge.get_pending_questions() == []
    answered = [question.answer for question in orchestrator.bridge.questions.values()]
    assert answered == ["[stopped before answer]"]
    setup_run = orchestrator.bridge.snapshot_setup_run()
    assert setup_run["waiting_for_input"] is False
    assert setup_run["active_question_id"] == ""
    assert setup_run["status"] == "stopped"
    assert "blocking_question_stopped" in _event_names(runlog)


def test_capability_mismatch_safe_abort_does_not_execute_or_broaden_retry(tmp_path: Path) -> None:
    facts = _facts(mode="fresh_install", is_live=True)
    group = _group(
        SetupStage.INSTALL_OR_SKIP,
        "sudo mkfs.ext4 /dev/sda2",
        group_id="format",
        title="Format selected root",
    )
    orchestrator, executed, runlog, model = _orchestrator(
        tmp_path,
        mode="fresh_install",
        facts=facts,
        plan=_plan(group),
    )
    assert isinstance(orchestrator.ctx.capability_store, CapabilityStore)
    orchestrator._prepare_group_operations = lambda *_args, **_kwargs: []  # type: ignore[method-assign]
    orchestrator._commands_for_group = lambda _stage, candidate: list(  # type: ignore[method-assign]
        candidate.commands
    )
    orchestrator.ctx.capability_store.grant(
        kind="format_filesystem",
        target="/dev/sda1",
        confirmation_text="FORMAT /dev/sda1",
        confirmation_id="preview-1",
        reason="test mismatched grant",
        preview_id="preview-1",
    )

    failure = orchestrator._execute_group(SetupStage.INSTALL_OR_SKIP, group, {})

    assert failure is not None
    assert failure.failure_type == "preflight"
    assert not any("mkfs" in command for command in executed)
    assert model.calls == []
    blocked = [
        event for event in runlog.events if event["event_type"] == "command_preflight_blocked"
    ]
    assert blocked
    reason = str(blocked[-1]["reason"])
    assert "format_filesystem" in reason
    assert "/dev/sda2" in reason
    assert "explicit QuickHatch capability" in reason


def test_fatal_preflight_block_does_not_execute_command_or_later_group(tmp_path: Path) -> None:
    dangerous = _group(
        SetupStage.INSTALL_OR_SKIP,
        "sudo wipefs -a /dev/sda",
        group_id="wipe",
        title="Wipe disk",
    )
    later = _group(
        SetupStage.INSTALL_OR_SKIP,
        "echo should-not-run",
        group_id="later",
        title="Later install group",
    )
    orchestrator, executed, runlog, _model = _orchestrator(
        tmp_path,
        mode="fresh_install",
        facts=_facts(mode="fresh_install", is_live=True),
        plan=_plan(dangerous, later),
    )

    with pytest.raises(RuntimeError):
        orchestrator._run_stage_groups(SetupStage.INSTALL_OR_SKIP, audit_after=False)

    assert not any("wipefs" in command for command in executed)
    assert not any("should-not-run" in command for command in executed)
    assert "command_preflight_blocked" in _event_names(runlog)
    assert orchestrator.bridge.snapshot_setup_run()["status"] != "success"


def test_no_new_destructive_group_starts_after_user_stop(tmp_path: Path) -> None:
    safe = _group(
        SetupStage.APPS,
        "sudo pacman -S --noconfirm vlc",
        group_id="safe",
        title="Install safe app",
    )
    destructive = _group(
        SetupStage.APPS,
        "sudo mkfs.ext4 /dev/sda1",
        group_id="destructive",
        title="Destructive follow-up should not start",
    )
    orchestrator, executed, runlog, _model = _orchestrator(
        tmp_path,
        plan=_plan(safe, destructive),
        stop_on_command="vlc",
    )

    with pytest.raises(RuntimeError, match="Agent stopped"):
        orchestrator._run_stage_groups(SetupStage.APPS, audit_after=False)

    assert any(command == "sudo pacman -S --noconfirm vlc" for command in executed)
    assert not any("mkfs" in command for command in executed)
    started_groups = [
        event["group"]["id"]
        for event in runlog.events
        if event["event_type"] == "command_group_started"
    ]
    assert started_groups == ["safe"]


def test_stop_runlog_and_support_bundle_include_abort_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    group = _group(
        SetupStage.APPS,
        "printf 'api_key=sk-redact' && sudo pacman -S --noconfirm vlc",
        group_id="stop-secret",
        title="Install app before stop",
    )
    later = _group(
        SetupStage.APPS,
        "sudo pacman -S --noconfirm gimp",
        group_id="later-after-stop",
        title="Later app after stop",
    )
    orchestrator, _executed, runlog, _model = _orchestrator(
        tmp_path,
        plan=_plan(group, later),
        stop_on_command="vlc",
    )
    monkeypatch.setattr(orchestrator, "_run_connect_and_probe", lambda: None)
    monkeypatch.setattr(orchestrator, "_run_research_and_plan", lambda: None)
    monkeypatch.setattr(orchestrator, "_run_install_or_skip", lambda: None)
    monkeypatch.setattr(orchestrator, "_run_postboot_driver", lambda: None)
    monkeypatch.setattr(orchestrator, "_run_desktop", lambda: None)
    monkeypatch.setattr(
        orchestrator,
        "_run_apps",
        lambda: orchestrator._run_stage_groups(SetupStage.APPS, audit_after=False),
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_final_audit",
        lambda: pytest.fail("final audit ran after stop"),
    )

    summary = orchestrator.run()
    bundle = export_support_bundle(runlog.path, out=tmp_path / "stop-bundle.zip")
    texts = _bundle_texts(Path(bundle.path))
    combined = "\n".join(texts.values()).lower()
    support_summary = json.loads(texts["support-summary.json"])

    assert "success" not in summary.lower()
    assert any(
        event_name in _event_names(runlog)
        for event_name in {"user_stop", "setup_stopped", "run_stopped", "stop_requested"}
    )
    assert support_summary["final_status"] in {"aborted", "stopped", "blocked"}
    assert "stop" in combined or "abort" in combined
    assert "sk-redact" not in combined
