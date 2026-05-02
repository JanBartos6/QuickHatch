from __future__ import annotations

import json

from quickhatch.setup.certify import run_vm_certification


def test_vm_certification_dry_run_writes_artifacts(tmp_path):
    iso = tmp_path / "ubuntu.iso"
    profile = tmp_path / "profile.json"
    iso.write_text("fake iso", encoding="utf-8")
    profile.write_text(json.dumps({}), encoding="utf-8")

    result = run_vm_certification(
        family="ubuntu",
        iso=iso,
        profile=profile,
        root=tmp_path / "certification",
        dry_run=True,
    )

    assert result.success
    assert result.status == "dry_run"
    artifact_dir = tmp_path / "certification" / "ubuntu"
    assert artifact_dir.exists()
    assert (next(artifact_dir.iterdir()) / "summary.json").exists()


def test_vm_certification_blocks_missing_iso(tmp_path):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({}), encoding="utf-8")

    result = run_vm_certification(
        family="arch",
        iso=tmp_path / "missing.iso",
        profile=profile,
        root=tmp_path / "certification",
        dry_run=True,
    )

    assert not result.success
    assert result.status == "blocked"
    assert result.blockers


def test_vm_certification_fails_closed_for_live_backend_until_wired(tmp_path):
    iso = tmp_path / "fedora.iso"
    profile = tmp_path / "profile.json"
    iso.write_text("fake iso", encoding="utf-8")
    profile.write_text(json.dumps({}), encoding="utf-8")

    result = run_vm_certification(
        family="fedora",
        iso=iso,
        profile=profile,
        root=tmp_path / "certification",
        dry_run=False,
    )

    assert not result.success
    assert result.status == "blocked"
    assert any("Live VM execution" in blocker for blocker in result.blockers)
