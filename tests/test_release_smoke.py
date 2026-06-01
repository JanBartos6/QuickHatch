from __future__ import annotations

import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from quickhatch.setup.release_smoke import TARGETED_PYTEST_FILES, run_release_smoke


def _passing_runner(command, **_kwargs):
    return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")


def test_release_smoke_passes_required_checks_and_reports_skips(tmp_path):
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(command)
        return _passing_runner(command, **kwargs)

    payload = run_release_smoke(
        cwd=tmp_path,
        runner=runner,
        include_certification=False,
    )

    assert payload["success"] is True
    assert payload["passed_count"] == 5
    assert payload["failed_count"] == 0
    assert payload["skipped_count"] == 4
    assert [check["name"] for check in payload["checks"][:5]] == [
        "ruff_check",
        "ruff_format_check",
        "pytest",
        "harness_health",
        "catalog_health",
    ]
    assert all(check["status"] == "skipped" for check in payload["checks"][5:])
    assert len(calls) == 5


def test_release_smoke_targeted_pytest_includes_tier1_safety_tests():
    assert "tests/test_setup_capabilities.py" in TARGETED_PYTEST_FILES
    assert "tests/test_setup_preflight.py" in TARGETED_PYTEST_FILES
    assert "tests/test_setup_desktop_session.py" in TARGETED_PYTEST_FILES
    assert "tests/test_setup_driver_uncertainty.py" in TARGETED_PYTEST_FILES
    assert "tests/test_setup_reboot_reconnect.py" in TARGETED_PYTEST_FILES
    assert "tests/test_setup_stop_abort.py" in TARGETED_PYTEST_FILES
    assert "tests/test_setup_model_contract.py" in TARGETED_PYTEST_FILES
    assert "tests/test_setup_research_grounding.py" in TARGETED_PYTEST_FILES
    assert "tests/test_setup_app_mapping.py" in TARGETED_PYTEST_FILES
    assert "tests/test_setup_final_audit_repair_loop.py" in TARGETED_PYTEST_FILES
    assert "tests/test_support_bundle_diagnostics.py" in TARGETED_PYTEST_FILES


def test_release_smoke_failed_required_check_fails_payload(tmp_path):
    def runner(command, **_kwargs):
        if command[:4] == [sys.executable, "-m", "ruff", "check"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="lint failed\n")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    payload = run_release_smoke(
        cwd=tmp_path,
        runner=runner,
        include_certification=False,
    )

    failed = [check for check in payload["checks"] if check["status"] == "failed"]
    assert payload["success"] is False
    assert payload["failed_count"] == 1
    assert payload["required_failed_count"] == 1
    assert failed[0]["name"] == "ruff_check"
    assert failed[0]["exit_code"] == 1
    assert failed[0]["stderr_tail"] == "lint failed\n"


def test_release_smoke_skips_unavailable_certification_prerequisites(tmp_path):
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(command)
        return _passing_runner(command, **kwargs)

    @contextmanager
    def unavailable_workspace():
        yield None, "certification fixtures unavailable"

    payload = run_release_smoke(
        cwd=tmp_path,
        runner=runner,
        certification_workspace_factory=unavailable_workspace,
    )

    certification_checks = [
        check for check in payload["checks"] if check["name"].startswith("certification_dry_run_")
    ]
    assert payload["success"] is True
    assert len(certification_checks) == 4
    assert all(check["status"] == "skipped" for check in certification_checks)
    assert all(
        check["reason"] == "certification fixtures unavailable" for check in certification_checks
    )
    assert not any("certify" in command for command in calls for command in command)


def test_release_smoke_runs_certification_dry_runs_without_vm_flags(tmp_path):
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(command)
        return _passing_runner(command, **kwargs)

    payload = run_release_smoke(cwd=tmp_path, runner=runner)

    certification_calls = [
        command
        for command in calls
        if command[:5] == [sys.executable, "-m", "quickhatch.cli", "certify", "vm"]
    ]
    assert payload["success"] is True
    assert len(certification_calls) == 4
    assert all("--dry-run" in command for command in certification_calls)
    assert all("--no-post-reboot" in command for command in certification_calls)
    assert all("--root" in command for command in certification_calls)


def test_release_smoke_certification_fixture_is_explicit_full_disk_dry_run(tmp_path):
    seen_profiles: list[dict[str, object]] = []

    def runner(command, **kwargs):
        if command[:5] == [sys.executable, "-m", "quickhatch.cli", "certify", "vm"]:
            profile_path = command[command.index("--profile") + 1]
            seen_profiles.append(json.loads(Path(profile_path).read_text(encoding="utf-8")))
        return _passing_runner(command, **kwargs)

    payload = run_release_smoke(cwd=tmp_path, runner=runner)

    assert payload["success"] is True
    assert len(seen_profiles) == 4
    assert all(
        profile
        == {
            "remote_setup_mode": "wipe_and_reinstall",
            "install_profile": "full_disk_wipe",
            "install_username": "quickhatch",
        }
        for profile in seen_profiles
    )


def test_release_smoke_cli_exits_nonzero_for_failed_payload(monkeypatch):
    from quickhatch import cli

    printed: list[dict[str, object]] = []

    monkeypatch.setattr(cli, "_print_json", lambda payload: printed.append(payload))
    monkeypatch.setattr(
        "quickhatch.setup.debugtools.release_smoke_payload",
        lambda **_kwargs: {"success": False, "checks": []},
    )

    with pytest.raises(SystemExit) as exc:
        cli._run_debug_command(
            SimpleNamespace(
                debug_command="release-smoke",
                full_pytest=False,
                skip_certification_dry_runs=False,
                timeout=10,
            )
        )

    assert exc.value.code == 1
    assert printed == [{"success": False, "checks": []}]
