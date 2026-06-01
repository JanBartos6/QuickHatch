from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from quickhatch.config import UserProfile
from quickhatch.setup.operations import parse_setup_operation
from quickhatch.setup.planner import (
    RepairActionValidationError,
    ResearchPlanValidationError,
    build_repair_prompt,
    build_research_prompt,
    extract_repair_action,
    extract_research_plan,
    model_json_loads,
)
from quickhatch.setup.types import CommandGroup, PlanItemStatus, SetupFacts, SetupStage

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "model_contract_scenarios.yaml"


def _load_fixture() -> dict[str, Any]:
    return yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))


FIXTURE = _load_fixture()
CASES = FIXTURE["cases"]


def _base_plan_payload() -> dict[str, Any]:
    return {
        "target_summary": "Arch Linux workstation with official apps.",
        "distro_family": "arch",
        "package_manager": "pacman",
        "driver_strategy": {
            "strategy": "mesa",
            "confidence": "medium",
            "source": "repo",
            "packages": ["mesa"],
            "services": [],
            "notes": "Use distro Mesa unless refreshed facts prove NVIDIA-specific work.",
            "evidence": [
                {
                    "source_type": "local_facts",
                    "summary": "No discrete NVIDIA device was proven in normalized facts.",
                    "confidence": "medium",
                }
            ],
        },
        "desktop_strategy": {
            "strategy": "default_desktop",
            "confidence": "medium",
            "source": "user_profile",
            "packages": [],
            "services": [],
            "notes": "Keep the current desktop unless the user requested a change.",
            "evidence": [
                {
                    "source_type": "user_profile",
                    "summary": "No custom desktop design was requested.",
                    "confidence": "medium",
                }
            ],
        },
        "command_groups": [
            {
                "id": "apps-vlc",
                "stage": "apps",
                "title": "Install VLC",
                "commands": ["sudo pacman -S --noconfirm vlc"],
                "verification": ["pacman -Q vlc"],
                "rationale": "Install requested media player from the official repository.",
                "source": "repo",
                "packages": ["vlc"],
                "optional": False,
                "optional_slow": False,
                "evidence": [
                    {
                        "source_type": "repo_search",
                        "query": "pacman -Ss vlc",
                        "summary": "VLC is available in the official repository.",
                        "confidence": "high",
                    }
                ],
            }
        ],
        "apps": [
            {
                "name": "VLC",
                "package_name": "vlc",
                "source": "repo",
                "status": "pending",
                "required": True,
                "optional_slow": False,
                "reason": "Package name verified through repo search.",
                "evidence": [
                    {
                        "source_type": "repo_search",
                        "query": "pacman -Ss vlc",
                        "summary": "VLC is available in the official repository.",
                        "confidence": "high",
                    }
                ],
            }
        ],
        "unresolved_questions": [],
        "verification_commands": {"apps": ["pacman -Q vlc"]},
        "required_items": ["VLC"],
        "optional_items": [],
        "planner_revision": 1,
    }


def _deep_merge(target: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value
    return target


def _remove_path(payload: dict[str, Any], dotted_path: str) -> None:
    current: Any = payload
    parts = dotted_path.split(".")
    for part in parts[:-1]:
        current = current[int(part)] if isinstance(current, list) else current[part]
    if isinstance(current, list):
        current.pop(int(parts[-1]))
    else:
        current.pop(parts[-1], None)


def _profile_for(input_spec: dict[str, Any]) -> UserProfile:
    profile = UserProfile()
    profile.apps_to_install = list(input_spec.get("profile_apps") or ["VLC"])
    return profile


def _wrap_json(raw: str, wrap: str) -> str:
    if wrap == "fenced_json":
        return f"```json\n{raw}\n```"
    if wrap == "prose":
        return f"Here is the structured output:\n{raw}\nDone."
    return raw


def _research_plan_text(input_spec: dict[str, Any]) -> str:
    if "raw_text" in input_spec:
        return str(input_spec["raw_text"])
    payload = _base_plan_payload()
    for dotted in input_spec.get("remove", []) or []:
        _remove_path(payload, dotted)
    if input_spec.get("patch"):
        _deep_merge(payload, copy.deepcopy(input_spec["patch"]))
    if input_spec.get("append_command_groups"):
        payload.setdefault("command_groups", []).extend(
            copy.deepcopy(input_spec["append_command_groups"])
        )
    return _wrap_json(json.dumps(payload), str(input_spec.get("wrap", "")))


def _json_text(input_spec: dict[str, Any]) -> str:
    if "raw_text" in input_spec:
        return str(input_spec["raw_text"])
    return _wrap_json(
        json.dumps(input_spec.get("raw_payload", input_spec.get("payload", {}))),
        str(input_spec.get("wrap", "")),
    )


def _stage(value: str) -> SetupStage:
    return SetupStage(str(value or "apps"))


def _failed_group(input_spec: dict[str, Any]) -> CommandGroup:
    fallback_stage = _stage(str(input_spec.get("fallback_stage", "apps")))
    raw = input_spec.get("failed_group") or {}
    return CommandGroup(
        id=str(raw.get("id", "failed-group")),
        stage=_stage(str(raw.get("stage", fallback_stage.value))),
        title=str(raw.get("title", "Failed group")),
        commands=list(raw.get("commands", ["false"])),
        packages=list(raw.get("packages", ["vlc"])),
        optional=bool(raw.get("optional", False)),
    )


def _repair_action(input_spec: dict[str, Any]) -> dict[str, Any]:
    text = _json_text(input_spec)
    fallback_stage = _stage(str(input_spec.get("fallback_stage", "apps")))
    return extract_repair_action(
        text,
        fallback_stage=fallback_stage,
        failed_group=_failed_group(input_spec),
    )


def _prompt_text(input_spec: dict[str, Any]) -> str:
    facts = SetupFacts(distro_family="arch", package_manager="pacman")
    profile = UserProfile()
    profile.apps_to_install = ["VLC"]
    if input_spec.get("prompt") == "repair":
        stage = _stage(str(input_spec.get("fallback_stage", "apps")))
        return build_repair_prompt(
            stage=stage,
            failed_group=CommandGroup(
                id="apps-vlc",
                stage=stage,
                title="Install VLC",
                commands=["sudo pacman -S --noconfirm vlc"],
                packages=["vlc"],
            ),
            failure_type="command",
            failed_command="sudo pacman -S --noconfirm vlc",
            stdout_tail="",
            stderr_tail="target not found: vlc-old-name",
            facts=facts,
            recent_results=[],
            native_search_active=False,
        )
    return build_research_prompt(
        profile,
        facts,
        system_info="Linux quickhatch 6.9.0",
        migration_plan_md="Install VLC and keep dangerous operations gated.",
        distro_primer_md="Use official repositories unless facts prove otherwise.",
        native_search_active=False,
    )


def _assert_mapping_paths(result: dict[str, Any], expected: dict[str, Any]) -> None:
    for key, value in (expected.get("path_equals") or {}).items():
        assert result.get(key) == value


def _assert_plan(plan: Any, expected: dict[str, Any]) -> None:
    expected_plan = expected.get("plan") or {}
    if "distro_family" in expected_plan:
        assert plan.distro_family == expected_plan["distro_family"]
    if "package_manager" in expected_plan:
        assert plan.package_manager == expected_plan["package_manager"]
    if "command_group_count" in expected_plan:
        assert len(plan.command_groups) == expected_plan["command_group_count"]
    if "first_group_stage" in expected_plan:
        assert plan.command_groups[0].stage.value == expected_plan["first_group_stage"]
    if "first_group_rationale" in expected_plan:
        assert plan.command_groups[0].rationale == expected_plan["first_group_rationale"]
    if "first_group_verification" in expected_plan:
        assert plan.command_groups[0].verification == expected_plan["first_group_verification"]
    if "first_group_optional_slow" in expected_plan:
        assert plan.command_groups[0].optional_slow is expected_plan["first_group_optional_slow"]
    if "first_group_operation_kinds" in expected_plan:
        assert [operation.kind for operation in plan.command_groups[0].operations] == expected_plan[
            "first_group_operation_kinds"
        ]
    if "driver_evidence_source" in expected_plan:
        assert (
            plan.driver_strategy.evidence[0].source_type == expected_plan["driver_evidence_source"]
        )
    if "unresolved_question_count" in expected_plan:
        assert len(plan.unresolved_questions) == expected_plan["unresolved_question_count"]

    apps = {app.name: app for app in plan.apps}
    for app_name, app_expected in (expected.get("apps") or {}).items():
        assert app_name in apps
        app = apps[app_name]
        if "status" in app_expected:
            assert app.status.value == app_expected["status"]
        if "source" in app_expected:
            assert app.source == app_expected["source"]
        if "reason_contains" in app_expected:
            assert app_expected["reason_contains"] in app.reason


def _assert_operation(operation: Any, expected: dict[str, Any]) -> None:
    expected_operation = expected.get("operation") or {}
    for key, value in expected_operation.items():
        assert getattr(operation, key) == value


def _assert_repair(action: dict[str, Any], expected: dict[str, Any]) -> None:
    if expected.get("repair_action"):
        assert action["action"] == expected["repair_action"]
    _assert_mapping_paths(action, expected)
    if expected.get("manual_app_names") is not None:
        assert action.get("app_names") == expected["manual_app_names"]
        assert action.get("reason")
    if expected.get("plan", {}).get("first_group_stage"):
        assert action["group"].stage.value == expected["plan"]["first_group_stage"]


def _assert_prompt(prompt: str, expected: dict[str, Any]) -> None:
    for needle in expected.get("prompt_contains") or []:
        assert needle in prompt
    for needle in expected.get("prompt_not_contains") or []:
        assert needle not in prompt


def _run_case(case: dict[str, Any]) -> Any:
    parser = case["parser"]
    input_spec = case.get("input") or {}
    if parser == "model_json_loads":
        return model_json_loads(_json_text(input_spec))
    if parser == "extract_research_plan":
        return extract_research_plan(_research_plan_text(input_spec), _profile_for(input_spec))
    if parser == "extract_repair_action":
        return _repair_action(input_spec)
    if parser == "parse_setup_operation":
        return parse_setup_operation(input_spec.get("payload"))
    if parser == "prompt_text":
        return _prompt_text(input_spec)
    raise AssertionError(f"Unsupported parser in fixture: {parser}")


def _expected_exception(parser: str) -> tuple[type[BaseException], ...]:
    if parser == "model_json_loads":
        return (json.JSONDecodeError, ResearchPlanValidationError)
    if parser == "extract_research_plan":
        return (ResearchPlanValidationError,)
    if parser == "extract_repair_action":
        return (RepairActionValidationError,)
    if parser == "parse_setup_operation":
        return (ValueError,)
    return (AssertionError,)


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_model_contract_fixture_cases(case: dict[str, Any]) -> None:
    expected = case["expected"]
    outcome = expected["outcome"]
    if outcome == "reject":
        with pytest.raises(_expected_exception(case["parser"])) as exc_info:
            _run_case(case)
        error_contains = expected.get("error_contains")
        if error_contains:
            assert error_contains.lower() in str(exc_info.value).lower()
        return

    result = _run_case(case)
    if case["parser"] == "model_json_loads":
        _assert_mapping_paths(result, expected)
    elif case["parser"] == "extract_research_plan":
        _assert_plan(result, expected)
    elif case["parser"] == "extract_repair_action":
        _assert_repair(result, expected)
    elif case["parser"] == "parse_setup_operation":
        _assert_operation(result, expected)
    elif case["parser"] == "prompt_text":
        _assert_prompt(result, expected)


def test_model_contract_fixture_has_expected_coverage() -> None:
    categories = {case["category"] for case in CASES}
    parsers = {case["parser"] for case in CASES}

    assert {
        "json_parseability",
        "plan_schema",
        "command_group_contract",
        "dangerous_operation_shape",
        "repair_contract",
        "manual_followup",
        "uncertainty",
        "evidence_contract",
        "prompt_contract",
    } <= categories
    assert {
        "model_json_loads",
        "extract_research_plan",
        "extract_repair_action",
        "parse_setup_operation",
        "prompt_text",
    } <= parsers
    assert len(CASES) >= 50


def test_prompt_contracts_remain_aligned_with_fixture_policy() -> None:
    policy = FIXTURE["metadata"]["policy"]
    research_prompt = _prompt_text({"prompt": "research"})
    repair_prompt = _prompt_text({"prompt": "repair", "fallback_stage": "apps"})

    assert "parseable" in policy
    assert "typed operations" in policy
    assert "Return JSON only" in research_prompt
    assert "repairing ONE failed remote-setup command group" in repair_prompt
    assert "manual_followup" in research_prompt
    assert "Manual follow-up is the last resort" in repair_prompt
    assert PlanItemStatus.MANUAL_FOLLOWUP.value == "manual_followup"
