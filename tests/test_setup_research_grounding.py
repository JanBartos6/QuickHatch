from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from quickhatch.analysis import app_catalog
from quickhatch.bridge.server import BridgeState
from quickhatch.config import AIConfig, UserProfile
from quickhatch.setup.orchestrator import GroupFailure, SetupOrchestrator, SetupRuntime
from quickhatch.setup.planner import build_repair_prompt, build_research_prompt
from quickhatch.setup.types import CommandGroup, SetupFacts, SetupStage

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "research_grounding_scenarios.yaml"


def _load_fixture() -> dict[str, Any]:
    return yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))


FIXTURE = _load_fixture()
SCENARIOS = FIXTURE["scenarios"]


def _scenario(mode: str) -> list[dict[str, Any]]:
    return [scenario for scenario in SCENARIOS if scenario["mode"] == mode]


def _facts(setup: dict[str, Any]) -> SetupFacts:
    return SetupFacts(
        distro_family=str(setup.get("family", "arch")),
        package_manager=str(setup.get("package_manager", "pacman")),
        community_helper=str(setup.get("community_helper", "none")),
        current_user="jan",
        login_user="jan",
        raw={"install_profile": "configure_existing"},
    )


def _profile(setup: dict[str, Any]) -> UserProfile:
    profile = UserProfile()
    profile.apps_to_install = list(setup.get("apps") or ["VLC"])
    return profile


def _base_plan_payload(
    setup: dict[str, Any],
    *,
    app_name: str,
    package: str,
    source: str,
    status: str,
    reason: str = "",
    evidence_source: str = "repo_search",
) -> dict[str, Any]:
    return {
        "target_summary": f"{setup.get('family', 'arch')} configure-existing app setup.",
        "distro_family": setup.get("family", "arch"),
        "package_manager": setup.get("package_manager", "pacman"),
        "driver_strategy": {
            "strategy": "no_special_driver",
            "confidence": "medium",
            "source": "local_facts",
            "packages": [],
            "services": [],
            "notes": "No driver change requested.",
            "evidence": [
                {
                    "source_type": "local_facts",
                    "summary": "No driver package decision needed for this grounding test.",
                }
            ],
        },
        "desktop_strategy": {
            "strategy": "keep_existing_desktop",
            "confidence": "medium",
            "source": "local_facts",
            "packages": [],
            "services": [],
            "notes": "No desktop change requested.",
            "evidence": [
                {
                    "source_type": "local_facts",
                    "summary": "Desktop setup is outside this grounding test.",
                }
            ],
        },
        "command_groups": [
            {
                "id": "apps-grounding",
                "stage": "apps",
                "title": "Record app package decision",
                "commands": ["echo app package decision is pending verification"],
                "verification": [],
                "rationale": "Keep app package decisions visible without executing package managers.",
                "source": source,
                "packages": [package] if package else [],
                "optional": False,
                "evidence": [
                    {
                        "source_type": evidence_source,
                        "query": f"{app_name} package",
                        "summary": "Grounding evidence was supplied by the lookup loop.",
                    }
                ],
            }
        ],
        "apps": [
            {
                "name": app_name,
                "package_name": package,
                "source": source,
                "status": status,
                "required": True,
                "reason": reason or "Package decision still needs execution verification.",
                "evidence": [
                    {
                        "source_type": evidence_source,
                        "query": f"{app_name} package",
                        "summary": "Grounding evidence was supplied by the lookup loop.",
                    }
                ],
            }
        ],
        "unresolved_questions": [],
        "verification_commands": {},
        "required_items": list(setup.get("apps") or [app_name]),
        "optional_items": [],
        "planner_revision": 1,
    }


def _model_response_for_step(setup: dict[str, Any], step: dict[str, Any]) -> str:
    if "action" in step and step["action"] != "retry_group":
        return json.dumps(step)
    if step.get("action") == "retry_group":
        return json.dumps(step)
    payload = _base_plan_payload(
        setup,
        app_name=str(step["plan_app"]),
        package=str(step.get("package", "")),
        source=str(step.get("source", "manual_followup")),
        status=str(step.get("status", "pending")),
        reason=str(step.get("reason", "")),
        evidence_source=str(step.get("evidence_source", "repo_search")),
    )
    return json.dumps({"action": "plan", "plan": payload})


def _runtime(
    *,
    profile: UserProfile,
    bridge: BridgeState,
    facts: SetupFacts,
    model_call,
    search_calls: list[dict[str, Any]],
    setup: dict[str, Any],
) -> SetupRuntime:
    def search(action: str, query: str, max_results: int | None = None) -> str:
        call = {"action": action, "query": query}
        if max_results is not None:
            call["max_results"] = max_results
        search_calls.append(call)
        errors = (setup.get("search_errors") or {}).get(action) or {}
        if query in errors:
            raise RuntimeError(str(errors[query]))
        return ((setup.get("search_results") or {}).get(action) or {}).get(query, "")

    return SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(provider="api_openai", endpoint="http://example", model_name="test"),
        host="127.0.0.1",
        user="jan",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=model_call,
        probe_facts=lambda **_kwargs: facts,
        detect_live_env=lambda: (False, "INSTALLED"),
        ssh_exec=lambda _command, _timeout: (0, "", ""),
        repo_search=lambda query: search("repo_search", query),
        community_search=lambda query: search("community_search", query),
        web_search=lambda query, max_results: search("web_search", query, max_results),
        wait_for_reconnect=lambda reconnect_user: (True, "fresh info", reconnect_user),
        run_pre_reboot_checks=lambda: (True, [], ""),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda current, is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=bool(setup.get("native_search_active", False)),
    )


def _assert_contains(text: str, expected: dict[str, Any]) -> None:
    for needle in expected.get("contains") or []:
        assert needle in text
    for needle in expected.get("not_contains") or []:
        assert needle not in text


@pytest.mark.parametrize("scenario", _scenario("prompt_research"), ids=lambda item: item["id"])
def test_research_prompt_grounding_contracts(
    scenario: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = scenario["setup"]
    if setup.get("catalog_hint"):
        hint = setup["catalog_hint"]
        entry = app_catalog.AppCatalogEntry(
            id="test:hinted",
            name=hint["name"],
            compatibility="native",
            alternatives=(hint["name"],),
            source_hint="repo",
            confidence="high",
            install_hints={
                "arch": {"source": "repo", "packages": [hint["arch_package"]], "notes": ""},
                "ubuntu": {"source": "repo", "packages": [hint["ubuntu_package"]], "notes": ""},
            },
        )
        monkeypatch.setattr(app_catalog, "load_catalog", lambda: (entry,))

    prompt = build_research_prompt(
        _profile(setup),
        _facts(setup),
        system_info="system info",
        migration_plan_md="Install selected apps.",
        distro_primer_md="Use target package-manager evidence.",
        native_search_active=bool(setup.get("native_search_active", False)),
        target_distro_family=str(setup.get("family", "")),
    )

    _assert_contains(prompt, scenario["expected"])


@pytest.mark.parametrize("scenario", _scenario("prompt_repair"), ids=lambda item: item["id"])
def test_repair_prompt_grounding_contracts(scenario: dict[str, Any]) -> None:
    setup = scenario["setup"]
    raw_group = setup["failed_group"]
    raw_failure = setup["failure"]
    stage = SetupStage(raw_group.get("stage", "apps"))
    prompt = build_repair_prompt(
        stage=stage,
        failed_group=CommandGroup(
            id="failed",
            stage=stage,
            title=raw_group["title"],
            commands=list(raw_group["commands"]),
            packages=list(raw_group.get("packages", [])),
        ),
        failure_type="command",
        failed_command=raw_failure["command"],
        stdout_tail="",
        stderr_tail=raw_failure["stderr"],
        facts=_facts(setup),
        recent_results=[],
        native_search_active=bool(setup.get("native_search_active", False)),
    )

    _assert_contains(prompt, scenario["expected"])


@pytest.mark.parametrize("scenario", _scenario("planning_loop"), ids=lambda item: item["id"])
def test_planner_lookup_grounding_loop(scenario: dict[str, Any]) -> None:
    setup = scenario["setup"]
    profile = _profile(setup)
    bridge = BridgeState(profile, persist_path=None)
    facts = _facts(setup)
    model_steps = list(setup["model_steps"])
    seen_messages: list[list[dict[str, str]]] = []
    search_calls: list[dict[str, Any]] = []

    def model_call(_cfg, messages, *_args):
        seen_messages.append(messages)
        index = len(seen_messages) - 1
        assert index < len(model_steps)
        return _model_response_for_step(setup, model_steps[index])

    runtime = _runtime(
        profile=profile,
        bridge=bridge,
        facts=facts,
        model_call=model_call,
        search_calls=search_calls,
        setup=setup,
    )
    orchestrator = SetupOrchestrator(runtime)
    orchestrator.ctx.facts = facts
    orchestrator.ctx.system_info = "system info"

    orchestrator._run_research_and_plan()

    expected = scenario["expected"]
    assert search_calls == expected.get("search_calls", [])
    message_text = "\n".join(
        message["content"] for messages in seen_messages for message in messages
    )
    for needle in expected.get("message_contains") or []:
        assert needle in message_text
    assert orchestrator.ctx.plan is not None
    apps = {app.name: app for app in orchestrator.ctx.plan.apps}
    for name, status in (expected.get("app_status") or {}).items():
        assert apps[name].status.value == status
    for name, source in (expected.get("app_source") or {}).items():
        assert apps[name].source == source
    for name, reason in (expected.get("app_reason_contains") or {}).items():
        assert reason in apps[name].reason


@pytest.mark.parametrize("scenario", _scenario("repair_loop"), ids=lambda item: item["id"])
def test_repair_lookup_grounding_loop(scenario: dict[str, Any]) -> None:
    setup = scenario["setup"]
    profile = _profile(setup)
    bridge = BridgeState(profile, persist_path=None)
    facts = _facts(setup)
    model_steps = list(setup["model_steps"])
    seen_messages: list[list[dict[str, str]]] = []
    search_calls: list[dict[str, Any]] = []

    def model_call(_cfg, messages, *_args):
        seen_messages.append(messages)
        index = len(seen_messages) - 1
        assert index < len(model_steps)
        return _model_response_for_step(setup, model_steps[index])

    runtime = _runtime(
        profile=profile,
        bridge=bridge,
        facts=facts,
        model_call=model_call,
        search_calls=search_calls,
        setup=setup,
    )
    orchestrator = SetupOrchestrator(runtime)
    orchestrator.ctx.facts = facts
    raw_group = setup["failed_group"]
    stage = SetupStage(raw_group.get("stage", "apps"))
    action = orchestrator._repair_failed_group(
        stage,
        CommandGroup(
            id="failed",
            stage=stage,
            title=raw_group["title"],
            commands=list(raw_group["commands"]),
            packages=list(raw_group.get("packages", [])),
        ),
        GroupFailure(
            "command",
            setup["failure"]["command"],
            "",
            setup["failure"]["stderr"],
            setup["failure"]["stderr"],
        ),
        repair_attempt=1,
    )

    expected = scenario["expected"]
    assert action["action"] == expected["repair_action"]
    assert search_calls == expected.get("search_calls", [])
    if expected.get("manual_app_names") is not None:
        assert action.get("app_names") == expected["manual_app_names"]
    message_text = "\n".join(
        message["content"] for messages in seen_messages for message in messages
    )
    for needle in expected.get("message_contains") or []:
        assert needle in message_text


@pytest.mark.parametrize(
    "scenario", _scenario("repair_provider_error"), ids=lambda item: item["id"]
)
def test_repair_provider_error_becomes_manual_followup(scenario: dict[str, Any]) -> None:
    setup = scenario["setup"]
    profile = _profile(setup)
    bridge = BridgeState(profile, persist_path=None)
    facts = _facts(setup)
    runtime = _runtime(
        profile=profile,
        bridge=bridge,
        facts=facts,
        model_call=lambda *_args: "{}",
        search_calls=[],
        setup=setup,
    )
    orchestrator = SetupOrchestrator(runtime)
    orchestrator.ctx.facts = facts
    orchestrator._repair_model_call = lambda _messages: (_ for _ in ()).throw(
        RuntimeError(setup["provider_error"])
    )
    raw_group = setup["failed_group"]
    stage = SetupStage(raw_group.get("stage", "apps"))

    action = orchestrator._repair_failed_group(
        stage,
        CommandGroup(
            id="failed",
            stage=stage,
            title=raw_group["title"],
            commands=list(raw_group["commands"]),
            packages=list(raw_group.get("packages", [])),
        ),
        GroupFailure(
            "command",
            setup["failure"]["command"],
            "",
            setup["failure"]["stderr"],
            setup["failure"]["stderr"],
        ),
        repair_attempt=1,
    )

    assert action["action"] == scenario["expected"]["repair_action"]
    assert scenario["expected"]["reason_contains"] in action["reason"]


@pytest.mark.parametrize(
    "scenario", _scenario("planning_provider_error"), ids=lambda item: item["id"]
)
def test_planning_provider_error_blocks_without_success(scenario: dict[str, Any]) -> None:
    setup = scenario["setup"]
    profile = _profile(setup)
    bridge = BridgeState(profile, persist_path=None)
    facts = _facts(setup)
    runtime = _runtime(
        profile=profile,
        bridge=bridge,
        facts=facts,
        model_call=lambda *_args: "{}",
        search_calls=[],
        setup=setup,
    )
    orchestrator = SetupOrchestrator(runtime)
    orchestrator._planner_model_call = lambda _messages: (_ for _ in ()).throw(
        RuntimeError(setup["provider_error"])
    )

    summary = orchestrator.run()
    setup_run = bridge.snapshot_setup_run()

    assert setup_run["status"] == scenario["expected"]["setup_status"]
    assert scenario["expected"]["summary_contains"] in summary
    assert "success" not in summary.lower()


def test_research_grounding_fixture_has_expected_coverage() -> None:
    categories = {scenario["category"] for scenario in SCENARIOS}
    modes = {scenario["mode"] for scenario in SCENARIOS}

    assert {
        "stale_package",
        "vendor_app",
        "source_ambiguity",
        "provider_native",
        "search_unavailable",
        "provider_error",
        "catalog_hint",
        "search_budget",
    } <= categories
    assert {
        "prompt_research",
        "prompt_repair",
        "planning_loop",
        "repair_loop",
        "repair_provider_error",
        "planning_provider_error",
    } <= modes
