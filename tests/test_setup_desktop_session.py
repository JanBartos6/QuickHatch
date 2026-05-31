from __future__ import annotations

import json
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from quickhatch.bridge.server import BridgeState
from quickhatch.config import AIConfig, UserProfile
from quickhatch.setup.audit import audit_stage, final_audit
from quickhatch.setup.orchestrator import SetupOrchestrator, SetupRuntime
from quickhatch.setup.support_bundle import export_support_bundle
from quickhatch.setup.types import (
    AuditFindingSeverity,
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

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "desktop_session_scenarios.yaml"
DESIGN_ELEMENTS = ("theme", "icons", "cursor", "wallpaper", "effects")
DESIGN_STATES = {
    "applied_and_verified",
    "persisted_but_not_live_verified",
    "attempted_but_failed",
    "unavailable",
    "manual_followup_required",
    "not_requested",
}
LIVE_APPLY_MARKERS = (
    "plasma-apply-",
    "lookandfeeltool",
    "qdbus",
    "qdbus6",
    "kscreen-doctor",
    "xrandr",
    "xfdesktop --reload",
    "nitrogen --set",
    "feh --bg",
    "swww ",
    "hyprpaper",
    "gsettings set org.gnome.desktop.background",
    "gsettings set org.gnome.desktop.interface gtk-theme",
    "gsettings set org.gnome.desktop.interface icon-theme",
    "gsettings set org.gnome.desktop.interface cursor-theme",
)
WRONG_USER_MARKERS = (
    "/root/",
    "HOME=/root",
    "sudo -u root",
    "sudo gsettings",
)


class MemoryRunLog:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, event_type: str, **payload: Any) -> None:
        self.events.append({"event_type": event_type, **payload})


def _load_fixture() -> dict[str, Any]:
    return yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))


FIXTURE = _load_fixture()
SCENARIOS: list[dict[str, Any]] = list(FIXTURE["scenarios"])


def _scenario(scenario_id: str) -> dict[str, Any]:
    for scenario in SCENARIOS:
        if scenario["id"] == scenario_id:
            return scenario
    raise AssertionError(f"missing scenario: {scenario_id}")


def _commands(scenario: dict[str, Any]) -> list[str]:
    return list(scenario["command_group"].get("commands") or [])


def _expected_state(scenario: dict[str, Any], element: str) -> str:
    return str(scenario["expected"]["design_state"][element])


def _is_live_apply_command(command: str) -> bool:
    lowered = command.lower()
    return any(marker in lowered for marker in LIVE_APPLY_MARKERS)


def _is_wrong_user_command(command: str) -> bool:
    lowered = command.lower()
    return any(marker.lower() in lowered for marker in WRONG_USER_MARKERS)


def _command_execution_marker(command: str) -> str:
    lowered = command.lower()
    for marker in (
        "kwriteconfig",
        "gsettings",
        "xfconf-query",
        "dconf",
        "kdeglobals",
        "kcminputrc",
        "kwinrc",
    ):
        if marker in lowered:
            return marker
    parts = command.split(maxsplit=1)
    return parts[0].lower() if parts else lowered


def _command_was_observed(command: str, executed: list[str]) -> bool:
    marker = _command_execution_marker(command)
    return any(marker in executed_command.lower() for executed_command in executed)


def _has_active_gui(scenario: dict[str, Any]) -> bool:
    facts_profile = scenario["facts_profile"]
    return bool(
        facts_profile.get("has_gui_session") is True
        and str(facts_profile.get("display") or "") not in {"", "unknown"}
    )


def _desktop_environment_name(raw: str) -> str:
    match raw:
        case "kde":
            return "KDE"
        case "gnome":
            return "GNOME"
        case "xfce":
            return "XFCE"
        case _:
            return ""


def _display_value(scenario: dict[str, Any]) -> str:
    value = str(scenario["facts_profile"].get("display") or "")
    return "" if value == "unknown" else value


def _facts_for_scenario(
    scenario: dict[str, Any],
    *,
    verified_elements: bool = False,
) -> SetupFacts:
    profile = scenario["facts_profile"]
    active_gui = _has_active_gui(scenario)
    desktop = str(profile.get("desktop") or "unknown")
    runtime = (
        DesktopRuntimeState.LIVE_APPLY_SUCCEEDED
        if active_gui
        else (
            DesktopRuntimeState.PENDING_RELOGIN
            if desktop not in {"none", "unknown"}
            else DesktopRuntimeState.BLOCKED_NO_GUI_SESSION
        )
    )
    design = DesktopDesignState(environment=_desktop_environment_name(desktop))
    if verified_elements:
        for element, state in scenario["expected"]["design_state"].items():
            if state == "applied_and_verified":
                _set_design_value(design, desktop, element, scenario["request"].get(element))
    return SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user=str(profile.get("target_user") or "quickhatch"),
        login_user=str(profile.get("target_user") or "quickhatch"),
        sudo_nopasswd=True,
        display_manager="sddm" if desktop == "kde" else "gdm" if desktop == "gnome" else "lightdm",
        display_manager_enabled=desktop not in {"none", "unknown"},
        display_manager_active=active_gui or desktop not in {"none", "unknown"},
        session_type="wayland" if profile.get("wayland") is True else "x11",
        desktop_runtime_state=runtime,
        displays=[
            DisplayRuntimeInfo(
                name="Virtual-1",
                current_mode="1920x1080",
                refresh_hz=60,
                primary=True,
            )
        ]
        if active_gui
        else [],
        desktop_design=design,
        raw={
            "QH_GUI_USER": str(profile.get("target_user") or "quickhatch"),
            "DISPLAY": _display_value(scenario),
        },
    )


def _set_design_value(
    design: DesktopDesignState,
    desktop: str,
    element: str,
    value: Any,
) -> None:
    text = str(value or "").strip() or "verified"
    if element == "theme":
        if desktop == "kde":
            design.kde_color_scheme = text.replace(" ", "")
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
    elif element == "effects" and desktop == "kde":
        effects = set(value or [])
        design.kde_wobbly_windows = "true" if "wobbly_windows" in effects else ""
        design.kde_magic_lamp = "true" if "magic_lamp" in effects else ""


def _runtime(
    profile: UserProfile,
    bridge: BridgeState,
    facts: SetupFacts,
    *,
    ssh_exec=None,
    probe_facts=None,
) -> SetupRuntime:
    return SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(provider="api_openai", endpoint="http://example", model_name="test"),
        host="127.0.0.1",
        user=facts.current_user or "quickhatch",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=lambda _cfg, _messages: json.dumps(
            {"action": "manual_followup", "reason": "desktop scenario follow-up"}
        ),
        probe_facts=probe_facts or (lambda **_kwargs: facts),
        detect_live_env=lambda: (False, "INSTALLED"),
        ssh_exec=ssh_exec or (lambda _command, _timeout: (0, "", "")),
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


def _recording_ssh_exec(executed: list[str]):
    def ssh_exec(command: str, _timeout: int) -> tuple[int, str, str]:
        executed.append(command)
        return (0, "", "")

    return ssh_exec


def _orchestrator_for_scenario(
    scenario: dict[str, Any],
    *,
    facts: SetupFacts | None = None,
    ssh_exec=None,
) -> tuple[SetupOrchestrator, MemoryRunLog]:
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    scenario_facts = facts or _facts_for_scenario(scenario)
    orchestrator = SetupOrchestrator(_runtime(profile, bridge, scenario_facts, ssh_exec=ssh_exec))
    runlog = MemoryRunLog()
    orchestrator.runlog = runlog  # type: ignore[assignment]
    orchestrator.ctx.facts = scenario_facts
    orchestrator.ctx.plan = _plan_for_scenario(scenario)
    orchestrator._refresh_facts = lambda stage: scenario_facts  # type: ignore[method-assign]
    orchestrator._ask_blocking_question = (  # type: ignore[method-assign]
        lambda *args, **kwargs: "manual follow-up"
    )
    return orchestrator, runlog


def _plan_for_scenario(scenario: dict[str, Any]) -> ResearchPlan:
    requested = ", ".join(
        f"{element}={value}"
        for element, value in scenario["request"].items()
        if value not in ("", [], None)
    )
    return ResearchPlan(
        target_summary=f"Desktop scenario {scenario['id']}: {requested}",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision(strategy="none", confidence="resolved_official"),
        desktop_strategy=StrategyDecision(
            strategy=requested or "desktop defaults",
            confidence="resolved_official",
            notes=requested,
        ),
    )


def _group_for_scenario(scenario: dict[str, Any]) -> CommandGroup:
    return CommandGroup(
        id=scenario["id"],
        stage=SetupStage.DESKTOP,
        title=scenario["command_group"].get("title") or scenario["id"],
        commands=_commands(scenario),
    )


def _manual_followup_reasons_for_scenario(scenario: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for element, state in scenario["expected"]["design_state"].items():
        if state in {
            "attempted_but_failed",
            "unavailable",
            "manual_followup_required",
            "persisted_but_not_live_verified",
        }:
            requested = scenario["request"].get(element)
            label = ", ".join(requested) if isinstance(requested, list) else str(requested or "")
            reasons.append(
                f"{element} unresolved for {scenario['id']}: {state}"
                + (f" ({label})" if label else "")
            )
    return reasons


def test_desktop_session_fixture_schema_and_coverage() -> None:
    assert FIXTURE["merge_summary"]["total_scenarios"] == len(SCENARIOS)
    assert 60 <= len(SCENARIOS) <= 120

    covered_by_element = {element: set() for element in DESIGN_ELEMENTS}
    categories = {scenario["category"] for scenario in SCENARIOS}
    for scenario in SCENARIOS:
        assert set(scenario["expected"]["design_state"]) == set(DESIGN_ELEMENTS)
        assert scenario["command_group"]["stage"] == "desktop"
        for element, state in scenario["expected"]["design_state"].items():
            assert state in DESIGN_STATES
            covered_by_element[element].add(state)

    assert {"kde", "gnome", "xfce", "unknown_desktop", "wrong_user"} <= categories
    for element in DESIGN_ELEMENTS:
        assert "not_requested" in covered_by_element[element]
        assert "manual_followup_required" in covered_by_element[element]
    assert "applied_and_verified" in covered_by_element["theme"]
    assert "persisted_but_not_live_verified" in covered_by_element["theme"]
    assert "attempted_but_failed" in covered_by_element["wallpaper"]
    assert "unavailable" in covered_by_element["icons"]


def test_no_gui_live_only_items_are_deferred_with_specific_evidence() -> None:
    mismatches: list[str] = []
    for scenario in SCENARIOS:
        expected = scenario["expected"]
        if expected["should_execute_live_apply"] is not False:
            continue
        if scenario["facts_profile"].get("has_gui_session") is True:
            continue
        live_commands = [
            command
            for command in _commands(scenario)
            if _is_live_apply_command(command) and not expected["should_allow_persistent_config"]
        ]
        if not live_commands:
            continue

        executed: list[str] = []
        facts = _facts_for_scenario(scenario)

        orchestrator, runlog = _orchestrator_for_scenario(
            scenario,
            facts=facts,
            ssh_exec=_recording_ssh_exec(executed),
        )
        state: dict[str, Any] = {}
        orchestrator.ctx.stage_state[SetupStage.DESKTOP.value] = state

        failure = orchestrator._execute_group(
            SetupStage.DESKTOP, _group_for_scenario(scenario), state
        )

        executed_live = [
            command
            for command in live_commands
            if any(command in executed_command for executed_command in executed)
        ]
        reason = str(state.get("display_manual_followup_reason") or "")
        if failure is not None:
            mismatches.append(f"{scenario['id']}: live-only no-GUI work fatal-blocked: {failure}")
        if executed_live:
            mismatches.append(
                f"{scenario['id']}: executed live-only no-GUI command(s): {executed_live}"
            )
        if not reason or "display" not in reason.lower():
            mismatches.append(
                f"{scenario['id']}: missing specific display/session follow-up reason"
            )
        if live_commands and not any(
            event["event_type"] == "desktop_runtime_item_manual_followup" for event in runlog.events
        ):
            mismatches.append(f"{scenario['id']}: missing runlog evidence for skipped live item")

    assert not mismatches, "desktop live-only routing mismatches:\n" + "\n".join(mismatches)


def test_persistent_config_scenarios_mark_persistence_without_fatal_block() -> None:
    mismatches: list[str] = []
    for scenario in SCENARIOS:
        expected = scenario["expected"]
        if expected["should_allow_persistent_config"] is not True:
            continue
        if not any(
            state == "persisted_but_not_live_verified"
            for state in expected["design_state"].values()
        ):
            continue
        if scenario["facts_profile"].get("has_gui_session") is True:
            continue
        persistent_commands = [
            command
            for command in _commands(scenario)
            if SetupOrchestrator._command_persists_desktop_config(command)
        ]
        if not persistent_commands:
            mismatches.append(
                f"{scenario['id']}: expected persistent config support but no command is recognized as persistent"
            )
            continue

        executed: list[str] = []

        facts = _facts_for_scenario(scenario)
        orchestrator, _runlog = _orchestrator_for_scenario(
            scenario,
            facts=facts,
            ssh_exec=_recording_ssh_exec(executed),
        )
        state: dict[str, Any] = {}
        orchestrator.ctx.stage_state[SetupStage.DESKTOP.value] = state

        failure = orchestrator._execute_group(
            SetupStage.DESKTOP, _group_for_scenario(scenario), state
        )
        audit = audit_stage(
            SetupStage.DESKTOP,
            facts,
            stage_state=state,
            plan=orchestrator.ctx.plan,
        )

        if failure is not None:
            mismatches.append(f"{scenario['id']}: persistent config fatal-blocked: {failure}")
        if not any(_command_was_observed(command, executed) for command in persistent_commands):
            mismatches.append(f"{scenario['id']}: recognized persistent command was not executed")
        if not state.get("persisted_config_written"):
            mismatches.append(f"{scenario['id']}: persistent config did not mark stage state")
        if audit.blockers:
            mismatches.append(
                f"{scenario['id']}: persistent config produced blockers: {audit.blockers}"
            )

    assert not mismatches, "desktop persistent-config mismatches:\n" + "\n".join(mismatches)


def test_design_element_outcomes_are_not_reported_as_plain_success() -> None:
    mismatches: list[str] = []
    for scenario in SCENARIOS:
        unresolved = [
            element
            for element in DESIGN_ELEMENTS
            if _expected_state(scenario, element)
            in {
                "persisted_but_not_live_verified",
                "attempted_but_failed",
                "unavailable",
                "manual_followup_required",
            }
            and scenario["request"].get(element) not in ("", [], None)
        ]
        if not unresolved:
            continue
        effect = scenario["expected"]["final_status_effect"]
        if effect == "none":
            mismatches.append(
                f"{scenario['id']}: unresolved elements {unresolved} allow plain success"
            )
        for element in unresolved:
            state = _expected_state(scenario, element)
            if state == "applied_and_verified":
                mismatches.append(f"{scenario['id']}: {element} is both unresolved and verified")

    assert not mismatches, "desktop final-status honesty mismatches:\n" + "\n".join(mismatches)


def test_wrong_user_desktop_commands_are_not_counted_as_target_user_persistence() -> None:
    mismatches: list[str] = []
    for scenario in SCENARIOS:
        if scenario["category"] != "wrong_user":
            continue
        wrong_user_commands = [
            command for command in _commands(scenario) if _is_wrong_user_command(command)
        ]
        if not wrong_user_commands:
            continue
        facts = _facts_for_scenario(scenario)
        orchestrator, _runlog = _orchestrator_for_scenario(scenario, facts=facts)
        state = orchestrator.ctx.stage_state.setdefault(SetupStage.DESKTOP.value, {})
        for command in wrong_user_commands:
            orchestrator._update_from_command(command, "", "", stage=SetupStage.DESKTOP)
        audit = audit_stage(
            SetupStage.DESKTOP,
            facts,
            stage_state=state,
            plan=orchestrator.ctx.plan,
        )
        final = final_audit(facts, orchestrator.ctx.plan, [audit])

        if state.get("persisted_config_written"):
            mismatches.append(
                f"{scenario['id']}: wrong-user command marked persisted_config_written"
            )
        if final.final_status == FinalSetupStatus.SUCCESS:
            mismatches.append(f"{scenario['id']}: wrong-user customization reported success")

    assert not mismatches, "wrong-user desktop targeting mismatches:\n" + "\n".join(mismatches)


def test_final_audit_maps_desktop_followups_to_non_success_status() -> None:
    mismatches: list[str] = []
    for scenario in SCENARIOS:
        reasons = _manual_followup_reasons_for_scenario(scenario)
        if not reasons:
            continue
        facts = _facts_for_scenario(scenario, verified_elements=True)
        plan = _plan_for_scenario(scenario)
        stage_result = AuditResult(
            stage=SetupStage.DESKTOP,
            passed=True,
            details={
                "desktop_manual_followups": [{"reason": reason} for reason in reasons],
                "desktop_design_manual_followups": reasons,
            },
        )

        result = final_audit(facts, plan, [stage_result])

        if result.final_status == FinalSetupStatus.SUCCESS:
            mismatches.append(f"{scenario['id']}: final audit hid desktop follow-up as success")
        if not any(finding.category == "desktop" for finding in result.findings):
            mismatches.append(f"{scenario['id']}: final audit omitted desktop finding")
        summaries = " ".join(finding.summary for finding in result.findings)
        for element in DESIGN_ELEMENTS:
            if element in " ".join(reasons) and element not in summaries:
                mismatches.append(
                    f"{scenario['id']}: final audit finding omitted element {element}"
                )

    assert not mismatches, "desktop final-audit follow-up mismatches:\n" + "\n".join(mismatches)


def test_facts_refresh_clears_stale_display_reason_but_preserves_design_followup() -> None:
    scenario = _scenario("no-gui-kde-live-wallpaper-skip")
    no_gui = _facts_for_scenario(scenario)
    orchestrator, _runlog = _orchestrator_for_scenario(scenario, facts=no_gui)
    state = {
        "display_manual_followup": True,
        "display_manual_followup_reason": (
            "Skipped display-dependent desktop command because no active target display facts "
            "were available over the real GUI session."
        ),
        "manual_followups": [
            {
                "reason": (
                    "Skipped display-dependent desktop command because no active target "
                    "display facts were available over the real GUI session."
                )
            },
            {"reason": "wallpaper manual follow-up remains unresolved"},
        ],
    }

    refreshed = _facts_for_scenario(
        _scenario("kde-wallpaper-live-verified"), verified_elements=True
    )
    orchestrator.ctx.facts = refreshed
    orchestrator._update_desktop_display_audit_state(state)

    assert "display_manual_followup_reason" not in state
    assert state["manual_followups"] == [
        {"reason": "wallpaper manual follow-up remains unresolved"}
    ]


def test_support_bundle_preserves_desktop_manual_followup_evidence(tmp_path: Path) -> None:
    scenario = _scenario("final-support-evidence-desktop-followup")
    facts = _facts_for_scenario(scenario)
    reasons = _manual_followup_reasons_for_scenario(scenario)
    finding_summary = "; ".join(reasons)
    runlog = tmp_path / "runs" / "desktop-followup.jsonl"
    runlog.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "event_type": "facts_refresh",
            "run_id": "desktop-followup",
            "facts": asdict(facts),
        },
        {
            "event_type": "desktop_runtime_item_manual_followup",
            "run_id": "desktop-followup",
            "stage": "desktop",
            "group_id": scenario["id"],
            "item_kind": "command",
            "command": scenario["command_group"]["commands"][0],
            "reason": reasons[0],
        },
        {
            "event_type": "final_audit",
            "run_id": "desktop-followup",
            "status": "manual_followup_required",
            "result": {
                "passed": True,
                "final_status": "manual_followup_required",
                "findings": [
                    {
                        "severity": AuditFindingSeverity.MANUAL_FOLLOWUP.value,
                        "category": "desktop",
                        "summary": finding_summary,
                    }
                ],
                "details": {
                    "desktop_manual_followups": [{"reason": reason} for reason in reasons],
                    "desktop_design_manual_followups": reasons,
                },
            },
        },
    ]
    runlog.write_text(
        "\n".join(json.dumps(event, default=str) for event in events) + "\n",
        encoding="utf-8",
    )

    result = export_support_bundle(runlog, out=tmp_path / "desktop-support.zip")
    with zipfile.ZipFile(result.path) as zf:
        final_audit_payload = json.loads(zf.read("final-audit.json").decode("utf-8"))
        manual_followups = json.loads(zf.read("manual-followups.json").decode("utf-8"))

    combined_final_details = json.dumps(final_audit_payload["details"])
    assert "theme" in combined_final_details
    assert "icons" in combined_final_details
    assert "cursor" in combined_final_details
    assert "wallpaper" in combined_final_details
    assert any(item["category"] == "desktop" for item in manual_followups)
    assert "wallpaper" in json.dumps(manual_followups)
