from __future__ import annotations

from quickhatch.setup.failure import classify_failure
from quickhatch.setup.health import run_harness_health


def test_harness_health_runs_certified_family_fast_gate(tmp_path):
    payload = run_harness_health(root=tmp_path / "health", keep_artifacts=True)

    assert payload["success"] is True
    assert payload["failed_count"] == 0
    names = {check["name"] for check in payload["checks"]}
    assert {"certify_dry_run_arch", "certify_dry_run_ubuntu"} <= names
    assert {"certify_dry_run_debian", "certify_dry_run_fedora"} <= names
    assert (tmp_path / "health").exists()


def test_harness_health_cleans_workspace_by_default(tmp_path):
    root = tmp_path / "health"

    payload = run_harness_health(root=root, keep_artifacts=False)

    assert payload["success"] is True
    assert root.exists()
    assert list(root.iterdir()) == []


def test_failure_classifier_handles_arch_mirror_repository_failure():
    result = classify_failure(
        command="pacman -Sy --noconfirm archlinux-keyring",
        rc=1,
        stderr="error: failed to synchronize all databases (no servers configured for repository)",
    )

    assert result.category.value == "network"
