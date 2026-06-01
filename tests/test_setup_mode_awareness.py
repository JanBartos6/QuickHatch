from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from quickhatch.bridge.server import BridgeState
from quickhatch.config import AIConfig, UserProfile
from quickhatch.setup.orchestrator import SetupOrchestrator, SetupRuntime
from quickhatch.setup.planner import (
    ResearchPlanValidationError,
    build_research_prompt,
    extract_research_plan,
)
from quickhatch.setup.types import ResearchPlan, SetupFacts, SetupStage

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "mode_awareness_scenarios.yaml"


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


def _profile(mode: dict[str, Any]) -> UserProfile:
    profile = UserProfile()
    profile.remote_setup_mode = str(mode.get("remote_setup_mode") or "configure_only")
    profile.install_profile = str(mode.get("install_profile") or "configure_existing")
    profile.install_partition_notes = str(mode.get("install_partition_notes") or "")
    profile.install_username = "quickhatch"
    return profile


def _facts(mode: dict[str, Any]) -> SetupFacts:
    fresh = str(mode.get("remote_setup_mode") or "") == "wipe_and_reinstall"
    family = str(mode.get("family") or "arch")
    package_manager = str(mode.get("package_manager") or "pacman")
    return SetupFacts(
        distro_family=family,
        os_id=family,
        package_manager=package_manager,
        community_helper="yay" if package_manager == "pacman" else "none",
        current_user="root" if fresh else "quickhatch",
        login_user="root" if fresh else "quickhatch",
        is_root=fresh,
        sudo_nopasswd=True,
        is_live=fresh,
        live_marker="LIVE" if fresh else "INSTALLED",
        session_type="" if fresh else "wayland",
        raw={
            "install_profile": mode.get("install_profile"),
            "remote_setup_mode": mode.get("remote_setup_mode"),
        },
    )


def _base_plan_payload(scenario: dict[str, Any]) -> dict[str, Any]:
    mode = scenario.get("mode") or {}
    return {
        "target_summary": f"Mode awareness scenario {scenario['id']}",
        "distro_family": mode.get("family", "arch"),
        "package_manager": mode.get("package_manager", "pacman"),
        "driver_strategy": {
            "strategy": "mode_aware_driver",
            "confidence": "resolved_official",
            "source": "local_facts",
            "packages": [],
            "services": [],
            "notes": "Driver work follows the selected setup mode.",
        },
        "desktop_strategy": {
            "strategy": "mode_aware_desktop",
            "confidence": "resolved_official",
            "source": "local_facts",
            "packages": [],
            "services": [],
            "notes": "Desktop work follows the selected setup mode.",
        },
        "command_groups": scenario["plan"]["command_groups"],
        "apps": [],
        "unresolved_questions": [],
        "verification_commands": {},
        "required_items": [],
        "optional_items": [],
        "planner_revision": 1,
    }


def _prompt_for(scenario: dict[str, Any]) -> str:
    return build_research_prompt(
        _profile(scenario.get("mode") or {}),
        _facts(scenario.get("mode") or {}),
        system_info="mode awareness target",
        migration_plan_md=scenario.get("rationale", ""),
        distro_primer_md="",
        native_search_active=False,
    )


def _extract_plan(scenario: dict[str, Any]) -> ResearchPlan:
    return extract_research_plan(
        json.dumps(_base_plan_payload(scenario)),
        _profile(scenario.get("mode") or {}),
    )


def _runtime_for(
    profile: UserProfile,
    facts: SetupFacts,
    model_call,
) -> tuple[SetupOrchestrator, MemoryRunLog]:
    bridge = BridgeState(profile, persist_path=None)
    runtime = SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(provider="api_openai", endpoint="http://example", model_name="test"),
        host="127.0.0.1",
        user=facts.current_user or "quickhatch",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=model_call,
        probe_facts=lambda **_kwargs: facts,
        detect_live_env=lambda: (facts.is_live, facts.live_marker),
        ssh_exec=lambda _command, _timeout: (0, "", ""),
        repo_search=lambda _query: "",
        community_search=lambda _query: "",
        web_search=lambda _query, _max_results: "",
        wait_for_reconnect=lambda reconnect_user: (True, "fresh info", reconnect_user),
        run_pre_reboot_checks=lambda: (True, [], "ok"),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda _current, _is_live: "",
        migration_plan_md="mode awareness",
        distro_primer_md="",
        native_search_active=False,
    )
    orchestrator = SetupOrchestrator(runtime)
    runlog = MemoryRunLog()
    orchestrator.runlog = runlog  # type: ignore[assignment]
    orchestrator.ctx.facts = facts
    return orchestrator, runlog


def test_mode_awareness_fixture_covers_goal_boundaries() -> None:
    metadata = FIXTURE["metadata"]
    assert metadata["total_scenarios"] == len(SCENARIOS)
    categories = set(metadata["scenarios_by_category"])
    assert {
        "configure_existing_prompt",
        "fresh_full_disk_prompt",
        "manual_partition_mapping_prompt",
        "postboot_app_setup",
        "driver_repair_prompt",
        "desktop_only_request",
        "planner_correction",
        "wrong_mode_rejection",
    }.issubset(categories)
    assert metadata["expected_behaviors"]["wrong_mode_dangerous_work_rejected"] >= 5
    assert metadata["expected_behaviors"]["model_corrects_after_rejection"] == 2


@pytest.mark.parametrize(
    "scenario",
    [scenario for scenario in SCENARIOS if scenario.get("expected", {}).get("prompt_contains")],
    ids=lambda scenario: scenario["id"],
)
def test_research_prompt_makes_setup_mode_explicit(scenario: dict[str, Any]) -> None:
    prompt = _prompt_for(scenario)

    for needle in scenario["expected"]["prompt_contains"]:
        assert needle in prompt


@pytest.mark.parametrize(
    "scenario",
    [scenario for scenario in SCENARIOS if scenario.get("expected", {}).get("outcome")],
    ids=lambda scenario: scenario["id"],
)
def test_plan_validation_matches_setup_mode(scenario: dict[str, Any]) -> None:
    expected = scenario["expected"]
    if expected["outcome"] == "reject":
        with pytest.raises(ResearchPlanValidationError) as exc_info:
            _extract_plan(scenario)
        assert expected["error_contains"].lower() in str(exc_info.value).lower()
        return

    plan = _extract_plan(scenario)
    assert plan.command_groups


@pytest.mark.parametrize(
    "scenario_id",
    [
        "correct-configure-existing-after-rejection",
        "correct-fresh-full-disk-after-missing-bootloader-rejection",
    ],
)
def test_planner_can_correct_after_mode_rejection(scenario_id: str) -> None:
    scenario = SCENARIO_BY_ID[scenario_id]
    profile = _profile(scenario["mode"])
    facts = _facts(scenario["mode"])
    model_calls: list[list[dict[str, str]]] = []

    if scenario["expected"]["final_stage"] == SetupStage.APPS.value:
        fixed_groups = [
            {
                "id": "apps-vlc",
                "stage": "apps",
                "title": "Install VLC on existing system",
                "commands": ["sudo pacman -S --noconfirm vlc"],
                "verification": ["pacman -Q vlc"],
                "packages": ["vlc"],
                "source": "repo",
            }
        ]
    else:
        fixed_groups = [
            {
                "id": "fresh-install",
                "stage": "install_or_skip",
                "title": "Install base system and bootloader",
                "operations": [
                    {
                        "kind": "mount_partition",
                        "target": "/dev/sda2",
                        "mountpoint": "/mnt",
                    },
                    {"kind": "write_fstab", "target": "/mnt/etc/fstab"},
                    {"kind": "install_bootloader", "target": "/dev/sda"},
                ],
                "commands": [
                    "pacstrap -K /mnt base linux linux-firmware",
                ],
                "packages": ["base", "linux", "linux-firmware"],
                "source": "repo",
            }
        ]

    bad_groups = [
        {
            "id": "wrong-mode",
            "stage": "install_or_skip"
            if scenario["mode"]["remote_setup_mode"] == "configure_only"
            else "apps",
            "title": "Wrong mode work",
            "commands": [scenario["bad_plan_command"]],
            "packages": ["vlc"],
            "source": "repo",
        }
    ]

    def payload(groups: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "target_summary": "Mode correction plan",
            "distro_family": "arch",
            "package_manager": "pacman",
            "driver_strategy": {"strategy": "none"},
            "desktop_strategy": {"strategy": "none"},
            "command_groups": groups,
            "apps": [],
            "unresolved_questions": [],
            "verification_commands": {},
            "required_items": [],
            "optional_items": [],
            "planner_revision": 1,
        }

    def model_call(_cfg: AIConfig, messages: list[dict[str, str]], *_args: object) -> str:
        model_calls.append(messages)
        if len(model_calls) == 1:
            return json.dumps({"action": "plan", "plan": payload(bad_groups)})
        assert "planner validator" in messages[-1]["content"]
        assert (
            scenario["expected"]["first_error_contains"].lower() in messages[-1]["content"].lower()
        )
        return json.dumps({"action": "plan", "plan": payload(fixed_groups)})

    orchestrator, runlog = _runtime_for(profile, facts, model_call)

    orchestrator._run_research_and_plan()

    assert len(model_calls) == 2
    assert orchestrator.ctx.plan is not None
    assert (
        orchestrator.ctx.plan.command_groups[0].stage.value == scenario["expected"]["final_stage"]
    )
    assert any(event["event_type"] == "planner_response_invalid" for event in runlog.events)
