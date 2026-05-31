from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pytest

from quickhatch.bridge.server import BridgeState
from quickhatch.config import AIConfig, UserProfile
from quickhatch.setup.orchestrator import SetupOrchestrator, SetupRuntime
from quickhatch.setup.types import (
    AppPlanItem,
    CommandGroup,
    DesktopRuntimeState,
    DisplayRuntimeInfo,
    FinalSetupStatus,
    PlanItemStatus,
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


class FactSequence:
    def __init__(self, *facts: SetupFacts) -> None:
        self._facts = list(facts)
        self.calls: list[dict[str, Any]] = []

    def detect_live_env(self) -> tuple[bool, str]:
        if self._facts:
            facts = self._facts[0]
            return facts.is_live, facts.live_marker
        return False, "INSTALLED"

    def probe_facts(self, **kwargs: Any) -> SetupFacts:
        self.calls.append(dict(kwargs))
        if self._facts:
            return self._facts.pop(0)
        fallback = kwargs.get("fallback")
        return fallback if isinstance(fallback, SetupFacts) else _facts()


class FakeSSH:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.precheck_outputs: list[str] = []
        self.command_results: dict[str, tuple[int, str, str]] = {}
        self.final_packages: set[str] = set()

    def __call__(self, command: str, _timeout: int) -> tuple[int, str, str]:
        self.commands.append(command)
        if "for p in" in command and "INSTALLED:%s" in command:
            if self.precheck_outputs:
                return (0, self.precheck_outputs.pop(0), "")
            return (0, "", "")
        if command in self.command_results:
            return self.command_results[command]
        for marker, result in self.command_results.items():
            if marker in command:
                return result
        if "rpm -qa --queryformat" in command:
            return (0, "\n".join(sorted(self.final_packages)) + "\n", "")
        if "dpkg-query -W -f='${db:Status-Abbrev} ${Package}\\n'" in command:
            return (0, "\n".join(sorted(self.final_packages)) + "\n", "")
        if "pacman -Qq" in command:
            return (0, "\n".join(sorted(self.final_packages)) + "\n", "")
        if "flatpak list --app" in command or "snap list" in command:
            return (0, "", "")
        if "QH_REBOOT_REQUIRED=" in command:
            return (0, "QH_REBOOT_REQUIRED=no\nQH_REBOOT_DETAIL=\n", "")
        return (0, "", "")


def _facts(
    *,
    family: str = "ubuntu",
    package_manager: str = "apt",
    is_live: bool = False,
    community_helper: str | None = None,
    desktop_runtime_state: DesktopRuntimeState = DesktopRuntimeState.PENDING_RELOGIN,
    displays: list[DisplayRuntimeInfo] | None = None,
    session_type: str = "",
) -> SetupFacts:
    return SetupFacts(
        distro_family=family,
        os_id=family,
        package_manager=package_manager,
        community_helper=community_helper
        if community_helper is not None
        else "yay"
        if package_manager == "pacman"
        else "none",
        init_system="systemd",
        current_user="root" if is_live else "jan",
        login_user="root" if is_live else "jan",
        is_root=is_live,
        sudo_nopasswd=not is_live,
        is_live=is_live,
        live_marker="LIVE" if is_live else "INSTALLED",
        desktop_runtime_state=desktop_runtime_state,
        display_manager="sddm" if not is_live else "",
        display_manager_enabled=not is_live,
        display_manager_active=bool(session_type),
        session_type=session_type,
        displays=displays or [],
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


def _plan(
    *groups: CommandGroup,
    apps: list[AppPlanItem] | None = None,
    family: str = "ubuntu",
    package_manager: str = "apt",
) -> ResearchPlan:
    return ResearchPlan(
        target_summary="facts refresh test",
        distro_family=family,
        package_manager=package_manager,
        driver_strategy=StrategyDecision("none", confidence="resolved_official", source="repo"),
        desktop_strategy=StrategyDecision("none", confidence="resolved_official", source="repo"),
        command_groups=list(groups),
        apps=apps or [],
    )


def _app_group(
    command: str,
    *,
    packages: list[str],
    group_id: str = "apps",
) -> CommandGroup:
    return CommandGroup(
        id=group_id,
        stage=SetupStage.APPS,
        title="Install apps",
        commands=[command],
        packages=packages,
        source="repo",
    )


def _desktop_group(command: str, *, group_id: str = "desktop") -> CommandGroup:
    return CommandGroup(
        id=group_id,
        stage=SetupStage.DESKTOP,
        title="Apply live desktop settings",
        commands=[command],
    )


def _orchestrator(
    *,
    facts: SetupFacts,
    plan: ResearchPlan,
    fact_sequence: FactSequence | None = None,
    ssh: FakeSSH | None = None,
    profile: UserProfile | None = None,
) -> tuple[SetupOrchestrator, FakeSSH, FactSequence, MemoryRunLog]:
    profile = profile or _profile()
    bridge = BridgeState(profile, persist_path=None)
    ssh = ssh or FakeSSH()
    fact_sequence = fact_sequence or FactSequence(facts)
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
        probe_facts=fact_sequence.probe_facts,
        detect_live_env=fact_sequence.detect_live_env,
        ssh_exec=ssh,
        repo_search=lambda _query: "",
        community_search=lambda _query: "",
        web_search=lambda _query, _max_results: "",
        wait_for_reconnect=lambda reconnect_user: (True, "fresh info", reconnect_user),
        run_pre_reboot_checks=lambda: (True, [], "ok"),
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
    orchestrator.ctx.plan = plan
    return orchestrator, ssh, fact_sequence, runlog


def _vlc(status: PlanItemStatus = PlanItemStatus.PENDING) -> AppPlanItem:
    return AppPlanItem("VLC", package_name="vlc", source="repo", required=True, status=status)


def test_refresh_after_successful_app_group_feeds_final_app_verification() -> None:
    before = _facts(package_manager="apt", family="ubuntu")
    after = _facts(package_manager="dnf", family="fedora")
    app = _vlc()
    group = _app_group("sudo apt install -y vlc", packages=["vlc"])
    ssh = FakeSSH()
    ssh.precheck_outputs = ["MISSING:vlc\n"]
    ssh.final_packages = {"vlc"}
    orchestrator, ssh, facts, runlog = _orchestrator(
        facts=before,
        plan=_plan(group, apps=[app]),
        fact_sequence=FactSequence(after),
        ssh=ssh,
    )

    orchestrator._run_stage_groups(SetupStage.APPS)
    blockers = orchestrator._verify_planned_packages_installed()

    assert blockers == []
    assert facts.calls
    assert orchestrator.ctx.facts is after
    assert app.status == PlanItemStatus.INSTALLED
    assert (
        "VLC"
        in orchestrator.ctx.stage_state[SetupStage.FINAL_AUDIT.value]["verified_installed_apps"]
    )
    assert any("rpm -qa --queryformat" in command for command in ssh.commands)
    assert any(event["event_type"] == "facts_refresh" for event in runlog.events)


def test_partial_app_success_survives_later_repair_manual_followup() -> None:
    facts = _facts()
    vlc = _vlc()
    kdenlive = AppPlanItem(
        "Kdenlive",
        package_name="kdenlive",
        source="repo",
        required=True,
    )
    group = _app_group("sudo apt install -y vlc kdenlive", packages=["vlc", "kdenlive"])
    ssh = FakeSSH()
    ssh.precheck_outputs = [
        "MISSING:vlc\nMISSING:kdenlive\n",
        "INSTALLED:vlc\nMISSING:kdenlive\n",
    ]
    ssh.command_results["install -y vlc kdenlive"] = (
        1,
        "",
        "Unable to locate package kdenlive",
    )
    ssh.final_packages = {"vlc"}
    orchestrator, _ssh, _facts_seq, _runlog = _orchestrator(
        facts=facts,
        plan=_plan(group, apps=[vlc, kdenlive]),
        fact_sequence=FactSequence(facts, facts),
        ssh=ssh,
    )
    orchestrator._repair_failed_group = lambda *_args, **_kwargs: {
        "action": "manual_followup",
        "reason": "Kdenlive package needs manual provider selection.",
        "app_names": ["Kdenlive"],
    }
    orchestrator._missing_repo_targets_from_output = lambda *_args, **_kwargs: []

    orchestrator._run_stage_groups(SetupStage.APPS)
    result = orchestrator._compute_final_audit_result()

    assert vlc.status == PlanItemStatus.INSTALLED
    assert kdenlive.status == PlanItemStatus.MANUAL_FOLLOWUP
    assert "Kdenlive package needs manual provider selection." in kdenlive.reason
    assert orchestrator.ctx.stage_state[SetupStage.FINAL_AUDIT.value][
        "verified_installed_apps"
    ] == ["VLC"]
    assert result.final_status == FinalSetupStatus.MANUAL_FOLLOWUP_REQUIRED
    assert not result.blockers


def test_aur_helper_bootstrap_updates_package_manager_state_before_retry() -> None:
    before = _facts(package_manager="pacman", family="arch", community_helper="none")
    after = _facts(package_manager="pacman", family="arch", community_helper="yay")
    group = _app_group("paru -S --noconfirm spotify", packages=["spotify"], group_id="aur")
    ssh = FakeSSH()
    orchestrator, ssh, facts, _runlog = _orchestrator(
        facts=before,
        plan=_plan(group, family="arch", package_manager="pacman"),
        fact_sequence=FactSequence(after),
        ssh=ssh,
    )

    failure = orchestrator._execute_group(SetupStage.APPS, group, {})

    assert failure is None
    assert orchestrator.ctx.facts.community_helper == "yay"
    assert any("quickhatch-yay-bin" in command for command in ssh.commands)
    assert any(command.startswith("yay -S --noconfirm spotify") for command in ssh.commands)
    assert not any(command.startswith("paru -S --noconfirm spotify") for command in ssh.commands)


def test_reboot_reconnect_fact_transition_gates_postboot_work_and_final_audit() -> None:
    live = _facts(is_live=True)
    installed = _facts(
        is_live=False,
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
        displays=[DisplayRuntimeInfo(name="HDMI-A-1", current_mode="1920x1080")],
        session_type="wayland",
    )
    group = _app_group("sudo apt install -y vlc", packages=["vlc"])
    ssh = FakeSSH()
    ssh.precheck_outputs = ["MISSING:vlc\n"]
    orchestrator, ssh, _facts_seq, _runlog = _orchestrator(
        facts=live,
        plan=_plan(group, apps=[_vlc()]),
        fact_sequence=FactSequence(installed),
        ssh=ssh,
    )

    with pytest.raises(RuntimeError, match="live environment"):
        orchestrator._run_stage_groups(SetupStage.APPS)
    assert group.commands[0] not in ssh.commands

    orchestrator._refresh_facts(stage=SetupStage.APPS)
    orchestrator._run_stage_groups(SetupStage.APPS)

    assert orchestrator.ctx.facts is installed
    assert any("install -y vlc" in command for command in ssh.commands)

    live_audit, *_ = _orchestrator(
        facts=live,
        plan=_plan(),
        fact_sequence=FactSequence(live),
        ssh=FakeSSH(),
    )
    live_result = live_audit._compute_final_audit_result()
    assert live_result.final_status == FinalSetupStatus.BLOCKED
    assert any("live environment" in blocker for blocker in live_result.blockers)

    installed_audit, *_ = _orchestrator(
        facts=live,
        plan=_plan(),
        fact_sequence=FactSequence(installed),
        ssh=FakeSSH(),
    )
    installed_result = installed_audit._compute_final_audit_result()
    assert installed_result.final_status == FinalSetupStatus.SUCCESS
    assert not any(
        "live environment" in finding.summary.lower() for finding in installed_result.findings
    )


def test_desktop_display_refresh_clears_stale_no_gui_manual_followup() -> None:
    no_display = _facts(
        desktop_runtime_state=DesktopRuntimeState.BLOCKED_NO_GUI_SESSION,
        displays=[],
        session_type="",
    )
    with_display = _facts(
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
        displays=[DisplayRuntimeInfo(name="HDMI-A-1", current_mode="1920x1080")],
        session_type="wayland",
    )
    group = _desktop_group("xrandr --query")
    orchestrator, _ssh, _facts_seq, _runlog = _orchestrator(
        facts=no_display,
        plan=_plan(group),
        fact_sequence=FactSequence(no_display),
    )
    orchestrator._ask_blocking_question = lambda *_args, **_kwargs: "manual follow-up"

    orchestrator._run_stage_groups(SetupStage.DESKTOP, audit_after=False)

    stage_state = orchestrator.ctx.stage_state[SetupStage.DESKTOP.value]
    assert group.status == PlanItemStatus.MANUAL_FOLLOWUP
    assert stage_state["display_manual_followup"] is True

    orchestrator.runtime.probe_facts = FactSequence(with_display).probe_facts
    orchestrator.runtime.detect_live_env = lambda: (False, "INSTALLED")
    orchestrator._run_stage_groups(SetupStage.DESKTOP, audit_after=False)
    orchestrator._update_desktop_display_audit_state(stage_state)
    fresh_desktop = orchestrator._compute_final_audit_result().details["fresh_stage_audits"][
        SetupStage.DESKTOP.value
    ]

    assert group.status == PlanItemStatus.COMPLETED
    assert stage_state.get("display_manual_followup") is not True
    assert not stage_state.get("display_manual_followup_reason")
    assert not stage_state.get("manual_followups")
    assert fresh_desktop["details"]["desktop_manual_followups"] == []


def test_final_audit_uses_latest_refreshed_facts() -> None:
    old_live = _facts(is_live=True)
    installed = _facts(
        is_live=False,
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
        displays=[DisplayRuntimeInfo(name="HDMI-A-1", current_mode="1920x1080")],
        session_type="wayland",
    )
    orchestrator, _ssh, facts, _runlog = _orchestrator(
        facts=old_live,
        plan=_plan(),
        fact_sequence=FactSequence(installed),
    )

    result = orchestrator._compute_final_audit_result()

    assert facts.calls
    assert orchestrator.ctx.facts is installed
    assert result.final_status == FinalSetupStatus.SUCCESS
    assert not any("live environment" in finding.summary.lower() for finding in result.findings)


def test_unresolved_manual_followup_survives_fact_refresh() -> None:
    before = _facts()
    after = _facts()
    app_followup = {"app": "Kdenlive", "reason": "Package provider needs manual selection."}
    orchestrator, _ssh, _facts_seq, _runlog = _orchestrator(
        facts=before,
        plan=_plan(apps=[_vlc()]),
        fact_sequence=FactSequence(after),
    )
    orchestrator.ctx.stage_state[SetupStage.APPS.value] = {
        "manual_followups": [app_followup],
        "failed_verifications": ["old provider failure"],
    }

    refreshed = orchestrator._refresh_facts(stage=SetupStage.APPS)

    assert refreshed is after
    assert orchestrator.ctx.stage_state[SetupStage.APPS.value]["manual_followups"] == [app_followup]
    assert orchestrator.ctx.stage_state[SetupStage.APPS.value]["failed_verifications"] == [
        "old provider failure"
    ]
    assert asdict(refreshed)["package_manager"] == "apt"
