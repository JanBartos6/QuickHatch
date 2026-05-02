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

from .certify import CERTIFIED_FAMILIES, run_vm_certification
from .failure import classify_failure
from .preflight import preflight_setup_decisions
from .types import FailureCategory, PreflightDecisionKind, SetupFacts


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
