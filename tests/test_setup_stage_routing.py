from __future__ import annotations

import re
from typing import Any

import pytest

from quickhatch.bridge.server import BridgeState
from quickhatch.config import AIConfig, UserProfile
from quickhatch.setup.orchestrator import GroupFailure, SetupOrchestrator, SetupRuntime
from quickhatch.setup.types import (
    AuditResult,
    CommandGroup,
    FinalSetupStatus,
    ResearchPlan,
    SetupFacts,
    SetupStage,
    StrategyDecision,
)


class MemoryRunLog:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, event_type: str, **payload: Any) -> None:
        self.events.append({"event_type": event_type, **payload})


def _facts(
    *,
    family: str = "arch",
    package_manager: str = "pacman",
    install_profile: str = "configure_existing",
    remote_setup_mode: str = "configure_only",
    is_live: bool = False,
) -> SetupFacts:
    return SetupFacts(
        distro_family=family,
        os_id=family,
        package_manager=package_manager,
        community_helper="yay" if package_manager == "pacman" else "none",
        current_user="jan" if not is_live else "root",
        login_user="jan" if not is_live else "root",
        is_root=is_live,
        sudo_nopasswd=True,
        is_live=is_live,
        live_marker="LIVE" if is_live else "INSTALLED",
        session_type="" if is_live else "wayland",
        raw={
            "install_profile": install_profile,
            "remote_setup_mode": remote_setup_mode,
        },
    )


def _profile(
    *,
    install_profile: str = "configure_existing",
    remote_setup_mode: str = "configure_only",
) -> UserProfile:
    profile = UserProfile()
    profile.install_profile = install_profile
    profile.remote_setup_mode = remote_setup_mode
    profile.install_username = "jan"
    return profile


def _plan(*groups: CommandGroup, family: str = "arch", package_manager: str = "pacman"):
    return ResearchPlan(
        target_summary="test setup",
        distro_family=family,
        package_manager=package_manager,
        driver_strategy=StrategyDecision("none", confidence="resolved_official"),
        desktop_strategy=StrategyDecision("none", confidence="resolved_official"),
        command_groups=list(groups),
    )


def _group(
    stage: SetupStage,
    command: str,
    *,
    group_id: str = "group",
    title: str = "Test group",
) -> CommandGroup:
    return CommandGroup(
        id=group_id,
        stage=stage,
        title=title,
        commands=[command],
        verification=[],
    )


def _orchestrator(
    *,
    profile: UserProfile | None = None,
    facts: SetupFacts | None = None,
    plan: ResearchPlan | None = None,
    ssh_exec=None,
    wait_for_reconnect=None,
    run_pre_reboot_checks=None,
) -> tuple[SetupOrchestrator, list[str], MemoryRunLog]:
    profile = profile or _profile()
    bridge = BridgeState(profile, persist_path=None)
    facts = facts or _facts()
    executed: list[str] = []

    def recording_ssh_exec(command: str, _timeout: int) -> tuple[int, str, str]:
        executed.append(command)
        if ssh_exec is not None:
            return ssh_exec(command, _timeout)
        return (0, "", "")

    runtime = SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(provider="api_openai", endpoint="http://example", model_name="test"),
        host="127.0.0.1",
        user=facts.current_user or "jan",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=lambda _cfg, _messages: "{}",
        probe_facts=lambda **_kwargs: facts,
        detect_live_env=lambda: (facts.is_live, facts.live_marker or ""),
        ssh_exec=recording_ssh_exec,
        repo_search=lambda _query: "",
        community_search=lambda _query: "",
        web_search=lambda _query, _max_results: "",
        wait_for_reconnect=wait_for_reconnect
        or (lambda reconnect_user: (True, "fresh info", reconnect_user)),
        run_pre_reboot_checks=run_pre_reboot_checks or (lambda: (True, [], "ok")),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda _current, _is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=False,
    )
    orchestrator = SetupOrchestrator(runtime)
    runlog = MemoryRunLog()
    orchestrator.runlog = runlog  # type: ignore[assignment]
    orchestrator.ctx.facts = facts
    orchestrator.ctx.plan = plan or _plan(
        family=facts.distro_family, package_manager=facts.package_manager
    )
    return orchestrator, executed, runlog


@pytest.mark.parametrize(
    "command",
    [
        "sudo pacstrap -K /mnt base linux",
        "sudo debootstrap stable /mnt http://deb.debian.org/debian",
        "sudo dnf --installroot=/mnt install -y kernel",
        "sudo mkfs.ext4 /dev/sda1",
        "sudo parted -s /dev/sda mklabel gpt",
        "sudo wipefs -a /dev/sda",
        "sudo mount /dev/sda2 /mnt",
        "printf 'UUID=x / ext4 defaults 0 1\\n' | sudo tee /mnt/etc/fstab",
        "printf 'UUID=x / ext4 defaults 0 1\\n' | sudo tee /target/etc/fstab",
        "sudo grub-install /dev/sda",
        "sudo bootctl install",
        "sudo efibootmgr -o 0001",
    ],
)
def test_configure_existing_rejects_fresh_install_leakage_before_executor(command: str):
    facts = _facts(install_profile="configure_existing", remote_setup_mode="configure_only")
    group = _group(SetupStage.APPS, command, group_id="leak")
    orchestrator, executed, runlog = _orchestrator(
        facts=facts,
        profile=_profile(install_profile="configure_existing", remote_setup_mode="configure_only"),
        plan=_plan(group),
    )

    failure = orchestrator._execute_group(SetupStage.APPS, group, {})

    assert failure is not None
    assert failure.failure_type == "preflight"
    assert executed == []
    assert any(event["event_type"] == "command_preflight_blocked" for event in runlog.events)
    assert any(
        "Safety/precondition block" in item["content"] for item in orchestrator.bridge.messages
    )


def test_configure_existing_allows_safe_app_desktop_and_diagnostic_groups():
    facts = _facts(install_profile="configure_existing", remote_setup_mode="configure_only")
    commands = [
        "sudo pacman -S --noconfirm vlc",
        "pacman -Ss vlc",
        "flatpak install -y flathub com.spotify.Client",
        "snap find spotify",
        "kwriteconfig5 --file kdeglobals --group Icons --key Theme candy-icons",
        "systemctl status sshd",
        "journalctl -u sshd --no-pager",
        "lsblk",
        "findmnt",
        "blkid",
    ]
    group = CommandGroup(
        id="safe-configure",
        stage=SetupStage.APPS,
        title="Safe configure-existing work",
        commands=commands,
    )
    orchestrator, executed, _runlog = _orchestrator(
        facts=facts,
        profile=_profile(install_profile="configure_existing", remote_setup_mode="configure_only"),
        plan=_plan(group),
    )

    failure = orchestrator._execute_group(SetupStage.APPS, group, {})

    assert failure is None
    assert "sudo pacman -S --noconfirm vlc" in executed
    assert any(command.endswith("pacman -Ss vlc") for command in executed)
    for command in commands[2:5]:
        assert command in executed
    assert any(command.endswith("systemctl status sshd") for command in executed)
    assert any(command.endswith("journalctl -u sshd --no-pager") for command in executed)
    for command in commands[7:]:
        assert command in executed


def test_configure_existing_normal_setup_does_not_request_install_spine_capabilities(
    monkeypatch: pytest.MonkeyPatch,
):
    facts = _facts(install_profile="configure_existing", remote_setup_mode="configure_only")
    group = _group(
        SetupStage.APPS,
        "sudo pacman -S --noconfirm vlc",
        group_id="safe-app-install",
    )
    orchestrator, executed, runlog = _orchestrator(
        facts=facts,
        profile=_profile(install_profile="configure_existing", remote_setup_mode="configure_only"),
        plan=_plan(group),
    )
    monkeypatch.setattr(
        orchestrator,
        "_grant_family_spine_capabilities",
        lambda *_args, **_kwargs: pytest.fail(
            "configure-existing requested install spine capabilities"
        ),
    )

    orchestrator._run_stage_groups(SetupStage.APPS, audit_after=False)

    assert executed[-1] == "sudo pacman -S --noconfirm vlc"
    assert orchestrator.ctx.capability_store is not None
    assert orchestrator.ctx.capability_store.to_payload()["grants"] == []
    install_capability_kinds = {
        "disk_partition",
        "format_filesystem",
        "mount_target",
        "write_fstab",
        "bootloader_install",
        "controlled_reboot",
    }
    granted = [
        event.get("capability", {}).get("kind")
        for event in runlog.events
        if event["event_type"] == "capability_granted"
    ]
    assert not (set(granted) & install_capability_kinds)


def test_fresh_install_without_confirmed_install_spine_blocks_before_executor():
    facts = _facts(
        install_profile="full_disk_wipe",
        remote_setup_mode="wipe_and_reinstall",
        is_live=True,
    )
    orchestrator, executed, runlog = _orchestrator(
        facts=facts,
        profile=_profile(install_profile="full_disk_wipe", remote_setup_mode="wipe_and_reinstall"),
        plan=_plan(family="arch", package_manager="pacman"),
    )

    with pytest.raises(RuntimeError, match="explicit confirmed target disk"):
        orchestrator._run_install_or_skip()

    assert executed == []
    assert any(
        event["event_type"] == "family_adapter_spine_not_enforced" for event in runlog.events
    )


def test_final_audit_blocks_when_fresh_install_still_reports_live_environment(
    monkeypatch: pytest.MonkeyPatch,
):
    facts = _facts(
        install_profile="full_disk_wipe",
        remote_setup_mode="wipe_and_reinstall",
        is_live=True,
    )
    orchestrator, _executed, _runlog = _orchestrator(
        facts=facts,
        profile=_profile(install_profile="full_disk_wipe", remote_setup_mode="wipe_and_reinstall"),
        plan=_plan(family="arch", package_manager="pacman"),
    )
    orchestrator.ctx.stage_results = [
        AuditResult(stage=stage, passed=True)
        for stage in (
            SetupStage.CONNECT_AND_PROBE,
            SetupStage.RESEARCH_AND_PLAN,
            SetupStage.INSTALL_OR_SKIP,
            SetupStage.POSTBOOT_DRIVER,
            SetupStage.DESKTOP,
            SetupStage.APPS,
        )
    ]
    monkeypatch.setattr(orchestrator, "_remote_final_verification", lambda: [])

    result = orchestrator._run_final_audit()

    assert result.final_status == FinalSetupStatus.BLOCKED
    assert result.passed is False
    assert any("live environment" in blocker for blocker in result.blockers)
    assert orchestrator.bridge.snapshot_setup_run()["status"] == "blocked"


@pytest.mark.parametrize("stage", [SetupStage.POSTBOOT_DRIVER, SetupStage.DESKTOP, SetupStage.APPS])
def test_fresh_install_postboot_stages_do_not_run_while_facts_are_live(stage: SetupStage):
    facts = _facts(
        install_profile="full_disk_wipe",
        remote_setup_mode="wipe_and_reinstall",
        is_live=True,
    )
    group = _group(stage, "sudo pacman -S --noconfirm vlc", group_id=f"{stage.value}-early")
    orchestrator, executed, runlog = _orchestrator(
        facts=facts,
        profile=_profile(install_profile="full_disk_wipe", remote_setup_mode="wipe_and_reinstall"),
        plan=_plan(group, family="arch", package_manager="pacman"),
    )

    with pytest.raises(RuntimeError, match=re.escape(stage.value)):
        orchestrator._run_stage_groups(stage, audit_after=False)

    assert executed == []
    stage_state = orchestrator.ctx.stage_state[stage.value]
    assert "live_environment_stage_block" in stage_state
    assert any(
        event["event_type"] == "postboot_stage_blocked_live_environment" for event in runlog.events
    )


def test_stop_request_prevents_stage_group_execution():
    facts = _facts(install_profile="configure_existing", remote_setup_mode="configure_only")
    group = _group(SetupStage.APPS, "sudo pacman -S --noconfirm vlc", group_id="after-stop")
    orchestrator, executed, _runlog = _orchestrator(
        facts=facts,
        profile=_profile(install_profile="configure_existing", remote_setup_mode="configure_only"),
        plan=_plan(group),
    )
    orchestrator.bridge.request_stop()

    with pytest.raises(RuntimeError, match="Agent stopped by user"):
        orchestrator._run_stage_groups(SetupStage.APPS, audit_after=False)

    assert executed == []


def test_safe_abort_prevents_later_stage_groups(monkeypatch: pytest.MonkeyPatch):
    facts = _facts(install_profile="configure_existing", remote_setup_mode="configure_only")
    abort_group = _group(SetupStage.APPS, "echo abort", group_id="abort")
    later_group = _group(SetupStage.APPS, "sudo mkfs.ext4 /dev/sda1", group_id="later")
    orchestrator, executed, _runlog = _orchestrator(
        facts=facts,
        profile=_profile(install_profile="configure_existing", remote_setup_mode="configure_only"),
        plan=_plan(abort_group, later_group),
    )
    calls: list[str] = []

    def execute_group(
        stage: SetupStage,
        group: CommandGroup,
        stage_state: dict[str, Any],
    ) -> GroupFailure | None:
        del stage
        calls.append(group.id)
        if group.id == "abort":
            stage_state["safe_abort"] = "pre-reboot audit failed"
            return GroupFailure(
                failure_type="safe_abort",
                command="pre-reboot audit",
                stdout="",
                stderr="",
                detail="pre-reboot audit failed",
            )
        pytest.fail("later stage group ran after safe abort")

    monkeypatch.setattr(orchestrator, "_execute_group", execute_group)

    with pytest.raises(RuntimeError, match="pre-reboot audit failed"):
        orchestrator._run_stage_groups(SetupStage.APPS, audit_after=False)

    assert calls == ["abort"]
    assert executed == []
