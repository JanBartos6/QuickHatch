"""Release smoke orchestration for local, non-VM technical checks."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SmokeRunner = Callable[..., subprocess.CompletedProcess[str]]

CERTIFICATION_FAMILIES = ("arch", "ubuntu", "debian", "fedora")
TARGETED_PYTEST_FILES = (
    "tests/test_setup_capabilities.py",
    "tests/test_setup_preflight.py",
    "tests/test_setup_health.py",
    "tests/test_setup_debugtools.py",
    "tests/test_setup_certify.py",
    "tests/test_setup_desktop_session.py",
    "tests/test_setup_design_feedback_loop.py",
    "tests/test_setup_driver_uncertainty.py",
    "tests/test_setup_reboot_reconnect.py",
    "tests/test_setup_final_audit.py",
    "tests/test_setup_final_audit_repair_loop.py",
    "tests/test_setup_orchestrator.py",
    "tests/test_app_catalog_contract.py",
    "tests/test_bridge_state.py",
    "tests/test_gui_endpoints.py",
    "tests/test_support_bundle.py",
    "tests/test_release_smoke.py",
)


@dataclass
class SmokeCheckResult:
    name: str
    status: str
    command: list[str]
    exit_code: int | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    required: bool = True
    reason: str = ""


@dataclass
class CertificationWorkspace:
    profile: Path
    certification_root: Path
    isos: dict[str, Path]


def _tail(text: object, *, limit: int = 1600) -> str:
    value = "" if text is None else str(text)
    if len(value) <= limit:
        return value
    return value[-limit:]


def _run_command_check(
    *,
    name: str,
    command: list[str],
    runner: SmokeRunner,
    cwd: Path,
    timeout: int,
    required: bool = True,
) -> SmokeCheckResult:
    try:
        completed = runner(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return SmokeCheckResult(
            name=name,
            status="failed",
            command=command,
            exit_code=None,
            stdout_tail=_tail(exc.stdout),
            stderr_tail=_tail(exc.stderr or f"Timed out after {timeout} seconds."),
            required=required,
            reason=f"timed out after {timeout} seconds",
        )
    except OSError as exc:
        return SmokeCheckResult(
            name=name,
            status="failed",
            command=command,
            exit_code=None,
            stderr_tail=_tail(str(exc)),
            required=required,
            reason=str(exc),
        )

    return SmokeCheckResult(
        name=name,
        status="passed" if completed.returncode == 0 else "failed",
        command=command,
        exit_code=completed.returncode,
        stdout_tail=_tail(completed.stdout),
        stderr_tail=_tail(completed.stderr),
        required=required,
    )


def _skipped_check(*, name: str, reason: str, command: list[str] | None = None) -> SmokeCheckResult:
    return SmokeCheckResult(
        name=name,
        status="skipped",
        command=command or [],
        required=False,
        reason=reason,
    )


def _pytest_command(cwd: Path, *, full_pytest: bool) -> list[str]:
    if full_pytest:
        return [sys.executable, "-m", "pytest", "-q"]
    targets = [target for target in TARGETED_PYTEST_FILES if (cwd / target).exists()]
    if not targets:
        return [sys.executable, "-m", "pytest", "-q"]
    return [sys.executable, "-m", "pytest", *targets, "-q"]


def _base_required_checks(cwd: Path, *, full_pytest: bool) -> list[tuple[str, list[str]]]:
    return [
        ("ruff_check", [sys.executable, "-m", "ruff", "check", "."]),
        ("ruff_format_check", [sys.executable, "-m", "ruff", "format", "--check", "."]),
        ("pytest", _pytest_command(cwd, full_pytest=full_pytest)),
        (
            "harness_health",
            [sys.executable, "-m", "quickhatch.cli", "debug", "harness-health"],
        ),
        (
            "catalog_health",
            [sys.executable, "-m", "quickhatch.cli", "debug", "catalog-health"],
        ),
    ]


@contextmanager
def _temporary_certification_workspace() -> Iterator[tuple[CertificationWorkspace | None, str]]:
    try:
        temp_dir = tempfile.TemporaryDirectory(prefix="quickhatch-release-smoke-")
    except OSError as exc:
        yield None, f"certification dry-run workspace unavailable: {exc}"
        return

    try:
        root = Path(temp_dir.name)
        input_dir = root / "inputs"
        input_dir.mkdir(parents=True, exist_ok=True)
        profile = input_dir / "profile.json"
        profile.write_text(
            json.dumps(
                {
                    "remote_setup_mode": "wipe_and_reinstall",
                    "install_profile": "full_disk_wipe",
                    "install_username": "quickhatch",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        isos: dict[str, Path] = {}
        for family in CERTIFICATION_FAMILIES:
            iso = input_dir / f"{family}.iso"
            iso.write_text("quickhatch release-smoke placeholder iso", encoding="utf-8")
            isos[family] = iso
        workspace = CertificationWorkspace(
            profile=profile,
            certification_root=root / "certification",
            isos=isos,
        )
    except OSError as exc:
        temp_dir.cleanup()
        yield None, f"certification dry-run workspace unavailable: {exc}"
        return

    try:
        yield workspace, ""
    finally:
        temp_dir.cleanup()


def _certification_command(family: str, workspace: CertificationWorkspace) -> list[str]:
    return [
        sys.executable,
        "-m",
        "quickhatch.cli",
        "certify",
        "vm",
        "--family",
        family,
        "--iso",
        str(workspace.isos[family]),
        "--profile",
        str(workspace.profile),
        "--dry-run",
        "--no-post-reboot",
        "--root",
        str(workspace.certification_root),
    ]


def run_release_smoke(
    *,
    cwd: Path | str | None = None,
    full_pytest: bool = False,
    include_certification: bool = True,
    timeout: int = 300,
    runner: SmokeRunner | None = None,
    certification_workspace_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Run the local release smoke gate and return a JSON-compatible summary."""

    cwd_path = Path(cwd) if cwd is not None else Path.cwd()
    run = runner or subprocess.run
    checks: list[SmokeCheckResult] = []

    for name, command in _base_required_checks(cwd_path, full_pytest=full_pytest):
        checks.append(
            _run_command_check(
                name=name,
                command=command,
                runner=run,
                cwd=cwd_path,
                timeout=timeout,
            )
        )

    if include_certification:
        workspace_factory = certification_workspace_factory or _temporary_certification_workspace
        with workspace_factory() as (workspace, skip_reason):
            if workspace is None:
                checks.extend(
                    _skipped_check(
                        name=f"certification_dry_run_{family}",
                        reason=skip_reason or "certification dry-run prerequisites unavailable",
                    )
                    for family in CERTIFICATION_FAMILIES
                )
            else:
                for family in CERTIFICATION_FAMILIES:
                    checks.append(
                        _run_command_check(
                            name=f"certification_dry_run_{family}",
                            command=_certification_command(family, workspace),
                            runner=run,
                            cwd=cwd_path,
                            timeout=timeout,
                        )
                    )
    else:
        checks.extend(
            _skipped_check(
                name=f"certification_dry_run_{family}",
                reason="certification dry-runs disabled",
            )
            for family in CERTIFICATION_FAMILIES
        )

    passed = [check for check in checks if check.status == "passed"]
    failed = [check for check in checks if check.status == "failed"]
    skipped = [check for check in checks if check.status == "skipped"]
    required_failed = [check for check in failed if check.required]
    return {
        "success": not required_failed,
        "check_count": len(checks),
        "passed_count": len(passed),
        "failed_count": len(failed),
        "skipped_count": len(skipped),
        "required_failed_count": len(required_failed),
        "cwd": str(cwd_path),
        "full_pytest": full_pytest,
        "checks": [asdict(check) for check in checks],
    }
