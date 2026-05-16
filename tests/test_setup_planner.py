from __future__ import annotations

import json

import pytest

from quickhatch.config import AppInfo, UserProfile
from quickhatch.setup.planner import (
    RepairActionValidationError,
    ResearchPlanValidationError,
    build_repair_prompt,
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


def test_extract_research_plan_accepts_typed_operations_without_commands():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload["command_groups"] = [
        {
            "id": "format-root",
            "stage": "install_or_skip",
            "title": "Format confirmed Linux root partition",
            "operations": [
                {
                    "kind": "format_partition",
                    "target": "/dev/nvme1n1p2",
                    "filesystem": "ext4",
                    "options": {"role": "root"},
                }
            ],
            "commands": [],
            "verification": ["test -b /dev/nvme1n1p2"],
        }
    ]

    plan = extract_research_plan(json.dumps(payload), profile)

    group = plan.command_groups[0]
    assert group.commands == []
    assert group.operations[0].kind == "format_partition"
    assert group.operations[0].target == "/dev/nvme1n1p2"


def test_extract_research_plan_normalizes_external_repo_app_sources():
    profile = UserProfile()
    profile.apps_to_install = ["Brave", "VLC media player"]
    payload = _base_plan_payload()
    payload["apps"] = [
        {
            "name": "Brave",
            "package_name": "brave-browser",
            "source": "brave_repo",
            "status": "installed",
            "required": True,
            "optional_slow": False,
        },
        {
            "name": "VLC media player",
            "package_name": "vlc",
            "source": "repo_rpmfusion",
            "status": "installed",
            "required": True,
            "optional_slow": False,
        },
    ]

    plan = extract_research_plan(json.dumps(payload), profile)

    assert [app.source for app in plan.apps] == ["repo", "repo"]


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


def test_extract_repair_action_accepts_top_level_evidence_for_retry_group():
    failed_group = CommandGroup(
        id="vbox",
        stage=SetupStage.POSTBOOT_DRIVER,
        title="Install VirtualBox guest drivers",
        commands=["sudo apt-get install -y virtualbox-guest-utils"],
    )
    action = extract_repair_action(
        json.dumps(
            {
                "action": "retry_group",
                "group": {
                    "id": "vbox-repair",
                    "stage": "postboot_driver",
                    "title": "Install VirtualBox guest drivers after repo refresh",
                    "commands": [
                        "sudo apt-get update",
                        "sudo apt-get install -y virtualbox-guest-utils virtualbox-guest-x11",
                    ],
                },
                "evidence": [
                    {
                        "source_type": "web_search",
                        "query": "ubuntu virtualbox guest utils package",
                        "summary": "VirtualBox guest packages are available from Ubuntu repositories.",
                    }
                ],
            }
        ),
        fallback_stage=SetupStage.POSTBOOT_DRIVER,
        failed_group=failed_group,
    )

    assert action["action"] == "retry_group"
    assert action["group"].evidence[0].source_type == "web_search"


def test_extract_research_plan_accepts_common_status_aliases():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload["apps"][0]["status"] = "uninstalled"
    payload["apps"][1]["status"] = "manual follow up"
    payload["apps"][1]["optional_slow"] = False

    plan = extract_research_plan(json.dumps(payload), profile)

    assert plan.apps[0].status == PlanItemStatus.PENDING
    assert plan.apps[1].status == PlanItemStatus.MANUAL_FOLLOWUP


def test_extract_research_plan_accepts_install_status_aliases():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload["apps"][0]["status"] = "install"
    payload["apps"][1]["package_name"] = "spotify"
    payload["apps"][1]["status"] = "resolved"
    payload["apps"][1]["optional_slow"] = False

    plan = extract_research_plan(json.dumps(payload), profile)

    assert plan.apps[0].status == PlanItemStatus.PENDING
    assert plan.apps[1].status == PlanItemStatus.PENDING


def test_extract_research_plan_accepts_native_status_alias():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload["apps"][0]["status"] = "native"
    payload["apps"][1]["package_name"] = "spotify"
    payload["apps"][1]["status"] = "native available"
    payload["apps"][1]["optional_slow"] = False

    plan = extract_research_plan(json.dumps(payload), profile)

    assert plan.apps[0].status == PlanItemStatus.PENDING
    assert plan.apps[1].status == PlanItemStatus.PENDING


def test_extract_research_plan_normalizes_package_manager_source_aliases():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload["command_groups"][0]["source"] = "pacman"
    payload["apps"][0]["source"] = "official_docs"
    payload["apps"][1]["source"] = "community_search"

    plan = extract_research_plan(json.dumps(payload), profile)

    assert plan.command_groups[0].source == "repo"
    assert plan.apps[0].source == "repo"
    assert plan.apps[1].source == "community"


def test_extract_research_plan_does_not_duplicate_requested_package_names():
    profile = UserProfile()
    profile.apps_to_install = ["firefox", "vlc"]
    payload = _base_plan_payload()
    payload["apps"] = [
        {
            "name": "Mozilla Firefox",
            "package_name": "firefox",
            "source": "repo_search",
            "status": "missing",
            "required": True,
            "evidence": [
                {
                    "source_type": "repo_search",
                    "summary": "Firefox is in the repo.",
                }
            ],
        },
        {
            "name": "VLC media player",
            "package_name": "vlc",
            "source": "repo",
            "status": "missing",
            "required": True,
            "evidence": [
                {
                    "source_type": "repo_search",
                    "summary": "VLC is in the repo.",
                }
            ],
        },
    ]

    plan = extract_research_plan(json.dumps(payload), profile)

    assert [app.name for app in plan.apps] == ["Mozilla Firefox", "VLC media player"]


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


def test_extract_research_plan_repairs_model_shell_backslash_escapes():
    profile = UserProfile()
    payload = _base_plan_payload()
    payload["command_groups"][0]["commands"] = [r'grep -q "\[multilib\]" /etc/pacman.conf']
    text = json.dumps(payload).replace(r"\\[", r"\[").replace(r"\\]", r"\]")

    plan = extract_research_plan(f"```json\n{text}\n```", profile)

    assert plan.command_groups[0].commands == [r'grep -q "\[multilib\]" /etc/pacman.conf']


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
    assert "catalog package hints as useful but fallible" in prompt
    assert "Missing app package strategy" in prompt
    assert "do not install Okular and mark Adobe Acrobat Reader installed" in prompt
    assert "VPN/firewall activation requires explicit consent" in prompt
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
    assert "VirtualBox with VMSVGA may report a `VMware SVGA` display adapter" in prompt
    assert "Do not install `virtualbox-guest-additions-iso`" in prompt
    assert "do not run or recommend `VBoxLinuxAdditions.run`" in prompt
    assert "mark VM guest integration as manual_followup" in prompt
    assert "Variables do not persist across SSH executions" in prompt
    assert "do not add `--noconfirm` to `pacstrap`" in prompt
    assert "prefer `file:///cdrom`" in prompt
    assert "avoid whitespace-sensitive checks like `grep -q ' / ' /mnt/etc/fstab`" in prompt
    assert "Treat desktop design as a contract" in prompt
    assert "Installing a theme/icon package is not the same as applying it" in prompt
    assert "cannot read arbitrary Windows-local paths" in prompt
    assert "transfer smoke test" in prompt


def test_build_repair_prompt_warns_about_vm_gpu_label_mismatch():
    prompt = build_repair_prompt(
        stage=SetupStage.POSTBOOT_DRIVER,
        failed_group=CommandGroup(
            id="guest-tools",
            stage=SetupStage.POSTBOOT_DRIVER,
            title="Guest tools",
            commands=["sudo systemctl is-active vmtoolsd.service"],
        ),
        failure_type="verification",
        failed_command="sudo systemctl is-active vmtoolsd.service",
        stdout_tail="inactive",
        stderr_tail="",
        facts=SetupFacts(
            distro_family="arch",
            package_manager="pacman",
            virtualization="oracle",
            system_vendor="innotek GmbH",
            product_name="VirtualBox",
            gpu_inventory=["VMware SVGA II Adapter"],
        ),
        recent_results=[],
        native_search_active=True,
    )

    assert "VirtualBox with VMSVGA may report a `VMware SVGA` display adapter" in prompt
    assert (
        "Do not repair VM guest integration by installing `virtualbox-guest-additions-iso`"
        in prompt
    )
    assert "running or recommending `VBoxLinuxAdditions.run`" in prompt
    assert "emit manual_followup for VM integration" in prompt
    assert '"virtualization": "oracle"' in prompt
    assert "standalone shell variable assignments" in prompt
    assert "adding `--noconfirm` to `pacstrap`" in prompt
    assert "Catalog hints are hints, not guarantees" in prompt
    assert "Manual follow-up is the last resort" in prompt
    assert "do not run unsafe vendor install scripts blindly" in prompt
    assert "do not install Okular and mark Adobe Acrobat Reader installed" in prompt
    assert "Do not enable/start VPN or firewall services as part of app repair" in prompt


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


def test_build_research_prompt_allows_fedora_live_installroot_not_anaconda():
    prompt = build_research_prompt(
        UserProfile(),
        SetupFacts(
            distro_family="fedora",
            package_manager="dnf",
            os_id="fedora",
            is_live=True,
            live_marker="LIVE:cmdline",
        ),
        system_info="Fedora live target",
        migration_plan_md="Install Fedora from the live environment.",
        distro_primer_md="",
        native_search_active=False,
    )

    assert "dnf --installroot=/mnt" in prompt
    assert "--use-host-config" in prompt
    assert "prefer one ext4 root partition mounted at `/mnt`" in prompt
    assert "do not create a separate `/boot` partition" in prompt
    assert "`kernel`, `kernel-core`" in prompt
    assert "`gawk`" in prompt
    assert "systemd-pam" in prompt
    assert "dbus-daemon" in prompt
    assert "ROOT_UUID=$(sudo blkid" in prompt
    assert "/mnt/etc/.pwd.lock" in prompt
    assert "ensure `/mnt/boot/vmlinuz-*` and `/mnt/boot/initramfs-*.img` exist" in prompt
    assert "parent disk device (not the root partition)" in prompt
    assert "Do not run Anaconda" in prompt
    assert "touch /mnt/.autorelabel" in prompt
    assert "experimental `dnf --installroot`" not in prompt
