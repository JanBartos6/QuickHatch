from __future__ import annotations

import json

import pytest

from quickhatch.config import UserProfile
from quickhatch.setup.planner import ResearchPlanValidationError, extract_research_plan
from quickhatch.setup.types import PlanItemStatus, SetupStage


def _base_plan_payload() -> dict:
    return {
        "target_summary": "Arch Linux workstation with NVIDIA and KDE.",
        "distro_family": "arch",
        "package_manager": "pacman",
        "driver_strategy": {
            "strategy": "official_nvidia_open",
            "confidence": "resolved_official",
            "source": "official",
            "packages": ["nvidia-open", "nvidia-utils"],
            "services": ["nvidia-persistenced"],
            "notes": "Use official packages.",
        },
        "desktop_strategy": {
            "strategy": "plasma",
            "confidence": "resolved_official",
            "source": "official",
            "packages": ["plasma-desktop", "sddm"],
            "services": ["sddm"],
            "notes": "Use KDE Plasma.",
        },
        "command_groups": [
            {
                "id": "grp-1",
                "stage": "postboot_driver",
                "title": "Install NVIDIA driver",
                "commands": ["sudo pacman -S --noconfirm nvidia-open nvidia-utils"],
                "verification": ["nvidia-smi"],
                "rationale": "Install current official driver stack.",
                "source": "repo",
                "packages": ["nvidia-open", "nvidia-utils"],
            }
        ],
        "apps": [
            {
                "name": "Brave",
                "package_name": "brave-bin",
                "source": "community",
                "status": "pending",
                "required": True,
                "optional_slow": False,
            },
            {
                "name": "ChatGPT",
                "package_name": "chatgpt-desktop-bin",
                "source": "community",
                "status": "pending",
                "required": False,
                "optional_slow": True,
            },
        ],
        "unresolved_questions": [],
        "verification_commands": {"postboot_driver": ["nvidia-smi"]},
        "required_items": ["driver", "desktop"],
        "optional_items": ["chatgpt-desktop-bin"],
        "planner_revision": 1,
    }


def test_extract_research_plan_parses_valid_json():
    profile = UserProfile()
    profile.apps_to_install = ["Brave", "ChatGPT"]
    plan = extract_research_plan(json.dumps(_base_plan_payload()), profile)

    assert plan.distro_family == "arch"
    assert plan.command_groups[0].stage == SetupStage.POSTBOOT_DRIVER
    assert plan.apps[0].name == "Brave"
    assert plan.apps[1].status == PlanItemStatus.OPTIONAL_SLOW_PENDING_CONFIRMATION


def test_extract_research_plan_rejects_incomplete_payload():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload.pop("command_groups")

    with pytest.raises(ResearchPlanValidationError):
        extract_research_plan(json.dumps(payload), profile)


def test_extract_research_plan_rejects_blank_command_group_commands():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload["command_groups"][0]["commands"] = ["", "   "]

    with pytest.raises(ResearchPlanValidationError):
        extract_research_plan(json.dumps(payload), profile)


def test_extract_research_plan_backfills_unresolved_requested_apps_as_manual_followup():
    profile = UserProfile()
    profile.apps_to_install = ["Brave", "LocalSend"]
    payload = _base_plan_payload()
    payload["apps"] = payload["apps"][:1]

    plan = extract_research_plan(json.dumps(payload), profile)
    states = {app.name: app.status for app in plan.apps}

    assert states["Brave"] == PlanItemStatus.PENDING
    assert states["LocalSend"] == PlanItemStatus.MANUAL_FOLLOWUP
