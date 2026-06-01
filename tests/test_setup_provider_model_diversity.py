from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from quickhatch.bridge.server import BridgeState
from quickhatch.config import AIConfig, UserProfile
from quickhatch.setup.orchestrator import GroupFailure, SetupOrchestrator, SetupRuntime
from quickhatch.setup.planner import (
    ResearchPlanValidationError,
    build_repair_prompt,
    build_research_prompt,
    extract_research_plan,
)
from quickhatch.setup.types import CommandGroup, PlanItemStatus, SetupFacts, SetupStage

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "provider_model_diversity_scenarios.yaml"


@dataclass
class MemoryRunLog:
    events: list[dict[str, Any]] = field(default_factory=list)

    def record(self, event_type: str, **payload: Any) -> None:
        self.events.append({"event_type": event_type, **payload})


def _load_fixture() -> dict[str, Any]:
    payload = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert isinstance(payload.get("scenarios"), list)
    return payload


FIXTURE = _load_fixture()
SCENARIOS = FIXTURE["scenarios"]
SCENARIO_BY_ID = {scenario["id"]: scenario for scenario in SCENARIOS}


def _profile(provider: dict[str, Any] | None = None) -> UserProfile:
    provider = provider or {}
    profile = UserProfile()
    profile.remote_setup_mode = "configure_only"
    profile.install_profile = "configure_existing"
    profile.apps_to_install = ["Example"]
    profile.ai_config = AIConfig(
        provider=str(provider.get("provider") or "local_lmstudio"),
        endpoint="http://example",
        model_name="test-model",
        native_search=bool(provider.get("native_search", False)),
    )
    return profile


def _facts() -> SetupFacts:
    return SetupFacts(
        distro_family="arch",
        os_id="arch",
        package_manager="pacman",
        community_helper="yay",
        current_user="quickhatch",
        login_user="quickhatch",
        sudo_nopasswd=True,
        is_live=False,
        live_marker="INSTALLED",
        raw={
            "install_profile": "configure_existing",
            "remote_setup_mode": "configure_only",
        },
    )


def _strategy(evidence_source: str = "local_facts") -> dict[str, Any]:
    return {
        "strategy": "no_special_driver",
        "confidence": "medium",
        "source": "local_facts",
        "packages": [],
        "services": [],
        "notes": "Use normalized target facts; do not claim unavailable provider search.",
        "evidence": [
            {
                "source_type": evidence_source,
                "summary": f"Evidence came from {evidence_source}.",
                "query": "example package",
                "confidence": "medium",
            }
        ],
    }


def _plan_payload(
    *,
    evidence_source: str = "local_facts",
    app_status: str = "pending",
) -> dict[str, Any]:
    return {
        "target_summary": "Provider/model diversity configure-existing plan.",
        "distro_family": "arch",
        "package_manager": "pacman",
        "driver_strategy": _strategy(evidence_source),
        "desktop_strategy": _strategy("local_facts"),
        "command_groups": [
            {
                "id": "apps-example",
                "stage": "apps",
                "title": "Install or defer Example",
                "commands": ["sudo pacman -S --noconfirm example"],
                "verification": ["pacman -Q example"],
                "packages": ["example"],
                "source": "repo",
                "evidence": [
                    {
                        "source_type": "local_facts",
                        "summary": "Target package manager is pacman.",
                    }
                ],
            }
        ],
        "apps": [
            {
                "name": "Example",
                "package_name": "example",
                "source": "repo" if app_status == "pending" else "manual_followup",
                "status": app_status,
                "required": True,
                "reason": "Scripted provider/model diversity case.",
                "evidence": [
                    {
                        "source_type": "local_facts",
                        "summary": "No external provider evidence required.",
                    }
                ],
            }
        ],
        "unresolved_questions": [],
        "verification_commands": {},
        "required_items": ["Example"],
        "optional_items": [],
        "planner_revision": 1,
    }


def _prompt(provider: dict[str, Any], *, native_search_active: bool) -> str:
    return build_research_prompt(
        _profile(provider),
        _facts(),
        system_info="provider/model diversity target",
        migration_plan_md="Install Example if it can be verified safely.",
        distro_primer_md="Use current target facts and harness lookups.",
        native_search_active=native_search_active,
    )


def _runtime(
    scenario: dict[str, Any],
    model_call,
    *,
    repo_search=None,
    web_search=None,
    native_search_active: bool | None = None,
) -> tuple[SetupOrchestrator, MemoryRunLog, BridgeState]:
    profile = _profile(scenario.get("provider") or {})
    facts = _facts()
    bridge = BridgeState(profile, persist_path=None)
    runtime = SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=profile.ai_config,
        host="127.0.0.1",
        user="quickhatch",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=model_call,
        probe_facts=lambda **_kwargs: facts,
        detect_live_env=lambda: (False, "INSTALLED"),
        ssh_exec=lambda _command, _timeout: (0, "", ""),
        repo_search=repo_search or (lambda _query: "[repo_search]\nexample"),
        community_search=lambda _query: "",
        web_search=web_search or (lambda _query, _max_results: ""),
        wait_for_reconnect=lambda reconnect_user: (True, "fresh info", reconnect_user),
        run_pre_reboot_checks=lambda: (True, [], "ok"),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda _current, _is_live: "",
        migration_plan_md="provider/model diversity",
        distro_primer_md="",
        native_search_active=(
            bool((scenario.get("provider") or {}).get("native_search"))
            if native_search_active is None
            else native_search_active
        ),
    )
    orchestrator = SetupOrchestrator(runtime)
    runlog = MemoryRunLog()
    orchestrator.runlog = runlog  # type: ignore[assignment]
    orchestrator.ctx.facts = facts
    return orchestrator, runlog, bridge


def _repair_group(stage: SetupStage = SetupStage.APPS) -> CommandGroup:
    return CommandGroup(
        id="apps-example",
        stage=stage,
        title="Install Example",
        commands=["sudo pacman -S --noconfirm example"],
        packages=["example"],
    )


def _repair_failure() -> GroupFailure:
    return GroupFailure(
        failure_type="command",
        command="sudo pacman -S --noconfirm example",
        stdout="",
        stderr="target not found: example",
        detail="target not found",
    )


def _event_types(runlog: MemoryRunLog) -> list[str]:
    return [str(event["event_type"]) for event in runlog.events]


def test_provider_model_diversity_fixture_covers_goal_boundaries() -> None:
    metadata = FIXTURE["metadata"]
    assert metadata["total_scenarios"] == len(SCENARIOS)
    categories = set(metadata["scenarios_by_category"])
    assert {
        "fake_evidence",
        "hosted_search_mode",
        "hosted_transient_error",
        "local_model",
        "lookup_budget",
        "lookup_failure",
        "repair_provider_outage",
        "weak_output_correction",
    }.issubset(categories)
    assert metadata["expected_behaviors"]["fake_native_search_evidence_rejected"] >= 3
    assert metadata["expected_behaviors"]["provider_outage_noninstall_manual_followup"] == 1
    assert metadata["expected_behaviors"]["provider_outage_install_blocks"] == 1
    assert metadata["rejected_ambiguous_cases"] == []


@pytest.mark.parametrize(
    "scenario_id,native_search_active",
    [
        ("hosted-planning-disables-native-search", False),
        ("local-prompt-no-native-search", False),
    ],
)
def test_research_prompt_guides_provider_search_mode(
    scenario_id: str,
    native_search_active: bool,
) -> None:
    scenario = SCENARIO_BY_ID[scenario_id]
    prompt = _prompt(scenario["provider"], native_search_active=native_search_active)

    for needle in scenario["expected"]["prompt_contains"]:
        assert needle in prompt


def test_direct_plan_validation_rejects_fake_native_search_when_inactive() -> None:
    scenario = SCENARIO_BY_ID["inactive-provider-native-evidence-rejected-direct"]

    with pytest.raises(ResearchPlanValidationError) as exc_info:
        extract_research_plan(
            json.dumps(_plan_payload(evidence_source="provider_native_search")),
            _profile(scenario["provider"]),
            provider_native_search_active=False,
        )

    assert scenario["expected"]["error_contains"] in str(exc_info.value)


def test_direct_plan_validation_accepts_native_search_when_active() -> None:
    scenario = SCENARIO_BY_ID["active-provider-native-evidence-accepted-direct"]

    plan = extract_research_plan(
        json.dumps(_plan_payload(evidence_source="provider_native_search")),
        _profile(scenario["provider"]),
        provider_native_search_active=True,
    )

    assert plan.driver_strategy.evidence[0].source_type == "provider_native_search"


def test_planner_retries_local_fake_native_evidence_and_accepts_correction() -> None:
    scenario = SCENARIO_BY_ID["correction-after-fake-evidence-uses-local-facts"]
    calls: list[list[dict[str, str]]] = []

    def model_call(_cfg: AIConfig, messages: list[dict[str, str]], *_args: object) -> str:
        calls.append(messages)
        if len(calls) == 1:
            return json.dumps(
                {
                    "action": "plan",
                    "plan": _plan_payload(evidence_source="provider_native_search"),
                }
            )
        assert "provider_native_search" in messages[-1]["content"]
        return json.dumps({"action": "plan", "plan": _plan_payload(evidence_source="local_facts")})

    orchestrator, runlog, _bridge = _runtime(scenario, model_call, native_search_active=False)

    orchestrator._run_research_and_plan()

    assert len(calls) == scenario["expected"]["model_retry_count"]
    assert scenario["expected"]["runlog_event"] in _event_types(runlog)
    assert orchestrator.ctx.plan is not None
    assert (
        orchestrator.ctx.plan.driver_strategy.evidence[0].source_type
        == scenario["expected"]["final_evidence_source"]
    )


def test_local_malformed_plan_corrects_without_executing_commands() -> None:
    scenario = SCENARIO_BY_ID["local-malformed-plan-corrects"]
    calls: list[list[dict[str, str]]] = []

    def model_call(_cfg: AIConfig, messages: list[dict[str, str]], *_args: object) -> str:
        calls.append(messages)
        if len(calls) == 1:
            return "I think the plan is {"
        assert "invalid JSON" in messages[-1]["content"]
        return json.dumps({"action": "plan", "plan": _plan_payload()})

    orchestrator, runlog, _bridge = _runtime(scenario, model_call, native_search_active=False)

    orchestrator._run_research_and_plan()

    assert len(calls) == scenario["expected"]["model_retry_count"]
    assert scenario["expected"]["runlog_event"] in _event_types(runlog)
    assert orchestrator.ctx.plan is not None


def test_lookup_failure_feeds_model_and_degrades_to_specific_manual_followup() -> None:
    scenario = SCENARIO_BY_ID["lookup-failure-manual-followup"]
    calls: list[list[dict[str, str]]] = []
    lookup_calls: list[str] = []

    def repo_search(query: str) -> str:
        lookup_calls.append(query)
        raise RuntimeError("429 rate limited")

    def model_call(_cfg: AIConfig, messages: list[dict[str, str]], *_args: object) -> str:
        calls.append(messages)
        if len(calls) == 1:
            return json.dumps(
                {
                    "action": "repo_search",
                    "query": "arch example package",
                    "reason": "Need current package facts.",
                }
            )
        assert "[repo_search failed]" in messages[-1]["content"]
        return json.dumps(
            {
                "action": "plan",
                "plan": _plan_payload(app_status="manual_followup"),
            }
        )

    orchestrator, runlog, _bridge = _runtime(
        scenario,
        model_call,
        repo_search=repo_search,
        native_search_active=False,
    )

    orchestrator._run_research_and_plan()

    assert lookup_calls == ["arch example package"]
    assert scenario["expected"]["runlog_event"] in _event_types(runlog)
    assert orchestrator.ctx.plan is not None
    assert orchestrator.ctx.plan.apps[0].status == PlanItemStatus.MANUAL_FOLLOWUP


def test_lookup_budget_stops_repeated_search_requests() -> None:
    scenario = SCENARIO_BY_ID["repeated-lookup-budget-forces-plan"]
    calls: list[list[dict[str, str]]] = []
    repo_searches: list[str] = []

    def model_call(_cfg: AIConfig, messages: list[dict[str, str]], *_args: object) -> str:
        calls.append(messages)
        if len(calls) <= 4:
            return json.dumps(
                {
                    "action": "repo_search",
                    "query": f"example package attempt {len(calls)}",
                    "reason": "stuck weak-model lookup loop",
                }
            )
        assert "Do not request another repo_search" in messages[-1]["content"]
        return json.dumps({"action": "plan", "plan": _plan_payload()})

    orchestrator, runlog, _bridge = _runtime(
        scenario,
        model_call,
        repo_search=lambda query: repo_searches.append(query) or f"[repo_search]\n{query}",
        native_search_active=False,
    )

    orchestrator._run_research_and_plan()

    assert len(repo_searches) == scenario["expected"]["max_lookup_calls"]
    assert scenario["expected"]["runlog_event"] in _event_types(runlog)


def test_web_search_max_results_is_bounded() -> None:
    scenario = SCENARIO_BY_ID["web-search-max-results-bounded"]
    max_results_seen: list[int] = []

    def model_call(_cfg: AIConfig, _messages: list[dict[str, str]], *_args: object) -> str:
        if not max_results_seen:
            return json.dumps(
                {
                    "action": "web_search",
                    "query": "example linux package",
                    "max_results": 999,
                    "reason": "Need package source.",
                }
            )
        return json.dumps({"action": "plan", "plan": _plan_payload()})

    orchestrator, _runlog, _bridge = _runtime(
        scenario,
        model_call,
        web_search=lambda _query, max_results: max_results_seen.append(max_results) or "",
        native_search_active=False,
    )

    orchestrator._run_research_and_plan()

    assert max_results_seen == [scenario["expected"]["bounded_max_results"]]


def test_transient_hosted_provider_error_retries_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = SCENARIO_BY_ID["hosted-provider-503-recovers"]
    monkeypatch.setattr("quickhatch.setup.orchestrator.time.sleep", lambda _seconds: None)
    calls = {"count": 0}

    def model_call(_cfg: AIConfig, _messages: list[dict[str, str]], *_args: object) -> str:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("503 Service Unavailable")
        return json.dumps({"action": "plan", "plan": _plan_payload()})

    orchestrator, runlog, bridge = _runtime(scenario, model_call, native_search_active=False)

    orchestrator._run_research_and_plan()

    assert calls["count"] == scenario["expected"]["model_retry_count"]
    assert scenario["expected"]["runlog_event"] in _event_types(runlog)
    assert any("Planning LLM call failed" in message["content"] for message in bridge.messages)


def test_repair_search_mode_matches_provider_capability() -> None:
    scenario = SCENARIO_BY_ID["hosted-repair-keeps-native-search"]
    seen_native_search: list[bool] = []
    seen_prompt: list[str] = []

    def model_call(cfg: AIConfig, messages: list[dict[str, str]], *_args: object) -> str:
        seen_native_search.append(bool(cfg.native_search))
        seen_prompt.append(messages[0]["content"])
        return json.dumps(
            {
                "action": "manual_followup",
                "reason": "Provider search says this app needs manual installation.",
                "app_names": ["Example"],
            }
        )

    orchestrator, _runlog, _bridge = _runtime(scenario, model_call, native_search_active=True)

    action = orchestrator._repair_failed_group(
        SetupStage.APPS,
        _repair_group(),
        _repair_failure(),
        repair_attempt=1,
    )

    assert action["action"] == scenario["expected"]["outcome"]
    assert seen_native_search == [scenario["expected"]["native_search_sent_to_model"]]
    for needle in scenario["expected"]["prompt_contains"]:
        assert needle in seen_prompt[0]


def test_app_repair_provider_outage_becomes_manual_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = SCENARIO_BY_ID["app-repair-provider-outage-manual-followup"]
    monkeypatch.setattr("quickhatch.setup.orchestrator.time.sleep", lambda _seconds: None)

    def model_call(_cfg: AIConfig, _messages: list[dict[str, str]], *_args: object) -> str:
        raise RuntimeError("429 Too Many Requests")

    orchestrator, runlog, _bridge = _runtime(scenario, model_call, native_search_active=True)

    action = orchestrator._repair_failed_group(
        SetupStage.APPS,
        _repair_group(),
        _repair_failure(),
        repair_attempt=1,
    )

    assert action["action"] == scenario["expected"]["outcome"]
    assert scenario["expected"]["runlog_event"] in _event_types(runlog)


def test_install_repair_provider_outage_remains_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = SCENARIO_BY_ID["install-repair-provider-outage-blocks"]
    monkeypatch.setattr("quickhatch.setup.orchestrator.time.sleep", lambda _seconds: None)

    def model_call(_cfg: AIConfig, _messages: list[dict[str, str]], *_args: object) -> str:
        raise RuntimeError("429 Too Many Requests")

    orchestrator, runlog, _bridge = _runtime(scenario, model_call, native_search_active=True)

    with pytest.raises(RuntimeError, match="Repair LLM call failed"):
        orchestrator._repair_failed_group(
            SetupStage.INSTALL_OR_SKIP,
            _repair_group(SetupStage.INSTALL_OR_SKIP),
            _repair_failure(),
            repair_attempt=1,
        )

    assert scenario["expected"]["runlog_event"] in _event_types(runlog)


def test_local_invalid_repair_output_exhaustion_becomes_manual_followup() -> None:
    scenario = SCENARIO_BY_ID["local-invalid-repair-exhaustion-manual-followup"]

    def model_call(_cfg: AIConfig, _messages: list[dict[str, str]], *_args: object) -> str:
        return "{"

    orchestrator, _runlog, _bridge = _runtime(scenario, model_call, native_search_active=False)

    action = orchestrator._repair_failed_group(
        SetupStage.APPS,
        _repair_group(),
        _repair_failure(),
        repair_attempt=1,
    )

    assert action["action"] == scenario["expected"]["outcome"]
    assert scenario["expected"]["reason_contains"] in action["reason"]


def test_repair_prompt_for_local_model_disables_native_search_copy() -> None:
    scenario = SCENARIO_BY_ID["local-prompt-no-native-search"]

    prompt = build_repair_prompt(
        stage=SetupStage.APPS,
        failed_group=_repair_group(),
        failure_type="command",
        failed_command="sudo pacman -S --noconfirm example",
        stdout_tail="",
        stderr_tail="target not found: example",
        facts=_facts(),
        recent_results=[],
        native_search_active=False,
    )

    assert "Provider-native search is NOT available in repair mode" in prompt
    assert scenario["provider"]["provider"].startswith("local_")
