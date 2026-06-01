from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

import quickhatch.setup.orchestrator as orchestrator_module
from quickhatch.bridge.server import BridgeState
from quickhatch.config import AIConfig, UserProfile
from quickhatch.setup.orchestrator import GroupFailure, SetupOrchestrator, SetupRuntime
from quickhatch.setup.planner import build_repair_prompt
from quickhatch.setup.preflight import preflight_setup_decisions
from quickhatch.setup.types import CommandGroup, PreflightDecisionKind, SetupFacts, SetupStage

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "repair_suggestion_quality_scenarios.yaml"


@dataclass
class MemoryRunLog:
    events: list[dict[str, Any]] = field(default_factory=list)

    def record(self, event_type: str, **payload: Any) -> None:
        self.events.append({"event_type": event_type, **payload})


class ScriptedModel:
    def __init__(self, responses: list[dict[str, Any]], *, provider_error: str = "") -> None:
        self.responses = list(responses)
        self.provider_error = provider_error
        self.calls: list[list[dict[str, str]]] = []

    def __call__(
        self,
        _config: AIConfig,
        messages: list[dict[str, str]],
        *_args: object,
    ) -> str:
        self.calls.append(messages)
        if self.provider_error:
            raise RuntimeError(self.provider_error)
        assert self.responses, "Scripted repair model exhausted"
        return json.dumps(self.responses.pop(0))


def _load_fixture() -> dict[str, Any]:
    payload = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert isinstance(payload.get("scenarios"), list)
    return payload


FIXTURE = _load_fixture()
SCENARIOS = FIXTURE["scenarios"]
SCENARIO_BY_ID = {scenario["id"]: scenario for scenario in SCENARIOS}


def _stage(raw: str) -> SetupStage:
    return SetupStage(str(raw))


def _facts(profile: dict[str, Any]) -> SetupFacts:
    family = str(profile.get("family") or "ubuntu")
    package_manager = str(profile.get("package_manager") or "apt")
    aur_helper = str(profile.get("aur_helper") or "none")
    return SetupFacts(
        distro_family=family,
        os_id=family,
        package_manager=package_manager,
        community_helper="" if aur_helper == "none" else aur_helper,
        current_user="quickhatch",
        login_user="quickhatch",
        is_root=False,
        sudo_nopasswd=True,
        is_live=False,
        live_marker="INSTALLED",
        session_type="wayland",
        raw={
            "install_profile": "configure_existing",
            "remote_setup_mode": "configure_only",
            "flatpak_available": profile.get("flatpak_available"),
        },
    )


def _group(raw: dict[str, Any], stage: SetupStage) -> CommandGroup:
    return CommandGroup(
        id=str(raw.get("id") or "group"),
        stage=_stage(str(raw.get("stage") or stage.value)),
        title=str(raw.get("title") or "Repair suggestion group"),
        commands=list(raw.get("commands") or []),
        verification=list(raw.get("verification") or []),
        packages=list(raw.get("packages") or []),
        source=str(raw.get("source") or ""),
        optional=bool(raw.get("optional", False)),
        evidence=list(raw.get("evidence") or []),
    )


def _failure(raw: dict[str, Any]) -> GroupFailure:
    return GroupFailure(
        str(raw.get("type") or "command"),
        str(raw.get("command") or ""),
        str(raw.get("stdout") or ""),
        str(raw.get("stderr") or ""),
        str(raw.get("detail") or ""),
    )


def _responses(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(turn["response"]) for turn in scenario.get("model_turns", []) or []]


def _orchestrator_for_scenario(
    scenario: dict[str, Any],
    *,
    model: ScriptedModel,
) -> tuple[SetupOrchestrator, MemoryRunLog, dict[str, list[str]]]:
    facts = _facts(scenario.get("facts_profile") or {})
    profile = UserProfile()
    profile.install_profile = "configure_existing"
    profile.remote_setup_mode = "configure_only"
    bridge = BridgeState(profile, persist_path=None)
    search_calls: dict[str, list[str]] = {
        "repo_search": [],
        "community_search": [],
        "web_search": [],
    }
    scripted_results = scenario.get("search_results") or {}

    def repo_search(query: str) -> str:
        search_calls["repo_search"].append(query)
        return str((scripted_results.get("repo_search") or {}).get(query, ""))

    def community_search(query: str) -> str:
        search_calls["community_search"].append(query)
        return str((scripted_results.get("community_search") or {}).get(query, ""))

    def web_search(query: str, _max_results: int) -> str:
        search_calls["web_search"].append(query)
        return str((scripted_results.get("web_search") or {}).get(query, ""))

    runtime = SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(provider="api_openai", endpoint="http://example", model_name="test"),
        host="127.0.0.1",
        user="quickhatch",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=model,
        probe_facts=lambda **_kwargs: facts,
        detect_live_env=lambda: (False, "INSTALLED"),
        ssh_exec=lambda _command, _timeout: (0, "", ""),
        repo_search=repo_search,
        community_search=community_search,
        web_search=web_search,
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
    return orchestrator, runlog, search_calls


def _run_repair(
    scenario: dict[str, Any],
    *,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> tuple[dict[str, Any], MemoryRunLog, dict[str, list[str]], ScriptedModel]:
    provider_error = str(scenario.get("provider_error") or "")
    if provider_error and monkeypatch is not None:
        monkeypatch.setattr(orchestrator_module.time, "sleep", lambda _seconds: None)
    model = ScriptedModel(_responses(scenario), provider_error=provider_error)
    orchestrator, runlog, search_calls = _orchestrator_for_scenario(scenario, model=model)
    stage = _stage(scenario["stage"])
    action = orchestrator._repair_failed_group(
        stage,
        _group(scenario["failed_group"], stage),
        _failure(scenario["failure"]),
        repair_attempt=1,
    )
    return action, runlog, search_calls, model


def test_repair_suggestion_fixture_covers_goal_boundaries() -> None:
    metadata = FIXTURE["metadata"]
    assert metadata["total_scenarios"] == len(SCENARIOS)
    categories = set(metadata["scenarios_by_category"])
    assert {
        "repeated_package_missing",
        "source_fallback",
        "aur_helper_missing",
        "package_lock",
        "mirror_404",
        "provider_429",
        "theme_unavailable",
        "desktop_verification_failed",
        "dangerous_repair_scope",
    }.issubset(categories)
    assert metadata["expected_behaviors"]["diagnose_first_when_useful"] >= 5
    assert metadata["expected_behaviors"]["explicit_strategy_change"] >= 5
    assert metadata["expected_behaviors"]["manual_followup_for_optional_unresolved"] >= 3
    assert metadata["rejected_ambiguous_cases"] == []


@pytest.mark.parametrize(
    "scenario",
    [scenario for scenario in SCENARIOS if scenario.get("expected", {}).get("prompt_contains")],
    ids=lambda scenario: scenario["id"],
)
def test_repair_prompt_contains_quality_guardrails(scenario: dict[str, Any]) -> None:
    stage = _stage(scenario["stage"])
    prompt = build_repair_prompt(
        stage=stage,
        failed_group=_group(scenario["failed_group"], stage),
        failure_type=str(scenario["failure"]["type"]),
        failed_command=str(scenario["failure"]["command"]),
        stdout_tail=str(scenario["failure"].get("stdout") or ""),
        stderr_tail=str(scenario["failure"].get("stderr") or ""),
        facts=_facts(scenario.get("facts_profile") or {}),
        recent_results=[],
        native_search_active=False,
    )

    for needle in scenario["expected"]["prompt_contains"]:
        assert needle in prompt


def test_same_missing_package_retry_is_rejected_until_search_changes_strategy() -> None:
    scenario = SCENARIO_BY_ID["repair-same-missing-package-rejected"]

    action, runlog, search_calls, model = _run_repair(scenario)

    assert action["action"] == "retry_group"
    assert "flatpak install" in " ".join(action["group"].commands)
    assert search_calls["repo_search"] == ["ubuntu obsidian package apt"]
    assert len(model.calls) == 3
    assert any(
        event["event_type"] == scenario["expected"]["rejected_event"] for event in runlog.events
    )
    correction_messages = [
        message["content"]
        for call in model.calls[1:]
        for message in call
        if message["role"] == "user"
    ]
    assert any("meaningfully different" in message for message in correction_messages)


@pytest.mark.parametrize(
    "scenario_id",
    [
        "repair-repo-search-flatpak-fallback",
        "repair-aur-helper-missing-bootstrap-or-followup",
        "repair-package-lock-diagnose-not-pkill",
        "repair-desktop-verification-focused-retry",
        "repair-package-db-lock-dangerous-counterpart",
    ],
)
def test_repair_actions_are_scoped_safe_and_strategy_explicit(scenario_id: str) -> None:
    scenario = SCENARIO_BY_ID[scenario_id]

    action, _runlog, search_calls, _model = _run_repair(scenario)

    expected = scenario["expected"]
    assert action["action"] == expected["final_action"]
    if expected.get("final_group_command_contains"):
        assert expected["final_group_command_contains"] in " ".join(action["group"].commands)
    for search_name, expected_queries in (expected.get("search_queries") or {}).items():
        assert search_calls[search_name] == expected_queries


def test_theme_unavailable_becomes_specific_manual_followup_after_search() -> None:
    scenario = SCENARIO_BY_ID["repair-theme-unavailable-search-then-followup"]

    action, _runlog, search_calls, _model = _run_repair(scenario)

    assert action["action"] == "manual_followup"
    assert scenario["expected"]["reason_contains"] in action["reason"]
    assert search_calls["community_search"] == ["AUR sweet mars kde theme"]


def test_provider_429_for_noninstall_repair_becomes_manual_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = SCENARIO_BY_ID["repair-provider-429-followup-noninstall"]

    action, runlog, _search_calls, model = _run_repair(scenario, monkeypatch=monkeypatch)

    assert len(model.calls) == 3
    assert action["action"] == "manual_followup"
    assert scenario["expected"]["reason_contains"] in action["reason"]
    assert any(
        event["event_type"] == "repair_model_unavailable_manual_followup" for event in runlog.events
    )


def test_dangerous_package_db_counterpart_still_blocks_without_capability() -> None:
    scenario = SCENARIO_BY_ID["repair-package-db-lock-dangerous-counterpart"]
    command = scenario["dangerous_counterpart"]["command"]

    decisions = preflight_setup_decisions(command, _facts(scenario["facts_profile"]))

    assert any(decision.kind == PreflightDecisionKind.BLOCK for decision in decisions)
    assert any(decision.category == "package_database_integrity" for decision in decisions)
