from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from quickhatch.bridge.server import BridgeState
from quickhatch.config import AIConfig, DisplayInfo, UserProfile
from quickhatch.setup.audit import audit_stage
from quickhatch.setup.orchestrator import (
    GroupFailure,
    SetupOrchestrator,
    SetupRuntime,
    is_non_blocking_failed_systemd_unit,
)
from quickhatch.setup.types import (
    AppPlanItem,
    CommandGroup,
    DesktopRuntimeState,
    DisplayRuntimeInfo,
    PlanItemStatus,
    ResearchPlan,
    SetupFacts,
    SetupStage,
    StrategyDecision,
)


def test_extract_created_user_from_chrooted_useradd():
    command = "arch-chroot /mnt useradd -m -G wheel,video -s /bin/bash jan"

    assert SetupOrchestrator._extract_created_user(command) == "jan"


def test_extract_created_user_from_chrooted_shell_useradd():
    command = (
        'arch-chroot /mnt sh -c "useradd -m -G wheel -s /bin/bash jan; '
        "echo 'jan:password' | chpasswd\""
    )

    assert SetupOrchestrator._extract_created_user(command) == "jan"


def test_extract_created_user_from_chrooted_shell_probe_then_useradd():
    command = (
        'sudo chroot /mnt sh -c "id jan >/dev/null 2>&1 || '
        "useradd -m -G sudo -s /bin/bash jan; echo 'jan:password' | chpasswd\""
    )

    assert SetupOrchestrator._extract_created_user(command) == "jan"


def test_extract_created_user_from_fedora_kickstart_user_line():
    command = (
        "PASS_HASH=$(openssl passwd -6 'password') && printf 'user --name=jan "
        '--groups=wheel --iscrypted --password=%s\\n\' "$PASS_HASH" > /tmp/qh.ks'
    )

    assert SetupOrchestrator._extract_created_user(command) == "jan"


def test_live_installer_disconnect_is_treated_as_reboot_boundary():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="fedora",
        package_manager="dnf",
        current_user="liveuser",
        is_live=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts

    assert orchestrator._looks_like_live_installer_reboot_disconnect(
        SetupStage.INSTALL_OR_SKIP,
        "sudo anaconda --kickstart=/tmp/qh.ks --text --noninteractive",
        "",
        "Connection to 127.0.0.1 closed by remote host.",
    )


def test_live_disk_partition_reread_failure_blocks_repairs():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="fedora",
        package_manager="dnf",
        current_user="liveuser",
        is_live=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    orchestrator.ctx.plan = ResearchPlan(
        target_summary="Fedora live install",
        distro_family="fedora",
        package_manager="dnf",
        driver_strategy=StrategyDecision("none", confidence="resolved_official"),
        desktop_strategy=StrategyDecision("gnome", confidence="resolved_official"),
        command_groups=[
            CommandGroup(
                id="disk",
                stage=SetupStage.INSTALL_OR_SKIP,
                title="Disk Prep",
                commands=["sudo parted -s /dev/sda mklabel msdos"],
                verification=[],
            )
        ],
    )
    repair_calls = {"count": 0}

    def execute_group(_stage, _group, _state):
        return GroupFailure(
            "live_reboot_required",
            "sudo parted -s /dev/sda mklabel msdos",
            "",
            (
                "Error: Partition(s) 2 on /dev/sda have been written, but we have "
                "been unable to inform the kernel of the change. You should reboot "
                "now before making further changes."
            ),
            "Live install disk state is stale; reboot live media.",
        )

    def repair_failed_group(*_args, **_kwargs):
        repair_calls["count"] += 1
        return {"action": "retry_group", "group": orchestrator.ctx.plan.command_groups[0]}

    orchestrator._execute_group = execute_group  # type: ignore[method-assign]
    orchestrator._repair_failed_group = repair_failed_group  # type: ignore[method-assign]

    try:
        orchestrator._run_stage_groups(SetupStage.INSTALL_OR_SKIP, audit_after=False)
    except RuntimeError as exc:
        assert "reboot live" in str(exc).lower()
    else:
        raise AssertionError("live disk reread failure should block setup")
    assert repair_calls["count"] == 0


def test_live_disk_partition_reread_detector_requires_live_install_stage():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="fedora",
        package_manager="dnf",
        current_user="liveuser",
        is_live=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    stderr = (
        "Error: Partition(s) 2 on /dev/sda have been written, but we have been "
        "unable to inform the kernel of the change. You should reboot now before "
        "making further changes."
    )

    assert orchestrator._looks_like_live_disk_reread_required(
        SetupStage.INSTALL_OR_SKIP,
        "sudo parted -s /dev/sda mklabel msdos",
        "",
        stderr,
    )
    assert not orchestrator._looks_like_live_disk_reread_required(
        SetupStage.APPS,
        "sudo parted -s /dev/sda mklabel msdos",
        "",
        stderr,
    )


def test_install_reboot_uses_sudo_for_nonroot_live_sessions():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="ubuntu",
        package_manager="apt",
        current_user="ubuntu",
        login_user="ubuntu",
        is_live=True,
        is_root=False,
    )
    commands: list[str] = []

    def ssh_exec(command: str, _timeout: int):
        commands.append(command)
        return (0, "", "")

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    )
    orchestrator.ctx.facts = facts

    orchestrator._issue_install_reboot()

    assert commands
    assert 'qh_sudo=; [ "$(id -u)" -eq 0 ] || qh_sudo=sudo' in commands[0]
    assert "$qh_sudo umount -R /mnt" in commands[0]
    assert "nohup sh -c" in commands[0]
    assert "systemctl reboot --force --force" in commands[0]
    assert "reboot -f || reboot" in commands[0]
    assert "quickhatch-reboot.log" in commands[0]


def test_install_reboot_blocks_when_reboot_command_is_denied():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="ubuntu",
        package_manager="apt",
        current_user="ubuntu",
        login_user="ubuntu",
        is_live=True,
        is_root=False,
    )
    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(
            profile,
            bridge,
            facts,
            ssh_exec=lambda _command, _timeout: (1, "", "Access denied"),
        )
    )
    orchestrator.ctx.facts = facts

    try:
        orchestrator._issue_install_reboot()
    except RuntimeError as exc:
        assert "Install reboot command failed" in str(exc)
        assert "Access denied" in str(exc)
    else:
        raise AssertionError("denied reboot should block instead of polling live ISO")


def test_install_reboot_accepts_ssh_disconnect_as_successful_reboot_boundary():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="ubuntu",
        package_manager="apt",
        current_user="ubuntu",
        login_user="ubuntu",
        is_live=True,
        is_root=False,
    )
    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(
            profile,
            bridge,
            facts,
            ssh_exec=lambda _command, _timeout: (
                255,
                "",
                "Connection to 127.0.0.1 closed by remote host.",
            ),
        )
    )
    orchestrator.ctx.facts = facts

    orchestrator._issue_install_reboot()


def test_ubuntu_live_arch_chroot_is_rewritten_to_sudo_chroot():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="ubuntu",
        package_manager="apt",
        current_user="ubuntu",
        login_user="ubuntu",
        is_live=True,
        is_root=False,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts

    rewritten = orchestrator._normalize_live_nonroot_chroot_command(
        "sudo arch-chroot /mnt sh -c 'apt update'",
        stage=SetupStage.INSTALL_OR_SKIP,
    )

    assert rewritten == "sudo chroot /mnt sh -c 'apt update'"


def test_arch_live_arch_chroot_keeps_arch_tool_but_adds_sudo():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="arch",
        login_user="arch",
        is_live=True,
        is_root=False,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts

    rewritten = orchestrator._normalize_live_nonroot_chroot_command(
        "arch-chroot /mnt pacman -Syu",
        stage=SetupStage.INSTALL_OR_SKIP,
    )

    assert rewritten == "sudo arch-chroot /mnt pacman -Syu"


def test_live_target_uefi_boot_fallback_installs_removable_grub():
    command = SetupOrchestrator._live_target_uefi_boot_fallback_command("/mnt")

    assert "[ -d /sys/firmware/efi ] || exit 0" in command
    assert "for qh_bind in dev dev/pts proc sys run" in command
    assert 'mount --bind "/$qh_bind" "$qh_root/$qh_bind"' in command
    assert "chroot \"$qh_root\" sh -c 'command -v grub-install" in command
    assert 'grub-install --target=x86_64-efi' in command
    assert '--bootloader-id=quickhatch' in command
    assert '--bootloader-id=quickhatch --recheck || true' in command
    assert '--removable --no-nvram --recheck' in command
    assert 'EFI/BOOT/BOOTX64.EFI' in command
    assert r"\EFI\quickhatch\grubx64.efi" in command
    assert 'update-grub' in command
    assert 'grub-mkconfig -o /boot/grub/grub.cfg' in command
    assert 'grub-editenv /boot/grub/grubenv create' in command
    assert "efibootmgr -v" in command
    assert "qh_efi_partuuid_lc" in command
    assert "blkid -s PARTUUID" in command
    assert "index(line, p) > 0" in command
    assert "efibootmgr -c" in command
    assert "efibootmgr -n" in command
    assert "efibootmgr -o" in command
    assert "BootOrder:" in command
    assert r"\EFI\BOOT\BOOTX64.EFI" in command
    assert "windows boot manager" in command
    assert "vbox harddisk" in command
    assert command.index(r"\EFI\quickhatch\grubx64.efi") < command.index("vbox harddisk")


def test_live_target_network_reconnect_command_seeds_networkmanager_dhcp():
    command = SetupOrchestrator._live_target_network_reconnect_command("/mnt")

    assert "quickhatch-wired-dhcp.nmconnection" in command
    assert "type=ethernet" in command
    assert "method=auto" in command
    assert "10-quickhatch-managed.conf" in command
    assert "unmanaged-devices=none" in command
    assert "01-quickhatch-networkmanager.yaml" in command
    assert "renderer: NetworkManager" in command
    assert 'chmod 600 "$qh_root/etc/netplan/01-quickhatch-networkmanager.yaml"' in command
    assert 'chroot "$qh_root" systemctl enable NetworkManager' in command
    assert 'chroot "$qh_root" systemctl enable ssh' in command


def test_fedora_live_media_reaches_research_planning():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="fedora",
        package_manager="dnf",
        current_user="liveuser",
        login_user="liveuser",
        is_live=True,
        live_marker="LIVE:cmdline",
    )
    model_calls = {"count": 0}

    def call_model(_cfg, _messages):
        model_calls["count"] += 1
        return "{}"

    runtime = _dummy_setup_runtime(
        profile,
        bridge,
        facts,
        probe_facts=lambda **_kwargs: facts,
    )
    runtime.call_model_text = call_model

    summary = SetupOrchestrator(runtime).run()

    assert "Research plan validation failed" in summary
    assert "Fedora-family live media is detected" not in summary
    assert model_calls["count"] > 0
    assert bridge.snapshot_setup_run()["status"] == "blocked"


def _dummy_setup_runtime(
    profile: UserProfile,
    bridge: BridgeState,
    facts: SetupFacts,
    *,
    ssh_exec=None,
    wait_for_reconnect=None,
    probe_facts=None,
    run_pre_reboot_checks=None,
    native_search_active: bool = False,
) -> SetupRuntime:
    return SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(provider="api_openai", endpoint="http://example", model_name="test"),
        host="127.0.0.1",
        user=facts.current_user or "jan",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=lambda cfg, messages: "{}",
        probe_facts=probe_facts or (lambda **kwargs: facts),
        detect_live_env=lambda: (False, "INSTALLED"),
        ssh_exec=ssh_exec or (lambda command, timeout: (0, "", "")),
        repo_search=lambda query: "",
        community_search=lambda query: "",
        web_search=lambda query, max_results: "",
        wait_for_reconnect=wait_for_reconnect
        or (lambda reconnect_user: (True, "fresh info", reconnect_user)),
        run_pre_reboot_checks=run_pre_reboot_checks or (lambda: (True, [], "")),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda current, is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=native_search_active,
    )


def test_systemd_health_ignores_transient_coredump_helpers():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(current_user="jan", login_user="jan", init_system="systemd")

    def ssh_exec(command: str, _timeout: int):
        assert "systemctl --failed" in command
        return (
            0,
            "\n".join(
                [
                    "drkonqi-coredump-processor@6-12290-90777_90778-0.service",
                    "systemd-coredump@0-123-0.service",
                    "whoopsie.path",
                    "whoopsie.service",
                ]
            ),
            "",
        )

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    )
    orchestrator.ctx.facts = facts

    assert orchestrator._verify_systemd_health() == []


def test_fstab_root_verification_blocks_empty_uuid():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(current_user="jan", login_user="jan", is_live=False)

    def ssh_exec(command: str, _timeout: int):
        assert "/etc/fstab" in command
        return (3, "UUID=\n", "")

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    )
    orchestrator.ctx.facts = facts

    blockers = orchestrator._verify_fstab_root_entry()

    assert len(blockers) == 1
    assert "Root fstab entry is missing or invalid" in blockers[0]
    assert "UUID=" in blockers[0]


def test_fstab_root_verification_accepts_valid_device_entry():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(current_user="jan", login_user="jan", is_live=False)

    def ssh_exec(_command: str, _timeout: int):
        return (0, "/dev/sda1\n", "")

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    )
    orchestrator.ctx.facts = facts

    assert orchestrator._verify_fstab_root_entry() == []


def test_systemd_health_still_blocks_real_failed_services():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(current_user="jan", login_user="jan", init_system="systemd")

    def ssh_exec(_command: str, _timeout: int):
        return (
            0,
            "\n".join(
                [
                    "drkonqi-coredump-processor@6-12290-90777_90778-0.service",
                    "NetworkManager.service",
                ]
            ),
            "",
        )

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    )
    orchestrator.ctx.facts = facts

    failures = orchestrator._verify_systemd_health()

    assert len(failures) == 1
    assert "NetworkManager.service" in failures[0]
    assert "drkonqi" not in failures[0]


def test_display_manager_session_health_blocks_false_active_dm():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        init_system="systemd",
        display_manager="gdm",
        display_manager_enabled=True,
        display_manager_active=True,
    )

    def ssh_exec(command: str, _timeout: int):
        assert "loginctl list-sessions" in command
        return (1, "", "")

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    )
    orchestrator.ctx.facts = facts

    blockers = orchestrator._verify_display_manager_session_health()

    assert blockers == [
        "Display manager is active, but no local greeter/user session was verified. "
        "Inspect `journalctl -u display-manager -b` or the concrete DM service before "
        "declaring desktop setup complete."
    ]


def test_display_manager_session_health_accepts_greeter_session():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        init_system="systemd",
        display_manager="gdm",
        display_manager_enabled=True,
        display_manager_active=True,
    )

    def ssh_exec(_command: str, _timeout: int):
        return (0, "c1 gdm-greeter greeter active wayland\n", "")

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    )
    orchestrator.ctx.facts = facts

    assert orchestrator._verify_display_manager_session_health() == []


def test_non_blocking_failed_systemd_unit_classifier():
    assert is_non_blocking_failed_systemd_unit(
        "drkonqi-coredump-processor@6-12290-90777_90778-0.service"
    )
    assert is_non_blocking_failed_systemd_unit("systemd-coredump@0-123-0.service")
    assert is_non_blocking_failed_systemd_unit("mcelog.service")
    assert is_non_blocking_failed_systemd_unit("whoopsie.path")
    assert is_non_blocking_failed_systemd_unit("whoopsie.service")
    assert not is_non_blocking_failed_systemd_unit("sddm.service")


def test_unanswered_optional_slow_apps_become_manual_followup_before_apps_audit():
    profile = UserProfile(apps_to_install=["Codex"])
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        current_user="jan",
        login_user="jan",
        package_manager="pacman",
        distro_family="arch",
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    orchestrator.ctx.plan = ResearchPlan(
        target_summary="test setup",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision("none", confidence="resolved_official", source="repo"),
        desktop_strategy=StrategyDecision("none", confidence="resolved_official", source="repo"),
        command_groups=[],
        apps=[
            AppPlanItem(
                name="Codex",
                package_name="openai-codex",
                source="community",
                status=PlanItemStatus.OPTIONAL_SLOW_PENDING_CONFIRMATION,
                required=True,
                optional_slow=True,
            )
        ],
    )

    orchestrator._run_apps()

    assert orchestrator.ctx.plan.apps[0].status == PlanItemStatus.MANUAL_FOLLOWUP
    assert "Optional slow app" in orchestrator.ctx.plan.apps[0].reason
    assert orchestrator.ctx.stage_results[-1].stage == SetupStage.APPS
    assert orchestrator.ctx.stage_results[-1].passed is True
    assert "Optional slow apps still need user confirmation" not in " ".join(
        orchestrator.ctx.stage_results[-1].blockers
    )
    assert any("Codex" in message["content"] for message in bridge.messages)


def test_connect_probe_runs_keep_windows_transfer_smoke_test():
    profile = UserProfile(data_transfer_method="keep_windows")
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        login_user="jan",
    )
    copied: dict[str, str] = {}
    commands_seen: list[str] = []

    def ssh_exec(command, _timeout):
        commands_seen.append(command)
        if command.startswith("echo 'QuickHatch connected!'"):
            return (0, "QuickHatch connected!\nLinux test\n", "")
        if "quickhatch-transfer-smoke" in command and "grep -qxF" in command:
            return (0, "", "")
        return (0, "", "")

    def copy_local_file_to_target(local_path: Path, remote_path: str, _timeout: int):
        copied["remote_path"] = remote_path
        copied["payload"] = local_path.read_text(encoding="utf-8").strip()
        return (0, "", "")

    runtime = _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    runtime.copy_local_file_to_target = copy_local_file_to_target
    orchestrator = SetupOrchestrator(runtime)

    orchestrator._start_run_state()
    orchestrator._run_connect_and_probe()

    assert copied["remote_path"].startswith("/tmp/quickhatch-transfer-smoke-")
    assert copied["payload"].startswith("quickhatch-transfer-smoke ")
    assert any("grep -qxF" in command for command in commands_seen)
    state = orchestrator.ctx.stage_state[SetupStage.CONNECT_AND_PROBE.value]
    assert state["transfer_smoke_test"]["status"] == "passed"


def test_connect_probe_transfer_smoke_failure_does_not_block_setup():
    profile = UserProfile(data_transfer_method="keep_windows")
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        login_user="jan",
    )

    def ssh_exec(command, _timeout):
        if command.startswith("echo 'QuickHatch connected!'"):
            return (0, "QuickHatch connected!\nLinux test\n", "")
        return (0, "", "")

    runtime = _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    runtime.copy_local_file_to_target = lambda _local, _remote, _timeout: (
        1,
        "",
        "permission denied",
    )
    orchestrator = SetupOrchestrator(runtime)

    orchestrator._start_run_state()
    orchestrator._run_connect_and_probe()

    state = orchestrator.ctx.stage_state[SetupStage.CONNECT_AND_PROBE.value]
    assert state["transfer_smoke_test"]["status"] == "failed"
    assert any(
        "Network file-transfer smoke test failed" in message.get("content", "")
        for message in bridge.messages
    )


def test_connect_probe_skips_transfer_smoke_for_external_drive():
    profile = UserProfile(data_transfer_method="external_drive")
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        login_user="jan",
    )
    copied = {"called": False}

    def ssh_exec(command, _timeout):
        if command.startswith("echo 'QuickHatch connected!'"):
            return (0, "QuickHatch connected!\nLinux test\n", "")
        return (0, "", "")

    runtime = _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    runtime.copy_local_file_to_target = lambda _local, _remote, _timeout: (
        copied.update(called=True) or (0, "", "")
    )
    orchestrator = SetupOrchestrator(runtime)

    orchestrator._start_run_state()
    orchestrator._run_connect_and_probe()

    assert copied["called"] is False
    assert "transfer_smoke_test" not in orchestrator.ctx.stage_state.get(
        SetupStage.CONNECT_AND_PROBE.value, {}
    )


def test_installed_nonroot_privileged_commands_are_sudo_normalized():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        login_user="jan",
        is_root=False,
        sudo_nopasswd=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="desktop",
        stage=SetupStage.DESKTOP,
        title="Install desktop",
        commands=[
            "pacman -S --noconfirm plasma-desktop sddm",
            "systemctl enable sddm",
        ],
    )

    commands = orchestrator._commands_for_group(SetupStage.DESKTOP, group)

    assert commands == [
        "sudo pacman -S --noconfirm plasma-desktop sddm",
        "sudo systemctl enable sddm",
    ]


def test_xfconf_query_counts_as_persisted_desktop_config():
    assert SetupOrchestrator._command_persists_desktop_config(
        "sudo -u ada dbus-run-session xfconf-query -c xsettings -p /Net/IconThemeName -s Papirus"
    )


def test_pending_login_display_preferences_become_manual_followup():
    profile = UserProfile()
    profile.hardware.displays = [
        DisplayInfo(
            name="VirtualBox Display",
            width_px=1280,
            height_px=800,
            refresh_hz=60.0,
            primary=True,
        )
    ]
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="ada",
        display_manager="lightdm",
        display_manager_enabled=True,
        display_manager_active=True,
        desktop_runtime_state=DesktopRuntimeState.PENDING_RELOGIN,
        displays=[],
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    state: dict[str, object] = {}

    orchestrator._update_desktop_display_audit_state(state)

    assert state["display_manual_followup"] is True
    assert state["display_preference_blockers"] == []
    assert "1280x800@60Hz" in str(state["display_manual_followup_reason"])


def test_installed_nonroot_privilege_normalization_preserves_user_commands():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        login_user="jan",
        is_root=False,
        sudo_nopasswd=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="desktop",
        stage=SetupStage.DESKTOP,
        title="Apply user config",
        commands=[
            "kwriteconfig6 --file kdeglobals --group Icons --key Theme candy-icons",
            "mkdir -p ~/.config/plasma-workspace",
        ],
    )

    commands = orchestrator._commands_for_group(SetupStage.DESKTOP, group)

    assert commands == group.commands


def test_installed_nonroot_privilege_normalization_preserves_root_sessions():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="root",
        login_user="root",
        is_root=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="driver",
        stage=SetupStage.POSTBOOT_DRIVER,
        title="Install driver",
        commands=["pacman -S --noconfirm nvidia-open nvidia-utils"],
    )

    commands = orchestrator._commands_for_group(SetupStage.POSTBOOT_DRIVER, group)

    assert commands == ["pacman -S --noconfirm nvidia-open nvidia-utils"]


def test_installed_nonroot_privilege_normalization_sudoes_privileged_tee_pipeline():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        login_user="jan",
        is_root=False,
        sudo_nopasswd=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="driver",
        stage=SetupStage.POSTBOOT_DRIVER,
        title="Write NVIDIA config",
        commands=[
            "printf '%s\\n' 'options nvidia-drm modeset=1' | tee /etc/modprobe.d/nvidia.conf >/dev/null"
        ],
    )

    commands = orchestrator._commands_for_group(SetupStage.POSTBOOT_DRIVER, group)

    assert commands == [
        "printf '%s\\n' 'options nvidia-drm modeset=1' | sudo tee /etc/modprobe.d/nvidia.conf >/dev/null"
    ]


def test_installed_nonroot_privilege_normalization_converts_echo_redirection_to_sudo_tee():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        login_user="jan",
        is_root=False,
        sudo_nopasswd=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="desktop",
        stage=SetupStage.DESKTOP,
        title="Configure SDDM autologin",
        commands=[
            "echo -e '[Autologin]\\nUser=jan\\nSession=plasma.desktop' > /etc/sddm.conf.d/autologin.conf"
        ],
    )

    commands = orchestrator._commands_for_group(SetupStage.DESKTOP, group)

    assert commands == [
        "echo -e '[Autologin]\\nUser=jan\\nSession=plasma.desktop' | sudo tee /etc/sddm.conf.d/autologin.conf >/dev/null"
    ]


def test_installed_nonroot_privilege_normalization_strips_same_user_runuser_wrapper():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="linus",
        login_user="linus",
        is_root=False,
        sudo_nopasswd=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="desktop",
        stage=SetupStage.DESKTOP,
        title="Configure KDE",
        commands=[
            "runuser -u linus -- kwriteconfig6 --file kdeglobals --group General --key ColorScheme 'Breeze Dark'"
        ],
    )

    commands = orchestrator._commands_for_group(SetupStage.DESKTOP, group)

    assert commands == [
        "kwriteconfig6 --file kdeglobals --group General --key ColorScheme 'Breeze Dark'"
    ]


def test_installed_nonroot_privilege_normalization_preserves_root_redirection():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="root",
        login_user="root",
        is_root=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="desktop",
        stage=SetupStage.DESKTOP,
        title="Configure SDDM autologin",
        commands=["echo -e '[Autologin]\\nUser=jan' > /etc/sddm.conf.d/autologin.conf"],
    )

    commands = orchestrator._commands_for_group(SetupStage.DESKTOP, group)

    assert commands == ["echo -e '[Autologin]\\nUser=jan' > /etc/sddm.conf.d/autologin.conf"]


def test_installed_stage_strips_stale_chroot_prefix_and_sudoes_inner_command():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        login_user="jan",
        is_root=False,
        sudo_nopasswd=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="driver",
        stage=SetupStage.POSTBOOT_DRIVER,
        title="Install guest utilities",
        commands=["arch-chroot /mnt pacman -S --noconfirm virtualbox-guest-utils"],
    )

    commands = orchestrator._commands_for_group(SetupStage.POSTBOOT_DRIVER, group)

    assert commands == ["sudo pacman -S --noconfirm virtualbox-guest-utils"]


def test_installed_stage_strips_stale_chroot_prefix_from_verification():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        login_user="jan",
        is_root=False,
        sudo_nopasswd=True,
    )
    commands_seen: list[str] = []

    def ssh_exec(command, timeout):
        commands_seen.append(command)
        return (0, "", "")

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    )
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="desktop",
        stage=SetupStage.DESKTOP,
        title="Verify SDDM",
        commands=["true"],
        verification=["arch-chroot /mnt systemctl is-enabled sddm"],
    )

    failure = orchestrator._execute_group(SetupStage.DESKTOP, group, {})

    assert failure is None
    assert commands_seen == ["true", "sudo systemctl is-enabled sddm"]


def test_model_call_retries_transient_errors(monkeypatch):
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(current_user="jan", login_user="jan")
    attempts = {"count": 0}
    monkeypatch.setattr("quickhatch.setup.orchestrator.time.sleep", lambda _seconds: None)

    timeouts: list[int] = []

    def model_call(_cfg, _messages, timeout_seconds):
        timeouts.append(timeout_seconds)
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("503 Service Unavailable")
        return "ok"

    runtime = _dummy_setup_runtime(profile, bridge, facts)
    runtime.call_model_text = model_call
    orchestrator = SetupOrchestrator(runtime)

    result = orchestrator._call_model_with_retries(
        runtime.config, [{"role": "user", "content": "test"}], context="planning"
    )

    assert result == "ok"
    assert attempts["count"] == 2
    assert timeouts == [90, 90]
    assert any("LLM call failed" in msg["content"] for msg in bridge.messages)
    assert any("attempt took" in msg["content"] for msg in bridge.messages)


def test_model_call_retry_keeps_legacy_test_callbacks_supported():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(current_user="jan", login_user="jan")

    def legacy_model_call(_cfg, _messages):
        return "ok"

    runtime = _dummy_setup_runtime(profile, bridge, facts)
    runtime.call_model_text = legacy_model_call
    orchestrator = SetupOrchestrator(runtime)

    result = orchestrator._call_model_with_retries(
        runtime.config, [{"role": "user", "content": "test"}], context="planning"
    )

    assert result == "ok"


def test_reconnect_key_accepts_verified_key_after_transient_failure(monkeypatch):
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(current_user="root", login_user="root", is_root=True)
    calls: list[str] = []
    monkeypatch.setattr("quickhatch.setup.orchestrator.time.sleep", lambda _seconds: None)

    def ssh_exec(command, timeout):
        calls.append(command)
        if command.startswith("set -e;"):
            return (255, "", "Connection closed by 127.0.0.1 port 2222")
        return (0, "", "")

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    )
    orchestrator.ctx.created_user = "jan"
    orchestrator._resolve_public_key_text = lambda: "ssh-ed25519 AAAATEST quickhatch"  # type: ignore[method-assign]

    orchestrator._ensure_reconnect_ssh_key()

    assert orchestrator.ctx.stage_state[SetupStage.INSTALL_OR_SKIP.value][
        "ssh_continuity_key_installed"
    ]
    assert len(calls) == 2
    assert "qh_root=/mnt" in calls[1]
    assert "qh_user=jan" in calls[1]
    assert "$qh_sudo grep" in calls[1]


def test_live_reconnect_key_creates_planned_target_user_when_repair_skipped_it():
    profile = UserProfile()
    profile.linux_user = "ubuntu"
    profile.default_account_password = "password"
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="ubuntu",
        package_manager="apt",
        current_user="ubuntu",
        login_user="ubuntu",
        is_live=True,
        is_root=False,
    )
    commands: list[str] = []
    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=lambda command, timeout: (0, "", ""))
    )
    orchestrator.ctx.facts = facts
    orchestrator.ctx.plan = ResearchPlan(
        target_summary="Ubuntu live install",
        distro_family="ubuntu",
        package_manager="apt",
        driver_strategy=StrategyDecision("none", confidence="resolved_official"),
        desktop_strategy=StrategyDecision("gnome", confidence="resolved_official"),
        command_groups=[
            CommandGroup(
                id="user",
                stage=SetupStage.INSTALL_OR_SKIP,
                title="Create user",
                commands=[
                    'sudo chroot /mnt sh -c "id jan >/dev/null 2>&1 || '
                    "useradd -m -G sudo -s /bin/bash jan; echo 'jan:password' | chpasswd\""
                ],
                verification=[],
            )
        ],
    )

    def ssh_exec(command, timeout):
        commands.append(command)
        if "for qh_root in" in command:
            return (0, "/mnt\n", "")
        if "live_target_admin_user" in command:
            return (1, "", "")
        if "awk -F:" in command and "groups=" in command:
            return (1, "", "")
        if "useradd" in command and "chroot" in command:
            return (0, "", "")
        if "authorized_keys" in command:
            return (0, "", "")
        return (0, "", "")

    orchestrator.runtime.ssh_exec = ssh_exec
    orchestrator._resolve_public_key_text = lambda: "ssh-ed25519 AAAATEST quickhatch"  # type: ignore[method-assign]

    orchestrator._ensure_reconnect_ssh_key()

    assert orchestrator.ctx.created_user == "jan"
    assert any("useradd" in command and "qh_user=jan" in command for command in commands)
    key_command = next(command for command in commands if "authorized_keys" in command)
    assert "qh_root=/mnt" in key_command
    assert "qh_user=jan" in key_command
    assert "$qh_sudo tee -a" in key_command


def test_reconnect_key_retries_after_transient_disconnect(monkeypatch):
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(current_user="root", login_user="root", is_root=True)
    install_attempts = {"count": 0}
    monkeypatch.setattr("quickhatch.setup.orchestrator.time.sleep", lambda _seconds: None)

    def ssh_exec(command, timeout):
        if command.startswith("set -e;"):
            install_attempts["count"] += 1
            if install_attempts["count"] == 1:
                return (255, "", "Connection closed by 127.0.0.1 port 2222")
            return (0, "", "")
        return (1, "", "not present yet")

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    )
    orchestrator.ctx.created_user = "jan"
    orchestrator._resolve_public_key_text = lambda: "ssh-ed25519 AAAATEST quickhatch"  # type: ignore[method-assign]

    orchestrator._ensure_reconnect_ssh_key()

    assert install_attempts["count"] == 2
    assert orchestrator.ctx.stage_state[SetupStage.INSTALL_OR_SKIP.value][
        "ssh_continuity_key_installed"
    ]


def test_reconnect_key_tolerates_extended_transient_disconnect(monkeypatch):
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(current_user="root", login_user="root", is_root=True)
    install_attempts = {"count": 0}
    monkeypatch.setattr("quickhatch.setup.orchestrator.time.sleep", lambda _seconds: None)

    def ssh_exec(command, timeout):
        if command.startswith("set -e;"):
            install_attempts["count"] += 1
            if install_attempts["count"] <= 5:
                return (255, "", "Connection closed by 127.0.0.1 port 2222")
            return (0, "", "")
        return (255, "", "Connection closed by 127.0.0.1 port 2222")

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    )
    orchestrator.ctx.created_user = "jan"
    orchestrator._resolve_public_key_text = lambda: "ssh-ed25519 AAAATEST quickhatch"  # type: ignore[method-assign]

    orchestrator._ensure_reconnect_ssh_key()

    assert install_attempts["count"] == 6
    assert orchestrator.ctx.stage_state[SetupStage.INSTALL_OR_SKIP.value][
        "ssh_continuity_key_installed"
    ]


def test_group_command_retries_transient_ssh_transport_failure(monkeypatch):
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(current_user="root", login_user="root", is_root=True)
    attempts = {"count": 0}
    monkeypatch.setattr("quickhatch.setup.orchestrator.time.sleep", lambda _seconds: None)

    def ssh_exec(command, timeout):
        attempts["count"] += 1
        if attempts["count"] <= 2:
            return (255, "", "Connection closed by 127.0.0.1 port 2222")
        return (0, "", "")

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    )
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="chown-key",
        stage=SetupStage.INSTALL_OR_SKIP,
        title="Fix SSH key ownership",
        commands=["arch-chroot /mnt chown -R jan:jan /home/jan/.ssh"],
    )

    failure = orchestrator._execute_group(SetupStage.INSTALL_OR_SKIP, group, {})

    assert failure is None
    assert attempts["count"] == 3
    assert any("Transient SSH transport failure" in msg["content"] for msg in bridge.messages)


def test_non_app_missing_repo_target_is_dropped_for_retry():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(current_user="jan", login_user="jan", is_root=False)
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    command = "sudo pacman -S --noconfirm virtualbox-guest-utils xf86-video-vmware"
    group = CommandGroup(
        id="vbox",
        stage=SetupStage.POSTBOOT_DRIVER,
        title="VirtualBox Guest Drivers",
        commands=[command],
        packages=["virtualbox-guest-utils", "xf86-video-vmware"],
    )
    failure = GroupFailure(
        failure_type="command",
        command=command,
        stdout="",
        stderr="error: target not found: xf86-video-vmware",
        detail="target not found",
    )

    repaired = orchestrator._auto_repair_missing_repo_targets(
        SetupStage.POSTBOOT_DRIVER, group, failure
    )

    assert repaired is not None
    assert repaired.commands == ["sudo pacman -S --noconfirm virtualbox-guest-utils"]
    assert repaired.packages == ["virtualbox-guest-utils"]
    assert orchestrator.ctx.stage_state[SetupStage.POSTBOOT_DRIVER.value]["manual_followups"] == [
        {
            "group_id": "vbox",
            "reason": (
                "Unavailable repository target(s) were dropped from 'VirtualBox Guest Drivers': "
                "xf86-video-vmware. Review or choose alternatives after setup."
            ),
        }
    ]
    assert any(
        "Removed unavailable non-app repo target" in msg["content"] for msg in bridge.messages
    )


def test_apps_missing_repo_target_is_not_silently_dropped():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(current_user="jan", login_user="jan", is_root=False)
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    command = "sudo pacman -S --noconfirm imaginary-app"
    group = CommandGroup(
        id="apps",
        stage=SetupStage.APPS,
        title="Install Apps",
        commands=[command],
        packages=["imaginary-app"],
    )
    failure = GroupFailure(
        failure_type="command",
        command=command,
        stdout="",
        stderr="error: target not found: imaginary-app",
        detail="target not found",
    )

    repaired = orchestrator._auto_repair_missing_repo_targets(SetupStage.APPS, group, failure)

    assert repaired is None


def test_desktop_dmrc_write_counts_as_persisted_config():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(current_user="jan", login_user="jan", is_root=False)
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts

    orchestrator._update_from_command(
        "echo '[Desktop]\nSession=plasma' > /home/jan/.dmrc",
        "",
        "",
        stage=SetupStage.DESKTOP,
    )

    assert orchestrator.ctx.stage_state[SetupStage.DESKTOP.value]["persisted_config_written"]


def test_desktop_config_marker_counts_as_persisted_config():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(current_user="jan", login_user="jan", is_root=False)
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts

    orchestrator._update_from_command(
        "sudo -u jan touch /home/jan/.config/plasma-minimal-marker",
        "",
        "",
        stage=SetupStage.DESKTOP,
    )

    assert orchestrator.ctx.stage_state[SetupStage.DESKTOP.value]["persisted_config_written"]


def test_plasma_live_apply_without_config_write_does_not_count_as_persisted_config():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        current_user="jan",
        login_user="jan",
        session_type="wayland",
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts

    orchestrator._update_from_command(
        "plasma-apply-lookandfeel -a com.github.eliverlara.sweet-mars",
        "",
        "",
        stage=SetupStage.DESKTOP,
    )

    assert "persisted_config_written" not in orchestrator.ctx.stage_state[SetupStage.DESKTOP.value]
    assert facts.desktop_runtime_state != DesktopRuntimeState.LIVE_APPLY_SUCCEEDED


def test_wallpaper_download_alone_does_not_count_as_persisted_desktop_config():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(current_user="jan", login_user="jan", is_root=False)
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts

    orchestrator._update_from_command(
        "wget -O /home/jan/wallpaper.jpg https://example.invalid/wallpaper.jpg",
        "",
        "",
        stage=SetupStage.DESKTOP,
    )

    assert "persisted_config_written" not in orchestrator.ctx.stage_state[SetupStage.DESKTOP.value]


def test_app_group_completion_reconciles_corrected_aur_aliases():
    profile = UserProfile(apps_to_install=["Brave", "Mullvad VPN"])
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(distro_family="arch", package_manager="pacman", current_user="jan")
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.plan = ResearchPlan(
        target_summary="Arch workstation",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision(strategy="none"),
        desktop_strategy=StrategyDecision(strategy="none"),
        apps=[
            AppPlanItem(
                name="Brave",
                package_name="brave-browser",
                source="community",
                status=PlanItemStatus.PENDING,
                required=True,
            ),
            AppPlanItem(
                name="Mullvad VPN",
                package_name="mullvad-vpn",
                source="community",
                status=PlanItemStatus.PENDING,
                required=True,
            ),
        ],
    )
    group = CommandGroup(
        id="aur-apps",
        stage=SetupStage.APPS,
        title="Install browsers and VPN",
        commands=["yay -S --noconfirm brave-bin mullvad-vpn-bin"],
        verification=["command -v brave", "command -v mullvad"],
        packages=["brave-bin", "mullvad-vpn-bin"],
    )

    orchestrator._mark_group_completion(group, stage=SetupStage.APPS)

    assert [app.status for app in orchestrator.ctx.plan.apps] == [
        PlanItemStatus.INSTALLED,
        PlanItemStatus.INSTALLED,
    ]


def test_apps_stage_synthesizes_missing_repo_install_group():
    profile = UserProfile(apps_to_install=["Mozilla Firefox", "VLC media player"])
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="ada",
        login_user="ada",
        sudo_nopasswd=True,
    )
    commands_seen: list[str] = []

    def ssh_exec(command, timeout):
        commands_seen.append(command)
        return (0, "", "")

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    )
    orchestrator.ctx.facts = facts
    orchestrator.ctx.plan = ResearchPlan(
        target_summary="Arch XFCE workstation",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision(strategy="none"),
        desktop_strategy=StrategyDecision(strategy="xfce"),
        apps=[
            AppPlanItem(
                name="Mozilla Firefox",
                package_name="firefox",
                source="pacman",
                status=PlanItemStatus.PENDING,
                required=True,
            ),
            AppPlanItem(
                name="VLC media player",
                package_name="vlc",
                source="repo",
                status=PlanItemStatus.PENDING,
                required=True,
            ),
        ],
    )

    orchestrator._run_apps()

    assert commands_seen == [
        "sudo pacman -S --noconfirm firefox vlc",
        "sudo pacman -Q firefox vlc",
    ]
    assert [app.status for app in orchestrator.ctx.plan.apps] == [
        PlanItemStatus.INSTALLED,
        PlanItemStatus.INSTALLED,
    ]
    assert any("Added missing app install group" in msg["content"] for msg in bridge.messages)


def test_apps_stage_marks_unsupported_community_source_manual_followup():
    profile = UserProfile(apps_to_install=["Brave"])
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="debian",
        package_manager="apt",
        current_user="ada",
        login_user="ada",
        sudo_nopasswd=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    orchestrator.ctx.plan = ResearchPlan(
        target_summary="Debian workstation",
        distro_family="debian",
        package_manager="apt",
        driver_strategy=StrategyDecision(strategy="none"),
        desktop_strategy=StrategyDecision(strategy="xfce"),
        apps=[
            AppPlanItem(
                name="Brave",
                package_name="brave-bin",
                source="community",
                status=PlanItemStatus.PENDING,
                required=True,
            )
        ],
    )

    orchestrator._run_apps()

    assert orchestrator.ctx.plan.apps[0].status == PlanItemStatus.MANUAL_FOLLOWUP
    assert "not supported automatically" in orchestrator.ctx.plan.apps[0].reason


def test_pacman_group_verification_falls_back_to_group_query():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="ada",
        login_user="ada",
        sudo_nopasswd=True,
    )
    commands_seen: list[str] = []

    def ssh_exec(command, timeout):
        commands_seen.append(command)
        return (0, "", "")

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    )
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="xfce",
        stage=SetupStage.DESKTOP,
        title="Install XFCE",
        commands=["true"],
        verification=["pacman -Qi xfce4"],
    )

    failure = orchestrator._execute_group(SetupStage.DESKTOP, group, {})

    assert failure is None
    assert commands_seen == [
        "true",
        "(pacman -Qi xfce4 >/dev/null 2>&1 || pacman -Qg xfce4 >/dev/null 2>&1)",
    ]


def test_apt_package_verification_accepts_snap_backed_apps():
    facts = SetupFacts(package_manager="apt")

    command = SetupOrchestrator._package_verification_command(["thunderbird"], facts)

    assert "dpkg-query -W thunderbird" in command
    assert "snap list thunderbird" in command


def test_apps_launcher_verification_uses_package_query_for_apt_snap_apps():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="ubuntu",
        package_manager="apt",
        current_user="jan",
        login_user="jan",
        sudo_nopasswd=True,
    )
    commands_seen: list[str] = []

    def ssh_exec(command, _timeout):
        commands_seen.append(command)
        return (0, "", "")

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    )
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="ubuntu-apps",
        stage=SetupStage.APPS,
        title="Install Ubuntu Apps",
        commands=["true"],
        verification=["thunderbird --version"],
        packages=["thunderbird"],
    )

    failure = orchestrator._execute_group(SetupStage.APPS, group, {})

    assert failure is None
    assert any("dpkg-query -W thunderbird" in command for command in commands_seen)
    assert any("snap list thunderbird" in command for command in commands_seen)
    assert all("thunderbird --version" not in command for command in commands_seen)


def test_apt_external_repo_groups_inject_download_prerequisites():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="ubuntu",
        package_manager="apt",
        current_user="jan",
        login_user="jan",
        sudo_nopasswd=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="brave",
        stage=SetupStage.APPS,
        title="Install Brave",
        commands=[
            "sudo curl -fsSLo /etc/apt/sources.list.d/brave.sources https://brave.example/sources"
        ],
    )

    commands = orchestrator._commands_for_group(SetupStage.APPS, group)

    prep = next(command for command in commands if " install -y curl ca-certificates gnupg" in command)
    assert prep.startswith("sudo apt -o Acquire::Retries=2")
    assert "Acquire::http::Timeout=20" in prep
    assert "Acquire::https::Timeout=20" in prep
    assert "Acquire::ForceIPv4=true" in prep
    assert " update && sudo env DEBIAN_FRONTEND=noninteractive apt -o " in prep
    assert any('mkdir -p "$HOME/.config" "$HOME/.local" "$HOME/snap"' in c for c in commands)


def test_ubuntu_apt_install_injects_component_sources_before_package_resolution():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="debian",
        os_id="ubuntu",
        package_manager="apt",
        current_user="jan",
        login_user="jan",
        sudo_nopasswd=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="vbox-drivers",
        stage=SetupStage.POSTBOOT_DRIVER,
        title="Install VirtualBox guest utilities",
        commands=[
            "sudo apt-get install -y virtualbox-guest-utils virtualbox-guest-x11 dkms"
        ],
    )

    commands = orchestrator._commands_for_group(SetupStage.POSTBOOT_DRIVER, group)

    assert "quickhatch-ubuntu-components.list" in commands[0]
    assert "restricted universe multiverse" in commands[0]
    assert "sudo apt-get -o Acquire::Retries=2" in commands[0]
    assert commands[1].startswith(
        "sudo env DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=2"
    )
    assert "install -y virtualbox-guest-utils virtualbox-guest-x11 dkms" in commands[1]


def test_apt_commands_get_bounded_network_timeouts():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="ubuntu",
        package_manager="apt",
        current_user="ubuntu",
        login_user="ubuntu",
        is_live=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="apt-prereqs",
        stage=SetupStage.INSTALL_OR_SKIP,
        title="Install debootstrap prerequisites",
        commands=["sudo apt-get update && sudo apt-get install -y debootstrap gdisk"],
    )

    commands = orchestrator._commands_for_group(SetupStage.INSTALL_OR_SKIP, group)

    apt_command = next(command for command in commands if "apt-get" in command)
    assert apt_command.count("Acquire::Retries=2") == 2
    assert apt_command.count("Acquire::http::Timeout=20") == 2
    assert apt_command.count("Acquire::https::Timeout=20") == 2
    assert apt_command.count("Acquire::ForceIPv4=true") == 2
    assert " update && sudo env DEBIAN_FRONTEND=noninteractive apt-get -o " in apt_command
    assert " install -y debootstrap gdisk" in apt_command


def test_apt_mutating_commands_get_noninteractive_frontend():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="debian",
        package_manager="apt",
        current_user="ada",
        login_user="ada",
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="desktop",
        stage=SetupStage.DESKTOP,
        title="Install XFCE",
        commands=["sudo apt install -y task-xfce-desktop lightdm"],
    )

    commands = orchestrator._commands_for_group(SetupStage.DESKTOP, group)

    assert any(
        command.startswith("sudo env DEBIAN_FRONTEND=noninteractive apt -o ")
        and " install -y task-xfce-desktop lightdm" in command
        for command in commands
    )


def test_desktop_cosmetic_package_verification_becomes_manual_followup():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        login_user="jan",
        sudo_nopasswd=True,
    )
    commands_seen: list[str] = []

    def ssh_exec(command, timeout):
        commands_seen.append(command)
        if "sweet-kde-theme-mars-git" in command:
            return (1, "", "")
        return (0, "", "")

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    )
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="neon-theme",
        stage=SetupStage.DESKTOP,
        title="Neon Aesthetic Deployment",
        commands=["true"],
        verification=["sudo pacman -Qm | grep -q sweet-kde-theme-mars-git"],
        packages=["sweet-kde-theme-mars-git", "candy-icons-git"],
    )
    stage_state: dict[str, object] = {}

    failure = orchestrator._execute_group(SetupStage.DESKTOP, group, stage_state)

    assert failure is None
    assert commands_seen == ["true", "sudo pacman -Qm | grep -q sweet-kde-theme-mars-git"]
    assert stage_state["failed_verifications"] == []
    assert stage_state["manual_followups"]
    assert "sweet-kde-theme-mars-git" in stage_state["manual_followups"][0]["reason"]
    assert any("manual follow-up" in message["content"] for message in bridge.messages)


def test_desktop_cosmetic_asset_verification_becomes_manual_followup():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        login_user="jan",
        sudo_nopasswd=True,
    )
    commands_seen: list[str] = []

    def ssh_exec(command, timeout):
        commands_seen.append(command)
        if "look-and-feel" in command:
            return (1, "", "missing theme id")
        return (0, "", "")

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    )
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="neon-assets",
        stage=SetupStage.DESKTOP,
        title="Neon Aesthetic Assets",
        commands=["true"],
        verification=["ls /usr/share/plasma/look-and-feel/ | grep -i Sweet-Mars"],
        packages=["sweet-kde-theme-git", "candy-icons-git"],
    )
    stage_state: dict[str, object] = {}

    failure = orchestrator._execute_group(SetupStage.DESKTOP, group, stage_state)

    assert failure is None
    assert commands_seen == ["true", "ls /usr/share/plasma/look-and-feel/ | grep -i Sweet-Mars"]
    assert stage_state["failed_verifications"] == []
    assert stage_state["manual_followups"]
    assert "look-and-feel" in stage_state["manual_followups"][0]["verification"]
    assert any("Cosmetic desktop verification" in message["content"] for message in bridge.messages)


def test_core_desktop_package_verification_still_blocks():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        login_user="jan",
        sudo_nopasswd=True,
    )

    def ssh_exec(command, timeout):
        if "plasma-desktop" in command:
            return (1, "", "missing")
        return (0, "", "")

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    )
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="desktop-core",
        stage=SetupStage.DESKTOP,
        title="KDE Plasma Core",
        commands=["true"],
        verification=["pacman -Qi plasma-desktop"],
        packages=["plasma-desktop"],
    )

    failure = orchestrator._execute_group(SetupStage.DESKTOP, group, {})

    assert failure is not None
    assert failure.failure_type == "verification"
    assert "plasma-desktop" in failure.command


def test_final_package_verification_accepts_known_package_aliases():
    profile = UserProfile(apps_to_install=["Brave", "Mullvad VPN"])
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(distro_family="arch", package_manager="pacman", current_user="jan")

    def ssh_exec(command, timeout):
        assert command == "pacman -Qq"
        assert timeout == 45
        return (0, "brave-bin\nmullvad-vpn-bin\n", "")

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    )
    orchestrator.ctx.facts = facts
    orchestrator.ctx.plan = ResearchPlan(
        target_summary="Arch workstation",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision(strategy="none"),
        desktop_strategy=StrategyDecision(strategy="none"),
        apps=[
            AppPlanItem(
                name="Brave",
                package_name="brave-browser",
                source="community",
                status=PlanItemStatus.INSTALLED,
                required=True,
            ),
            AppPlanItem(
                name="Mullvad VPN",
                package_name="mullvad-vpn",
                source="community",
                status=PlanItemStatus.INSTALLED,
                required=True,
            ),
        ],
    )

    assert orchestrator._verify_planned_packages_installed() == []


def test_final_package_verification_accepts_external_repo_sources():
    profile = UserProfile(apps_to_install=["Git", "VLC media player", "Brave"])
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(distro_family="fedora", package_manager="dnf", current_user="jan")

    def ssh_exec(command, timeout):
        assert command == "rpm -qa --queryformat '%{NAME}\\n'"
        assert timeout == 45
        return (0, "git\nvlc\nbrave-browser\n", "")

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    )
    orchestrator.ctx.facts = facts
    orchestrator.ctx.plan = ResearchPlan(
        target_summary="Fedora workstation",
        distro_family="fedora",
        package_manager="dnf",
        driver_strategy=StrategyDecision(strategy="none"),
        desktop_strategy=StrategyDecision(strategy="none"),
        apps=[
            AppPlanItem(
                name="Git",
                package_name="git",
                source="repo",
                status=PlanItemStatus.INSTALLED,
                required=True,
            ),
            AppPlanItem(
                name="VLC media player",
                package_name="vlc",
                source="repo_rpmfusion",
                status=PlanItemStatus.INSTALLED,
                required=True,
            ),
            AppPlanItem(
                name="Brave",
                package_name="brave-browser",
                source="brave_repo",
                status=PlanItemStatus.INSTALLED,
                required=True,
            ),
        ],
    )

    assert orchestrator._verify_planned_packages_installed() == []
    final_state = orchestrator.ctx.stage_state[SetupStage.FINAL_AUDIT.value]
    assert final_state["verified_installed_apps"] == ["Brave", "Git", "VLC media player"]
    assert final_state["unverified_installed_apps"] == []


def test_final_package_verification_accepts_user_flatpak_apps():
    profile = UserProfile(apps_to_install=["Firefox", "VLC"])
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(distro_family="fedora", package_manager="dnf", current_user="jan")
    commands: list[str] = []

    def ssh_exec(command, timeout):
        commands.append(command)
        assert timeout == 45
        if "rpm -qa" in command:
            return (0, "firefox\nflatpak\n", "")
        if "flatpak list" in command:
            return (0, "org.videolan.VLC\n", "")
        raise AssertionError(f"unexpected command: {command}")

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    )
    orchestrator.ctx.facts = facts
    orchestrator.ctx.plan = ResearchPlan(
        target_summary="Fedora workstation",
        distro_family="fedora",
        package_manager="dnf",
        driver_strategy=StrategyDecision(strategy="none"),
        desktop_strategy=StrategyDecision(strategy="none"),
        apps=[
            AppPlanItem(
                name="Firefox",
                package_name="firefox",
                source="repo",
                status=PlanItemStatus.INSTALLED,
                required=True,
            ),
            AppPlanItem(
                name="VLC",
                package_name="org.videolan.VLC",
                source="flatpak",
                status=PlanItemStatus.INSTALLED,
                required=True,
            ),
        ],
    )

    assert orchestrator._verify_planned_packages_installed() == []
    final_state = orchestrator.ctx.stage_state[SetupStage.FINAL_AUDIT.value]
    assert final_state["verified_installed_apps"] == ["Firefox", "VLC"]
    assert final_state["unverified_installed_apps"] == []
    assert any("flatpak list --app --columns=application --user" in cmd for cmd in commands)


def test_desktop_display_audit_blocks_resolution_mismatch():
    profile = UserProfile()
    profile.hardware.displays = [
        DisplayInfo(
            name="Primary 4K",
            width_px=3840,
            height_px=2160,
            refresh_hz=60,
            primary=True,
        )
    ]
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        current_user="jan",
        login_user="jan",
        displays=[DisplayRuntimeInfo(name="HDMI-A-1", current_mode="1920x1080", refresh_hz=60)],
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    state = {"persisted_config_written": True}

    orchestrator._update_desktop_display_audit_state(state)
    result = audit_stage(SetupStage.DESKTOP, facts, stage_state=state)

    assert not result.passed
    assert any("Display mode mismatch" in blocker for blocker in result.blockers)


def test_desktop_commands_record_kde_design_contract():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(distro_family="arch", package_manager="pacman", current_user="jan")
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))

    orchestrator._update_from_command(
        "kwriteconfig6 --file kdeglobals --group Icons --key Theme candy-icons && "
        "kwriteconfig6 --file kwinrc --group Plugins --key wobblywindowsEnabled true && "
        "plasma-apply-wallpaperimage /home/jan/Pictures/masterpiece.png",
        "",
        "",
        stage=SetupStage.DESKTOP,
    )

    state = orchestrator.ctx.stage_state[SetupStage.DESKTOP.value]
    assert state["expected_kde_icon_theme"] == "candy-icons"
    assert state["expected_kde_wobbly_windows"] == "true"
    assert state["expected_kde_wallpaper"] == "/home/jan/Pictures/masterpiece.png"


def test_same_user_wrapper_strip_preserves_shell_pipelines():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(distro_family="arch", package_manager="pacman", current_user="linus")
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts

    command = (
        "sudo -u linus kreadconfig6 --file kdeglobals --group Icons --key Theme "
        "| grep -q Papirus-Dark"
    )

    assert (
        orchestrator._strip_same_user_execution_wrapper(command)
        == "kreadconfig6 --file kdeglobals --group Icons --key Theme | grep -q Papirus-Dark"
    )


def test_desktop_run_prompts_on_display_mismatch_then_manual_followup(monkeypatch):
    profile = UserProfile()
    profile.hardware.displays = [
        DisplayInfo(
            name="Primary 4K",
            width_px=3840,
            height_px=2160,
            refresh_hz=60,
            primary=True,
        )
    ]
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        current_user="jan",
        login_user="jan",
        displays=[DisplayRuntimeInfo(name="HDMI-A-1", current_mode="2259x1271", refresh_hz=60)],
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    orchestrator.ctx.plan = ResearchPlan(
        target_summary="test setup",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision("none"),
        desktop_strategy=StrategyDecision("kde"),
        command_groups=[],
    )
    orchestrator.ctx.stage_state[SetupStage.DESKTOP.value] = {"persisted_config_written": True}
    questions: list[str] = []

    def ask(question, **kwargs):
        questions.append(question)
        return "manual follow-up"

    monkeypatch.setattr(orchestrator, "_ask_blocking_question", ask)

    orchestrator._run_desktop()

    result = orchestrator.ctx.stage_results[-1]
    state = orchestrator.ctx.stage_state[SetupStage.DESKTOP.value]
    assert result.stage == SetupStage.DESKTOP
    assert result.passed
    assert state["display_manual_followup"] is True
    assert state["display_preference_blockers"] == []
    assert questions and "Display mode mismatch" in questions[0]
    assert any("Display tuning left for manual follow-up" in m["content"] for m in bridge.messages)


def test_desktop_run_retry_rechecks_display_mismatch(monkeypatch):
    profile = UserProfile()
    profile.hardware.displays = [
        DisplayInfo(
            name="Primary 4K",
            width_px=3840,
            height_px=2160,
            refresh_hz=60,
            primary=True,
        )
    ]
    bridge = BridgeState(profile, persist_path=None)
    stale = SetupFacts(
        current_user="jan",
        login_user="jan",
        displays=[DisplayRuntimeInfo(name="HDMI-A-1", current_mode="2259x1271", refresh_hz=60)],
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
    )
    fresh = SetupFacts(
        current_user="jan",
        login_user="jan",
        displays=[DisplayRuntimeInfo(name="HDMI-A-1", current_mode="3840x2160", refresh_hz=60)],
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, stale))
    orchestrator.ctx.facts = stale
    orchestrator.ctx.plan = ResearchPlan(
        target_summary="test setup",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision("none"),
        desktop_strategy=StrategyDecision("kde"),
        command_groups=[],
    )
    orchestrator.ctx.stage_state[SetupStage.DESKTOP.value] = {"persisted_config_written": True}

    def refresh(stage):
        orchestrator.ctx.facts = fresh

    monkeypatch.setattr(orchestrator, "_ask_blocking_question", lambda *args, **kwargs: "retry")
    monkeypatch.setattr(orchestrator, "_refresh_facts", refresh)

    orchestrator._run_desktop()

    result = orchestrator.ctx.stage_results[-1]
    state = orchestrator.ctx.stage_state[SetupStage.DESKTOP.value]
    assert result.passed
    assert state["display_preference_blockers"] == []
    assert not state.get("display_manual_followup")


def test_desktop_display_audit_accepts_matching_resolution_and_refresh():
    profile = UserProfile()
    profile.hardware.displays = [
        DisplayInfo(
            name="Primary 4K",
            width_px=3840,
            height_px=2160,
            refresh_hz=60,
            primary=True,
        )
    ]
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        current_user="jan",
        login_user="jan",
        displays=[DisplayRuntimeInfo(name="HDMI-A-1", current_mode="3840x2160", refresh_hz=59.95)],
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    state = {"persisted_config_written": True}

    orchestrator._update_desktop_display_audit_state(state)
    result = audit_stage(SetupStage.DESKTOP, facts, stage_state=state)

    assert result.passed
    assert state["display_preference_blockers"] == []


def test_desktop_display_audit_blocks_unverified_display_preferences():
    profile = UserProfile()
    profile.hardware.displays = [
        DisplayInfo(
            name="Primary 4K",
            width_px=3840,
            height_px=2160,
            refresh_hz=60,
            primary=True,
        )
    ]
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        current_user="jan",
        login_user="jan",
        display_manager_enabled=False,
        desktop_runtime_state=DesktopRuntimeState.BLOCKED_NO_GUI_SESSION,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    state = {"persisted_config_written": True}

    orchestrator._update_desktop_display_audit_state(state)
    result = audit_stage(SetupStage.DESKTOP, facts, stage_state=state)

    assert not result.passed
    assert any("could not be runtime-verified" in blocker for blocker in result.blockers)


def test_desktop_runtime_commands_are_wrapped_with_real_gui_environment():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        current_user="jan",
        login_user="jan",
        raw={"QH_GUI_USER": "jan"},
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts

    wrapped = orchestrator._wrap_desktop_runtime_command(
        "plasma-apply-wallpaperimage /home/jan/wallpaper.jpg",
        stage=SetupStage.DESKTOP,
    )

    assert wrapped.startswith("bash -lc ")
    assert "plasmashell" in wrapped
    assert "DBUS_SESSION_BUS_ADDRESS" in wrapped
    assert "plasma-apply-wallpaperimage" in wrapped


def test_desktop_runtime_group_prompts_then_manual_followup_when_no_display(monkeypatch):
    profile = UserProfile()
    profile.hardware.displays = [
        DisplayInfo(
            name="Primary 4K",
            width_px=3840,
            height_px=2160,
            refresh_hz=60,
            primary=True,
        )
    ]
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        current_user="jan",
        login_user="jan",
        display_manager_enabled=True,
        display_manager_active=True,
        desktop_runtime_state=DesktopRuntimeState.PENDING_RELOGIN,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    monkeypatch.setattr(orchestrator, "_refresh_facts", lambda stage: facts)
    monkeypatch.setattr(
        orchestrator,
        "_ask_blocking_question",
        lambda *args, **kwargs: "manual follow-up",
    )
    group = CommandGroup(
        id="live-theme",
        stage=SetupStage.DESKTOP,
        title="Apply live wallpaper",
        commands=["plasma-apply-wallpaperimage /home/jan/wallpaper.jpg"],
    )
    state = {"persisted_config_written": True}

    assert not orchestrator._desktop_runtime_ready_or_followup(group, state)
    orchestrator._update_desktop_display_audit_state(state)
    result = audit_stage(SetupStage.DESKTOP, facts, stage_state=state)

    assert state["display_manual_followup"]
    assert state["display_preference_blockers"] == []
    assert result.passed


def test_desktop_package_group_does_not_require_runtime_display(monkeypatch):
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        current_user="jan",
        login_user="jan",
        display_manager_enabled=True,
        display_manager_active=True,
        desktop_runtime_state=DesktopRuntimeState.PENDING_RELOGIN,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    asked = {"value": False}
    monkeypatch.setattr(orchestrator, "_refresh_facts", lambda stage: facts)
    monkeypatch.setattr(
        orchestrator,
        "_ask_blocking_question",
        lambda *args, **kwargs: asked.__setitem__("value", True),
    )
    group = CommandGroup(
        id="desktop-packages",
        stage=SetupStage.DESKTOP,
        title="Install KDE Plasma",
        commands=["sudo pacman -S --noconfirm plasma-desktop sddm"],
    )

    assert orchestrator._desktop_runtime_ready_or_followup(group, {})
    assert asked["value"] is False


def test_gsettings_desktop_group_counts_as_persistent_not_runtime_only(monkeypatch):
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        current_user="jan",
        login_user="jan",
        desktop_runtime_state=DesktopRuntimeState.PENDING_RELOGIN,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    asked = {"value": False}
    monkeypatch.setattr(orchestrator, "_refresh_facts", lambda stage: facts)
    monkeypatch.setattr(
        orchestrator,
        "_ask_blocking_question",
        lambda *args, **kwargs: asked.__setitem__("value", True),
    )
    group = CommandGroup(
        id="gnome-persist",
        stage=SetupStage.DESKTOP,
        title="GNOME Persisted Preferences",
        commands=[
            "gsettings set org.gnome.desktop.interface color-scheme 'prefer-dark'",
        ],
    )

    assert orchestrator._desktop_runtime_ready_or_followup(group, {})
    assert orchestrator._desktop_runtime_command_ready_or_followup(
        group.commands[0],
        group,
        {},
        item_kind="command",
    )
    assert asked["value"] is False


def test_mixed_desktop_group_skips_only_runtime_items_without_display(monkeypatch):
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        login_user="jan",
        display_manager_enabled=True,
        display_manager_active=True,
        desktop_runtime_state=DesktopRuntimeState.PENDING_RELOGIN,
    )
    executed: list[str] = []

    def ssh_exec(command, _timeout):
        executed.append(command)
        return (0, "", "")

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    )
    orchestrator.ctx.facts = facts
    monkeypatch.setattr(orchestrator, "_refresh_facts", lambda stage: facts)
    monkeypatch.setattr(
        orchestrator,
        "_ask_blocking_question",
        lambda *args, **kwargs: "manual follow-up",
    )
    group = CommandGroup(
        id="kde-mixed",
        stage=SetupStage.DESKTOP,
        title="KDE Install and Design",
        commands=[
            "sudo pacman -S --noconfirm plasma-desktop sddm",
            "sudo systemctl enable sddm",
            "plasma-apply-wallpaperimage /home/jan/wallpaper.jpg",
        ],
        verification=[
            "systemctl is-enabled sddm",
            "kscreen-doctor -o",
        ],
    )
    state: dict[str, object] = {}
    orchestrator.ctx.stage_state[SetupStage.DESKTOP.value] = state

    failure = orchestrator._execute_group(SetupStage.DESKTOP, group, state)

    assert failure is None
    assert any("pacman -S" in command for command in executed)
    assert any("systemctl enable sddm" in command for command in executed)
    assert any("systemctl is-enabled sddm" in command for command in executed)
    assert not any("plasma-apply-wallpaperimage" in command for command in executed)
    assert not any("kscreen-doctor" in command for command in executed)
    assert state["persisted_config_written"]
    assert state["display_manual_followup"]
    skipped = state["skipped_runtime_desktop_items"]
    assert any(item["kind"] == "command" for item in skipped)
    assert any(item["kind"] == "verification" for item in skipped)


def test_desktop_stage_starts_enabled_inactive_display_manager():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    before = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        init_system="systemd",
        current_user="linus",
        login_user="linus",
        display_manager="gdm",
        display_manager_enabled=True,
        display_manager_active=False,
        desktop_runtime_state=DesktopRuntimeState.BLOCKED_NO_GUI_SESSION,
    )
    after = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        init_system="systemd",
        current_user="linus",
        login_user="linus",
        display_manager="gdm",
        display_manager_enabled=True,
        display_manager_active=True,
        desktop_runtime_state=DesktopRuntimeState.PENDING_RELOGIN,
    )
    commands: list[str] = []

    def ssh_exec(command, _timeout):
        commands.append(command)
        return (0, "", "")

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(
            profile,
            bridge,
            before,
            ssh_exec=ssh_exec,
            probe_facts=lambda **kwargs: after,
        )
    )
    orchestrator.ctx.facts = before

    orchestrator._ensure_display_manager_started()

    assert any("systemctl start display-manager.service" in command for command in commands)
    assert orchestrator.ctx.facts.display_manager_active is True


def test_desktop_stage_polls_after_starting_display_manager(monkeypatch):
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    before = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        init_system="systemd",
        current_user="linus",
        login_user="linus",
        display_manager="sddm",
        display_manager_enabled=True,
        display_manager_active=False,
        desktop_runtime_state=DesktopRuntimeState.BLOCKED_NO_GUI_SESSION,
    )
    still_starting = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        init_system="systemd",
        current_user="linus",
        login_user="linus",
        display_manager="sddm",
        display_manager_enabled=True,
        display_manager_active=False,
        desktop_runtime_state=DesktopRuntimeState.BLOCKED_NO_GUI_SESSION,
    )
    active = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        init_system="systemd",
        current_user="linus",
        login_user="linus",
        display_manager="sddm",
        display_manager_enabled=True,
        display_manager_active=True,
        desktop_runtime_state=DesktopRuntimeState.PENDING_RELOGIN,
    )
    probes = iter([still_starting, active])

    monkeypatch.setattr("quickhatch.setup.orchestrator.time.sleep", lambda _seconds: None)

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(
            profile,
            bridge,
            before,
            ssh_exec=lambda _command, _timeout: (0, "", ""),
            probe_facts=lambda **kwargs: next(probes),
        )
    )
    orchestrator.ctx.facts = before

    orchestrator._ensure_display_manager_started()

    assert orchestrator.ctx.facts.display_manager_active is True


def test_fstab_mount_verification_uses_field_aware_check():
    command = SetupOrchestrator._normalize_fstab_mount_verification("grep ' / ' /mnt/etc/fstab")

    assert command.startswith("awk ")
    assert '$2 == "/"' in command
    assert command.endswith("/mnt/etc/fstab")


def test_fstab_boot_verification_uses_field_aware_check():
    command = SetupOrchestrator._normalize_fstab_mount_verification(
        "grep -q ' /boot ' /mnt/etc/fstab"
    )

    assert command.startswith("awk ")
    assert '$2 == "/boot"' in command


def test_standalone_assignment_combines_with_next_command():
    commands = SetupOrchestrator._combine_standalone_assignments(
        [
            "UCODE=$(grep -qi intel /proc/cpuinfo && echo intel-ucode || echo amd-ucode)",
            'pacstrap -K /mnt base linux "$UCODE"',
        ]
    )

    assert commands == [
        'UCODE=$(grep -qi intel /proc/cpuinfo && echo intel-ucode || echo amd-ucode) && pacstrap -K /mnt base linux "$UCODE"'
    ]


def test_pre_reboot_checks_retry_transient_ssh_disconnect(monkeypatch):
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(current_user="root", login_user="root", is_root=True)
    attempts = {"count": 0}
    monkeypatch.setattr("quickhatch.setup.orchestrator.time.sleep", lambda _seconds: None)

    def run_pre_reboot_checks():
        attempts["count"] += 1
        if attempts["count"] == 1:
            return (
                False,
                ["pre-reboot check script failed to run"],
                "FAIL pre-reboot check script failed to run (rc=255): Connection closed by 127.0.0.1 port 2222",
            )
        return (True, [], "ok")

    runtime = _dummy_setup_runtime(profile, bridge, facts)
    runtime.run_pre_reboot_checks = run_pre_reboot_checks
    orchestrator = SetupOrchestrator(runtime)

    ok, failures, summary = orchestrator._run_pre_reboot_checks_with_retries()

    assert ok is True
    assert failures == []
    assert summary == "ok"
    assert attempts["count"] == 2
    assert any("transient SSH disconnect" in msg["content"] for msg in bridge.messages)


def test_pre_reboot_checks_tolerate_extended_transient_ssh_disconnects(monkeypatch):
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(current_user="root", login_user="root", is_root=True)
    attempts = {"count": 0}
    monkeypatch.setattr("quickhatch.setup.orchestrator.time.sleep", lambda _seconds: None)

    def run_pre_reboot_checks():
        attempts["count"] += 1
        if attempts["count"] < 6:
            return (
                False,
                ["pre-reboot check script failed to run"],
                "FAIL pre-reboot check script failed to run (rc=255): Connection closed by 127.0.0.1 port 2222",
            )
        return (True, [], "ok")

    runtime = _dummy_setup_runtime(profile, bridge, facts)
    runtime.run_pre_reboot_checks = run_pre_reboot_checks
    orchestrator = SetupOrchestrator(runtime)

    ok, failures, summary = orchestrator._run_pre_reboot_checks_with_retries()

    assert ok is True
    assert failures == []
    assert summary == "ok"
    assert attempts["count"] == 6
    assert any("retrying (6/8)" in msg["content"] for msg in bridge.messages)


def test_pre_reboot_checks_do_not_retry_real_boot_failures(monkeypatch):
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(current_user="root", login_user="root", is_root=True)
    attempts = {"count": 0}
    monkeypatch.setattr("quickhatch.setup.orchestrator.time.sleep", lambda _seconds: None)

    def run_pre_reboot_checks():
        attempts["count"] += 1
        return (False, ["[kernel] missing"], "kernel missing")

    runtime = _dummy_setup_runtime(profile, bridge, facts)
    runtime.run_pre_reboot_checks = run_pre_reboot_checks
    orchestrator = SetupOrchestrator(runtime)

    ok, failures, summary = orchestrator._run_pre_reboot_checks_with_retries()

    assert ok is False
    assert failures == ["[kernel] missing"]
    assert summary == "kernel missing"
    assert attempts["count"] == 1


def test_arch_nvidia_group_rewrites_legacy_driver_install_and_adds_boot_config():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        gpu_vendors=["nvidia"],
        current_user="jan",
        login_user="jan",
        sudo_nopasswd=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="driver",
        stage=SetupStage.POSTBOOT_DRIVER,
        title="NVIDIA Proprietary Driver Setup",
        commands=[
            "sudo pacman -Syu --noconfirm nvidia nvidia-utils lib32-nvidia-utils nvidia-settings",
            "export KERNEL_PKG=$(pacman -Q linux | awk '{print $1}')",
            'export NV_PKG=$([ "$KERNEL_PKG" = "linux" ] && echo "nvidia" || echo "nvidia-dkms")',
            "sudo pacman -S --noconfirm $NV_PKG nvidia-utils",
        ],
        packages=["nvidia"],
    )

    commands = orchestrator._commands_for_group(SetupStage.POSTBOOT_DRIVER, group)

    assert commands[0] == (
        "sudo pacman -Syu --needed --noconfirm "
        "linux-headers nvidia-open nvidia-utils lib32-nvidia-utils nvidia-settings"
    )
    assert not any("$NV_PKG" in command for command in commands)
    assert not any(" --noconfirm nvidia " in command for command in commands)
    assert any("blacklist nouveau" in command for command in commands)
    assert any("mkinitcpio -P" in command for command in commands)
    assert any("grub-mkconfig" in command for command in commands)


def test_arch_nvidia_group_does_not_rewrite_pre_turing_gpu_to_default_packages():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        gpu_vendors=["nvidia"],
        gpu_inventory=["NVIDIA GeForce GTX 1080"],
        current_user="jan",
        login_user="jan",
        sudo_nopasswd=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="driver",
        stage=SetupStage.POSTBOOT_DRIVER,
        title="NVIDIA Driver Setup",
        commands=["sudo pacman -S --noconfirm nvidia-open nvidia-utils"],
        packages=["nvidia-open"],
    )

    commands = orchestrator._commands_for_group(SetupStage.POSTBOOT_DRIVER, group)

    assert commands == ["sudo pacman -S --noconfirm nvidia-open nvidia-utils"]


def test_arch_nvidia_group_keeps_open_stack_for_rtx_gpu():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        gpu_vendors=["nvidia"],
        gpu_inventory=["NVIDIA GA102 [GeForce RTX 3080]"],
        current_user="jan",
        login_user="jan",
        sudo_nopasswd=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="driver",
        stage=SetupStage.POSTBOOT_DRIVER,
        title="NVIDIA Driver Setup",
        commands=["sudo pacman -S --noconfirm nvidia nvidia-utils"],
        packages=["nvidia"],
    )

    commands = orchestrator._commands_for_group(SetupStage.POSTBOOT_DRIVER, group)

    assert "linux-headers nvidia-open nvidia-utils" in commands[0]


def test_arch_nvidia_group_uses_lts_open_package_for_lts_kernel():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        kernel_release="6.18.1-1-lts",
        gpu_vendors=["nvidia"],
        gpu_inventory=["NVIDIA GeForce RTX 3080"],
        current_user="jan",
        login_user="jan",
        sudo_nopasswd=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="driver",
        stage=SetupStage.POSTBOOT_DRIVER,
        title="NVIDIA Driver Setup",
        commands=["sudo pacman -S --noconfirm nvidia-open nvidia-utils"],
        packages=["nvidia-open"],
    )

    commands = orchestrator._commands_for_group(SetupStage.POSTBOOT_DRIVER, group)

    assert "linux-lts-headers nvidia-lts-open nvidia-utils" in commands[0]


def test_arch_nvidia_group_uses_open_dkms_for_zen_kernel():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        kernel_release="6.18.1-zen1-1-zen",
        gpu_vendors=["nvidia"],
        gpu_inventory=["NVIDIA GeForce RTX 3080"],
        current_user="jan",
        login_user="jan",
        sudo_nopasswd=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="driver",
        stage=SetupStage.POSTBOOT_DRIVER,
        title="NVIDIA Driver Setup",
        commands=["sudo pacman -S --noconfirm nvidia-open nvidia-utils"],
        packages=["nvidia-open"],
    )

    commands = orchestrator._commands_for_group(SetupStage.POSTBOOT_DRIVER, group)

    assert "linux-zen-headers nvidia-open-dkms nvidia-utils" in commands[0]


def test_arch_nvidia_stage_reboots_once_when_nouveau_is_bound_before_verification():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    fact_state = {
        "facts": SetupFacts(
            distro_family="arch",
            package_manager="pacman",
            gpu_vendors=["nvidia"],
            current_user="jan",
            login_user="jan",
            sudo_nopasswd=True,
            nvidia_smi_state="present_but_failing",
            nvidia_kernel_driver="nouveau",
            nvidia_nouveau_bound=True,
        )
    }
    commands: list[str] = []

    def ssh_exec(command, timeout):
        commands.append(command)
        return (0, "", "")

    def wait_for_reconnect(reconnect_user):
        fact_state["facts"] = SetupFacts(
            distro_family="arch",
            package_manager="pacman",
            gpu_vendors=["nvidia"],
            current_user=reconnect_user,
            login_user=reconnect_user,
            sudo_nopasswd=True,
            nvidia_smi_state="ok",
            nvidia_kernel_driver="nvidia",
            nvidia_nouveau_bound=False,
            driver_modules=["nvidia", "nvidia_drm"],
        )
        return (True, "fresh info", reconnect_user)

    runtime = _dummy_setup_runtime(
        profile,
        bridge,
        fact_state["facts"],
        ssh_exec=ssh_exec,
        wait_for_reconnect=wait_for_reconnect,
        probe_facts=lambda **kwargs: fact_state["facts"],
    )
    orchestrator = SetupOrchestrator(runtime)
    orchestrator.ctx.facts = fact_state["facts"]
    stage_state: dict[str, object] = {}
    group = CommandGroup(
        id="driver",
        stage=SetupStage.POSTBOOT_DRIVER,
        title="NVIDIA Proprietary Driver Setup",
        commands=["sudo pacman -S --noconfirm nvidia-open nvidia-utils"],
        verification=["nvidia-smi"],
        packages=["nvidia-open"],
    )

    orchestrator._maybe_reboot_for_nvidia_driver(SetupStage.POSTBOOT_DRIVER, group, stage_state)
    orchestrator._maybe_reboot_for_nvidia_driver(SetupStage.POSTBOOT_DRIVER, group, stage_state)

    reboot_commands = [command for command in commands if "reboot" in command]
    assert reboot_commands == ["sudo systemctl enable sshd NetworkManager || true; sudo reboot"]
    assert stage_state["nvidia_reboot_attempted"] is True
    assert orchestrator.ctx.facts.nvidia_smi_state == "ok"


def test_live_install_bootstraps_nopasswd_sudo_after_reconnect(tmp_path):
    profile = UserProfile()
    pubkey = tmp_path / "quickhatch.pub"
    pubkey.write_text("ssh-ed25519 AAAATEST quickhatch\n", encoding="utf-8")
    profile.ssh_public_key_path = str(pubkey)
    bridge = BridgeState(profile, persist_path=None)
    initial_facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="root",
        login_user="root",
        is_root=True,
        is_live=True,
        live_marker="LIVE:marker=/run/archiso",
    )
    installed_facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        init_system="systemd",
        current_user="jan",
        login_user="jan",
        is_live=False,
        sudo_nopasswd=False,
    )
    fact_state = {"facts": initial_facts}
    bootstrap_calls: list[str] = []

    def wait_for_reconnect(reconnect_user):
        fact_state["facts"] = installed_facts
        return True, "fresh installed system", reconnect_user

    def bootstrap(facts):
        bootstrap_calls.append(facts.current_user)
        facts.sudo_nopasswd = True
        return facts

    runtime = _dummy_setup_runtime(
        profile,
        bridge,
        initial_facts,
        ssh_exec=lambda command, timeout: (0, "", ""),
        wait_for_reconnect=wait_for_reconnect,
        probe_facts=lambda **kwargs: fact_state["facts"],
    )
    runtime.bootstrap_nopasswd_sudo = bootstrap
    orchestrator = SetupOrchestrator(runtime)
    orchestrator.ctx.facts = initial_facts
    orchestrator.ctx.created_user = "jan"
    orchestrator.ctx.plan = ResearchPlan(
        target_summary="Arch VM",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision(strategy="none"),
        desktop_strategy=StrategyDecision(strategy="none"),
    )

    orchestrator._run_install_or_skip()

    assert bootstrap_calls == ["jan"]
    assert orchestrator.ctx.facts is not None
    assert orchestrator.ctx.facts.sudo_nopasswd is True


def test_arch_aur_helper_bootstrap_is_injected_before_yay_when_missing():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="none",
        current_user="jan",
        login_user="jan",
        sudo_nopasswd=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="theme",
        stage=SetupStage.DESKTOP,
        title="Apply theme",
        commands=["yay -S --noconfirm sweet-mars-kde-theme-git candy-icons-git"],
        packages=["sweet-mars-kde-theme-git", "candy-icons-git"],
    )

    commands = orchestrator._commands_for_group(SetupStage.DESKTOP, group)

    assert "yay-bin.git" in commands[0]
    assert "timedatectl set-ntp true" in commands[0]
    assert "makepkg -s --noconfirm" in commands[0]
    assert commands[1] == "yay -S --noconfirm sweet-mars-kde-theme-git candy-icons-git"


def test_arch_aur_helper_bootstrap_updates_facts_after_success():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="none",
        current_user="jan",
        login_user="jan",
        sudo_nopasswd=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts

    bootstrap = orchestrator._commands_for_group(
        SetupStage.DESKTOP,
        CommandGroup(
            id="theme",
            stage=SetupStage.DESKTOP,
            title="Apply theme",
            commands=["yay -S --noconfirm candy-icons-git"],
        ),
    )[0]
    orchestrator._update_from_command(bootstrap, "", "", stage=SetupStage.DESKTOP)

    assert orchestrator.ctx.facts.community_helper == "yay"
    assert orchestrator._commands_for_group(
        SetupStage.DESKTOP,
        CommandGroup(
            id="theme",
            stage=SetupStage.DESKTOP,
            title="Apply theme",
            commands=["yay -S --noconfirm candy-icons-git"],
        ),
    ) == ["yay -S --noconfirm candy-icons-git"]


def test_arch_aur_helper_bootstrap_is_root_safe_and_wraps_yay_commands():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="none",
        current_user="root",
        login_user="root",
        is_root=True,
        sudo_nopasswd=True,
    )
    commands_seen: list[str] = []

    def ssh_exec(command, timeout):
        commands_seen.append(command)
        return (0, "", "")

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    )
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="theme",
        stage=SetupStage.DESKTOP,
        title="Apply theme",
        commands=["yay -S --noconfirm candy-icons-git"],
        packages=["candy-icons-git"],
    )

    failure = orchestrator._execute_group(SetupStage.DESKTOP, group, {})

    assert failure is None
    assert "timedatectl set-ntp true" in commands_seen[0]
    assert "useradd -m -G wheel -s /bin/bash quickhatch-build" in commands_seen[0]
    assert "makepkg --noconfirm" in commands_seen[0]
    assert commands_seen[1] == (
        "sudo -u quickhatch-build bash -lc 'yay -S --noconfirm candy-icons-git'"
    )


def test_reconnect_key_install_uses_target_home_numeric_owner(tmp_path):
    profile = UserProfile()
    pubkey = tmp_path / "quickhatch.pub"
    pubkey.write_text("ssh-ed25519 AAAATEST quickhatch\n", encoding="utf-8")
    profile.ssh_public_key_path = str(pubkey)
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="root",
        login_user="root",
        is_root=True,
    )
    commands_seen: list[str] = []

    def ssh_exec(command, timeout):
        commands_seen.append(command)
        return (0, "", "")

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    )
    orchestrator.ctx.created_user = "jan"

    orchestrator._ensure_reconnect_ssh_key()

    assert len(commands_seen) == 1
    assert "stat -c '%u:%g' \"$qh_home\"" in commands_seen[0]
    assert 'chown "$qh_owner"' in commands_seen[0]
    assert "-o jan" not in commands_seen[0]
    assert "-g jan" not in commands_seen[0]


def test_execute_group_bootstraps_yay_when_preflight_sees_raw_yay():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="none",
        current_user="jan",
        login_user="jan",
        sudo_nopasswd=True,
    )
    commands_seen: list[str] = []

    def ssh_exec(command, timeout):
        commands_seen.append(command)
        return (0, "", "")

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    )
    orchestrator.ctx.facts = facts
    # Simulate a future code path/regression that reaches preflight without the
    # normal _commands_for_group bootstrap injection. Preflight recovery must
    # still turn this into bootstrap + retry, not a terminal setup block.
    orchestrator._commands_for_group = lambda stage, group: list(group.commands)  # type: ignore[method-assign]
    group = CommandGroup(
        id="theme",
        stage=SetupStage.DESKTOP,
        title="Apply theme",
        commands=["yay -S --noconfirm sweet-mars-kde-theme-git candy-icons-git"],
        packages=["sweet-mars-kde-theme-git", "candy-icons-git"],
    )

    failure = orchestrator._execute_group(SetupStage.DESKTOP, group, {})

    assert failure is None
    assert "yay-bin.git" in commands_seen[0]
    assert commands_seen[1] == "yay -S --noconfirm sweet-mars-kde-theme-git candy-icons-git"


def test_execute_group_bootstraps_yay_and_rewrites_raw_paru():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="none",
        current_user="jan",
        login_user="jan",
        sudo_nopasswd=True,
    )
    commands_seen: list[str] = []

    def ssh_exec(command, timeout):
        commands_seen.append(command)
        return (0, "", "")

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
    )
    orchestrator.ctx.facts = facts
    orchestrator._commands_for_group = lambda stage, group: list(group.commands)  # type: ignore[method-assign]
    group = CommandGroup(
        id="theme",
        stage=SetupStage.DESKTOP,
        title="Apply theme",
        commands=["paru -S --noconfirm candy-icons-git"],
        packages=["candy-icons-git"],
    )

    failure = orchestrator._execute_group(SetupStage.DESKTOP, group, {})

    assert failure is None
    assert "yay-bin.git" in commands_seen[0]
    assert commands_seen[1] == "yay -S --noconfirm candy-icons-git"


def test_orchestrator_rewrites_paru_to_detected_yay_helper():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        sudo_nopasswd=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="theme",
        stage=SetupStage.DESKTOP,
        title="Apply theme",
        commands=["paru -S --noconfirm candy-icons-git"],
    )

    rewritten = orchestrator._rewrite_arch_community_helper_command(
        "paru -S --noconfirm candy-icons-git",
        stage=SetupStage.DESKTOP,
        group=group,
    )

    assert rewritten == "yay -S --noconfirm candy-icons-git"


def test_orchestrator_blocks_on_required_planning_question():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
    )
    plan_payload = {
        "target_summary": "Arch workstation.",
        "distro_family": "arch",
        "package_manager": "pacman",
        "driver_strategy": {
            "strategy": "no_special_driver",
            "confidence": "resolved_official",
            "source": "official",
            "packages": [],
            "services": [],
            "notes": "",
        },
        "desktop_strategy": {
            "strategy": "plasma",
            "confidence": "resolved_official",
            "source": "official",
            "packages": ["plasma-desktop"],
            "services": ["sddm"],
            "notes": "",
        },
        "command_groups": [
            {
                "id": "desktop-1",
                "stage": "desktop",
                "title": "Persist desktop config",
                "commands": ["echo desktop"],
                "verification": [],
                "rationale": "",
                "source": "repo",
                "packages": [],
            }
        ],
        "apps": [],
        "unresolved_questions": [
            {
                "question": "Install the optional ChatGPT wrapper?",
                "required": True,
                "choices": ["yes", "no"],
                "timeout_seconds": 0.01,
            }
        ],
        "verification_commands": {},
        "required_items": ["desktop"],
        "optional_items": [],
        "planner_revision": 1,
    }
    runtime = SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(provider="api_openai", endpoint="http://example", model_name="test"),
        host="127.0.0.1",
        user="jan",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=lambda cfg, messages: json.dumps(plan_payload),
        probe_facts=lambda **kwargs: facts,
        detect_live_env=lambda: (False, "INSTALLED"),
        ssh_exec=lambda command, timeout: (0, "", ""),
        repo_search=lambda query: "",
        community_search=lambda query: "",
        web_search=lambda query, max_results: "",
        wait_for_reconnect=lambda reconnect_user: (True, "fresh info", reconnect_user),
        run_pre_reboot_checks=lambda: (True, [], ""),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda current, is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=False,
    )

    summary = SetupOrchestrator(runtime).run()

    assert "unanswered required questions" in summary.lower()
    setup_run = bridge.snapshot_setup_run()
    assert setup_run["status"] == "blocked"
    pending = bridge.get_pending_questions()
    assert pending == []


def test_orchestrator_continues_after_answered_required_planning_question():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        session_type="wayland",
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
    )
    plan_payload = {
        "target_summary": "Arch workstation.",
        "distro_family": "arch",
        "package_manager": "pacman",
        "driver_strategy": {
            "strategy": "no_special_driver",
            "confidence": "resolved_official",
            "source": "official",
            "packages": [],
            "services": [],
            "notes": "",
        },
        "desktop_strategy": {
            "strategy": "plasma",
            "confidence": "resolved_official",
            "source": "official",
            "packages": ["plasma-desktop"],
            "services": ["sddm"],
            "notes": "",
        },
        "command_groups": [
            {
                "id": "desktop-1",
                "stage": "desktop",
                "title": "Persist desktop config",
                "commands": [
                    "kwriteconfig6 --file kdeglobals --group Icons --key Theme candy-icons"
                ],
                "verification": [],
                "rationale": "",
                "source": "repo",
                "packages": [],
            }
        ],
        "apps": [],
        "unresolved_questions": [
            {
                "question": "Proceed with KDE?",
                "required": True,
                "choices": ["yes", "no"],
                "timeout_seconds": 2,
            }
        ],
        "verification_commands": {},
        "required_items": ["desktop"],
        "optional_items": [],
        "planner_revision": 1,
    }

    def answer_pending_question():
        deadline = time.time() + 2
        while time.time() < deadline:
            pending = bridge.get_pending_questions()
            if pending:
                bridge.answer_question(pending[0].id, "yes")
                return
            time.sleep(0.01)
        raise AssertionError("Timed out waiting for planning question.")

    helper = threading.Thread(target=answer_pending_question)
    helper.start()
    try:
        runtime = SetupRuntime(
            profile=profile,
            bridge=bridge,
            config=AIConfig(provider="api_openai", endpoint="http://example", model_name="test"),
            host="127.0.0.1",
            user="jan",
            ssh_port=22,
            key_path=None,
            system_info_fn=lambda: "system info",
            call_model_text=lambda cfg, messages: json.dumps(plan_payload),
            probe_facts=lambda **kwargs: facts,
            detect_live_env=lambda: (False, "INSTALLED"),
            ssh_exec=lambda command, timeout: (0, "", ""),
            repo_search=lambda query: "",
            community_search=lambda query: "",
            web_search=lambda query, max_results: "",
            wait_for_reconnect=lambda reconnect_user: (True, "fresh info", reconnect_user),
            run_pre_reboot_checks=lambda: (True, [], ""),
            smoke_test_preboot_script=lambda: None,
            bootstrap_nopasswd_sudo=lambda current: current,
            runtime_prompt_block=lambda current, is_live: "",
            migration_plan_md="",
            distro_primer_md="",
            native_search_active=False,
        )

        summary = SetupOrchestrator(runtime).run()
    finally:
        helper.join(timeout=2)

    assert "Arch workstation" in summary


def test_orchestrator_retries_invalid_planner_json_before_blocking():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        session_type="wayland",
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
    )
    model_calls = {"count": 0}
    seen_messages: list[list[dict[str, str]]] = []

    valid_plan = {
        "target_summary": "Arch workstation.",
        "distro_family": "arch",
        "package_manager": "pacman",
        "driver_strategy": {
            "strategy": "no_special_driver",
            "confidence": "resolved_official",
            "source": "official",
            "packages": [],
            "services": [],
            "notes": "",
        },
        "desktop_strategy": {
            "strategy": "plasma",
            "confidence": "resolved_official",
            "source": "official",
            "packages": ["plasma-desktop"],
            "services": ["sddm"],
            "notes": "",
        },
        "command_groups": [
            {
                "id": "desktop-1",
                "stage": "desktop",
                "title": "Persist desktop config",
                "commands": [
                    "kwriteconfig6 --file kdeglobals --group Icons --key Theme candy-icons"
                ],
                "verification": [],
                "packages": [],
            }
        ],
        "apps": [],
        "unresolved_questions": [],
        "verification_commands": {},
        "required_items": ["desktop"],
        "optional_items": [],
        "planner_revision": 1,
    }

    def model_call(_cfg, messages):
        seen_messages.append(messages)
        model_calls["count"] += 1
        if model_calls["count"] == 1:
            return '{"target_summary": "unterminated'
        assert "planner parser" in messages[-1]["content"]
        return json.dumps(valid_plan)

    runtime = _dummy_setup_runtime(profile, bridge, facts)
    runtime.call_model_text = model_call

    summary = SetupOrchestrator(runtime).run()

    assert model_calls["count"] == 2
    assert "Arch workstation" in summary


def test_orchestrator_retries_planner_validation_error_before_blocking():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        session_type="wayland",
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
    )
    model_calls = {"count": 0}

    bad_plan = {
        "target_summary": "Arch workstation.",
        "distro_family": "arch",
        "package_manager": "pacman",
        "driver_strategy": {
            "strategy": "no_special_driver",
            "confidence": "resolved_official",
            "source": "official",
            "packages": [],
            "services": [],
            "notes": "",
        },
        "desktop_strategy": {
            "strategy": "plasma",
            "confidence": "resolved_official",
            "source": "official",
            "packages": ["plasma-desktop"],
            "services": ["sddm"],
            "notes": "",
        },
        "command_groups": [
            {
                "id": "desktop-1",
                "stage": "desktop",
                "title": "Persist desktop config",
                "commands": [
                    "kwriteconfig6 --file kdeglobals --group Icons --key Theme candy-icons"
                ],
                "verification": ["verify that KDE is configured"],
            }
        ],
        "apps": [],
        "unresolved_questions": [],
        "verification_commands": {},
        "required_items": ["desktop"],
        "optional_items": [],
        "planner_revision": 1,
    }
    fixed_plan = dict(bad_plan)
    fixed_plan["command_groups"] = [
        {
            "id": "desktop-1",
            "stage": "desktop",
            "title": "Persist desktop config",
            "commands": ["kwriteconfig6 --file kdeglobals --group Icons --key Theme candy-icons"],
            "verification": [],
        }
    ]

    def model_call(_cfg, messages):
        model_calls["count"] += 1
        if model_calls["count"] == 1:
            return json.dumps(bad_plan)
        assert "planner validator" in messages[-1]["content"]
        return json.dumps(fixed_plan)

    runtime = _dummy_setup_runtime(profile, bridge, facts)
    runtime.call_model_text = model_call

    summary = SetupOrchestrator(runtime).run()

    assert model_calls["count"] == 2
    assert "Arch workstation" in summary


def test_orchestrator_uses_harness_search_during_non_native_planning():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
    )
    seen_searches: list[str] = []
    model_calls = {"count": 0}

    def model_call(_cfg, messages):
        model_calls["count"] += 1
        if model_calls["count"] == 1:
            return json.dumps(
                {"action": "repo_search", "query": "nvidia open", "reason": "resolve package"}
            )
        return json.dumps(
            {
                "action": "plan",
                "plan": {
                    "target_summary": "Arch workstation.",
                    "distro_family": "arch",
                    "package_manager": "pacman",
                    "driver_strategy": {
                        "strategy": "official_nvidia_open",
                        "confidence": "resolved_official",
                        "source": "official",
                        "packages": ["nvidia-open"],
                        "services": [],
                        "notes": "",
                    },
                    "desktop_strategy": {
                        "strategy": "plasma",
                        "confidence": "resolved_official",
                        "source": "official",
                        "packages": ["plasma-desktop"],
                        "services": ["sddm"],
                        "notes": "",
                    },
                    "command_groups": [
                        {
                            "id": "desktop-1",
                            "stage": "desktop",
                            "title": "Persist desktop config",
                            "commands": ["echo desktop"],
                            "verification": [],
                            "rationale": "",
                            "source": "repo",
                            "packages": [],
                        }
                    ],
                    "apps": [],
                    "unresolved_questions": [],
                    "verification_commands": {},
                    "required_items": ["desktop"],
                    "optional_items": [],
                    "planner_revision": 1,
                },
            }
        )

    runtime = SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(
            provider="local_lmstudio", endpoint="http://localhost:1234/v1", model_name="test"
        ),
        host="127.0.0.1",
        user="jan",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=model_call,
        probe_facts=lambda **kwargs: facts,
        detect_live_env=lambda: (False, "INSTALLED"),
        ssh_exec=lambda command, timeout: (0, "", ""),
        repo_search=lambda query: (
            seen_searches.append(query) or "[repo_search results]\nnvidia-open"
        ),
        community_search=lambda query: "",
        web_search=lambda query, max_results: "",
        wait_for_reconnect=lambda reconnect_user: (True, "fresh info", reconnect_user),
        run_pre_reboot_checks=lambda: (True, [], ""),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda current, is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=False,
    )

    summary = SetupOrchestrator(runtime).run()

    assert seen_searches == ["nvidia open"]
    assert "desktop runtime state is still unknown" in summary.lower()


def test_orchestrator_keeps_harness_lookup_available_during_native_planning():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
    )
    seen_native_flags: list[bool] = []
    seen_searches: list[str] = []
    model_calls = {"count": 0}

    def model_call(cfg, _messages):
        seen_native_flags.append(bool(cfg.native_search))
        model_calls["count"] += 1
        if model_calls["count"] == 1:
            return json.dumps(
                {"action": "repo_search", "query": "nvidia open", "reason": "resolve package"}
            )
        return json.dumps(
            {
                "action": "plan",
                "plan": {
                    "target_summary": "Arch workstation.",
                    "distro_family": "arch",
                    "package_manager": "pacman",
                    "driver_strategy": {
                        "strategy": "official_nvidia_open",
                        "confidence": "resolved_official",
                        "source": "official",
                        "packages": ["nvidia-open"],
                        "services": [],
                        "notes": "",
                    },
                    "desktop_strategy": {
                        "strategy": "plasma",
                        "confidence": "resolved_official",
                        "source": "official",
                        "packages": ["plasma-desktop"],
                        "services": ["sddm"],
                        "notes": "",
                    },
                    "command_groups": [
                        {
                            "id": "desktop-1",
                            "stage": "desktop",
                            "title": "Persist desktop config",
                            "commands": ["echo desktop"],
                            "verification": [],
                            "rationale": "",
                            "source": "repo",
                            "packages": [],
                        }
                    ],
                    "apps": [],
                    "unresolved_questions": [],
                    "verification_commands": {},
                    "required_items": ["desktop"],
                    "optional_items": [],
                    "planner_revision": 1,
                },
            }
        )

    runtime = SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(provider="api_openai", endpoint="http://example", model_name="test"),
        host="127.0.0.1",
        user="jan",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=model_call,
        probe_facts=lambda **kwargs: facts,
        detect_live_env=lambda: (False, "INSTALLED"),
        ssh_exec=lambda command, timeout: (0, "", ""),
        repo_search=lambda query: (
            seen_searches.append(query) or "[repo_search results]\nnvidia-open"
        ),
        community_search=lambda query: "",
        web_search=lambda query, max_results: "",
        wait_for_reconnect=lambda reconnect_user: (True, "fresh info", reconnect_user),
        run_pre_reboot_checks=lambda: (True, [], ""),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda current, is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=True,
    )

    summary = SetupOrchestrator(runtime).run()

    assert seen_native_flags == [True, True]
    assert seen_searches == ["nvidia open"]
    assert "desktop runtime state is still unknown" in summary.lower()


def test_live_install_failed_pre_reboot_checks_block_before_reconnect():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    plan_payload = {
        "target_summary": "Arch live install.",
        "distro_family": "arch",
        "package_manager": "pacman",
        "driver_strategy": {
            "strategy": "no_special_driver",
            "confidence": "resolved_official",
            "source": "official",
            "packages": [],
            "services": [],
            "notes": "",
        },
        "desktop_strategy": {
            "strategy": "plasma",
            "confidence": "resolved_official",
            "source": "official",
            "packages": ["plasma-desktop"],
            "services": ["sddm"],
            "notes": "",
        },
        "command_groups": [
            {
                "id": "install-1",
                "stage": "install_or_skip",
                "title": "Install base system",
                "commands": ["echo install base", "reboot"],
                "verification": [],
                "rationale": "",
                "source": "repo",
                "packages": [],
            }
        ],
        "apps": [],
        "unresolved_questions": [],
        "verification_commands": {},
        "required_items": ["install"],
        "optional_items": [],
        "planner_revision": 1,
    }
    reconnect_calls = {"count": 0}
    reboot_commands: list[str] = []

    def probe_facts(**kwargs):
        return SetupFacts(
            distro_family="arch",
            package_manager="pacman",
            community_helper="none",
            current_user="root",
            login_user="root",
            is_live=kwargs.get("is_live", True),
            live_marker=kwargs.get("live_marker", "LIVE"),
        )

    def ssh_exec(command, _timeout):
        if "reboot" in command:
            reboot_commands.append(command)
        return (0, "", "")

    runtime = SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(provider="api_openai", endpoint="http://example", model_name="test"),
        host="127.0.0.1",
        user="root",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=lambda cfg, messages: json.dumps(plan_payload),
        probe_facts=probe_facts,
        detect_live_env=lambda: (True, "LIVE"),
        ssh_exec=ssh_exec,
        repo_search=lambda query: "",
        community_search=lambda query: "",
        web_search=lambda query, max_results: "",
        wait_for_reconnect=lambda reconnect_user: (
            reconnect_calls.__setitem__("count", 1) or (True, "fresh info", reconnect_user)
        ),
        run_pre_reboot_checks=lambda: (False, ["[kernel] missing"], "kernel missing"),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda current, is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=False,
    )

    summary = SetupOrchestrator(runtime).run()

    assert "kernel missing" in summary
    assert reconnect_calls["count"] == 0
    assert reboot_commands == []


def test_live_install_passes_guard_then_reboots_and_reconnects_once():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    live_state = {"is_live": True}
    reconnect_calls = {"count": 0}
    commands: list[str] = []
    plan_payload = {
        "target_summary": "Arch live install.",
        "distro_family": "arch",
        "package_manager": "pacman",
        "driver_strategy": {
            "strategy": "no_special_driver",
            "confidence": "resolved_official",
            "source": "official",
            "packages": [],
            "services": [],
            "notes": "",
        },
        "desktop_strategy": {
            "strategy": "plasma",
            "confidence": "resolved_official",
            "source": "official",
            "packages": ["plasma-desktop"],
            "services": ["sddm"],
            "notes": "",
        },
        "command_groups": [
            {
                "id": "install-1",
                "stage": "install_or_skip",
                "title": "Install base system",
                "commands": ["echo install base", "reboot"],
                "verification": [],
                "rationale": "",
                "source": "repo",
                "packages": [],
            },
            {
                "id": "desktop-1",
                "stage": "desktop",
                "title": "Persist desktop config",
                "commands": [
                    "kwriteconfig6 --file kdeglobals --group Icons --key Theme candy-icons"
                ],
                "verification": [],
                "rationale": "",
                "source": "repo",
                "packages": [],
            },
        ],
        "apps": [],
        "unresolved_questions": [],
        "verification_commands": {},
        "required_items": ["install", "desktop"],
        "optional_items": [],
        "planner_revision": 1,
    }

    def probe_facts(**kwargs):
        is_live = bool(kwargs.get("is_live", live_state["is_live"]))
        return SetupFacts(
            distro_family="arch",
            package_manager="pacman",
            community_helper="none",
            current_user="root" if is_live else "jan",
            login_user="root" if is_live else "jan",
            is_live=is_live,
            live_marker="LIVE" if is_live else "INSTALLED",
            session_type="" if is_live else "wayland",
            desktop_runtime_state=(
                DesktopRuntimeState.BLOCKED_NO_GUI_SESSION
                if is_live
                else DesktopRuntimeState.LIVE_APPLY_SUCCEEDED
            ),
        )

    def ssh_exec(command, _timeout):
        commands.append(command)
        return (0, "", "")

    def wait_for_reconnect(reconnect_user):
        reconnect_calls["count"] += 1
        live_state["is_live"] = False
        return (True, "fresh info", "jan")

    runtime = SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(provider="api_openai", endpoint="http://example", model_name="test"),
        host="127.0.0.1",
        user="root",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=lambda cfg, messages: json.dumps(plan_payload),
        probe_facts=probe_facts,
        detect_live_env=lambda: (
            live_state["is_live"],
            "LIVE" if live_state["is_live"] else "INSTALLED",
        ),
        ssh_exec=ssh_exec,
        repo_search=lambda query: "",
        community_search=lambda query: "",
        web_search=lambda query, max_results: "",
        wait_for_reconnect=wait_for_reconnect,
        run_pre_reboot_checks=lambda: (True, [], "ok"),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda current, is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=False,
    )

    summary = SetupOrchestrator(runtime).run()

    assert reconnect_calls["count"] == 1
    assert commands.count("reboot") == 0
    assert any("umount -R /mnt" in command and "reboot" in command for command in commands)
    assert "Arch live install" in summary


def test_live_install_installs_reconnect_key_before_reboot(tmp_path):
    public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestQuickHatchKey quickhatch"
    pub_path = tmp_path / "quickhatch_ed25519.pub"
    pub_path.write_text(public_key, encoding="utf-8")
    profile = UserProfile(ssh_public_key_path=str(pub_path))
    bridge = BridgeState(profile, persist_path=None)
    live_state = {"is_live": True}
    commands: list[str] = []
    plan_payload = {
        "target_summary": "Arch live install.",
        "distro_family": "arch",
        "package_manager": "pacman",
        "driver_strategy": {
            "strategy": "no_special_driver",
            "confidence": "resolved_official",
            "source": "official",
            "packages": [],
            "services": [],
            "notes": "",
        },
        "desktop_strategy": {
            "strategy": "plasma",
            "confidence": "resolved_official",
            "source": "official",
            "packages": ["plasma-desktop"],
            "services": ["sddm"],
            "notes": "",
        },
        "command_groups": [
            {
                "id": "install-1",
                "stage": "install_or_skip",
                "title": "Install base system",
                "commands": [
                    "arch-chroot /mnt useradd -m -G wheel -s /bin/bash jan",
                    "reboot",
                ],
                "verification": [],
                "rationale": "",
                "source": "repo",
                "packages": [],
            },
            {
                "id": "desktop-1",
                "stage": "desktop",
                "title": "Persist desktop config",
                "commands": [
                    "kwriteconfig6 --file kdeglobals --group Icons --key Theme candy-icons"
                ],
                "verification": [],
                "rationale": "",
                "source": "repo",
                "packages": [],
            },
        ],
        "apps": [],
        "unresolved_questions": [],
        "verification_commands": {},
        "required_items": ["install", "desktop"],
        "optional_items": [],
        "planner_revision": 1,
    }

    def probe_facts(**kwargs):
        is_live = bool(kwargs.get("is_live", live_state["is_live"]))
        return SetupFacts(
            distro_family="arch",
            package_manager="pacman",
            community_helper="none",
            current_user="root" if is_live else "jan",
            login_user="root" if is_live else "jan",
            is_live=is_live,
            live_marker="LIVE" if is_live else "INSTALLED",
            session_type="" if is_live else "wayland",
            desktop_runtime_state=(
                DesktopRuntimeState.BLOCKED_NO_GUI_SESSION
                if is_live
                else DesktopRuntimeState.LIVE_APPLY_SUCCEEDED
            ),
        )

    def ssh_exec(command, _timeout):
        commands.append(command)
        return (0, "", "")

    def wait_for_reconnect(reconnect_user):
        live_state["is_live"] = False
        return (True, "fresh info", reconnect_user)

    runtime = SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(provider="api_openai", endpoint="http://example", model_name="test"),
        host="127.0.0.1",
        user="root",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=lambda cfg, messages: json.dumps(plan_payload),
        probe_facts=probe_facts,
        detect_live_env=lambda: (
            live_state["is_live"],
            "LIVE" if live_state["is_live"] else "INSTALLED",
        ),
        ssh_exec=ssh_exec,
        repo_search=lambda query: "",
        community_search=lambda query: "",
        web_search=lambda query, max_results: "",
        wait_for_reconnect=wait_for_reconnect,
        run_pre_reboot_checks=lambda: (True, [], "ok"),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda current, is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=False,
    )

    summary = SetupOrchestrator(runtime).run()

    key_command = next(command for command in commands if "authorized_keys" in command)
    reboot_command = next(command for command in commands if "reboot" in command)
    assert commands.index(key_command) < commands.index(reboot_command)
    assert public_key in key_command
    assert "Arch live install" in summary


def test_live_install_injects_disk_cleanup_before_destructive_partitioning():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    live_state = {"is_live": True}
    commands: list[str] = []
    plan_payload = {
        "target_summary": "Arch live install.",
        "distro_family": "arch",
        "package_manager": "pacman",
        "driver_strategy": {
            "strategy": "no_special_driver",
            "confidence": "resolved_official",
            "source": "official",
            "packages": [],
            "services": [],
            "notes": "",
        },
        "desktop_strategy": {
            "strategy": "plasma",
            "confidence": "resolved_official",
            "source": "official",
            "packages": ["plasma-desktop"],
            "services": ["sddm"],
            "notes": "",
        },
        "command_groups": [
            {
                "id": "install-1",
                "stage": "install_or_skip",
                "title": "Partition and install",
                "commands": ["parted -s /dev/sda mklabel gpt", "umount -R /mnt", "reboot"],
                "verification": [],
                "rationale": "",
                "source": "repo",
                "packages": [],
            },
            {
                "id": "desktop-1",
                "stage": "desktop",
                "title": "Persist desktop config",
                "commands": [
                    "kwriteconfig6 --file kdeglobals --group Icons --key Theme candy-icons"
                ],
                "verification": [],
                "rationale": "",
                "source": "repo",
                "packages": [],
            },
        ],
        "apps": [],
        "unresolved_questions": [],
        "verification_commands": {},
        "required_items": ["install", "desktop"],
        "optional_items": [],
        "planner_revision": 1,
    }

    def probe_facts(**kwargs):
        is_live = bool(kwargs.get("is_live", live_state["is_live"]))
        return SetupFacts(
            distro_family="arch",
            package_manager="pacman",
            community_helper="none",
            current_user="root" if is_live else "jan",
            login_user="root" if is_live else "jan",
            is_live=is_live,
            live_marker="LIVE" if is_live else "INSTALLED",
            session_type="" if is_live else "wayland",
            desktop_runtime_state=(
                DesktopRuntimeState.BLOCKED_NO_GUI_SESSION
                if is_live
                else DesktopRuntimeState.LIVE_APPLY_SUCCEEDED
            ),
        )

    def ssh_exec(command, _timeout):
        commands.append(command)
        return (0, "", "")

    def wait_for_reconnect(reconnect_user):
        live_state["is_live"] = False
        return (True, "fresh info", "jan")

    runtime = SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(provider="api_openai", endpoint="http://example", model_name="test"),
        host="127.0.0.1",
        user="root",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=lambda cfg, messages: json.dumps(plan_payload),
        probe_facts=probe_facts,
        detect_live_env=lambda: (
            live_state["is_live"],
            "LIVE" if live_state["is_live"] else "INSTALLED",
        ),
        ssh_exec=ssh_exec,
        repo_search=lambda query: "",
        community_search=lambda query: "",
        web_search=lambda query, max_results: "",
        wait_for_reconnect=wait_for_reconnect,
        run_pre_reboot_checks=lambda: (True, [], "ok"),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda current, is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=False,
    )

    summary = SetupOrchestrator(runtime).run()

    parted_index = commands.index("parted -s /dev/sda mklabel gpt")
    assert commands[parted_index - 5] == "sudo swapoff -a || true"
    assert (
        commands[parted_index - 4]
        == "mountpoint -q /mnt/sysimage && sudo umount -R /mnt/sysimage || true"
    )
    assert (
        commands[parted_index - 3]
        == "mountpoint -q /mnt/sysroot && sudo umount -R /mnt/sysroot || true"
    )
    assert commands[parted_index - 2] == "mountpoint -q /mnt && sudo umount -R /mnt || true"
    assert "lsblk -lnpo NAME /dev/sda" in commands[parted_index - 1]
    assert 'sudo umount "$p"' in commands[parted_index - 1]
    assert "umount -R /mnt" not in commands
    messages = [message["content"] for message in bridge.messages]
    assert "`parted -s /dev/sda mklabel gpt`" in messages
    assert "Arch live install" in summary


def test_arch_live_pacstrap_gets_keyring_prep():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="root",
        login_user="root",
        is_live=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="arch-install",
        stage=SetupStage.INSTALL_OR_SKIP,
        title="Arch base install",
        commands=["pacstrap -K /mnt base linux linux-firmware"],
    )

    commands = orchestrator._commands_for_group(SetupStage.INSTALL_OR_SKIP, group)

    pacstrap_index = commands.index("pacstrap -K /mnt base linux linux-firmware")
    assert "pacman-key --populate archlinux" in commands[pacstrap_index - 1]
    assert "pacman -Sy --noconfirm archlinux-keyring" in commands[pacstrap_index - 1]


def test_arch_live_pacstrap_does_not_duplicate_existing_keyring_prep():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="root",
        login_user="root",
        is_live=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="arch-install",
        stage=SetupStage.INSTALL_OR_SKIP,
        title="Arch base install",
        commands=[
            "pacman-key --init && pacman-key --populate archlinux",
            "pacstrap -K /mnt base linux linux-firmware",
        ],
    )

    commands = orchestrator._commands_for_group(SetupStage.INSTALL_OR_SKIP, group)

    assert sum("pacman-key --populate archlinux" in command for command in commands) == 1


def test_live_fedora_anaconda_install_gets_disk_cleanup_and_long_timeout():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="fedora",
        package_manager="dnf",
        current_user="liveuser",
        login_user="liveuser",
        is_live=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="fedora-install",
        stage=SetupStage.INSTALL_OR_SKIP,
        title="Fedora Anaconda Install",
        commands=[
            "printf 'clearpart --all --drives=sda\\nautopart\\n' > /tmp/qh.ks",
            "sudo anaconda --kickstart=/tmp/qh.ks -C --noninteractive",
        ],
    )

    commands = orchestrator._commands_for_group(SetupStage.INSTALL_OR_SKIP, group)

    ks_index = commands.index("printf 'clearpart --all --drives=sda\\nautopart\\n' > /tmp/qh.ks")
    anaconda_index = commands.index("sudo anaconda --kickstart=/tmp/qh.ks -C --noninteractive")
    assert commands[ks_index - 5] == "sudo swapoff -a || true"
    assert commands[ks_index - 4] == (
        "mountpoint -q /mnt/sysimage && sudo umount -R /mnt/sysimage || true"
    )
    assert commands[ks_index - 3] == (
        "mountpoint -q /mnt/sysroot && sudo umount -R /mnt/sysroot || true"
    )
    assert commands[ks_index - 2] == "mountpoint -q /mnt && sudo umount -R /mnt || true"
    assert "lsblk -lnpo NAME /dev/sda" in commands[ks_index - 1]
    assert orchestrator._command_timeout(commands[anaconda_index]) == 1800


def test_installroot_package_bootstrap_gets_long_timeout():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(distro_family="fedora", package_manager="dnf", is_live=True)
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))

    timeout = orchestrator._command_timeout(
        "REL=$(. /etc/os-release; echo ${VERSION_ID%%.*}) && sudo dnf "
        '--installroot=/mnt --releasever="$REL" --use-host-config install -y '
        "@core @standard kernel NetworkManager openssh-server"
    )

    assert timeout == 1800


def test_live_fstab_blkid_command_is_normalized_to_sudo():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(distro_family="fedora", package_manager="dnf", is_live=True)
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="fstab",
        stage=SetupStage.INSTALL_OR_SKIP,
        title="Write fstab",
        commands=[
            'UUID=$(blkid -s UUID -o value /dev/sda1) && '
            'echo "UUID=$UUID / ext4 defaults 1 1" | sudo tee /mnt/etc/fstab'
        ],
    )

    commands = orchestrator._commands_for_group(SetupStage.INSTALL_OR_SKIP, group)

    assert any(
        "UUID=$(sudo blkid -s UUID -o value /dev/sda1)" in command for command in commands
    )


def test_fedora_live_user_setup_gets_hidden_shadow_lock_cleanup():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(distro_family="fedora", package_manager="dnf", is_live=True)
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="users",
        stage=SetupStage.INSTALL_OR_SKIP,
        title="Create Fedora user",
        commands=[
            "sudo chroot /mnt useradd -m fedoraqa",
            "sudo chroot /mnt sh -c \"echo 'fedoraqa:password' | chpasswd\"",
        ],
    )

    commands = orchestrator._commands_for_group(SetupStage.INSTALL_OR_SKIP, group)

    setenforce_index = commands.index("sudo setenforce 0 || true")
    cleanup_index = next(i for i, command in enumerate(commands) if ".pwd.lock" in command)
    assert setenforce_index < cleanup_index
    assert cleanup_index < commands.index("sudo chroot /mnt useradd -m fedoraqa")
    assert "/mnt/etc/passwd.lock" in commands[cleanup_index]


def test_mount_point_probe_is_normalized_to_mountpoint_command():
    assert (
        SetupOrchestrator._normalize_mountpoint_probe_command(
            "sudo mount --point /mnt/proc >/dev/null || true"
        )
        == "sudo mountpoint -q /mnt/proc >/dev/null || true"
    )


def test_live_nonroot_target_path_verification_uses_sudo():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="fedora",
        package_manager="dnf",
        is_live=True,
        is_root=False,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts

    assert (
        orchestrator._normalize_live_nonroot_target_path_verification(
            "test -f /mnt/boot/grub2/grub.cfg",
            stage=SetupStage.INSTALL_OR_SKIP,
        )
        == "sudo test -f /mnt/boot/grub2/grub.cfg"
    )
    assert (
        orchestrator._normalize_live_nonroot_target_path_verification(
            "[ -f /mnt/boot/grub2/grub.cfg ]",
            stage=SetupStage.INSTALL_OR_SKIP,
        )
        == "sudo test -f /mnt/boot/grub2/grub.cfg"
    )


def test_fedora_live_missing_kernel_pre_reboot_gets_artifact_repair():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="fedora",
        os_id="fedora",
        package_manager="dnf",
        is_live=True,
    )
    attempts = {"count": 0}
    commands: list[str] = []

    def run_pre_reboot_checks():
        attempts["count"] += 1
        if attempts["count"] == 1:
            return (
                False,
                [
                    (
                        "[kernel] /boot missing kernel or initramfs "
                        "(vmlinuz / initramfs / initrd count: 0 / 0)."
                    )
                ],
                "FAIL [kernel] /boot missing kernel or initramfs",
            )
        return (True, [], "ok")

    def ssh_exec(command: str, timeout: int):
        commands.append(command)
        assert timeout == 300
        return (0, "", "")

    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(
            profile,
            bridge,
            facts,
            ssh_exec=ssh_exec,
            run_pre_reboot_checks=run_pre_reboot_checks,
        )
    )
    orchestrator.ctx.facts = facts

    ok, failures, summary = orchestrator._run_pre_reboot_checks_with_retries()

    assert ok
    assert failures == []
    assert summary == "ok"
    assert attempts["count"] == 2
    assert len(commands) == 1
    assert "/usr/lib/modules" in commands[0]
    assert "dracut --force --kver" in commands[0]
    assert "grub2-mkconfig -o /boot/grub2/grub.cfg" in commands[0]


def test_ubuntu_live_debootstrap_gets_prereqs_and_chroot_binds():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="debian",
        os_id="ubuntu",
        os_like="debian",
        package_manager="apt",
        current_user="ubuntu",
        login_user="ubuntu",
        is_live=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="ubuntu-live-base",
        stage=SetupStage.INSTALL_OR_SKIP,
        title="Ubuntu debootstrap install",
        commands=[
            "sudo debootstrap --arch=amd64 noble /mnt http://archive.ubuntu.com/ubuntu",
            "sudo chroot /mnt sh -c 'apt update && apt install -y linux-generic ubuntu-desktop'",
        ],
    )

    commands = orchestrator._commands_for_group(SetupStage.INSTALL_OR_SKIP, group)

    debootstrap_command = next(command for command in commands if " debootstrap --" in command)
    debootstrap_index = commands.index(debootstrap_command)
    chroot_command = next(
        command
        for command in commands
        if "sudo chroot /mnt sh -c" in command and "linux-generic ubuntu-desktop" in command
    )
    chroot_index = commands.index(chroot_command)
    assert "apt-get -o " in commands[0]
    assert " install -y debootstrap" in commands[0]
    assert "Acquire::http::Timeout=20" in commands[0]
    assert "Acquire::http::Timeout=20" in chroot_command
    assert debootstrap_command.startswith("if [ -d /cdrom/dists/noble ]")
    assert "base-files base-passwd bash coreutils dpkg libc6" in debootstrap_command
    assert "sudo debootstrap --no-check-gpg --arch=amd64 noble /mnt file:///cdrom" in (
        debootstrap_command
    )
    assert "http://archive.ubuntu.com/ubuntu" in debootstrap_command
    assert "mount --bind" in commands[chroot_index - 1]
    assert "/mnt" in commands[chroot_index - 1]
    assert debootstrap_index < chroot_index


def test_live_nonroot_chroot_verification_gets_sudo():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="debian",
        package_manager="apt",
        current_user="ubuntu",
        login_user="ubuntu",
        is_live=True,
        is_root=False,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts

    assert (
        orchestrator._normalize_live_nonroot_chroot_command(
            "chroot /mnt id jan && chroot /mnt ls /boot/grub/grub.cfg",
            stage=SetupStage.INSTALL_OR_SKIP,
        )
        == "sudo chroot /mnt id jan && sudo chroot /mnt ls /boot/grub/grub.cfg"
    )


def test_debian_live_efi_layout_keeps_boot_on_root_filesystem():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="debian",
        package_manager="apt",
        current_user="ubuntu",
        login_user="ubuntu",
        is_live=True,
        is_root=False,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="mounts",
        stage=SetupStage.INSTALL_OR_SKIP,
        title="Mount target",
        commands=[
            "sudo mount /dev/sda2 /mnt && sudo mkdir -p /mnt/boot && sudo mount /dev/sda1 /mnt/boot",
            "sudo mkdir -p /mnt/boot/efi && sudo mount /dev/sda1 /mnt/boot/efi",
            "sudo sh -c 'printf \"UUID=%s / ext4 defaults 0 1\\nUUID=%s /boot vfat defaults 0 2\\n\" \"$ROOT_UUID\" \"$BOOT_UUID\" > /mnt/etc/fstab'",
            'sudo chroot /mnt sh -c "grub-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=ubuntu"',
        ],
    )

    commands = orchestrator._commands_for_group(SetupStage.INSTALL_OR_SKIP, group)

    assert commands[0] == "sudo mount /dev/sda2 /mnt && sudo mkdir -p /mnt/boot/efi && sudo mount /dev/sda1 /mnt/boot/efi"
    assert commands[1] == "sudo mkdir -p /mnt/boot/efi && sudo mount /dev/sda1 /mnt/boot/efi"
    assert not any("/mnt/boot/efi/efi" in command for command in commands)
    assert any("/boot/efi vfat" in command for command in commands)
    assert any("--efi-directory=/boot/efi" in command for command in commands)


def test_ubuntu_live_debootstrap_prefers_cdrom_with_split_arch_option():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="debian",
        os_id="ubuntu",
        os_like="debian",
        package_manager="apt",
        current_user="ubuntu",
        login_user="ubuntu",
        is_live=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="ubuntu-live-base",
        stage=SetupStage.INSTALL_OR_SKIP,
        title="Ubuntu debootstrap install",
        commands=[
            "sudo debootstrap --arch amd64 resolute /mnt https://archive.ubuntu.com/ubuntu",
        ],
    )

    commands = orchestrator._commands_for_group(SetupStage.INSTALL_OR_SKIP, group)

    debootstrap_command = next(command for command in commands if " debootstrap --" in command)
    assert debootstrap_command.startswith("if [ -d /cdrom/dists/resolute ]")
    assert "base-files base-passwd bash coreutils dpkg libc6" in debootstrap_command
    assert (
        "sudo debootstrap --no-check-gpg --arch amd64 resolute /mnt file:///cdrom"
        in debootstrap_command
    )
    assert "sudo debootstrap --arch amd64 resolute /mnt http://archive.ubuntu.com/ubuntu" in (
        debootstrap_command
    )


def test_ubuntu_live_debootstrap_rewrites_variable_mirror_with_cdrom_no_check_gpg():
    command = (
        'MIRROR=$(test -d /cdrom/dists/resolute && echo "file:///cdrom" || '
        'echo "http://archive.ubuntu.com/ubuntu") && '
        'sudo debootstrap --arch=amd64 resolute /mnt "$MIRROR"'
    )

    rewritten = SetupOrchestrator._rewrite_debootstrap_http_mirror_to_cdrom_fallback(command)

    assert rewritten == (
        'MIRROR=$(test -d /cdrom/dists/resolute && echo "http://archive.ubuntu.com/ubuntu" || '
        'echo "http://archive.ubuntu.com/ubuntu") && '
        'sudo debootstrap --arch=amd64 resolute /mnt "$MIRROR"'
    )


def test_ubuntu_live_debootstrap_rewrites_complex_cdrom_shell_to_network_mirror():
    command = (
        "MIRROR=$([ -d /cdrom/dists/resolute ] && echo 'file:///cdrom' || "
        "echo 'http://archive.ubuntu.com/ubuntu') && "
        'sudo debootstrap --no-check-gpg --arch=amd64 resolute /mnt "$MIRROR"'
    )

    rewritten = SetupOrchestrator._rewrite_debootstrap_http_mirror_to_cdrom_fallback(command)

    assert "file:///cdrom" not in rewritten
    assert rewritten.count("http://archive.ubuntu.com/ubuntu") == 2
    assert 'sudo debootstrap --no-check-gpg --arch=amd64 resolute /mnt "$MIRROR"' in rewritten


def test_ubuntu_live_debootstrap_rewrites_default_archive_to_fast_mirror():
    command = "sudo debootstrap --arch=amd64 resolute /mnt http://archive.ubuntu.com/ubuntu"

    rewritten = SetupOrchestrator._rewrite_debootstrap_http_mirror_to_cdrom_fallback(command)

    assert "sudo debootstrap --arch=amd64 resolute /mnt http://archive.ubuntu.com/ubuntu" in (
        rewritten
    )


def test_ubuntu_live_debootstrap_does_not_wrap_guarded_shell_conditionals():
    command = (
        "if [ ! -f /mnt/usr/bin/apt ]; then "
        "sudo debootstrap resolute /mnt http://archive.ubuntu.com/ubuntu; "
        "fi"
    )

    rewritten = SetupOrchestrator._rewrite_debootstrap_http_mirror_to_cdrom_fallback(command)

    assert rewritten == (
        "if [ ! -f /mnt/usr/bin/apt ]; then "
        "sudo debootstrap resolute /mnt http://archive.ubuntu.com/ubuntu; "
        "fi"
    )


def test_live_resolv_conf_copy_is_idempotent_for_bind_mounted_run():
    command = "sudo cp /etc/resolv.conf /mnt/etc/resolv.conf"

    rewritten = SetupOrchestrator._rewrite_live_resolv_conf_copy(command)

    assert rewritten.startswith("sudo sh -c ")
    assert "cmp -s /etc/resolv.conf /mnt/etc/resolv.conf" in rewritten
    assert "rm -f /mnt/etc/resolv.conf" in rewritten
    assert "cp -L /etc/resolv.conf /mnt/etc/resolv.conf" in rewritten


def test_package_search_keeps_short_timeout():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(distro_family="arch", package_manager="pacman")
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))

    assert orchestrator._command_timeout("pacman -Ss nvidia") == 300


def test_fedora_kickstart_printf_escapes_directives_and_pins_releasever():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="fedora",
        os_id="fedora",
        package_manager="dnf",
        is_live=True,
        raw={"QH_OS_VERSION_ID": "44"},
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts

    command = orchestrator._normalize_fedora_kickstart_command(
        'printf "url --mirrorlist=https://mirrors.fedoraproject.org/mirrorlist?repo=fedora-$releasever&arch=$basearch\\n%packages\\n@core\\n%end\\n" > /tmp/qh.ks'
    )

    assert "repo=fedora-44" in command
    assert "%%packages" in command
    assert "%%end" in command
    assert "%packages" not in command.replace("%%packages", "")


def test_fedora_dnf5_config_manager_add_repo_is_normalized():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="fedora",
        os_id="fedora",
        package_manager="dnf",
        raw={"QH_OS_VERSION_ID": "44"},
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="brave-repo",
        stage=SetupStage.APPS,
        title="Add Brave repository",
        commands=[
            "sudo dnf config-manager --add-repo https://brave-browser-rpm-release.s3.brave.com/brave-browser.repo",
            "sudo dnf config-manager --add-repo=https://example.test/example.repo",
        ],
    )

    commands = orchestrator._commands_for_group(SetupStage.APPS, group)

    assert commands == [
        "sudo dnf config-manager addrepo --from-repofile https://brave-browser-rpm-release.s3.brave.com/brave-browser.repo",
        "sudo dnf config-manager addrepo --from-repofile https://example.test/example.repo",
    ]


def test_fedora_dnf4_config_manager_add_repo_is_left_alone():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="fedora",
        os_id="fedora",
        package_manager="dnf",
        raw={"QH_OS_VERSION_ID": "40"},
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts

    command = orchestrator._normalize_fedora_dnf_config_manager_command(
        "sudo dnf config-manager --add-repo https://example.test/example.repo"
    )

    assert command == "sudo dnf config-manager --add-repo https://example.test/example.repo"


def test_group_failure_message_redacts_ssh_key_comment():
    message = SetupOrchestrator._format_group_failure_message(
        "SSH continuity",
        "Command failed",
        (
            "grep -qxF 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKE "
            "honz.bartos@example.test' ~/.ssh/authorized_keys"
        ),
        1,
        "",
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKE honz.bartos@example.test",
    )

    assert "AAAAC3" not in message
    assert "honz.bartos@example.test" not in message
    assert "ssh-key ***REDACTED***" in message


def test_live_arch_install_injects_vconsole_before_pacstrap():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="root",
        login_user="root",
        is_root=True,
        is_live=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="base",
        stage=SetupStage.INSTALL_OR_SKIP,
        title="Install base",
        commands=["mount /dev/sda2 /mnt", "pacstrap -K /mnt base linux linux-firmware"],
    )

    commands = orchestrator._commands_for_group(SetupStage.INSTALL_OR_SKIP, group)

    pacstrap_index = commands.index("pacstrap -K /mnt base linux linux-firmware")
    before_pacstrap = commands[:pacstrap_index]
    assert any("vconsole.conf" in command and "KEYMAP=us" in command for command in before_pacstrap)


def test_live_arch_vconsole_injection_does_not_split_assignment_from_pacstrap():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="root",
        login_user="root",
        is_root=True,
        is_live=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    group = CommandGroup(
        id="base",
        stage=SetupStage.INSTALL_OR_SKIP,
        title="Install base",
        commands=[
            "UCODE=$(grep -qi intel /proc/cpuinfo && echo intel-ucode || echo amd-ucode)",
            'pacstrap -K /mnt base linux linux-firmware "$UCODE"',
        ],
    )

    commands = orchestrator._commands_for_group(SetupStage.INSTALL_OR_SKIP, group)

    pacstrap_command = next(command for command in commands if "pacstrap -K /mnt" in command)
    assert pacstrap_command.startswith("UCODE=$(grep -qi intel")
    assert "&& pacstrap -K /mnt" in pacstrap_command
    pacstrap_index = commands.index(pacstrap_command)
    assert pacstrap_index > 0
    assert any(
        "vconsole.conf" in command and "KEYMAP=us" in command
        for command in commands[:pacstrap_index]
    )


def test_pre_format_partition_verification_uses_partition_nodes_not_filesystems():
    group = CommandGroup(
        id="partition",
        stage=SetupStage.INSTALL_OR_SKIP,
        title="Partition disk",
        commands=[
            "parted -s /dev/nvme0n1 mklabel gpt",
            "parted -s /dev/nvme0n1 mkpart ESP fat32 1MiB 513MiB",
            "parted -s /dev/nvme0n1 mkpart primary ext4 513MiB 100%",
        ],
    )

    assert (
        SetupOrchestrator._normalize_pre_format_partition_verification(
            "parted -ms /dev/nvme0n1 print | grep -q '^1:.*:fat32:'",
            group=group,
        )
        == "test -b /dev/nvme0n1p1"
    )
    assert (
        SetupOrchestrator._normalize_pre_format_partition_verification(
            "parted -ms /dev/sda print | grep -q '^2:.*:ext4:'",
            group=group,
        )
        == "test -b /dev/sda2"
    )


def test_partition_verification_keeps_filesystem_check_after_mkfs():
    group = CommandGroup(
        id="format",
        stage=SetupStage.INSTALL_OR_SKIP,
        title="Format disk",
        commands=[
            "mkfs.fat -F32 /dev/sda1",
            "mkfs.ext4 -F /dev/sda2",
        ],
    )
    verification = "parted -ms /dev/sda print | grep -q '^2:.*:ext4:'"

    assert (
        SetupOrchestrator._normalize_pre_format_partition_verification(
            verification,
            group=group,
        )
        == verification
    )


def test_sysfs_module_parameter_verification_uses_sudo_for_non_root_installed_session():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        login_user="jan",
        is_root=False,
        is_live=False,
        sudo_nopasswd=True,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts

    command = orchestrator._normalize_sysfs_module_parameter_verification(
        "cat /sys/module/nvidia_drm/parameters/modeset | grep -iq y",
        stage=SetupStage.POSTBOOT_DRIVER,
    )

    assert command == "sudo cat /sys/module/nvidia_drm/parameters/modeset | grep -iq y"


def test_sysfs_module_parameter_verification_preserves_root_session():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="root",
        login_user="root",
        is_root=True,
        is_live=False,
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    verification = "cat /sys/module/nvidia_drm/parameters/modeset | grep -iq y"

    command = orchestrator._normalize_sysfs_module_parameter_verification(
        verification,
        stage=SetupStage.POSTBOOT_DRIVER,
    )

    assert command == verification


def test_orchestrator_repairs_failed_command_group_with_search_and_retry():
    profile = UserProfile(apps_to_install=["Brave"])
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        session_type="wayland",
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
    )
    commands_seen: list[str] = []
    repo_searches: list[str] = []
    model_calls = {"count": 0}

    def model_call(_cfg, _messages):
        model_calls["count"] += 1
        if model_calls["count"] == 1:
            return json.dumps(
                {
                    "target_summary": "Arch workstation.",
                    "distro_family": "arch",
                    "package_manager": "pacman",
                    "driver_strategy": {
                        "strategy": "no_special_driver",
                        "confidence": "resolved_official",
                        "source": "official",
                        "packages": [],
                        "services": [],
                        "notes": "",
                    },
                    "desktop_strategy": {
                        "strategy": "plasma",
                        "confidence": "resolved_official",
                        "source": "official",
                        "packages": ["plasma-desktop"],
                        "services": ["sddm"],
                        "notes": "",
                    },
                    "command_groups": [
                        {
                            "id": "desktop-1",
                            "stage": "desktop",
                            "title": "Persist desktop config",
                            "commands": [
                                "kwriteconfig6 --file kdeglobals --group Icons --key Theme candy-icons"
                            ],
                            "verification": [],
                            "rationale": "",
                            "source": "repo",
                            "packages": [],
                        },
                        {
                            "id": "apps-1",
                            "stage": "apps",
                            "title": "Install Brave",
                            "commands": ["bad install brave"],
                            "verification": [],
                            "rationale": "",
                            "source": "community",
                            "packages": ["brave-bin"],
                        },
                    ],
                    "apps": [
                        {
                            "name": "Brave",
                            "package_name": "brave-bin",
                            "source": "community",
                            "status": "pending",
                            "required": True,
                            "optional_slow": False,
                        }
                    ],
                    "unresolved_questions": [],
                    "verification_commands": {},
                    "required_items": ["desktop", "apps"],
                    "optional_items": [],
                    "planner_revision": 1,
                }
            )
        if model_calls["count"] == 2:
            return json.dumps(
                {
                    "action": "repo_search",
                    "query": "brave-bin arch package",
                    "reason": "resolve package",
                }
            )
        return json.dumps(
            {
                "action": "retry_group",
                "group": {
                    "id": "apps-1-repair",
                    "stage": "apps",
                    "title": "Install Brave with resolved package",
                    "commands": ["echo repaired brave install"],
                    "verification": [],
                    "rationale": "Use the resolved package path.",
                    "source": "community",
                    "packages": ["brave-bin"],
                },
            }
        )

    def ssh_exec(command, _timeout):
        commands_seen.append(command)
        if command == "bad install brave":
            return (1, "", "target not found: brave")
        if command == "pacman -Qq":
            return (0, "brave-bin\n", "")
        return (0, "", "")

    runtime = SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(
            provider="local_lmstudio", endpoint="http://localhost:1234/v1", model_name="test"
        ),
        host="127.0.0.1",
        user="jan",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=model_call,
        probe_facts=lambda **kwargs: facts,
        detect_live_env=lambda: (False, "INSTALLED"),
        ssh_exec=ssh_exec,
        repo_search=lambda query: repo_searches.append(query) or "brave-bin",
        community_search=lambda query: "",
        web_search=lambda query, max_results: "",
        wait_for_reconnect=lambda reconnect_user: (True, "fresh info", reconnect_user),
        run_pre_reboot_checks=lambda: (True, [], ""),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda current, is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=False,
    )

    summary = SetupOrchestrator(runtime).run()

    assert repo_searches == ["brave-bin arch package"]
    assert "echo repaired brave install" in commands_seen
    assert "Verified installed: Brave" in summary


def test_repair_keeps_provider_native_search_enabled_when_available():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
    )
    seen_native_search: list[bool] = []
    seen_prompt: list[str] = []

    def model_call(cfg, messages):
        seen_native_search.append(bool(cfg.native_search))
        seen_prompt.append(messages[0]["content"])
        return json.dumps(
            {
                "action": "manual_followup",
                "reason": "Native search showed this wrapper is better handled manually.",
                "app_names": ["Example"],
            }
        )

    runtime = _dummy_setup_runtime(
        profile,
        bridge,
        facts,
        native_search_active=True,
    )
    runtime.call_model_text = model_call
    orchestrator = SetupOrchestrator(runtime)
    orchestrator.ctx.facts = facts

    action = orchestrator._repair_failed_group(
        SetupStage.APPS,
        CommandGroup(
            id="apps-1",
            stage=SetupStage.APPS,
            title="Install Example",
            commands=["yay -S --noconfirm example-bin"],
            packages=["example-bin"],
        ),
        GroupFailure(
            failure_type="command",
            command="yay -S --noconfirm example-bin",
            stdout="",
            stderr="target not found: example-bin",
            detail="target not found",
        ),
        repair_attempt=1,
    )

    assert action["action"] == "manual_followup"
    assert seen_native_search == [True]
    assert "Provider-native search is ENABLED in repair mode" in seen_prompt[0]
    assert "Harness search actions are still allowed" in seen_prompt[0]


def test_repair_rejects_retry_without_search_or_evidence_for_package_failures():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
    )
    model_calls = {"count": 0}
    repo_searches: list[str] = []

    def model_call(_cfg, messages):
        model_calls["count"] += 1
        if model_calls["count"] == 1:
            return json.dumps(
                {
                    "action": "retry_group",
                    "group": {
                        "id": "apps-1-repair",
                        "stage": "apps",
                        "title": "Guess another package",
                        "commands": ["yay -S --noconfirm brave-browser-bin"],
                    },
                }
            )
        if model_calls["count"] == 2:
            assert "Do not retry this package/driver/desktop/app failure" in messages[-1]["content"]
            return json.dumps(
                {
                    "action": "repo_search",
                    "query": "brave arch package",
                    "reason": "Need current package evidence before retry.",
                }
            )
        assert "[repo_search]" in messages[-1]["content"]
        return json.dumps(
            {
                "action": "retry_group",
                "group": {
                    "id": "apps-1-repair",
                    "stage": "apps",
                    "title": "Install Brave with evidence",
                    "commands": ["yay -S --noconfirm brave-bin"],
                },
            }
        )

    runtime = _dummy_setup_runtime(profile, bridge, facts)
    runtime.call_model_text = model_call
    runtime.repo_search = lambda query: repo_searches.append(query) or "brave-bin"
    orchestrator = SetupOrchestrator(runtime)
    orchestrator.ctx.facts = facts

    action = orchestrator._repair_failed_group(
        SetupStage.APPS,
        CommandGroup(
            id="apps-1",
            stage=SetupStage.APPS,
            title="Install Brave",
            commands=["yay -S --noconfirm brave-browser-bin"],
            packages=["brave-bin"],
        ),
        GroupFailure(
            failure_type="command",
            command="yay -S --noconfirm brave-browser-bin",
            stdout="",
            stderr="target not found: brave-browser-bin",
            detail="target not found",
        ),
        repair_attempt=1,
    )

    assert action["action"] == "retry_group"
    assert action["group"].commands == ["yay -S --noconfirm brave-bin"]
    assert repo_searches == ["brave arch package"]
    assert model_calls["count"] == 3


def test_orchestrator_does_not_run_stage_verifications_per_group():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        session_type="wayland",
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
    )
    commands_seen: list[str] = []
    plan_payload = {
        "target_summary": "Arch workstation.",
        "distro_family": "arch",
        "package_manager": "pacman",
        "driver_strategy": {
            "strategy": "no_special_driver",
            "confidence": "resolved_official",
            "source": "official",
            "packages": [],
            "services": [],
            "notes": "",
        },
        "desktop_strategy": {
            "strategy": "plasma",
            "confidence": "resolved_official",
            "source": "official",
            "packages": ["plasma-desktop"],
            "services": ["sddm"],
            "notes": "",
        },
        "command_groups": [
            {
                "id": "desktop-1",
                "stage": "desktop",
                "title": "Persist desktop config",
                "commands": [
                    "kwriteconfig6 --file kdeglobals --group Icons --key Theme candy-icons"
                ],
                "verification": [],
                "rationale": "",
                "source": "repo",
                "packages": [],
            }
        ],
        "apps": [],
        "unresolved_questions": [],
        "verification_commands": {"desktop": ["bad global verifier"]},
        "required_items": ["desktop"],
        "optional_items": [],
        "planner_revision": 1,
    }

    def ssh_exec(command, _timeout):
        commands_seen.append(command)
        if command == "bad global verifier":
            return (1, "", "should not run per command group")
        return (0, "", "")

    runtime = SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(provider="api_openai", endpoint="http://example", model_name="test"),
        host="127.0.0.1",
        user="jan",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=lambda cfg, messages: json.dumps(plan_payload),
        probe_facts=lambda **kwargs: facts,
        detect_live_env=lambda: (False, "INSTALLED"),
        ssh_exec=ssh_exec,
        repo_search=lambda query: "",
        community_search=lambda query: "",
        web_search=lambda query, max_results: "",
        wait_for_reconnect=lambda reconnect_user: (True, "fresh info", reconnect_user),
        run_pre_reboot_checks=lambda: (True, [], ""),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda current, is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=False,
    )

    summary = SetupOrchestrator(runtime).run()

    assert "bad global verifier" not in commands_seen
    assert "Arch workstation" in summary


def test_orchestrator_retries_malformed_repair_json():
    profile = UserProfile(apps_to_install=["Brave"])
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        session_type="wayland",
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
    )
    commands_seen: list[str] = []
    model_calls = {"count": 0}

    def model_call(_cfg, _messages):
        model_calls["count"] += 1
        if model_calls["count"] == 1:
            return json.dumps(
                {
                    "target_summary": "Arch workstation.",
                    "distro_family": "arch",
                    "package_manager": "pacman",
                    "driver_strategy": {
                        "strategy": "no_special_driver",
                        "confidence": "resolved_official",
                        "source": "official",
                        "packages": [],
                        "services": [],
                        "notes": "",
                    },
                    "desktop_strategy": {
                        "strategy": "plasma",
                        "confidence": "resolved_official",
                        "source": "official",
                        "packages": ["plasma-desktop"],
                        "services": ["sddm"],
                        "notes": "",
                    },
                    "command_groups": [
                        {
                            "id": "desktop-1",
                            "stage": "desktop",
                            "title": "Persist desktop config",
                            "commands": [
                                "kwriteconfig6 --file kdeglobals --group Icons --key Theme candy-icons"
                            ],
                        },
                        {
                            "id": "apps-1",
                            "stage": "apps",
                            "title": "Install Brave",
                            "commands": ["bad install brave"],
                            "packages": ["brave-bin"],
                        },
                    ],
                    "apps": [
                        {
                            "name": "Brave",
                            "package_name": "brave-bin",
                            "source": "community",
                            "status": "pending",
                            "required": True,
                        }
                    ],
                    "unresolved_questions": [],
                    "verification_commands": {},
                    "required_items": ["desktop", "apps"],
                    "optional_items": [],
                    "planner_revision": 1,
                }
            )
        if model_calls["count"] == 2:
            return '{"action": "retry_group", "group": {"id": "broken", "commands": ["unterminated'
        assert "invalid JSON" in _messages[-1]["content"]
        return json.dumps(
            {
                "action": "retry_group",
                "group": {
                    "id": "apps-1-repair",
                    "stage": "apps",
                    "title": "Install Brave after malformed repair retry",
                    "commands": ["echo repaired brave install"],
                    "verification": [],
                    "packages": ["brave-bin"],
                    "evidence": [
                        {
                            "source_type": "local_facts",
                            "summary": "This test exercises parser recovery, not package resolution.",
                        }
                    ],
                },
            }
        )

    def ssh_exec(command, _timeout):
        commands_seen.append(command)
        if command == "bad install brave":
            return (1, "", "target not found: brave")
        if command == "pacman -Qq":
            return (0, "brave-bin\n", "")
        return (0, "", "")

    runtime = SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(
            provider="local_lmstudio", endpoint="http://localhost:1234/v1", model_name="test"
        ),
        host="127.0.0.1",
        user="jan",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=model_call,
        probe_facts=lambda **kwargs: facts,
        detect_live_env=lambda: (False, "INSTALLED"),
        ssh_exec=ssh_exec,
        repo_search=lambda query: "",
        community_search=lambda query: "",
        web_search=lambda query, max_results: "",
        wait_for_reconnect=lambda reconnect_user: (True, "fresh info", reconnect_user),
        run_pre_reboot_checks=lambda: (True, [], ""),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda current, is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=False,
    )

    summary = SetupOrchestrator(runtime).run()

    assert model_calls["count"] == 3
    assert "echo repaired brave install" in commands_seen
    assert "Verified installed: Brave" in summary


def test_final_audit_blocks_when_completed_app_package_is_missing():
    profile = UserProfile(apps_to_install=["Brave"])
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="yay",
        init_system="unknown",
        current_user="jan",
        login_user="jan",
        session_type="wayland",
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
    )
    plan_payload = {
        "target_summary": "Arch workstation.",
        "distro_family": "arch",
        "package_manager": "pacman",
        "driver_strategy": {
            "strategy": "no_special_driver",
            "confidence": "resolved_official",
            "source": "official",
            "packages": [],
            "services": [],
            "notes": "",
        },
        "desktop_strategy": {
            "strategy": "plasma",
            "confidence": "resolved_official",
            "source": "official",
            "packages": ["plasma-desktop"],
            "services": ["sddm"],
            "notes": "",
        },
        "command_groups": [
            {
                "id": "desktop-1",
                "stage": "desktop",
                "title": "Persist desktop config",
                "commands": [
                    "kwriteconfig6 --file kdeglobals --group Icons --key Theme candy-icons"
                ],
                "verification": [],
                "rationale": "",
                "source": "repo",
                "packages": [],
            },
            {
                "id": "apps-1",
                "stage": "apps",
                "title": "Install Brave",
                "commands": ["echo fake brave install"],
                "verification": [],
                "rationale": "",
                "source": "community",
                "packages": ["brave-bin"],
            },
        ],
        "apps": [
            {
                "name": "Brave",
                "package_name": "brave-bin",
                "source": "community",
                "status": "pending",
                "required": True,
            }
        ],
        "unresolved_questions": [],
        "verification_commands": {},
        "required_items": ["desktop", "apps"],
        "optional_items": [],
        "planner_revision": 1,
    }

    def ssh_exec(command, _timeout):
        if command == "pacman -Qq":
            return (0, "plasma-desktop\n", "")
        return (0, "", "")

    runtime = SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(
            provider="local_lmstudio", endpoint="http://localhost:1234/v1", model_name="test"
        ),
        host="127.0.0.1",
        user="jan",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=lambda cfg, messages: json.dumps(plan_payload),
        probe_facts=lambda **kwargs: facts,
        detect_live_env=lambda: (False, "INSTALLED"),
        ssh_exec=ssh_exec,
        repo_search=lambda query: "",
        community_search=lambda query: "",
        web_search=lambda query, max_results: "",
        wait_for_reconnect=lambda reconnect_user: (True, "fresh info", reconnect_user),
        run_pre_reboot_checks=lambda: (True, [], ""),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda current, is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=False,
    )

    summary = SetupOrchestrator(runtime).run()

    assert "final package verification failed" in summary.lower()
    assert "Brave" in summary


def test_setup_summary_only_claims_verified_installed_apps():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(distro_family="arch", package_manager="pacman", current_user="jan")
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.plan = ResearchPlan(
        target_summary="Arch workstation.",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision("none"),
        desktop_strategy=StrategyDecision("plasma"),
        command_groups=[],
        apps=[
            AppPlanItem(
                name="Brave",
                package_name="brave-bin",
                source="community",
                status=PlanItemStatus.INSTALLED,
            ),
            AppPlanItem(
                name="Cursor",
                package_name="cursor-bin",
                source="community",
                status=PlanItemStatus.INSTALLED,
            ),
            AppPlanItem(
                name="ChatGPT",
                source="manual_followup",
                status=PlanItemStatus.MANUAL_FOLLOWUP,
                reason="desktop wrapper left for manual follow-up",
            ),
            AppPlanItem(
                name="Codex",
                source="community",
                status=PlanItemStatus.EXPLICITLY_SKIPPED,
            ),
        ],
    )
    orchestrator.ctx.stage_state[SetupStage.FINAL_AUDIT.value] = {
        "verified_installed_apps": ["Brave"],
        "unverified_installed_apps": ["Cursor"],
    }

    summary = orchestrator._build_summary()

    assert "Installed/configured" not in summary
    assert "Verified installed: Brave" in summary
    assert "Manual follow-up: ChatGPT, Cursor" in summary
    assert "Skipped: Codex" in summary


def test_final_package_verification_accepts_user_profile_source_apps():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(distro_family="arch", package_manager="pacman", current_user="jan")
    runtime = _dummy_setup_runtime(profile, bridge, facts)
    runtime.ssh_exec = lambda command, _timeout: (
        0,
        "firefox\nflatpak\nkate\nkonsole\ndolphin\nspectacle\nark\n",
        "",
    )
    orchestrator = SetupOrchestrator(runtime)
    orchestrator.ctx.facts = facts
    orchestrator.ctx.plan = ResearchPlan(
        target_summary="Arch workstation.",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision("none"),
        desktop_strategy=StrategyDecision("plasma"),
        command_groups=[],
        apps=[
            AppPlanItem(
                name="Mozilla Firefox",
                package_name="firefox",
                source="user_profile",
                status=PlanItemStatus.INSTALLED,
            ),
            AppPlanItem(
                name="Flatpak",
                package_name="flatpak",
                source="user_profile",
                status=PlanItemStatus.INSTALLED,
            ),
        ],
    )

    blockers = orchestrator._verify_planned_packages_installed()

    final_state = orchestrator.ctx.stage_state[SetupStage.FINAL_AUDIT.value]
    assert blockers == []
    assert final_state["verified_installed_apps"] == ["Flatpak", "Mozilla Firefox"]
    assert final_state["unverified_installed_apps"] == []
    assert "Manual follow-up" not in orchestrator._build_summary()


def test_orchestrator_can_convert_failed_verification_into_manual_followup_after_question():
    profile = UserProfile(apps_to_install=["ChatGPT"])
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        session_type="wayland",
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
    )
    model_calls = {"count": 0}

    def model_call(_cfg, _messages):
        model_calls["count"] += 1
        if model_calls["count"] == 1:
            return json.dumps(
                {
                    "target_summary": "Arch workstation.",
                    "distro_family": "arch",
                    "package_manager": "pacman",
                    "driver_strategy": {
                        "strategy": "no_special_driver",
                        "confidence": "resolved_official",
                        "source": "official",
                        "packages": [],
                        "services": [],
                        "notes": "",
                    },
                    "desktop_strategy": {
                        "strategy": "plasma",
                        "confidence": "resolved_official",
                        "source": "official",
                        "packages": ["plasma-desktop"],
                        "services": ["sddm"],
                        "notes": "",
                    },
                    "command_groups": [
                        {
                            "id": "desktop-1",
                            "stage": "desktop",
                            "title": "Persist desktop config",
                            "commands": [
                                "kwriteconfig6 --file kdeglobals --group Icons --key Theme candy-icons"
                            ],
                            "verification": [],
                            "rationale": "",
                            "source": "repo",
                            "packages": [],
                        },
                        {
                            "id": "apps-1",
                            "stage": "apps",
                            "title": "Install ChatGPT",
                            "commands": ["echo install chatgpt"],
                            "verification": ["test -f /opt/chatgpt"],
                            "rationale": "",
                            "source": "community",
                            "packages": ["pake-chatgpt"],
                        },
                    ],
                    "apps": [
                        {
                            "name": "ChatGPT",
                            "package_name": "pake-chatgpt",
                            "source": "community",
                            "status": "pending",
                            "required": True,
                            "optional_slow": True,
                        }
                    ],
                    "unresolved_questions": [],
                    "verification_commands": {},
                    "required_items": ["desktop", "apps"],
                    "optional_items": [],
                    "planner_revision": 1,
                }
            )
        if model_calls["count"] == 2:
            return json.dumps(
                {
                    "action": "question",
                    "question": "ChatGPT verification failed. Leave it as manual follow-up?",
                    "choices": ["manual follow-up", "retry"],
                    "required": True,
                    "timeout_seconds": 2,
                }
            )
        return json.dumps(
            {
                "action": "manual_followup",
                "reason": "User chose to leave ChatGPT as manual follow-up after verification failed.",
                "app_names": ["ChatGPT"],
            }
        )

    def ssh_exec(command, _timeout):
        if command == "test -f /opt/chatgpt":
            return (1, "", "missing")
        return (0, "", "")

    def answer_pending_question():
        deadline = time.time() + 2
        while time.time() < deadline:
            pending = bridge.get_pending_questions()
            if pending:
                bridge.answer_question(pending[0].id, "manual follow-up")
                return
            time.sleep(0.01)
        raise AssertionError("Timed out waiting for blocking repair question.")

    helper = threading.Thread(target=answer_pending_question)
    helper.start()
    try:
        runtime = SetupRuntime(
            profile=profile,
            bridge=bridge,
            config=AIConfig(
                provider="local_lmstudio", endpoint="http://localhost:1234/v1", model_name="test"
            ),
            host="127.0.0.1",
            user="jan",
            ssh_port=22,
            key_path=None,
            system_info_fn=lambda: "system info",
            call_model_text=model_call,
            probe_facts=lambda **kwargs: facts,
            detect_live_env=lambda: (False, "INSTALLED"),
            ssh_exec=ssh_exec,
            repo_search=lambda query: "",
            community_search=lambda query: "",
            web_search=lambda query, max_results: "",
            wait_for_reconnect=lambda reconnect_user: (True, "fresh info", reconnect_user),
            run_pre_reboot_checks=lambda: (True, [], ""),
            smoke_test_preboot_script=lambda: None,
            bootstrap_nopasswd_sudo=lambda current: current,
            runtime_prompt_block=lambda current, is_live: "",
            migration_plan_md="",
            distro_primer_md="",
            native_search_active=False,
        )

        summary = SetupOrchestrator(runtime).run()
    finally:
        helper.join(timeout=2)

    assert "Manual follow-up: ChatGPT" in summary


def test_orchestrator_blocks_when_required_repair_question_times_out():
    profile = UserProfile(apps_to_install=["Brave"])
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        session_type="wayland",
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
    )
    model_calls = {"count": 0}

    def model_call(_cfg, _messages):
        model_calls["count"] += 1
        if model_calls["count"] == 1:
            return json.dumps(
                {
                    "target_summary": "Arch workstation.",
                    "distro_family": "arch",
                    "package_manager": "pacman",
                    "driver_strategy": {
                        "strategy": "no_special_driver",
                        "confidence": "resolved_official",
                        "source": "official",
                        "packages": [],
                        "services": [],
                        "notes": "",
                    },
                    "desktop_strategy": {
                        "strategy": "plasma",
                        "confidence": "resolved_official",
                        "source": "official",
                        "packages": ["plasma-desktop"],
                        "services": ["sddm"],
                        "notes": "",
                    },
                    "command_groups": [
                        {
                            "id": "desktop-1",
                            "stage": "desktop",
                            "title": "Persist desktop config",
                            "commands": [
                                "kwriteconfig6 --file kdeglobals --group Icons --key Theme candy-icons"
                            ],
                            "verification": [],
                            "rationale": "",
                            "source": "repo",
                            "packages": [],
                        },
                        {
                            "id": "apps-1",
                            "stage": "apps",
                            "title": "Install Brave",
                            "commands": ["bad install brave"],
                            "verification": [],
                            "rationale": "",
                            "source": "community",
                            "packages": ["brave-bin"],
                        },
                    ],
                    "apps": [
                        {
                            "name": "Brave",
                            "package_name": "brave-bin",
                            "source": "community",
                            "status": "pending",
                            "required": True,
                        }
                    ],
                    "unresolved_questions": [],
                    "verification_commands": {},
                    "required_items": ["desktop", "apps"],
                    "optional_items": [],
                    "planner_revision": 1,
                }
            )
        return json.dumps(
            {
                "action": "question",
                "question": "Should I try a manual package build for Brave?",
                "choices": ["yes", "no"],
                "required": True,
                "timeout_seconds": 0.01,
            }
        )

    def ssh_exec(command, _timeout):
        if command == "bad install brave":
            return (1, "", "target not found")
        return (0, "", "")

    runtime = SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(
            provider="local_lmstudio", endpoint="http://localhost:1234/v1", model_name="test"
        ),
        host="127.0.0.1",
        user="jan",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=model_call,
        probe_facts=lambda **kwargs: facts,
        detect_live_env=lambda: (False, "INSTALLED"),
        ssh_exec=ssh_exec,
        repo_search=lambda query: "",
        community_search=lambda query: "",
        web_search=lambda query, max_results: "",
        wait_for_reconnect=lambda reconnect_user: (True, "fresh info", reconnect_user),
        run_pre_reboot_checks=lambda: (True, [], ""),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda current, is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=False,
    )

    summary = SetupOrchestrator(runtime).run()

    assert "required item" in summary.lower()
    pending = bridge.get_pending_questions()
    assert pending == []


def test_orchestrator_stop_interrupts_blocking_repair_question():
    profile = UserProfile(apps_to_install=["Brave"])
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        session_type="wayland",
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
    )
    model_calls = {"count": 0}

    def model_call(_cfg, _messages):
        model_calls["count"] += 1
        if model_calls["count"] == 1:
            return json.dumps(
                {
                    "target_summary": "Arch workstation.",
                    "distro_family": "arch",
                    "package_manager": "pacman",
                    "driver_strategy": {
                        "strategy": "no_special_driver",
                        "confidence": "resolved_official",
                        "source": "official",
                        "packages": [],
                        "services": [],
                        "notes": "",
                    },
                    "desktop_strategy": {
                        "strategy": "plasma",
                        "confidence": "resolved_official",
                        "source": "official",
                        "packages": ["plasma-desktop"],
                        "services": ["sddm"],
                        "notes": "",
                    },
                    "command_groups": [
                        {
                            "id": "desktop-1",
                            "stage": "desktop",
                            "title": "Persist desktop config",
                            "commands": [
                                "kwriteconfig6 --file kdeglobals --group Icons --key Theme candy-icons"
                            ],
                            "verification": [],
                            "rationale": "",
                            "source": "repo",
                            "packages": [],
                        },
                        {
                            "id": "apps-1",
                            "stage": "apps",
                            "title": "Install Brave",
                            "commands": ["bad install brave"],
                            "verification": [],
                            "rationale": "",
                            "source": "community",
                            "packages": ["brave-bin"],
                        },
                    ],
                    "apps": [
                        {
                            "name": "Brave",
                            "package_name": "brave-bin",
                            "source": "community",
                            "status": "pending",
                            "required": True,
                        }
                    ],
                    "unresolved_questions": [],
                    "verification_commands": {},
                    "required_items": ["desktop", "apps"],
                    "optional_items": [],
                    "planner_revision": 1,
                }
            )
        return json.dumps(
            {
                "action": "question",
                "question": "Try a manual package build?",
                "choices": ["yes", "no"],
                "required": True,
                "timeout_seconds": 30,
            }
        )

    def ssh_exec(command, _timeout):
        if command == "bad install brave":
            return (1, "", "target not found")
        return (0, "", "")

    def stop_when_question_appears():
        deadline = time.time() + 2
        while time.time() < deadline:
            if bridge.get_pending_questions():
                bridge.request_stop()
                return
            time.sleep(0.01)
        raise AssertionError("Timed out waiting for blocking repair question.")

    helper = threading.Thread(target=stop_when_question_appears)
    helper.start()
    try:
        runtime = SetupRuntime(
            profile=profile,
            bridge=bridge,
            config=AIConfig(
                provider="local_lmstudio", endpoint="http://localhost:1234/v1", model_name="test"
            ),
            host="127.0.0.1",
            user="jan",
            ssh_port=22,
            key_path=None,
            system_info_fn=lambda: "system info",
            call_model_text=model_call,
            probe_facts=lambda **kwargs: facts,
            detect_live_env=lambda: (False, "INSTALLED"),
            ssh_exec=ssh_exec,
            repo_search=lambda query: "",
            community_search=lambda query: "",
            web_search=lambda query, max_results: "",
            wait_for_reconnect=lambda reconnect_user: (True, "fresh info", reconnect_user),
            run_pre_reboot_checks=lambda: (True, [], ""),
            smoke_test_preboot_script=lambda: None,
            bootstrap_nopasswd_sudo=lambda current: current,
            runtime_prompt_block=lambda current, is_live: "",
            migration_plan_md="",
            distro_primer_md="",
            native_search_active=False,
        )

        summary = SetupOrchestrator(runtime).run()
    finally:
        helper.join(timeout=2)

    assert "stopped by user while waiting for an answer" in summary.lower()


def test_stage_specific_repair_budgets_match_risk():
    profile = UserProfile()
    orchestrator = SetupOrchestrator(
        _dummy_setup_runtime(profile, BridgeState(profile, persist_path=None), SetupFacts())
    )

    assert orchestrator._group_repair_budget(SetupStage.INSTALL_OR_SKIP) == 3
    assert orchestrator._group_repair_budget(SetupStage.POSTBOOT_DRIVER) == 6
    assert orchestrator._group_repair_budget(SetupStage.DESKTOP) == 10
    assert orchestrator._group_repair_budget(SetupStage.APPS) == 10


def test_repair_budget_gate_can_extend_and_continue(monkeypatch):
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(distro_family="arch", package_manager="pacman", current_user="jan")
    group = CommandGroup(
        id="apps",
        stage=SetupStage.APPS,
        title="Install apps",
        commands=["install apps"],
        verification=["verify apps"],
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    orchestrator.ctx.plan = ResearchPlan(
        target_summary="Arch workstation",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision("none"),
        desktop_strategy=StrategyDecision("none"),
        command_groups=[group],
    )
    executions = {"count": 0}
    repair_attempts: list[int] = []
    questions: list[tuple[str, list[str] | None]] = []

    def execute_group(_stage, _group, _state):
        executions["count"] += 1
        if executions["count"] <= 2:
            return GroupFailure("verification", "verify apps", "", "missing", "still missing")
        return None

    def repair_failed_group(_stage, failed_group, _failure, *, repair_attempt):
        repair_attempts.append(repair_attempt)
        return {"action": "retry_group", "group": failed_group}

    def ask_question(question, *, choices, **_kwargs):
        questions.append((question, choices))
        return "try 3 more repairs"

    monkeypatch.setattr(orchestrator, "_group_repair_budget", lambda _stage: 1)
    monkeypatch.setattr(orchestrator, "_execute_group", execute_group)
    monkeypatch.setattr(orchestrator, "_repair_failed_group", repair_failed_group)
    monkeypatch.setattr(orchestrator, "_ask_blocking_question", ask_question)

    orchestrator._run_stage_groups(SetupStage.APPS, audit_after=False)

    assert executions["count"] == 3
    assert repair_attempts == [1, 2]
    assert questions
    assert questions[0][1] == ["try 3 more repairs", "manual follow-up", "stop setup"]
    assert group.status == PlanItemStatus.COMPLETED


def test_repair_budget_gate_can_mark_non_install_group_manual_followup(monkeypatch):
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(distro_family="arch", package_manager="pacman", current_user="jan")
    group = CommandGroup(
        id="theme",
        stage=SetupStage.DESKTOP,
        title="Apply theme",
        commands=["apply theme"],
        verification=["verify theme"],
    )
    orchestrator = SetupOrchestrator(_dummy_setup_runtime(profile, bridge, facts))
    orchestrator.ctx.facts = facts
    orchestrator.ctx.plan = ResearchPlan(
        target_summary="Arch workstation",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision("none"),
        desktop_strategy=StrategyDecision("plasma"),
        command_groups=[group],
    )
    questions: list[tuple[str, list[str] | None]] = []

    monkeypatch.setattr(orchestrator, "_group_repair_budget", lambda _stage: 0)
    monkeypatch.setattr(
        orchestrator,
        "_execute_group",
        lambda _stage, _group, _state: GroupFailure(
            "verification", "verify theme", "", "missing", "theme still missing"
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_repair_failed_group",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("repair should not run after manual follow-up choice")
        ),
    )

    def ask_question(question, *, choices, **_kwargs):
        questions.append((question, choices))
        return "manual follow-up"

    monkeypatch.setattr(orchestrator, "_ask_blocking_question", ask_question)

    orchestrator._run_stage_groups(SetupStage.DESKTOP, audit_after=False)

    assert questions
    assert questions[0][1] == ["try 3 more repairs", "manual follow-up", "stop setup"]
    assert group.status == PlanItemStatus.MANUAL_FOLLOWUP
