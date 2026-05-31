from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from quickhatch.bridge.server import BridgeState
from quickhatch.config import AIConfig, UserProfile
from quickhatch.setup.audit import final_audit
from quickhatch.setup.orchestrator import SetupOrchestrator, SetupRuntime
from quickhatch.setup.types import (
    AuditResult,
    CommandGroup,
    DesktopDesignState,
    DesktopRuntimeState,
    DisplayRuntimeInfo,
    FinalSetupStatus,
    ResearchPlan,
    SetupFacts,
    SetupStage,
    StrategyDecision,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "design_feedback_scenarios.yaml"
DESIGN_ELEMENTS = ("theme", "icons", "cursor", "wallpaper", "effects")
UNVERIFIED_STATES = {
    "asset_missing",
    "failed_to_apply",
    "manual_followup_required",
    "no_active_gui_session",
    "persisted_but_not_live_verified",
    "wrong_desktop_environment",
    "wrong_user_or_session",
}
REPAIR_ACTIONS = {"install_asset", "retry_apply", "switch_de_strategy"}


@dataclass
class MemoryRunLog:
    events: list[dict[str, Any]] = field(default_factory=list)

    def record(self, event_type: str, **payload: Any) -> None:
        self.events.append({"event_type": event_type, **payload})


@dataclass
class CurrentHarnessOutcome:
    scenario_id: str
    stage_state: dict[str, Any]
    stage_results: list[AuditResult]
    final_status: FinalSetupStatus | None
    final_result: AuditResult | None
    executed_commands: list[str]
    refresh_count: int
    runlog: MemoryRunLog
    error: str = ""


class ScriptedFactsProvider:
    """Return scenario facts snapshots without probing a real desktop."""

    def __init__(self, scenario: dict[str, Any]) -> None:
        self.scenario = scenario
        self.refresh_count = 0

    def before(self) -> SetupFacts:
        return _facts_from_snapshot(self.scenario, "facts_before")

    def __call__(self, **_kwargs: Any) -> SetupFacts:
        self.refresh_count += 1
        if self.refresh_count == 1:
            return _facts_from_snapshot(self.scenario, "facts_after_attempt")
        return _facts_from_snapshot(self.scenario, "repair_result", nested=True)


def _load_fixture() -> dict[str, Any]:
    payload = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert isinstance(payload.get("scenarios"), list)
    return payload


FIXTURE = _load_fixture()
SCENARIOS: list[dict[str, Any]] = list(FIXTURE["scenarios"])


def _scenario(scenario_id: str) -> dict[str, Any]:
    for scenario in SCENARIOS:
        if scenario["id"] == scenario_id:
            return scenario
    raise AssertionError(f"missing design feedback scenario: {scenario_id}")


def _requested_value(scenario: dict[str, Any], element: str) -> Any:
    return scenario["request"].get(element)


def _is_requested(scenario: dict[str, Any], element: str) -> bool:
    value = _requested_value(scenario, element)
    return value not in ("", [], None)


def _expected_state(scenario: dict[str, Any], element: str) -> str:
    return str(scenario["expected"]["element_status"][element])


def _has_unverified_requested_element(scenario: dict[str, Any]) -> bool:
    return any(
        _is_requested(scenario, element) and _expected_state(scenario, element) in UNVERIFIED_STATES
        for element in DESIGN_ELEMENTS
    )


def _observed_value(scenario: dict[str, Any], snapshot: str, element: str) -> Any:
    if snapshot == "facts_after_repair":
        facts = scenario["repair_result"]["facts_after_repair"]
    else:
        facts = scenario[snapshot]
    if element == "effects":
        return list(facts.get("effects") or [])
    return facts.get(f"active_{element}", "")


def _normalize_design_value(value: Any) -> str:
    if isinstance(value, list):
        return "|".join(sorted(_normalize_design_value(item) for item in value))
    text = str(value or "").strip().lower()
    if text.startswith("file://"):
        text = text.removeprefix("file://")
    return text.replace("_", "-").replace(" ", "-")


def _values_match(expected: Any, observed: Any) -> bool:
    if isinstance(expected, list) or isinstance(observed, list):
        return set(expected or []) == set(observed or [])
    expected_text = _normalize_design_value(expected)
    observed_text = _normalize_design_value(observed)
    return bool(expected_text) and (
        expected_text == observed_text or expected_text in observed_text
    )


def _after_attempt_mismatches(scenario: dict[str, Any]) -> list[str]:
    mismatches: list[str] = []
    for element in DESIGN_ELEMENTS:
        if not _is_requested(scenario, element):
            continue
        if not _values_match(
            _requested_value(scenario, element),
            _observed_value(scenario, "facts_after_attempt", element),
        ):
            mismatches.append(element)
    return mismatches


def _desktop_runtime_state(scenario: dict[str, Any]) -> DesktopRuntimeState:
    has_gui = scenario["desktop"]["has_gui_session"]
    if has_gui is True:
        return DesktopRuntimeState.LIVE_APPLY_SUCCEEDED
    if has_gui == "unknown":
        return DesktopRuntimeState.PENDING_RELOGIN
    return DesktopRuntimeState.BLOCKED_NO_GUI_SESSION


def _facts_from_snapshot(
    scenario: dict[str, Any],
    snapshot: str,
    *,
    nested: bool = False,
) -> SetupFacts:
    desktop = scenario["desktop"]
    values = scenario[snapshot]["facts_after_repair"] if nested else scenario[snapshot]
    environment = str(desktop["environment"]).strip().lower()
    design = DesktopDesignState(environment=environment.upper() if environment else "")
    _set_design_value(design, environment, "theme", values.get("active_theme", ""))
    _set_design_value(design, environment, "icons", values.get("active_icons", ""))
    _set_design_value(design, environment, "cursor", values.get("active_cursor", ""))
    _set_design_value(design, environment, "wallpaper", values.get("active_wallpaper", ""))
    _set_design_effects(design, environment, values.get("effects") or [])
    has_gui = desktop["has_gui_session"] is True
    return SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        init_system="systemd",
        current_user=str(desktop["target_user"]),
        login_user=str(desktop["target_user"]),
        sudo_nopasswd=True,
        display_manager="sddm" if environment == "kde" else "gdm",
        display_manager_enabled=True,
        display_manager_active=True,
        desktop_runtime_state=_desktop_runtime_state(scenario),
        displays=[DisplayRuntimeInfo(name="Virtual-1", current_mode="1920x1080", primary=True)]
        if has_gui
        else [],
        desktop_design=design,
        raw={
            "QH_GUI_USER": str(desktop["target_user"]),
            "QH_GUI_HOME": str(desktop["target_home"]),
            "DISPLAY": ":0" if has_gui else "",
        },
    )


def _set_design_value(
    design: DesktopDesignState,
    desktop: str,
    element: str,
    value: Any,
) -> None:
    text = str(value or "")
    if element == "theme":
        if desktop == "kde":
            design.kde_color_scheme = text
        elif desktop == "gnome":
            design.gnome_gtk_theme = text
        elif desktop == "xfce":
            design.xfce_gtk_theme = text
    elif element == "icons":
        if desktop == "kde":
            design.kde_icon_theme = text
        elif desktop == "gnome":
            design.gnome_icon_theme = text
        elif desktop == "xfce":
            design.xfce_icon_theme = text
    elif element == "cursor":
        if desktop == "kde":
            design.kde_cursor_theme = text
        elif desktop == "gnome":
            design.gnome_cursor_theme = text
        elif desktop == "xfce":
            design.xfce_cursor_theme = text
    elif element == "wallpaper":
        if desktop == "kde":
            design.kde_wallpaper = text
        elif desktop == "gnome":
            design.gnome_wallpaper = text
        elif desktop == "xfce":
            design.xfce_wallpaper = text


def _set_design_effects(design: DesktopDesignState, desktop: str, effects: list[str]) -> None:
    if desktop != "kde":
        return
    effect_set = {str(effect).lower() for effect in effects}
    design.kde_wobbly_windows = "true" if "wobbly_windows" in effect_set else ""
    design.kde_magic_lamp = "true" if "magic_lamp" in effect_set else ""


def _plan_for_scenario(scenario: dict[str, Any]) -> ResearchPlan:
    requested = ", ".join(
        f"{element}={value}"
        for element, value in scenario["request"].items()
        if value not in ("", [], None)
    )
    return ResearchPlan(
        target_summary=f"Design feedback scenario {scenario['id']}: {requested}",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision(strategy="none", confidence="resolved_official"),
        desktop_strategy=StrategyDecision(
            strategy=requested or "desktop defaults",
            confidence="resolved_official",
            notes=requested,
        ),
        command_groups=[
            CommandGroup(
                id=scenario["id"],
                stage=SetupStage.DESKTOP,
                title=f"Design feedback {scenario['id']}",
                commands=list(scenario["attempted_commands"] or []),
            )
        ],
    )


def _runtime(
    profile: UserProfile,
    bridge: BridgeState,
    facts: SetupFacts,
    facts_provider: ScriptedFactsProvider,
    executed: list[str],
) -> SetupRuntime:
    def ssh_exec(command: str, _timeout: int) -> tuple[int, str, str]:
        executed.append(command)
        scenario = facts_provider.scenario
        result = scenario["command_result"]
        return int(result["rc"]), str(result["stdout"] or ""), str(result["stderr"] or "")

    def repair_model_response(_cfg: AIConfig, _messages: list[dict[str, str]]) -> str:
        repair = facts_provider.scenario["repair_result"]
        action = str(repair.get("action") or "none")
        if action in REPAIR_ACTIONS:
            return json.dumps(
                {
                    "action": "retry_group",
                    "commands": ["true # focused design repair placeholder"],
                    "reason": f"focused repair requested by fixture action {action}",
                }
            )
        return json.dumps(
            {
                "action": "manual_followup",
                "reason": "design feedback fixture requested manual follow-up",
            }
        )

    return SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(provider="api_openai", endpoint="http://example", model_name="test"),
        host="127.0.0.1",
        user=facts.current_user or "quickhatch",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=repair_model_response,
        probe_facts=facts_provider,
        detect_live_env=lambda: (False, "INSTALLED"),
        ssh_exec=ssh_exec,
        repo_search=lambda _query: "",
        community_search=lambda _query: "",
        web_search=lambda _query, max_results: "",
        wait_for_reconnect=lambda reconnect_user: (True, "fresh info", reconnect_user),
        run_pre_reboot_checks=lambda: (True, [], ""),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda _current, _is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=False,
    )


def _run_current_harness_scenario(scenario: dict[str, Any]) -> CurrentHarnessOutcome:
    provider = ScriptedFactsProvider(scenario)
    before = provider.before()
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    executed: list[str] = []
    orchestrator = SetupOrchestrator(_runtime(profile, bridge, before, provider, executed))
    runlog = MemoryRunLog()
    orchestrator.runlog = runlog  # type: ignore[assignment]
    orchestrator.ctx.facts = before
    orchestrator.ctx.plan = _plan_for_scenario(scenario)
    orchestrator._ask_blocking_question = (  # type: ignore[method-assign]
        lambda *args, **kwargs: "manual follow-up"
    )
    try:
        orchestrator._run_stage_groups(SetupStage.DESKTOP, audit_after=True)
        final_result = final_audit(
            orchestrator.ctx.facts or before,
            orchestrator.ctx.plan,
            orchestrator.ctx.stage_results,
        )
        final_status = final_result.final_status
        error = ""
    except Exception as exc:  # noqa: BLE001 - tests report harness mismatch details.
        final_result = None
        final_status = FinalSetupStatus.BLOCKED
        error = str(exc)
    return CurrentHarnessOutcome(
        scenario_id=scenario["id"],
        stage_state=dict(orchestrator.ctx.stage_state.get(SetupStage.DESKTOP.value, {})),
        stage_results=list(orchestrator.ctx.stage_results),
        final_status=final_status,
        final_result=final_result,
        executed_commands=executed,
        refresh_count=provider.refresh_count,
        runlog=runlog,
        error=error,
    )


def _desktop_followup_text(outcome: CurrentHarnessOutcome) -> str:
    parts: list[str] = []
    for result in outcome.stage_results:
        details = result.details or {}
        parts.extend(str(item) for item in details.get("desktop_design_manual_followups") or [])
        parts.extend(str(item) for item in details.get("display_preference_manual_followups") or [])
        for item in details.get("desktop_manual_followups") or []:
            parts.append(str(item.get("reason") if isinstance(item, dict) else item))
    if outcome.final_result is not None:
        parts.extend(finding.summary for finding in outcome.final_result.findings)
    return "\n".join(part for part in parts if part)


def test_design_feedback_fixture_schema_and_coverage() -> None:
    assert FIXTURE["merge_summary"]["total_scenarios"] == len(SCENARIOS)
    assert 50 <= len(SCENARIOS) <= 100

    covered_by_element = {element: set() for element in DESIGN_ELEMENTS}
    categories = {scenario["category"] for scenario in SCENARIOS}
    repair_actions = {scenario["repair_result"]["action"] for scenario in SCENARIOS}

    for scenario in SCENARIOS:
        assert set(scenario["expected"]["element_status"]) == set(DESIGN_ELEMENTS)
        assert set(scenario["request"]) == set(DESIGN_ELEMENTS)
        assert scenario["desktop"]["target_user"] == "quickhatch"
        assert scenario["desktop"]["target_home"] == "/home/quickhatch"
        for element in DESIGN_ELEMENTS:
            covered_by_element[element].add(_expected_state(scenario, element))

    assert {"theme", "wallpaper", "icons", "cursor", "effects"} <= categories
    assert {"wrong_de", "wrong_user", "no_session", "repair", "final_audit"} <= categories
    assert REPAIR_ACTIONS <= repair_actions
    for element in DESIGN_ELEMENTS:
        assert "applied_and_verified" in covered_by_element[element]
        assert "not_requested" in covered_by_element[element]


def test_command_success_is_not_enough_for_unverified_requested_elements() -> None:
    mismatches: list[str] = []
    for scenario in SCENARIOS:
        if int(scenario["command_result"]["rc"]) != 0:
            continue
        if not _after_attempt_mismatches(scenario):
            continue
        if not _has_unverified_requested_element(scenario):
            continue

        outcome = _run_current_harness_scenario(scenario)
        if outcome.final_status == FinalSetupStatus.SUCCESS:
            mismatches.append(
                f"{scenario['id']}: rc=0 with observed mismatch "
                f"{_after_attempt_mismatches(scenario)} became plain success"
            )

    assert not mismatches, "command-success feedback gaps:\n" + "\n".join(mismatches)


def test_harness_refreshes_facts_after_design_attempt_before_deciding_success() -> None:
    for scenario_id in (
        "theme-kde-rc0-unchanged",
        "wallpaper-kde-retry-still-unchanged",
        "final-audit-clean-no-repair",
    ):
        outcome = _run_current_harness_scenario(_scenario(scenario_id))

        assert outcome.executed_commands, scenario_id
        assert outcome.refresh_count >= 1, scenario_id
        assert any(event["event_type"] == "facts_refresh" for event in outcome.runlog.events)


def test_desired_vs_observed_comparison_names_the_failed_element() -> None:
    mismatches: list[str] = []
    for scenario_id in (
        "theme-kde-rc0-unchanged",
        "wallpaper-gnome-rc0-unchanged",
        "icons-kde-rc0-still-breeze",
        "cursor-gnome-asset-missing",
    ):
        scenario = _scenario(scenario_id)
        outcome = _run_current_harness_scenario(scenario)
        followup_text = _desktop_followup_text(outcome).lower()
        for element in _after_attempt_mismatches(scenario):
            if element not in followup_text:
                mismatches.append(
                    f"{scenario_id}: follow-up evidence did not name {element}; "
                    f"status={outcome.final_status}, error={outcome.error!r}"
                )

    assert not mismatches, "desired-vs-observed feedback gaps:\n" + "\n".join(mismatches)


def test_focused_repair_runs_for_supported_active_session_mismatches() -> None:
    mismatches: list[str] = []
    for scenario in SCENARIOS:
        if scenario["desktop"]["has_gui_session"] is not True:
            continue
        if scenario["repair_result"]["action"] not in REPAIR_ACTIONS:
            continue
        if not _after_attempt_mismatches(scenario):
            continue
        if int(scenario["command_result"]["rc"]) != 0:
            continue

        outcome = _run_current_harness_scenario(scenario)
        repair_events = [
            event for event in outcome.runlog.events if "repair" in str(event.get("event_type", ""))
        ]
        if outcome.refresh_count < 2 and not repair_events:
            mismatches.append(
                f"{scenario['id']}: expected focused {scenario['repair_result']['action']} "
                "after observed mismatch, but no repair/follow-up refresh occurred"
            )

    assert not mismatches, "focused design repair gaps:\n" + "\n".join(mismatches)


def test_no_active_gui_session_does_not_claim_live_visual_success() -> None:
    mismatches: list[str] = []
    for scenario in SCENARIOS:
        if scenario["category"] != "no_session":
            continue
        outcome = _run_current_harness_scenario(scenario)
        if outcome.final_status == FinalSetupStatus.SUCCESS:
            mismatches.append(f"{scenario['id']}: no active GUI reported plain success")
        for element in DESIGN_ELEMENTS:
            if _expected_state(scenario, element) == "no_active_gui_session":
                followup_text = _desktop_followup_text(outcome).lower()
                if element not in followup_text and "display" not in followup_text:
                    mismatches.append(
                        f"{scenario['id']}: no-GUI follow-up omitted {element}/display reason"
                    )

    assert not mismatches, "no-active-GUI feedback gaps:\n" + "\n".join(mismatches)


def test_wrong_de_and_wrong_user_are_not_treated_as_verified_success() -> None:
    mismatches: list[str] = []
    for scenario in SCENARIOS:
        if scenario["category"] not in {"wrong_de", "wrong_user"}:
            continue
        outcome = _run_current_harness_scenario(scenario)
        if outcome.final_status == FinalSetupStatus.SUCCESS:
            mismatches.append(
                f"{scenario['id']}: {scenario['category']} scenario became plain success"
            )
        if scenario["category"] == "wrong_user" and any(
            state == "wrong_user_or_session"
            for state in scenario["expected"]["element_status"].values()
        ):
            text = _desktop_followup_text(outcome).lower()
            if "wrong" not in text and "different home" not in text and "root" not in text:
                mismatches.append(f"{scenario['id']}: wrong-user reason missing from evidence")

    assert not mismatches, "wrong desktop/session feedback gaps:\n" + "\n".join(mismatches)


def test_per_element_manual_followup_preserves_successful_sibling_elements() -> None:
    scenario = _scenario("icons-xfce-fail-cursor-succeeds")
    outcome = _run_current_harness_scenario(scenario)
    element_followup_text = "\n".join(
        str(item)
        for result in outcome.stage_results
        for item in (result.details.get("desktop_design_manual_followups") or [])
    ).lower()

    assert _expected_state(scenario, "icons") == "failed_to_apply"
    assert _expected_state(scenario, "cursor") == "applied_and_verified"
    assert "icon" in element_followup_text
    assert "cursor" not in element_followup_text
    assert outcome.final_status in {
        FinalSetupStatus.SUCCESS_WITH_WARNINGS,
        FinalSetupStatus.MANUAL_FOLLOWUP_REQUIRED,
    }


def test_final_status_honesty_for_unapplied_or_unverified_design_elements() -> None:
    mismatches: list[str] = []
    for scenario in SCENARIOS:
        expected_effect = scenario["expected"]["final_status_effect"]
        if expected_effect == "none":
            continue
        outcome = _run_current_harness_scenario(scenario)
        if outcome.final_status == FinalSetupStatus.SUCCESS:
            mismatches.append(f"{scenario['id']}: expected {expected_effect}, got plain success")

    assert not mismatches, "final-status design honesty gaps:\n" + "\n".join(mismatches)


def test_runlog_evidence_includes_command_result_observed_facts_and_followup_reason() -> None:
    scenario = _scenario("wallpaper-kde-retry-still-unchanged")
    outcome = _run_current_harness_scenario(scenario)
    event_text = json.dumps(outcome.runlog.events, default=str).lower()
    followup_text = _desktop_followup_text(outcome).lower()

    assert "plasma-apply-wallpaperimage" in event_text
    assert '"rc": 0' in event_text
    assert "facts_refresh" in event_text
    assert "wallpaper" in event_text
    assert "wallpaper" in followup_text
    assert "mismatch" in followup_text or "not verified" in followup_text
