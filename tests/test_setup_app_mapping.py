from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from quickhatch.analysis import app_catalog
from quickhatch.analysis.app_mapper import map_apps
from quickhatch.bridge.server import BridgeState
from quickhatch.config import AIConfig, AppInfo, UserProfile
from quickhatch.setup.orchestrator import GroupFailure, SetupOrchestrator, SetupRuntime
from quickhatch.setup.planner import build_research_prompt, extract_research_plan
from quickhatch.setup.preflight import preflight_setup_command
from quickhatch.setup.types import (
    AppPlanItem,
    CommandGroup,
    PlanItemStatus,
    ResearchPlan,
    SetupFacts,
    SetupStage,
    StrategyDecision,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "app_mapping_scenarios.yaml"


def _load_fixture() -> dict[str, Any]:
    return yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))


FIXTURE = _load_fixture()
CASES = FIXTURE["cases"]


def _cases(mode: str) -> list[dict[str, Any]]:
    return [case for case in CASES if case["mode"] == mode]


def _facts(case: dict[str, Any]) -> SetupFacts:
    profile = case.get("facts_profile") or {}
    family = str(profile.get("family", "arch"))
    package_manager = str(profile.get("package_manager", "pacman"))
    return SetupFacts(
        distro_family=family,
        package_manager=package_manager,
        community_helper=str(profile.get("community_helper", "none")),
        current_user="jan",
        login_user="jan",
        sudo_nopasswd=True,
        raw={"install_profile": "configure_existing"},
    )


def _profile(case: dict[str, Any]) -> UserProfile:
    profile = UserProfile()
    profile.apps_to_install = list(case.get("requested_apps") or [])
    profile.apps = [AppInfo(name=name, source="winget") for name in profile.apps_to_install]
    return profile


def _status_value(status: PlanItemStatus | str) -> str:
    return status.value if isinstance(status, PlanItemStatus) else str(status)


def _catalog_entry(raw: dict[str, Any]) -> app_catalog.AppCatalogEntry:
    return app_catalog.AppCatalogEntry(
        id=str(raw.get("id") or f"test:{raw['name']}"),
        name=str(raw["name"]),
        match_names=tuple(raw.get("match_names", []) or [raw["name"]]),
        compatibility=str(raw.get("compatibility", "unknown")),
        alternatives=tuple(raw.get("alternatives", []) or [raw["name"]]),
        source_hint=str(raw.get("source_hint", "unknown")),
        confidence=str(raw.get("confidence", "low")),
        category=str(raw.get("category", "other")),
        notes=str(raw.get("notes", "")),
        install_hints=dict(raw.get("install_hints") or {}),
    )


def _install_test_catalog(monkeypatch: pytest.MonkeyPatch, entries: list[dict[str, Any]]) -> None:
    catalog_entries = tuple(_catalog_entry(raw) for raw in entries)
    monkeypatch.setattr(app_catalog, "load_catalog", lambda: catalog_entries)
    app_catalog._catalog_exact_index.cache_clear()
    app_catalog._catalog_phrase_candidates.cache_clear()


def _base_plan_payload(case: dict[str, Any], apps: list[dict[str, Any]]) -> dict[str, Any]:
    facts = case.get("facts_profile") or {}
    return {
        "target_summary": f"{facts.get('family', 'generic')} app mapping test",
        "distro_family": facts.get("family", "generic"),
        "package_manager": facts.get("package_manager", "generic"),
        "driver_strategy": {
            "strategy": "no_driver_change",
            "confidence": "medium",
            "source": "local_facts",
            "packages": [],
            "services": [],
            "notes": "Driver setup is outside app mapping tests.",
            "evidence": [
                {
                    "source_type": "local_facts",
                    "summary": "No driver package decision needed.",
                }
            ],
        },
        "desktop_strategy": {
            "strategy": "keep_existing_desktop",
            "confidence": "medium",
            "source": "local_facts",
            "packages": [],
            "services": [],
            "notes": "Desktop setup is outside app mapping tests.",
            "evidence": [
                {
                    "source_type": "local_facts",
                    "summary": "No desktop package decision needed.",
                }
            ],
        },
        "command_groups": [
            {
                "id": "apps-mapping-policy",
                "stage": "apps",
                "title": "Record app mapping decision",
                "commands": ["printf 'app mapping decision recorded\\n'"],
                "verification": [],
                "rationale": "This test validates planning decisions without package execution.",
                "source": "repo",
                "packages": [],
                "evidence": [
                    {
                        "source_type": "local_facts",
                        "summary": "Command group is a harmless placeholder for parser tests.",
                    }
                ],
            }
        ],
        "apps": apps,
        "unresolved_questions": [],
        "verification_commands": {},
        "required_items": list(case.get("requested_apps") or []),
        "optional_items": [],
        "planner_revision": 1,
    }


def _planner_app_from_step(step: dict[str, Any]) -> dict[str, Any]:
    app_name = str(step["plan_app"])
    package = str(step.get("package_name", ""))
    evidence_source = str(step.get("evidence_source", "repo_search"))
    return {
        "name": app_name,
        "package_name": package,
        "source": str(step.get("source", "manual_followup")),
        "status": str(step.get("status", "pending")),
        "required": True,
        "reason": str(step.get("reason", "")),
        "evidence": [
            {
                "source_type": evidence_source,
                "query": f"{app_name} package",
                "summary": "App mapping decision came from mocked target evidence.",
            }
        ],
    }


def _model_response_for_step(case: dict[str, Any], step: dict[str, Any]) -> str:
    if "action" in step:
        return json.dumps(step)
    payload = _base_plan_payload(case, [_planner_app_from_step(step)])
    return json.dumps({"action": "plan", "plan": payload})


def _runtime(
    *,
    case: dict[str, Any],
    profile: UserProfile,
    bridge: BridgeState,
    facts: SetupFacts,
    model_call=lambda _cfg, _messages: "{}",
    search_calls: list[dict[str, Any]] | None = None,
    executed_commands: list[str] | None = None,
) -> SetupRuntime:
    search_calls = search_calls if search_calls is not None else []
    executed_commands = executed_commands if executed_commands is not None else []
    search_results = case.get("search_results") or {}

    def search(action: str, query: str, max_results: int | None = None) -> str:
        call = {"action": action, "query": query}
        if max_results is not None:
            call["max_results"] = max_results
        search_calls.append(call)
        return ((search_results.get(action) or {}).get(query)) or ""

    def ssh_exec(command: str, _timeout: int) -> tuple[int, str, str]:
        executed_commands.append(command)
        if "flatpak search" in command:
            flatpak_results = search_results.get("flatpak_search") or {}
            for query, result in flatpak_results.items():
                if str(query) in command:
                    return 0, str(result), ""
            return 0, "", ""
        return 0, "", ""

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
        ssh_exec=ssh_exec,
        repo_search=lambda query: search("repo_search", query),
        community_search=lambda query: search("community_search", query),
        web_search=lambda query, max_results: search("web_search", query, max_results),
        wait_for_reconnect=lambda reconnect_user: (True, "fresh info", reconnect_user),
        run_pre_reboot_checks=lambda: (True, [], ""),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda current, is_live: "",
        migration_plan_md="Install selected apps.",
        distro_primer_md="Use target package-manager evidence for app packages.",
    )


def _plan_from_case(case: dict[str, Any]) -> ResearchPlan:
    apps = [
        AppPlanItem(
            name=str(app["name"]),
            package_name=str(app.get("package_name", "")),
            source=str(app.get("source", "manual_followup")),
            status=PlanItemStatus(str(app.get("status", "pending"))),
            required=bool(app.get("required", True)),
        )
        for app in case.get("planner_apps", [])
    ]
    return ResearchPlan(
        target_summary="App mapping plan",
        distro_family=str((case.get("facts_profile") or {}).get("family", "arch")),
        package_manager=str((case.get("facts_profile") or {}).get("package_manager", "pacman")),
        driver_strategy=StrategyDecision(strategy="none"),
        desktop_strategy=StrategyDecision(strategy="none"),
        apps=apps,
    )


def _group_from_case(case: dict[str, Any]) -> CommandGroup:
    group = case["command_group"]
    return CommandGroup(
        id="apps",
        stage=SetupStage.APPS,
        title="Install selected apps",
        commands=[str(group["command"])],
        packages=list(group.get("packages") or []),
        source=str((case.get("app") or {}).get("source") or "repo"),
    )


def _assert_expected_apps(plan: ResearchPlan, expected: dict[str, Any]) -> None:
    by_name = {app.name: app for app in plan.apps}
    for name, app_expected in (expected.get("apps") or {}).items():
        assert name in by_name
        app = by_name[name]
        if "package_name" in app_expected:
            assert app.package_name == app_expected["package_name"]
        if "source" in app_expected:
            assert app.source == app_expected["source"]
        if "status" in app_expected:
            assert _status_value(app.status) == app_expected["status"]
        if "reason_contains" in app_expected:
            assert app_expected["reason_contains"] in app.reason


def test_fixture_merge_report_matches_cases() -> None:
    assert FIXTURE["metadata"]["merge_report"]["total_cases"] == len(CASES)


@pytest.mark.parametrize("case", _cases("app_mapper"), ids=lambda item: item["id"])
def test_app_mapper_labels_windows_only_and_alternatives(case: dict[str, Any]) -> None:
    mapped = map_apps([AppInfo(name=name, source="winget") for name in case["requested_apps"]])
    by_name = {app.name: app for app in mapped}
    expected = case["expected"]
    app = by_name[expected["app"]]

    assert app.compatibility == expected["compatibility"]
    if "linux_alternative" in expected:
        assert app.linux_alternative == expected["linux_alternative"]
    if "linux_alternative_contains" in expected:
        assert expected["linux_alternative_contains"] in (app.linux_alternative or "")
    if "notes_contains" in expected:
        assert expected["notes_contains"] in (app.notes or "")


@pytest.mark.parametrize("case", _cases("catalog_context"), ids=lambda item: item["id"])
def test_selected_app_catalog_context_stays_advisory(
    case: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    if case.get("catalog_entry"):
        _install_test_catalog(monkeypatch, [case["catalog_entry"]])
    rows = app_catalog.selected_app_catalog_context(
        case["requested_apps"],
        scanned_apps=[],
        distro_family=case["facts_profile"]["family"],
    )

    assert len(rows) == 1
    row = rows[0]
    expected = case["expected"]
    assert row["name"] == expected["name"]
    assert row["compatibility"] == expected["compatibility"]
    if "source_hint" in expected:
        assert row["source_hint"] == expected["source_hint"]
    if "notes_contains" in expected:
        assert expected["notes_contains"] in row["notes"]
    if "active_install_hint" in expected:
        for key, value in expected["active_install_hint"].items():
            assert row["active_install_hint"][key] == value


@pytest.mark.parametrize("case", _cases("prompt_contract"), ids=lambda item: item["id"])
def test_research_prompt_contains_app_mapping_guardrails(case: dict[str, Any]) -> None:
    prompt = build_research_prompt(
        _profile(case),
        _facts(case),
        system_info="system info",
        migration_plan_md="Install selected apps.",
        distro_primer_md="Use current target package evidence.",
        native_search_active=False,
        target_distro_family=case["facts_profile"]["family"],
    )

    for needle in case["expected"]["contains"]:
        assert needle in prompt


@pytest.mark.parametrize("case", _cases("planner_parse"), ids=lambda item: item["id"])
def test_planner_app_items_normalize_sources_and_missing_requests(case: dict[str, Any]) -> None:
    payload = _base_plan_payload(case, list(case.get("planner_apps") or []))
    plan = extract_research_plan(json.dumps(payload), _profile(case))

    _assert_expected_apps(plan, case["expected"])


@pytest.mark.parametrize("case", _cases("planner_loop"), ids=lambda item: item["id"])
def test_planner_loop_maps_apps_from_target_evidence(case: dict[str, Any]) -> None:
    facts = _facts(case)
    profile = _profile(case)
    bridge = BridgeState(profile, persist_path=None)
    search_calls: list[dict[str, Any]] = []
    steps = iter(case["model_steps"])

    def model_call(_config: AIConfig, _messages: list[dict[str, str]]) -> str:
        return _model_response_for_step(case, next(steps))

    orchestrator = SetupOrchestrator(
        _runtime(
            case=case,
            profile=profile,
            bridge=bridge,
            facts=facts,
            model_call=model_call,
            search_calls=search_calls,
        )
    )
    orchestrator.ctx.facts = facts
    orchestrator.ctx.system_info = "system info"

    orchestrator._run_research_and_plan()

    assert search_calls == case["expected"]["search_calls"]
    assert orchestrator.ctx.plan is not None
    _assert_expected_apps(orchestrator.ctx.plan, case["expected"])


@pytest.mark.parametrize("case", _cases("missing_repair"), ids=lambda item: item["id"])
def test_missing_app_package_repair_uses_search_and_fallback(case: dict[str, Any]) -> None:
    facts = _facts(case)
    profile = _profile(case)
    bridge = BridgeState(profile, persist_path=None)
    executed: list[str] = []
    orchestrator = SetupOrchestrator(
        _runtime(case=case, profile=profile, bridge=bridge, facts=facts, executed_commands=executed)
    )
    orchestrator.ctx.facts = facts
    raw_app = case["app"]
    app = AppPlanItem(
        name=raw_app["name"],
        package_name=raw_app["package_name"],
        source=raw_app["source"],
        required=bool(raw_app.get("required", True)),
    )
    orchestrator.ctx.plan = ResearchPlan(
        target_summary="App mapping repair",
        distro_family=facts.distro_family,
        package_manager=facts.package_manager,
        driver_strategy=StrategyDecision(strategy="none"),
        desktop_strategy=StrategyDecision(strategy="none"),
        apps=[app],
    )
    group = _group_from_case(case)
    failure = GroupFailure(
        failure_type="command",
        command=group.commands[0],
        stdout="",
        stderr=str(case["failure"]["stderr"]),
        detail=str(case["failure"]["stderr"]),
    )

    repaired = orchestrator._auto_repair_missing_repo_targets(SetupStage.APPS, group, failure)

    assert repaired is not None
    assert case["expected"]["retry_command"] in repaired.commands
    assert app.package_name == case["expected"]["package_name"]
    assert app.source == case["expected"]["source"]
    assert app.status == PlanItemStatus.PENDING


@pytest.mark.parametrize("case", _cases("run_apps"), ids=lambda item: item["id"])
def test_unsupported_app_source_becomes_specific_followup(case: dict[str, Any]) -> None:
    facts = _facts(case)
    profile = _profile(case)
    bridge = BridgeState(profile, persist_path=None)
    executed: list[str] = []
    orchestrator = SetupOrchestrator(
        _runtime(case=case, profile=profile, bridge=bridge, facts=facts, executed_commands=executed)
    )
    orchestrator.ctx.facts = facts
    orchestrator.ctx.plan = _plan_from_case(case)

    orchestrator._run_apps()

    _assert_expected_apps(orchestrator.ctx.plan, case["expected"])
    if case["expected"].get("no_install_commands"):
        install_commands = [
            command
            for command in executed
            if " install " in f" {command} "
            or " -S " in f" {command} "
            or " apk add " in f" {command} "
        ]
        assert install_commands == []
    elif "executed_commands" in case["expected"]:
        assert executed == case["expected"]["executed_commands"]


@pytest.mark.parametrize("case", _cases("prefilter"), ids=lambda item: item["id"])
def test_already_installed_apps_are_filtered_without_poisoning_others(
    case: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    facts = _facts(case)
    profile = _profile(case)
    bridge = BridgeState(profile, persist_path=None)
    orchestrator = SetupOrchestrator(
        _runtime(case=case, profile=profile, bridge=bridge, facts=facts)
    )
    orchestrator.ctx.facts = facts
    orchestrator.ctx.plan = _plan_from_case(case)
    group = _group_from_case(case)

    monkeypatch.setattr(
        orchestrator,
        "_query_installed_native_packages",
        lambda _packages, _facts: set(case["installed_packages"]),
    )

    replacement = orchestrator._filter_installed_app_group(group, {})

    if case["expected"].get("skipped_group"):
        assert replacement is None
    else:
        assert replacement is not None
        assert replacement.commands == [case["expected"]["replacement_command"]]
        assert replacement.packages == case["expected"]["replacement_packages"]
    assert orchestrator.ctx.plan is not None
    _assert_expected_apps(orchestrator.ctx.plan, case["expected"])


@pytest.mark.parametrize("case", _cases("partial_failure"), ids=lambda item: item["id"])
def test_one_missing_app_package_does_not_poison_group(case: dict[str, Any]) -> None:
    facts = _facts(case)
    profile = _profile(case)
    bridge = BridgeState(profile, persist_path=None)
    orchestrator = SetupOrchestrator(
        _runtime(case=case, profile=profile, bridge=bridge, facts=facts)
    )
    orchestrator.ctx.facts = facts
    orchestrator.ctx.plan = _plan_from_case(case)
    group = _group_from_case(case)
    failure = GroupFailure(
        failure_type="command",
        command=group.commands[0],
        stdout="",
        stderr=str(case["failure"]["stderr"]),
        detail=str(case["failure"]["stderr"]),
    )

    assert orchestrator._auto_repair_missing_repo_targets(SetupStage.APPS, group, failure) is None
    repaired = orchestrator._auto_repair_missing_app_targets(
        group,
        failure,
        set(case["failure"]["missing_packages"]),
        manual_after_repair=True,
        manual_reason="Cursor package was unresolved after target search/repair.",
    )

    assert repaired is not None
    assert repaired.commands == [case["expected"]["retry_command"]]
    assert orchestrator.ctx.plan is not None
    _assert_expected_apps(orchestrator.ctx.plan, case["expected"])


@pytest.mark.parametrize("case", _cases("preflight"), ids=lambda item: item["id"])
def test_wrong_package_manager_mapping_is_not_hard_block(case: dict[str, Any]) -> None:
    assert preflight_setup_command(case["command"], _facts(case)) is None
