from __future__ import annotations

import json

import pytest

from quickhatch.config import AppInfo, UserProfile
from quickhatch.setup.planner import (
    RepairActionValidationError,
    ResearchPlanValidationError,
    build_research_prompt,
    extract_repair_action,
    extract_research_plan,
)
from quickhatch.setup.types import CommandGroup, PlanItemStatus, SetupFacts, SetupStage


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


def test_extract_research_plan_accepts_common_status_aliases():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload["apps"][0]["status"] = "uninstalled"
    payload["apps"][1]["status"] = "manual follow up"
    payload["apps"][1]["optional_slow"] = False

    plan = extract_research_plan(json.dumps(payload), profile)

    assert plan.apps[0].status == PlanItemStatus.PENDING
    assert plan.apps[1].status == PlanItemStatus.MANUAL_FOLLOWUP


def test_extract_research_plan_accepts_ready_status_alias():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload["apps"][0]["status"] = "ready"

    plan = extract_research_plan(json.dumps(payload), profile)

    assert plan.apps[0].status == PlanItemStatus.PENDING


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


def test_extract_research_plan_rejects_one_letter_group_verification():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload["command_groups"][0]["verification"] = ["a"]

    with pytest.raises(ResearchPlanValidationError, match="not a real shell command"):
        extract_research_plan(json.dumps(payload), profile)


def test_extract_research_plan_accepts_single_string_group_verification():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload["command_groups"][0]["verification"] = "nvidia-smi"

    plan = extract_research_plan(json.dumps(payload), profile)

    assert plan.command_groups[0].verification == ["nvidia-smi"]


def test_extract_research_plan_rejects_prose_group_verification():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload["command_groups"][0]["verification"] = [
        "lsblk /dev/sda should show sda1 as EFI and sda2 mounted at /mnt."
    ]

    with pytest.raises(ResearchPlanValidationError, match="not a real shell command"):
        extract_research_plan(json.dumps(payload), profile)


def test_extract_research_plan_rejects_one_letter_stage_verification():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload["verification_commands"] = {"postboot_driver": ["a"]}

    with pytest.raises(ResearchPlanValidationError, match="not a real shell command"):
        extract_research_plan(json.dumps(payload), profile)


def test_extract_research_plan_accepts_single_string_stage_verification():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload["verification_commands"] = {"postboot_driver": "nvidia-smi"}

    plan = extract_research_plan(json.dumps(payload), profile)

    assert plan.verification_commands["postboot_driver"] == ["nvidia-smi"]


def test_extract_repair_action_rejects_stage_drift():
    payload = {
        "action": "retry_group",
        "group": {
            "id": "apps-repair",
            "stage": "desktop",
            "title": "Wrong stage repair",
            "commands": ["echo wrong"],
        },
    }

    with pytest.raises(RepairActionValidationError, match="stage must remain apps"):
        extract_repair_action(
            json.dumps(payload),
            fallback_stage=SetupStage.APPS,
            failed_group=CommandGroup(
                id="apps",
                stage=SetupStage.APPS,
                title="Install app",
                commands=["false"],
            ),
        )


def test_extract_research_plan_normalizes_common_install_substages():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload["command_groups"] = [
        {
            "id": "disk",
            "stage": "disk_prepare",
            "title": "Prepare disk",
            "commands": ["parted -s /dev/sda print"],
        },
        {
            "id": "base",
            "stage": "base_install",
            "title": "Install base system",
            "commands": ["pacstrap -K /mnt base linux"],
        },
        {
            "id": "boot",
            "stage": "bootloader",
            "title": "Install bootloader",
            "commands": ["arch-chroot /mnt grub-install --target=x86_64-efi"],
        },
    ]

    plan = extract_research_plan(json.dumps(payload), profile)

    assert {group.stage for group in plan.command_groups} == {SetupStage.INSTALL_OR_SKIP}


def test_extract_research_plan_backfills_unresolved_requested_apps_as_manual_followup():
    profile = UserProfile()
    profile.apps_to_install = ["Brave", "LocalSend"]
    payload = _base_plan_payload()
    payload["apps"] = payload["apps"][:1]

    plan = extract_research_plan(json.dumps(payload), profile)
    states = {app.name: app.status for app in plan.apps}

    assert states["Brave"] == PlanItemStatus.PENDING
    assert states["LocalSend"] == PlanItemStatus.MANUAL_FOLLOWUP


def test_build_research_prompt_includes_selected_app_catalog():
    profile = UserProfile()
    profile.apps_to_install = ["GitHub Desktop"]
    profile.apps = [AppInfo(name="GitHub Desktop", source="winget")]

    prompt = build_research_prompt(
        profile,
        SetupFacts(distro_family="arch", package_manager="pacman"),
        system_info="Linux target",
        migration_plan_md="Install selected apps.",
        distro_primer_md="Arch primer",
        native_search_active=False,
    )

    assert "selected_app_catalog" in prompt
    assert "GitHub Desktop" in prompt
    assert "catalog_id" in prompt
    assert "source_hint" in prompt
    assert "Use selected_app_catalog as curated prior knowledge" in prompt
    assert "active_install_hint" in prompt
    assert "Do not invent stages such as disk_prepare" in prompt
    assert "Prefer `yay` for AUR packages when no helper is detected" in prompt


def test_build_research_prompt_uses_target_distro_over_live_facts(monkeypatch):
    from quickhatch.analysis import app_catalog

    profile = UserProfile()
    profile.apps_to_install = ["Hinted App"]
    entry = app_catalog.AppCatalogEntry(
        id="override:hinted",
        name="Hinted App",
        compatibility="native",
        alternatives=("Hinted App",),
        source_hint="repo",
        confidence="high",
        install_hints={
            "arch": {"source": "repo", "packages": ["arch-pkg"], "notes": ""},
            "ubuntu": {"source": "repo", "packages": ["ubuntu-pkg"], "notes": ""},
        },
    )
    monkeypatch.setattr(app_catalog, "load_catalog", lambda: (entry,))

    prompt = build_research_prompt(
        profile,
        SetupFacts(distro_family="arch", package_manager="pacman", is_live=True),
        system_info="Arch live ISO",
        migration_plan_md="Install Ubuntu.",
        distro_primer_md="Ubuntu primer",
        native_search_active=False,
        target_distro_family="Ubuntu LTS",
    )

    assert "ubuntu-pkg" in prompt
    assert "arch-pkg" not in prompt


def test_build_research_prompt_includes_all_hints_when_distro_unknown(monkeypatch):
    from quickhatch.analysis import app_catalog

    profile = UserProfile()
    profile.apps_to_install = ["Hinted App"]
    entry = app_catalog.AppCatalogEntry(
        id="override:hinted",
        name="Hinted App",
        compatibility="native",
        alternatives=("Hinted App",),
        source_hint="repo",
        confidence="high",
        install_hints={
            "arch": {"source": "repo", "packages": ["arch-pkg"], "notes": ""},
            "fedora": {"source": "repo", "packages": ["fedora-pkg"], "notes": ""},
        },
    )
    monkeypatch.setattr(app_catalog, "load_catalog", lambda: (entry,))

    prompt = build_research_prompt(
        profile,
        SetupFacts(distro_family="unknown", package_manager="unknown"),
        system_info="unknown target",
        migration_plan_md="Install selected apps.",
        distro_primer_md="",
        native_search_active=False,
    )

    assert "install_hints" in prompt
    assert "arch-pkg" in prompt
    assert "fedora-pkg" in prompt
