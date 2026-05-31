from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from quickhatch.setup.preflight import preflight_setup_decisions
from quickhatch.setup.types import CommandGroup, PreflightDecisionKind, SetupFacts, SetupStage

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "configure_existing_command_groups.yaml"


@dataclass(frozen=True)
class ConfigureExistingResult:
    case_id: str
    expected: str
    outcome: str
    details: str = ""


def _load_cases() -> list[dict[str, Any]]:
    payload = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    cases = payload.get("cases")
    assert isinstance(cases, list)
    return cases


CONFIGURE_EXISTING_CASES = _load_cases()


def _facts_for_case(case: dict[str, Any]) -> SetupFacts:
    profile = case["facts_profile"]
    family = str(profile["family"])
    package_manager = str(profile["package_manager"])
    has_gui_session = profile["has_gui_session"]
    desktop = str(profile["desktop"])
    aur_helper = str(profile["aur_helper"])
    return SetupFacts(
        distro_family="unknown" if family == "generic" else family,
        package_manager="unknown" if package_manager == "generic" else package_manager,
        community_helper="none" if aur_helper in {"none", "unknown"} else aur_helper,
        init_system="systemd",
        current_user="quickhatch",
        login_user="quickhatch",
        is_root=False,
        sudo_nopasswd=True,
        is_live=False,
        session_type="x11" if has_gui_session is True else "",
        desktop_runtime_state=(
            "live_apply_succeeded" if has_gui_session is True else "blocked_no_gui_session"
        ),
        raw={
            "install_profile": "configure_existing",
            "desktop": desktop,
            "has_gui_session": has_gui_session,
            "flatpak_available": profile["flatpak_available"],
            "snap_available": profile["snap_available"],
        },
    )


def _command_group_for_case(case: dict[str, Any]) -> CommandGroup:
    group = case["command_group"]
    stage = (
        SetupStage(group["stage"])
        if group["stage"] in SetupStage._value2member_map_
        else SetupStage.APPS
    )
    return CommandGroup(
        id=case["id"],
        stage=stage,
        title=str(group["title"]),
        commands=list(group["commands"]),
        rationale=str(case.get("rationale", "")),
        source="configure_existing_fixture",
        optional=case["expected"]["outcome"] in {"allow_with_advisory", "manual_followup"},
    )


def _block_decisions(command_group: CommandGroup, facts: SetupFacts) -> list[str]:
    blocks: list[str] = []
    for command in command_group.commands:
        decisions = preflight_setup_decisions(command, facts)
        for decision in decisions:
            if decision.kind == PreflightDecisionKind.BLOCK:
                blocks.append(f"{command} :: {decision.category}: {decision.reason}")
    return blocks


def _evaluate_case(case: dict[str, Any]) -> ConfigureExistingResult:
    expected = str(case["expected"]["outcome"])
    facts = _facts_for_case(case)
    group = _command_group_for_case(case)
    blocks = _block_decisions(group, facts)

    if expected in {"allow", "allow_with_advisory"}:
        if blocks:
            return ConfigureExistingResult(case["id"], expected, "false_block", "\n".join(blocks))
        return ConfigureExistingResult(case["id"], expected, "pass")
    if expected == "manual_followup":
        if blocks:
            return ConfigureExistingResult(
                case["id"],
                expected,
                "wrong_manual_followup_fatal",
                "\n".join(blocks),
            )
        return ConfigureExistingResult(case["id"], expected, "pass")
    if expected == "block":
        if not blocks:
            return ConfigureExistingResult(
                case["id"],
                expected,
                "unsafe_allow",
                "no hard block from configure-existing preflight",
            )
        return ConfigureExistingResult(case["id"], expected, "pass")
    raise AssertionError(f"Unhandled configure-existing outcome: {expected}")


def _format_results(results: list[ConfigureExistingResult]) -> str:
    failures = [result for result in results if result.outcome != "pass"]
    false_blocks = [result for result in failures if result.outcome == "false_block"]
    unsafe_allows = [result for result in failures if result.outcome == "unsafe_allow"]
    manual_followup_errors = [
        result for result in failures if result.outcome == "wrong_manual_followup_fatal"
    ]
    lines = [
        "configure-existing command group mismatches",
        f"cases loaded: {len(results)}",
        f"passing cases: {len(results) - len(failures)}",
        f"failing cases: {len(failures)}",
        f"false blocks: {len(false_blocks)}",
        f"unsafe allows: {len(unsafe_allows)}",
        f"wrong manual-followup/fatal classification: {len(manual_followup_errors)}",
        "missing test hooks: 0 (preflight decisions support block/advisory classification)",
    ]
    for title, bucket in (
        ("false blocks", false_blocks),
        ("unsafe allows", unsafe_allows),
        ("wrong manual-followup/fatal classification", manual_followup_errors),
    ):
        if not bucket:
            continue
        lines.append(f"\n{title}:")
        for result in bucket:
            lines.append(f"- {result.case_id} ({result.expected})")
            if result.details:
                lines.append(f"  {result.details}")
    return "\n".join(lines)


def test_configure_existing_command_group_fixture_matches_current_preflight() -> None:
    results = [_evaluate_case(case) for case in CONFIGURE_EXISTING_CASES]
    failures = [result for result in results if result.outcome != "pass"]

    assert not failures, _format_results(results)


def test_configure_existing_blocks_fresh_install_leakage_boundaries() -> None:
    cases = [
        case
        for case in CONFIGURE_EXISTING_CASES
        if case["category"]
        in {"forbidden_install_leakage", "package_db", "network_ssh", "model_repair"}
        and case["expected"]["outcome"] == "block"
    ]
    assert cases
    results = [_evaluate_case(case) for case in cases]
    failures = [result for result in results if result.outcome != "pass"]

    assert not failures, _format_results(results)


def test_configure_existing_safe_app_desktop_diagnostics_do_not_hard_block() -> None:
    cases = [
        case
        for case in CONFIGURE_EXISTING_CASES
        if case["category"] in {"apps", "desktop", "diagnostics", "driver", "safe_service"}
        and case["expected"]["outcome"] in {"allow", "allow_with_advisory", "manual_followup"}
    ]
    assert cases
    results = [_evaluate_case(case) for case in cases]
    failures = [result for result in results if result.outcome != "pass"]

    assert not failures, _format_results(results)


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(case, id=case["id"])
        for case in CONFIGURE_EXISTING_CASES
        if case["facts_profile"]["has_gui_session"] is False
        and case["expected"]["outcome"] != "block"
    ],
)
def test_no_gui_session_cases_defer_without_fatal_block(case: dict[str, Any]) -> None:
    result = _evaluate_case(case)

    assert result.outcome == "pass", _format_results([result])


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(case, id=case["id"])
        for case in CONFIGURE_EXISTING_CASES
        if case["expected"]["final_status_effect"]
        in {"success_with_warnings", "manual_followup_required"}
        and case["expected"]["outcome"] != "block"
    ],
)
def test_optional_app_theme_driver_followup_does_not_derail_setup(case: dict[str, Any]) -> None:
    result = _evaluate_case(case)

    assert result.outcome == "pass", _format_results([result])
