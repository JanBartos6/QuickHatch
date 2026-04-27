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
            "evidence": [
                {
                    "source_type": "repo_search",
                    "query": "pacman -Ss nvidia-open",
                    "summary": "Arch repo exposes nvidia-open and nvidia-utils.",
                    "confidence": "high",
                }
            ],
        },
        "desktop_strategy": {
            "strategy": "plasma",
            "confidence": "resolved_official",
            "source": "official",
            "packages": ["plasma-desktop", "sddm"],
            "services": ["sddm"],
            "notes": "Use KDE Plasma.",
            "evidence": [
                {
                    "source_type": "repo_search",
                    "query": "pacman -Ss plasma-desktop sddm",
                    "summary": "Arch repo exposes Plasma and SDDM packages.",
                    "confidence": "high",
                }
            ],
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
                "evidence": [
                    {
                        "source_type": "repo_search",
                        "query": "pacman -Ss nvidia-open",
                        "summary": "nvidia-open is available in official repositories.",
                        "confidence": "high",
                    }
                ],
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
                "evidence": [
                    {
                        "source_type": "community_search",
                        "query": "brave-bin",
                        "summary": "Brave is packaged through the Arch user repository.",
                        "confidence": "high",
                    }
                ],
            },
            {
                "name": "ChatGPT",
                "package_name": "chatgpt-desktop-bin",
                "source": "community",
                "status": "pending",
                "required": False,
                "optional_slow": True,
                "evidence": [
                    {
                        "source_type": "community_search",
                        "query": "chatgpt desktop arch",
                        "summary": "Desktop wrapper is community packaged and slow/optional.",
                        "confidence": "medium",
                    }
                ],
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
    assert plan.command_groups[0].evidence[0].source_type == "repo_search"
    assert plan.driver_strategy.evidence[0].confidence == "high"
    assert plan.apps[0].name == "Brave"
    assert plan.apps[0].evidence[0].source_type == "community_search"
    assert plan.apps[1].status == PlanItemStatus.OPTIONAL_SLOW_PENDING_CONFIRMATION


def test_extract_research_plan_accepts_single_evidence_objects():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload["driver_strategy"]["evidence"] = {
        "source_type": "provider_native_search",
        "query": "Arch NVIDIA driver packages",
        "summary": "Use the current official NVIDIA open module package.",
        "confidence": "medium",
    }
    payload["command_groups"][0]["evidence"] = {
        "source_type": "repo_search",
        "query": "nvidia-open",
        "summary": "Package is available in the official repository.",
        "confidence": "high",
    }

    plan = extract_research_plan(json.dumps(payload), profile)

    assert plan.driver_strategy.evidence[0].source_type == "provider_native_search"
    assert plan.command_groups[0].evidence[0].source_type == "repo_search"


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


def test_extract_research_plan_rejects_malformed_group_evidence():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload["command_groups"][0]["evidence"] = [{"source_type": "repo_search"}]

    with pytest.raises(ResearchPlanValidationError, match="needs source_type and summary"):
        extract_research_plan(json.dumps(payload), profile)


def test_extract_research_plan_accepts_single_string_group_verification():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload["command_groups"][0]["verification"] = "nvidia-smi"

    plan = extract_research_plan(json.dumps(payload), profile)

    assert plan.command_groups[0].verification == ["nvidia-smi"]


def test_extract_research_plan_accepts_object_group_verification():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload["command_groups"][0]["verification"] = {
        "commands": ["nvidia-smi", {"command": "lsmod | grep nvidia"}]
    }

    plan = extract_research_plan(json.dumps(payload), profile)

    assert plan.command_groups[0].verification == ["nvidia-smi", "lsmod | grep nvidia"]


def test_research_prompt_requires_split_desktop_live_apply_groups():
    profile = UserProfile()
    facts = SetupFacts(package_manager="pacman", distro_family="arch")

    prompt = build_research_prompt(
        profile,
        facts,
        system_info="",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=False,
    )

    assert "Split desktop work into separate groups" in prompt
    assert "plasma-apply-*" in prompt
    assert "harness may skip live-apply groups" in prompt


def test_extract_research_plan_normalizes_lsblk_mountpoint_verification():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload["command_groups"][0]["verification"] = "lsblk /mnt"

    plan = extract_research_plan(json.dumps(payload), profile)

    assert plan.command_groups[0].verification == ["mountpoint -q /mnt"]


def test_extract_research_plan_normalizes_brittle_fstab_root_verification():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload["command_groups"][0]["verification"] = "grep -q ' / ' /mnt/etc/fstab"

    plan = extract_research_plan(json.dumps(payload), profile)

    assert plan.command_groups[0].verification == [
        "awk '$2 == \"/\" { found=1 } END { exit found ? 0 : 1 }' /mnt/etc/fstab"
    ]


def test_required_command_group_ignores_optional_slow_flag():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload["command_groups"][0]["optional"] = False
    payload["command_groups"][0]["optional_slow"] = True

    plan = extract_research_plan(json.dumps(payload), profile)

    assert plan.command_groups[0].optional is False
    assert plan.command_groups[0].optional_slow is False


def test_extract_research_plan_rejects_prose_group_verification():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload["command_groups"][0]["verification"] = [
        "lsblk /dev/sda should show sda1 as EFI and sda2 mounted at /mnt."
    ]

    with pytest.raises(ResearchPlanValidationError, match="not a real shell command"):
        extract_research_plan(json.dumps(payload), profile)


def test_extract_research_plan_rejects_heredoc_command_fragments():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload["command_groups"][0]["commands"] = [
        "arch-chroot /mnt /bin/bash <<'EOF_CHROOT'",
        "locale-gen",
        "EOF_CHROOT",
    ]

    with pytest.raises(ResearchPlanValidationError, match="heredoc syntax"):
        extract_research_plan(json.dumps(payload), profile)


def test_extract_repair_action_rejects_heredoc_retry_group():
    failed_group = CommandGroup(
        id="chroot",
        stage=SetupStage.INSTALL_OR_SKIP,
        title="Configure chroot",
        commands=["true"],
    )
    payload = {
        "action": "retry_group",
        "group": {
            "id": "chroot",
            "stage": "install_or_skip",
            "title": "Configure chroot",
            "commands": ["arch-chroot /mnt sh <<EOF", "EOF"],
        },
    }

    with pytest.raises(RepairActionValidationError, match="heredoc syntax"):
        extract_repair_action(
            json.dumps(payload),
            fallback_stage=SetupStage.INSTALL_OR_SKIP,
            failed_group=failed_group,
        )


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


def test_extract_research_plan_accepts_object_stage_verification():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload["verification_commands"] = {"postboot_driver": {"checks": [{"command": "nvidia-smi"}]}}

    plan = extract_research_plan(json.dumps(payload), profile)

    assert plan.verification_commands["postboot_driver"] == ["nvidia-smi"]


def test_extract_research_plan_tolerates_prose_around_json():
    profile = UserProfile()
    payload = _base_plan_payload()

    plan = extract_research_plan(
        "Here is the plan:\n" + json.dumps(payload) + "\nDone.",
        profile,
    )

    assert plan.distro_family == "arch"


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


def test_extract_research_plan_rejects_harness_owned_command_group_stages():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload["command_groups"][0]["stage"] = "connect_and_probe"

    with pytest.raises(ResearchPlanValidationError, match="harness-owned stage"):
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


def test_build_research_prompt_includes_selected_app_catalog():
    profile = UserProfile()
    profile.apps_to_install = ["GitHub Desktop"]
    profile.apps = [AppInfo(name="GitHub Desktop", source="winget")]
    profile.ssh_public_key_path = "C:/Users/example/.ssh/id_ed25519.pub"

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
    assert "Executable command_groups may ONLY use" in prompt
    assert "connect_and_probe, research_and_plan, and final_audit are harness-owned" in prompt
    assert "Do not invent stages such as disk_prepare" in prompt
    assert "Prefer `yay` for AUR packages when no helper is detected" in prompt
    assert "evidence entries are objects" in prompt
    assert "provider_native_search" in prompt
    assert "ssh_key_status" in prompt
    assert '"public_key_path_configured": true' in prompt
    assert "Do not ask the user for raw SSH key content" in prompt
    assert "QuickHatch handles final key copying/validation" in prompt
    assert "verify active outputs, resolution, and refresh" in prompt
    assert "no GUI session is reachable" in prompt
    assert "Do not optimize for or replay any prior QuickHatch run" in prompt
    assert "Never assume Arch, KDE, NVIDIA, AUR" in prompt
    assert "Treat desktop design as a contract" in prompt
    assert "Installing a theme/icon package is not the same as applying it" in prompt
    assert "cannot read arbitrary Windows-local paths" in prompt
    assert "transfer smoke test" in prompt


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
