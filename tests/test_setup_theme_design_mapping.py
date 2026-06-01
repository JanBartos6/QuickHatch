from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from quickhatch.bridge.server import BridgeState
from quickhatch.config import AIConfig, Preferences, UserProfile
from quickhatch.setup.audit import audit_stage, final_audit
from quickhatch.setup.orchestrator import SetupOrchestrator, SetupRuntime
from quickhatch.setup.planner import build_research_prompt, extract_research_plan
from quickhatch.setup.types import (
    DesktopDesignState,
    DesktopRuntimeState,
    DisplayRuntimeInfo,
    FinalSetupStatus,
    ResearchPlan,
    SetupFacts,
    SetupStage,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "theme_design_mapping_scenarios.yaml"
LIVE_APPLY_MARKERS = (
    "plasma-apply",
    "qdbus",
    "kscreen-doctor",
    "xrandr",
    "gsettings set org.gnome.desktop.background",
    "xfdesktop --reload",
    "feh --bg",
)


def _load_fixture() -> dict[str, Any]:
    return yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))


FIXTURE = _load_fixture()
SCENARIOS = FIXTURE["scenarios"]


def _scenarios(mode: str) -> list[dict[str, Any]]:
    return [scenario for scenario in SCENARIOS if scenario["mode"] == mode]


def _profile(scenario: dict[str, Any]) -> UserProfile:
    request = scenario["user_request"]
    profile = UserProfile()
    profile.preferences = Preferences(
        desktop_look=str(request.get("desktop_look", "")),
        icon_style=str(request.get("icon_style", "")),
        wallpaper_style=str(request.get("wallpaper_style", "")),
        additional_notes=str(request.get("additional_notes", "")),
    )
    profile.additional_info = str(request.get("additional_notes", ""))
    return profile


def _facts(scenario: dict[str, Any], *, verified: bool = False) -> SetupFacts:
    facts_profile = scenario["facts_profile"]
    desktop = str(facts_profile.get("desktop") or "unknown").lower()
    has_gui = facts_profile.get("has_gui_session") is True
    design = DesktopDesignState(environment=desktop.upper() if desktop != "unknown" else "")
    if verified:
        _fill_verified_design(design, desktop, scenario["expected"].get("required_elements") or [])
    return SetupFacts(
        distro_family="arch" if facts_profile.get("package_manager") == "pacman" else "ubuntu",
        package_manager=str(facts_profile.get("package_manager", "pacman")),
        current_user="quickhatch",
        login_user="quickhatch",
        sudo_nopasswd=True,
        display_manager="sddm" if desktop == "kde" else "gdm" if desktop == "gnome" else "lightdm",
        display_manager_enabled=desktop not in {"none", "unknown"},
        display_manager_active=has_gui or desktop not in {"none", "unknown"},
        desktop_runtime_state=(
            DesktopRuntimeState.LIVE_APPLY_SUCCEEDED
            if has_gui
            else DesktopRuntimeState.PENDING_RELOGIN
        ),
        displays=[DisplayRuntimeInfo(name="Virtual-1", current_mode="1920x1080", primary=True)]
        if has_gui
        else [],
        desktop_design=design,
        raw={
            "DISPLAY": ""
            if str(facts_profile.get("display")) == "unknown"
            else facts_profile.get("display", ""),
            "QH_GUI_USER": "quickhatch",
            "QH_GUI_HOME": "/home/quickhatch",
        },
    )


def _fill_verified_design(design: DesktopDesignState, desktop: str, elements: list[str]) -> None:
    if "theme" in elements:
        if desktop == "kde":
            design.kde_color_scheme = "BreezeDark"
        elif desktop == "gnome":
            design.gnome_gtk_theme = "Adwaita-dark"
        elif desktop == "xfce":
            design.xfce_gtk_theme = "Greybird-dark"
    if "icons" in elements:
        if desktop == "kde":
            design.kde_icon_theme = "Papirus-Dark"
        elif desktop == "gnome":
            design.gnome_icon_theme = "Papirus-Dark"
        elif desktop == "xfce":
            design.xfce_icon_theme = "Papirus-Dark"
    if "cursor" in elements:
        if desktop == "kde":
            design.kde_cursor_theme = "Bibata-Modern-Ice"
        elif desktop == "gnome":
            design.gnome_cursor_theme = "Bibata-Modern-Ice"
        elif desktop == "xfce":
            design.xfce_cursor_theme = "Bibata-Modern-Ice"
    if "wallpaper" in elements:
        if desktop == "kde":
            design.kde_wallpaper = "/home/quickhatch/Pictures/wallpaper.jpg"
        elif desktop == "gnome":
            design.gnome_wallpaper = "file:///home/quickhatch/Pictures/wallpaper.jpg"
        elif desktop == "xfce":
            design.xfce_wallpaper = "/home/quickhatch/Pictures/wallpaper.jpg"
    if "effects" in elements and desktop == "kde":
        design.kde_wobbly_windows = "true"
        design.kde_magic_lamp = "true"


def _strategy_payload(scenario: dict[str, Any]) -> dict[str, Any]:
    strategy = scenario["plan"]["desktop_strategy"]
    return {
        "strategy": strategy["strategy"],
        "confidence": "medium",
        "source": "user_profile",
        "packages": [],
        "services": [],
        "notes": strategy["notes"],
        "evidence": [
            {
                "source_type": "user_profile",
                "summary": "User appearance request mapped to desktop-aware actions.",
            }
        ],
    }


def _plan_payload(scenario: dict[str, Any]) -> dict[str, Any]:
    facts_profile = scenario["facts_profile"]
    groups = []
    for raw_group in scenario["plan"]["command_groups"]:
        groups.append(
            {
                "id": raw_group["id"],
                "stage": "desktop",
                "title": raw_group["title"],
                "commands": list(raw_group["commands"]),
                "verification": list(raw_group.get("verification") or []),
                "rationale": "Apply concrete desktop design state and verify it.",
                "source": "repo",
                "packages": list(raw_group.get("packages") or []),
                "optional": True,
                "status": raw_group.get("status", "pending"),
                "evidence": [
                    {
                        "source_type": "user_profile",
                        "summary": "Desktop design mapping scenario.",
                    }
                ],
            }
        )
    return {
        "target_summary": scenario["plan"]["target_summary"],
        "distro_family": "arch" if facts_profile.get("package_manager") == "pacman" else "ubuntu",
        "package_manager": facts_profile.get("package_manager", "pacman"),
        "driver_strategy": {
            "strategy": "no_driver_change",
            "confidence": "medium",
            "source": "local_facts",
            "packages": [],
            "services": [],
            "notes": "Driver setup is outside theme/design mapping tests.",
            "evidence": [
                {
                    "source_type": "local_facts",
                    "summary": "No driver decision needed.",
                }
            ],
        },
        "desktop_strategy": _strategy_payload(scenario),
        "command_groups": groups,
        "apps": [],
        "unresolved_questions": [],
        "verification_commands": {},
        "required_items": ["desktop_design"],
        "optional_items": [],
        "planner_revision": 1,
    }


def _plan(scenario: dict[str, Any]) -> ResearchPlan:
    return extract_research_plan(json.dumps(_plan_payload(scenario)), _profile(scenario))


def _all_commands(plan: ResearchPlan) -> list[str]:
    return [command for group in plan.command_groups for command in group.commands]


def _all_verification(plan: ResearchPlan) -> list[str]:
    return [command for group in plan.command_groups for command in group.verification]


def _joined(values: list[str]) -> str:
    return "\n".join(values).lower()


def _has_marker(values: list[str], marker: str) -> bool:
    return marker.lower() in _joined(values)


def _stage_state_for_audit(scenario: dict[str, Any], plan: ResearchPlan) -> dict[str, Any]:
    commands = _all_commands(plan)
    stage_state: dict[str, Any] = {
        "persisted_config_written": True,
        "desktop_design_attempted": any(
            marker in _joined(commands)
            for marker in ("kwriteconfig", "gsettings", "xfconf-query", "kdeglobals")
        ),
        "wallpaper_attempted": "wallpaper" in _joined(commands)
        or "picture-uri" in _joined(commands),
    }
    if scenario["category"] == "no_active_gui_session":
        stage_state["manual_followups"] = [
            {
                "reason": (
                    "Desktop design settings were persisted but not live-verified because "
                    "no active GUI session was available."
                )
            }
        ]
    if scenario["category"] == "theme_unavailable":
        stage_state["manual_followups"] = [
            {
                "reason": (
                    "Theme package sweet-mars-kde-theme-git was unavailable; verify or "
                    "choose an alternative theme manually."
                )
            }
        ]
    return stage_state


def _runtime(profile: UserProfile, bridge: BridgeState, facts: SetupFacts) -> SetupRuntime:
    return SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(provider="api_openai", endpoint="http://example", model_name="test"),
        host="127.0.0.1",
        user=facts.current_user or "quickhatch",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=lambda _config, _messages: "{}",
        probe_facts=lambda **_kwargs: facts,
        detect_live_env=lambda: (False, "INSTALLED"),
        ssh_exec=lambda _command, _timeout: (0, "", ""),
        repo_search=lambda _query: "",
        community_search=lambda _query: "",
        web_search=lambda _query, _max_results: "",
        wait_for_reconnect=lambda reconnect_user: (True, "fresh info", reconnect_user),
        run_pre_reboot_checks=lambda: (True, [], ""),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda current, is_live: "",
    )


def test_fixture_merge_report_matches_scenarios() -> None:
    assert FIXTURE["metadata"]["merge_report"]["total_scenarios"] == len(SCENARIOS)


@pytest.mark.parametrize("scenario", _scenarios("prompt_contract"), ids=lambda item: item["id"])
def test_research_prompt_contains_design_mapping_guardrails(scenario: dict[str, Any]) -> None:
    prompt = build_research_prompt(
        _profile(scenario),
        _facts(scenario),
        system_info="system info",
        migration_plan_md="Apply requested desktop appearance.",
        distro_primer_md="Use desktop-environment-specific commands.",
        native_search_active=False,
        target_distro_family="arch",
    )

    for needle in scenario["expected"]["prompt_contains"]:
        assert needle in prompt


@pytest.mark.parametrize("scenario", _scenarios("plan_contract"), ids=lambda item: item["id"])
def test_theme_design_plans_are_desktop_aware_and_verifiable(scenario: dict[str, Any]) -> None:
    plan = _plan(scenario)
    expected = scenario["expected"]
    strategy_text = f"{plan.desktop_strategy.strategy}\n{plan.desktop_strategy.notes}"
    commands = _all_commands(plan)
    verification = _all_verification(plan)

    assert all(group.stage == SetupStage.DESKTOP for group in plan.command_groups)
    assert all(group.verification for group in plan.command_groups)
    for needle in expected.get("desktop_strategy_contains") or []:
        assert str(needle).lower() in strategy_text.lower()
    for element in expected.get("required_elements") or []:
        assert (
            str(element).lower() in f"{plan.target_summary}\n{plan.desktop_strategy.notes}".lower()
        )
    for marker in expected.get("persistent_markers") or []:
        assert _has_marker(commands, marker)
    for marker in expected.get("live_apply_markers") or []:
        assert _has_marker(commands, marker)
    for marker in expected.get("verification_markers") or []:
        assert _has_marker(verification, marker)
    for marker in expected.get("disallowed_markers") or []:
        assert not _has_marker(commands, marker)


@pytest.mark.parametrize(
    "scenario",
    [item for item in _scenarios("plan_contract") if item["category"] == "no_active_gui_session"],
    ids=lambda item: item["id"],
)
def test_no_gui_design_mapping_uses_persistent_config_without_live_apply(
    scenario: dict[str, Any],
) -> None:
    plan = _plan(scenario)
    commands = _all_commands(plan)

    assert any(
        _has_marker(commands, marker) for marker in scenario["expected"]["persistent_markers"]
    )
    assert not any(_has_marker(commands, marker) for marker in LIVE_APPLY_MARKERS)


@pytest.mark.parametrize("scenario", _scenarios("plan_contract"), ids=lambda item: item["id"])
def test_desktop_design_expectations_are_recorded_per_element(scenario: dict[str, Any]) -> None:
    facts = _facts(scenario)
    profile = _profile(scenario)
    bridge = BridgeState(profile, persist_path=None)
    orchestrator = SetupOrchestrator(_runtime(profile, bridge, facts))
    state: dict[str, Any] = {}
    plan = _plan(scenario)

    for command in _all_commands(plan):
        orchestrator._record_desktop_design_expectations(command, state)

    expected_keys = set()
    desktop = str(scenario["facts_profile"]["desktop"])
    for element in scenario["expected"].get("required_elements") or []:
        if desktop == "kde":
            if element == "theme":
                expected_keys.add("expected_kde_color_scheme")
            elif element == "icons":
                expected_keys.add("expected_kde_icon_theme")
            elif element == "cursor":
                expected_keys.add("expected_kde_cursor_theme")
            elif element == "wallpaper" and "wallpaper" in _joined(_all_commands(plan)):
                expected_keys.add("expected_kde_wallpaper")
            elif element == "effects":
                expected_keys.update({"expected_kde_wobbly_windows", "expected_kde_magic_lamp"})
        elif desktop == "gnome":
            if element == "theme":
                expected_keys.add("expected_gnome_gtk_theme")
            elif element == "icons":
                expected_keys.add("expected_gnome_icon_theme")
            elif element == "cursor":
                expected_keys.add("expected_gnome_cursor_theme")
            elif element == "wallpaper":
                expected_keys.add("expected_gnome_wallpaper")
        elif desktop == "xfce":
            if element == "icons":
                expected_keys.add("expected_xfce_icon_theme")
            elif element == "cursor":
                expected_keys.add("expected_xfce_cursor_theme")
            elif element == "wallpaper":
                expected_keys.add("expected_xfce_wallpaper")

    if expected_keys:
        assert expected_keys.intersection(state), state


@pytest.mark.parametrize(
    "scenario",
    [item for item in SCENARIOS if item["expected"].get("final_status_if_unverified")],
    ids=lambda item: item["id"],
)
def test_unverified_design_mapping_is_not_plain_success(scenario: dict[str, Any]) -> None:
    plan = _plan(scenario) if scenario.get("plan") else _plan(_scenarios("plan_contract")[0])
    facts = _facts(scenario, verified=False)
    stage_state = _stage_state_for_audit(scenario, plan)

    desktop_result = audit_stage(
        SetupStage.DESKTOP,
        facts,
        plan=plan,
        stage_state=stage_state,
    )
    result = final_audit(facts, plan, [desktop_result])

    assert result.final_status == FinalSetupStatus(
        scenario["expected"]["final_status_if_unverified"]
    )
    assert result.final_status != FinalSetupStatus.SUCCESS
    if scenario["expected"].get("summary_contains"):
        combined = "\n".join(finding.summary for finding in result.findings)
        assert scenario["expected"]["summary_contains"] in combined


@pytest.mark.parametrize("scenario", _scenarios("plan_contract"), ids=lambda item: item["id"])
def test_verified_design_mapping_can_pass_when_facts_match(scenario: dict[str, Any]) -> None:
    if scenario["category"] in {"no_active_gui_session", "wrong_desktop_environment"}:
        pytest.skip("These scenarios intentionally require follow-up or DE substitution.")
    if (
        "effects" in (scenario["expected"].get("required_elements") or [])
        and scenario["facts_profile"]["desktop"] != "kde"
    ):
        pytest.skip("Current fact model only verifies concrete KDE effect state.")
    plan = _plan(scenario)
    facts = _facts(scenario, verified=True)
    desktop_result = audit_stage(
        SetupStage.DESKTOP,
        facts,
        plan=plan,
        stage_state={"persisted_config_written": True},
    )
    result = final_audit(facts, plan, [desktop_result])

    assert result.final_status in {FinalSetupStatus.SUCCESS, FinalSetupStatus.SUCCESS_WITH_WARNINGS}
