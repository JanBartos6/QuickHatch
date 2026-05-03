"""Fast local harness health checks.

This module intentionally avoids SSH and real VM mutation. It exercises the
same deterministic adapter/certification/preflight/classifier paths that full
setup runs depend on, so harness regressions can be caught in seconds.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from quickhatch.bridge.server import BridgeState
from quickhatch.config import AIConfig, UserProfile

from .certify import CERTIFIED_FAMILIES, run_vm_certification
from .facts import setup_fact_probe_command
from .failure import classify_failure
from .families import get_family_installer
from .orchestrator import SetupOrchestrator, SetupRuntime
from .preflight import preflight_setup_decisions
from .types import (
    AppPlanItem,
    CommandGroup,
    DesktopDesignState,
    DesktopRuntimeState,
    FailureCategory,
    PlanItemStatus,
    PreflightDecisionKind,
    ResearchPlan,
    SetupFacts,
    SetupStage,
    StrategyDecision,
)


def _enum_value(value: Any) -> Any:
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, dict):
        return {key: _enum_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_enum_value(item) for item in value]
    return value


@contextmanager
def _health_workspace(root: Path | str | None, keep_artifacts: bool) -> Iterator[Path]:
    if root:
        root_path = Path(root)
        root_path.mkdir(parents=True, exist_ok=True)
        if keep_artifacts:
            yield root_path
            return
        path = Path(tempfile.mkdtemp(prefix="run-", dir=root_path))
        try:
            yield path
        finally:
            shutil.rmtree(path, ignore_errors=True)
        return

    with tempfile.TemporaryDirectory(prefix="quickhatch-harness-health-") as tmp:
        yield Path(tmp)


def _record(
    checks: list[dict[str, Any]],
    *,
    name: str,
    passed: bool,
    category: str,
    details: dict[str, Any] | None = None,
) -> None:
    checks.append(
        {
            "name": name,
            "category": category,
            "passed": passed,
            "details": details or {},
        }
    )


def _run_certification_dry_run_checks(workspace: Path) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    input_dir = workspace / "inputs"
    input_dir.mkdir(parents=True, exist_ok=True)
    profile = input_dir / "profile.json"
    profile.write_text(json.dumps({}), encoding="utf-8")
    cert_root = workspace / "certification"

    for family in sorted(CERTIFIED_FAMILIES):
        iso = input_dir / f"{family}.iso"
        iso.write_text("fake iso", encoding="utf-8")
        result = run_vm_certification(
            family=family,
            iso=iso,
            profile=profile,
            root=cert_root,
            dry_run=True,
        )
        artifact_dir = Path(result.artifact_dir)
        expected_files = [
            "summary.json",
            "adapter-spine.json",
            "certify-install.sh",
            "certification-run.jsonl",
            "final-facts.json",
        ]
        missing = [name for name in expected_files if not (artifact_dir / name).exists()]
        _record(
            checks,
            name=f"certify_dry_run_{family}",
            category="certification",
            passed=result.success and not missing,
            details={
                "status": result.status,
                "artifact_dir": str(artifact_dir),
                "missing_artifacts": missing,
                "blockers": list(result.blockers),
            },
        )
    return checks


def _run_preflight_policy_checks() -> list[dict[str, Any]]:
    scenarios = [
        {
            "name": "aur_helper_missing_is_advisory",
            "command": "yay -S --noconfirm brave-bin",
            "facts": SetupFacts(package_manager="pacman", community_helper="none"),
            "expected_kind": PreflightDecisionKind.ADVISORY.value,
            "expected_category": "guidance",
        },
        {
            "name": "privilege_precondition_is_shadow",
            "command": "pacman -S --noconfirm nvidia-open",
            "facts": SetupFacts(
                distro_family="arch",
                package_manager="pacman",
                current_user="jan",
                login_user="jan",
                is_root=False,
            ),
            "expected_kind": PreflightDecisionKind.SHADOW.value,
            "expected_category": "privilege_precondition",
        },
        {
            "name": "sudo_password_prompt_is_block",
            "command": "sudo pacman -S --noconfirm nvidia-open",
            "facts": SetupFacts(
                distro_family="arch",
                package_manager="pacman",
                current_user="jan",
                login_user="jan",
                is_live=False,
                is_root=False,
                sudo_nopasswd=False,
            ),
            "expected_kind": PreflightDecisionKind.BLOCK.value,
            "expected_category": "sudo_password",
        },
        {
            "name": "vpn_activation_is_block",
            "command": "sudo systemctl enable --now mullvad-daemon",
            "facts": SetupFacts(package_manager="pacman", community_helper="yay"),
            "expected_kind": PreflightDecisionKind.BLOCK.value,
            "expected_category": "ssh_continuity",
        },
    ]
    checks: list[dict[str, Any]] = []
    for scenario in scenarios:
        decisions = [
            _enum_value(asdict(decision))
            for decision in preflight_setup_decisions(
                scenario["command"],
                scenario["facts"],
            )
        ]
        passed = any(
            decision["kind"] == scenario["expected_kind"]
            and decision["category"] == scenario["expected_category"]
            for decision in decisions
        )
        _record(
            checks,
            name=str(scenario["name"]),
            category="preflight",
            passed=passed,
            details={
                "command": scenario["command"],
                "expected_kind": scenario["expected_kind"],
                "expected_category": scenario["expected_category"],
                "decisions": decisions,
            },
        )
    return checks


def _run_failure_classifier_checks() -> list[dict[str, Any]]:
    scenarios = [
        {
            "name": "arch_mirrorlist_no_servers_is_network",
            "command": "pacman -Sy --noconfirm archlinux-keyring",
            "stderr": "error: failed to synchronize all databases (no servers configured for repository)",
            "expected": FailureCategory.NETWORK.value,
        },
        {
            "name": "missing_package_is_package_missing",
            "command": "sudo pacman -S nope",
            "stderr": "error: target not found: nope",
            "expected": FailureCategory.PACKAGE_MISSING.value,
        },
        {
            "name": "plain_sudo_password_is_sudo_password",
            "command": "sudo pacman -S nvidia-open",
            "stderr": "sudo: a terminal is required to read the password",
            "expected": FailureCategory.SUDO_PASSWORD.value,
        },
        {
            "name": "nvidia_sysfs_probe_failure_is_driver_pending_reboot",
            "command": "cat /sys/module/nvidia_drm/parameters/modeset | grep -iq y",
            "stderr": "cat: /sys/module/nvidia_drm/parameters/modeset: Permission denied",
            "expected": FailureCategory.DRIVER_PENDING_REBOOT.value,
        },
    ]
    checks: list[dict[str, Any]] = []
    for scenario in scenarios:
        result = classify_failure(
            command=scenario["command"],
            rc=1,
            stderr=scenario["stderr"],
        )
        payload = _enum_value(asdict(result))
        _record(
            checks,
            name=str(scenario["name"]),
            category="failure_classifier",
            passed=payload["category"] == scenario["expected"],
            details={
                "command": scenario["command"],
                "stderr": scenario["stderr"],
                "expected": scenario["expected"],
                "actual": payload,
            },
        )
    return checks


def _dummy_setup_runtime(
    profile: UserProfile,
    bridge: BridgeState,
    facts: SetupFacts,
    *,
    ssh_exec: Any | None = None,
    probe_facts: Any | None = None,
) -> SetupRuntime:
    return SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(provider="health", model_name="health"),
        host="127.0.0.1",
        user=facts.current_user or "jan",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "harness health system info",
        call_model_text=lambda _cfg, _messages: "{}",
        probe_facts=probe_facts or (lambda **_kwargs: facts),
        detect_live_env=lambda: (facts.is_live, facts.live_marker or "INSTALLED"),
        ssh_exec=ssh_exec or (lambda _command, _timeout: (0, "", "")),
        repo_search=lambda _query: "",
        community_search=lambda _query: "",
        web_search=lambda _query, _max_results: "",
        wait_for_reconnect=lambda reconnect_user: (True, "reconnected", reconnect_user),
        run_pre_reboot_checks=lambda: (True, [], ""),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda _current, _is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=False,
    )


def _run_final_audit_semantic_checks() -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    checks.extend(_run_package_final_audit_checks())
    checks.extend(_run_desktop_final_audit_checks())
    checks.extend(_run_fact_probe_semantic_checks())
    return checks


def _run_resume_policy_checks() -> list[dict[str, Any]]:
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    stale_facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        gpu_vendors=["nvidia"],
        gpu_inventory=["NVIDIA GA102 [GeForce RTX 3080]"],
        current_user="jan",
        login_user="jan",
        sudo_nopasswd=True,
        nvidia_smi_state="present_but_failing",
        nvidia_kernel_driver="driver",
        nvidia_nouveau_bound=False,
    )
    healthy_facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        gpu_vendors=["nvidia"],
        gpu_inventory=["NVIDIA GA102 [GeForce RTX 3080]"],
        current_user="jan",
        login_user="jan",
        sudo_nopasswd=True,
        nvidia_smi_state="ok",
        nvidia_kernel_driver="nvidia",
        nvidia_nouveau_bound=False,
        driver_modules=["nvidia", "nvidia_drm"],
    )
    executed: list[str] = []
    runtime = _dummy_setup_runtime(
        profile,
        bridge,
        stale_facts,
        ssh_exec=lambda command, _timeout: executed.append(command) or (0, "", ""),
        probe_facts=lambda **_kwargs: healthy_facts,
    )
    orchestrator = SetupOrchestrator(runtime)
    orchestrator.ctx.facts = stale_facts
    orchestrator.ctx.plan = ResearchPlan(
        target_summary="Arch NVIDIA workstation",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision(
            "nvidia-open",
            confidence="resolved_official",
            source="repo",
            packages=["nvidia-open", "nvidia-utils"],
        ),
        desktop_strategy=StrategyDecision("kde", confidence="resolved_official", source="repo"),
        command_groups=[
            CommandGroup(
                id="driver",
                stage=SetupStage.POSTBOOT_DRIVER,
                title="NVIDIA Driver & Kernel Configuration",
                commands=[
                    "sudo pacman -Syu --needed --noconfirm linux-headers nvidia-open nvidia-utils",
                    "sudo mkinitcpio -P",
                ],
                verification=["nvidia-smi"],
                packages=["nvidia-open", "nvidia-utils"],
            )
        ],
    )
    orchestrator._run_postboot_driver()
    stage_state = orchestrator.ctx.stage_state.get(SetupStage.POSTBOOT_DRIVER.value, {})
    checks: list[dict[str, Any]] = []
    _record(
        checks,
        name="resume_postboot_driver_audits_before_mutating",
        category="resume_policy",
        passed=not executed and stage_state.get("skipped_current_state_already_valid") is True,
        details={"executed_commands": executed, "stage_state": stage_state},
    )
    return checks


def _run_app_stage_prefilter_checks() -> list[dict[str, Any]]:
    cases = [
        (
            "arch",
            "pacman",
            "sudo pacman -S --noconfirm git python",
            "sudo pacman -S --needed --noconfirm python",
        ),
        (
            "ubuntu",
            "apt",
            "sudo apt-get install -y git python3",
            "sudo env DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=2 "
            "-o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20 "
            "-o Acquire::ForceIPv4=true install -y python3",
        ),
        (
            "debian",
            "apt",
            "sudo apt-get install -y git python3",
            "sudo env DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=2 "
            "-o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20 "
            "-o Acquire::ForceIPv4=true install -y python3",
        ),
        ("fedora", "dnf", "sudo dnf install -y git python3", "sudo dnf install -y python3"),
    ]
    checks: list[dict[str, Any]] = []
    for family, package_manager, command, expected in cases:
        profile = UserProfile()
        bridge = BridgeState(profile, persist_path=None)
        facts = SetupFacts(
            distro_family=family,
            package_manager=package_manager,
            current_user="jan",
            sudo_nopasswd=True,
        )
        executed: list[str] = []

        def ssh_exec(
            remote_command: str,
            _timeout: int,
            _executed: list[str] = executed,
        ) -> tuple[int, str, str]:
            _executed.append(remote_command)
            if "INSTALLED:" in remote_command:
                return (0, "INSTALLED:git\nMISSING:python3\nMISSING:python\n", "")
            return (0, "", "")

        runtime = _dummy_setup_runtime(profile, bridge, facts, ssh_exec=ssh_exec)
        orchestrator = SetupOrchestrator(runtime)
        orchestrator.ctx.facts = facts
        orchestrator.ctx.plan = ResearchPlan(
            target_summary=f"{family} workstation.",
            distro_family=family,
            package_manager=package_manager,
            driver_strategy=StrategyDecision("none"),
            desktop_strategy=StrategyDecision("desktop"),
            command_groups=[
                CommandGroup(
                    id=f"apps-{family}",
                    stage=SetupStage.APPS,
                    title="Install apps",
                    commands=[command],
                    packages=["git", "python" if package_manager == "pacman" else "python3"],
                )
            ],
        )
        orchestrator._run_stage_groups(SetupStage.APPS, audit_after=False)
        expected_package = "python" if package_manager == "pacman" else "python3"
        exact_filtered_command_seen = expected in executed
        filtered_install_seen = any(
            expected_package in remote_command and "git" not in remote_command
            for remote_command in executed
        )
        _record(
            checks,
            name=f"apps_prefilter_skips_installed_packages_{family}",
            category="apps_prefilter",
            passed=exact_filtered_command_seen
            and filtered_install_seen
            and command not in executed,
            details={
                "executed_commands": executed,
                "expected_filtered_command": expected,
                "original_command": command,
            },
        )
    return checks


def _run_package_final_audit_checks() -> list[dict[str, Any]]:
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(distro_family="arch", package_manager="pacman", current_user="jan")
    installed_packages = "\n".join(
        [
            "bitwarden",
            "claude-desktop-bin",
            "discord",
            "git",
            "github-desktop-bin",
            "openai-codex",
            "steam",
            "stirling-pdf-bin",
        ]
    )
    runtime = _dummy_setup_runtime(
        profile,
        bridge,
        facts,
        ssh_exec=lambda _command, _timeout: (0, installed_packages, ""),
    )
    orchestrator = SetupOrchestrator(runtime)
    orchestrator.ctx.facts = facts
    orchestrator.ctx.plan = ResearchPlan(
        target_summary="Arch workstation.",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision("none"),
        desktop_strategy=StrategyDecision("plasma"),
        apps=[
            AppPlanItem(name="Bitwarden", source="repo", status=PlanItemStatus.INSTALLED),
            AppPlanItem(name="Claude", source="community", status=PlanItemStatus.INSTALLED),
            AppPlanItem(name="Codex", source="community", status=PlanItemStatus.INSTALLED),
            AppPlanItem(name="Discord", source="repo", status=PlanItemStatus.INSTALLED),
            AppPlanItem(name="Git", source="repo", status=PlanItemStatus.INSTALLED),
            AppPlanItem(
                name="GitHub Desktop",
                source="community",
                status=PlanItemStatus.INSTALLED,
            ),
            AppPlanItem(name="Steam", source="repo", status=PlanItemStatus.INSTALLED),
            AppPlanItem(name="Stirling-PDF", source="community", status=PlanItemStatus.INSTALLED),
        ],
    )

    blockers = orchestrator._verify_planned_packages_installed()
    final_state = orchestrator.ctx.stage_state.get(SetupStage.FINAL_AUDIT.value, {})
    checks: list[dict[str, Any]] = []
    _record(
        checks,
        name="final_audit_infers_known_app_aliases",
        category="final_audit",
        passed=not blockers and not final_state.get("unverified_installed_apps"),
        details={
            "blockers": blockers,
            "verified_installed_apps": final_state.get("verified_installed_apps", []),
            "unverified_installed_apps": final_state.get("unverified_installed_apps", []),
        },
    )

    unknown_runtime = _dummy_setup_runtime(
        profile,
        bridge,
        facts,
        ssh_exec=lambda _command, _timeout: (0, "bitwarden\n", ""),
    )
    unknown_orchestrator = SetupOrchestrator(unknown_runtime)
    unknown_orchestrator.ctx.facts = facts
    unknown_orchestrator.ctx.plan = ResearchPlan(
        target_summary="Arch workstation.",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision("none"),
        desktop_strategy=StrategyDecision("plasma"),
        apps=[
            AppPlanItem(
                name="Totally Unknown Vendor App",
                source="manual_followup",
                status=PlanItemStatus.INSTALLED,
            )
        ],
    )
    unknown_blockers = unknown_orchestrator._verify_planned_packages_installed()
    unknown_state = unknown_orchestrator.ctx.stage_state.get(SetupStage.FINAL_AUDIT.value, {})
    _record(
        checks,
        name="final_audit_records_unknown_installed_app_without_identifier_as_followup",
        category="final_audit",
        passed=unknown_blockers == []
        and {
            "app": "Totally Unknown Vendor App",
            "reason": "installed package was not found under the planned identifiers",
        }
        in unknown_state.get("app_verification_manual_followups", []),
        details={
            "blockers": unknown_blockers,
            "unverified_installed_apps": unknown_state.get("unverified_installed_apps", []),
            "app_verification_manual_followups": unknown_state.get(
                "app_verification_manual_followups", []
            ),
        },
    )

    manual_runtime = _dummy_setup_runtime(profile, bridge, facts)
    manual_orchestrator = SetupOrchestrator(manual_runtime)
    manual_orchestrator.ctx.facts = facts
    manual_orchestrator.ctx.plan = ResearchPlan(
        target_summary="Arch workstation.",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision("none"),
        desktop_strategy=StrategyDecision("plasma"),
        apps=[
            AppPlanItem(
                name="Totally Unknown Vendor App",
                source="manual_followup",
                status=PlanItemStatus.MANUAL_FOLLOWUP,
                reason="No Linux package found.",
            )
        ],
    )
    manual_blockers = manual_orchestrator._verify_planned_packages_installed()
    _record(
        checks,
        name="final_audit_ignores_manual_followup_apps_without_identifier",
        category="final_audit",
        passed=manual_blockers == [],
        details={"blockers": manual_blockers},
    )
    return checks


def _run_desktop_final_audit_checks() -> list[dict[str, Any]]:
    from .audit import audit_stage

    checks: list[dict[str, Any]] = []
    state = {
        "persisted_config_written": True,
        "desktop_design_attempted": True,
        "wallpaper_attempted": True,
        "expected_kde_icon_theme": "candy-icons",
        "expected_kde_wobbly_windows": "true",
        "expected_kde_magic_lamp": "true",
    }
    good_facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
        desktop_design=DesktopDesignState(
            environment="KDE",
            kde_icon_theme="candy-icons",
            kde_wobbly_windows="true",
            kde_magic_lamp="true",
            kde_wallpaper="file:///home/jan/Pictures/masterpiece.png",
        ),
    )
    good_result = audit_stage(SetupStage.DESKTOP, good_facts, stage_state=state)
    _record(
        checks,
        name="desktop_audit_accepts_verified_kde_design_contract",
        category="final_audit",
        passed=good_result.passed,
        details=asdict(good_result),
    )

    bad_facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
        desktop_design=DesktopDesignState(
            environment="KDE",
            kde_icon_theme="breeze",
            kde_wobbly_windows="",
            kde_magic_lamp="",
            kde_wallpaper="file:///home/jan/Pictures/masterpiece.png",
        ),
    )
    bad_result = audit_stage(SetupStage.DESKTOP, bad_facts, stage_state=state)
    _record(
        checks,
        name="desktop_audit_records_reset_or_missing_kde_design_contract_as_followup",
        category="final_audit",
        passed=bad_result.passed
        and any(
            "KDE icon theme mismatch" in blocker
            for blocker in bad_result.details.get("desktop_design_manual_followups", [])
        )
        and any(
            "KWin wobbly windows was expected" in blocker
            for blocker in bad_result.details.get("desktop_design_manual_followups", [])
        ),
        details=asdict(bad_result),
    )
    return checks


def _run_fact_probe_semantic_checks() -> list[dict[str, Any]]:
    command = setup_fact_probe_command()
    required_fragments = [
        "qh_kde_config_value()",
        "kreadconfig6",
        "QH_KDE_ICON_THEME",
        "QH_KDE_WOBBLY",
        "QH_KDE_MAGIC_LAMP",
    ]
    missing = [fragment for fragment in required_fragments if fragment not in command]
    checks: list[dict[str, Any]] = []
    _record(
        checks,
        name="fact_probe_reads_kde_design_with_kreadconfig_fallback",
        category="final_audit",
        passed=not missing,
        details={"missing_fragments": missing},
    )
    return checks


def _run_family_spine_semantic_checks() -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for family in CERTIFIED_FAMILIES:
        installer = get_family_installer(family)
        spine = installer.build_install_spine(
            SetupFacts(distro_family=family, is_live=True),
            UserProfile(),
        )
        joined = "\n".join(spine.commands)
        hardcoded_partition_paths = [path for path in ("/dev/sda1", "/dev/sda2") if path in joined]
        _record(
            checks,
            name=f"family_spine_uses_partition_path_helper_{family}",
            category="family_spine",
            passed=(
                'case "$DISK" in *[0-9]) ESP="${DISK}p1"; ROOT="${DISK}p2"' in joined
                and not hardcoded_partition_paths
            ),
            details={"hardcoded_partition_paths": hardcoded_partition_paths},
        )
        _record(
            checks,
            name=f"family_spine_uses_conditional_recovery_groups_{family}",
            category="family_spine",
            passed="-G wheel,sudo" not in joined
            and "getent group wheel" in joined
            and "getent group sudo" in joined,
            details={},
        )
    return checks


def run_harness_health(
    *,
    root: Path | str | None = None,
    keep_artifacts: bool = False,
) -> dict[str, Any]:
    """Run the fast, zero-SSH harness health gate."""

    with _health_workspace(root, keep_artifacts) as workspace:
        checks = [
            *_run_certification_dry_run_checks(workspace),
            *_run_preflight_policy_checks(),
            *_run_failure_classifier_checks(),
            *_run_final_audit_semantic_checks(),
            *_run_resume_policy_checks(),
            *_run_family_spine_semantic_checks(),
            *_run_app_stage_prefilter_checks(),
        ]
        failed = [check for check in checks if not check["passed"]]
        return {
            "success": not failed,
            "workspace": str(workspace),
            "keep_artifacts": keep_artifacts,
            "check_count": len(checks),
            "failed_count": len(failed),
            "checks": checks,
        }
