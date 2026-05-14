from __future__ import annotations

import json

from quickhatch.setup.certify import run_vm_certification
from quickhatch.setup.replay import scan_runlogs
from quickhatch.wizard.vm_verify import VerifyResult


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
    artifact = next(artifact_dir.iterdir())
    assert (artifact / "summary.json").exists()
    assert b"\r\n" not in (artifact / "certify-install.sh").read_bytes()
    events = [
        json.loads(line)
        for line in (artifact / "certification-run.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(event["event_type"] == "certification_preflight_checked" for event in events)
    assert any(event["event_type"] == "capability_granted" for event in events)


def test_vm_certification_qemu_defaults_to_virtio_disk(tmp_path):
    iso = tmp_path / "arch.iso"
    profile = tmp_path / "profile.json"
    iso.write_text("fake iso", encoding="utf-8")
    profile.write_text(json.dumps({}), encoding="utf-8")

    result = run_vm_certification(
        family="arch",
        iso=iso,
        profile=profile,
        root=tmp_path / "certification",
        backend="qemu",
        dry_run=True,
    )

    artifact = next((tmp_path / "certification" / "arch").iterdir())
    assert result.success
    assert "QUICKHATCH_TARGET_DISK=${QUICKHATCH_TARGET_DISK:-/dev/vda}" in (
        artifact / "certify-install.sh"
    ).read_text(encoding="utf-8")


def test_vm_certification_artifact_dirs_do_not_collide(tmp_path):
    iso = tmp_path / "arch.iso"
    profile = tmp_path / "profile.json"
    iso.write_text("fake iso", encoding="utf-8")
    profile.write_text(json.dumps({}), encoding="utf-8")

    first = run_vm_certification(
        family="arch",
        iso=iso,
        profile=profile,
        root=tmp_path / "certification",
        dry_run=True,
    )
    second = run_vm_certification(
        family="arch",
        iso=iso,
        profile=profile,
        root=tmp_path / "certification",
        dry_run=True,
    )

    assert first.artifact_dir != second.artifact_dir


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
    scanned = scan_runlogs(tmp_path / "certification")
    assert scanned.failure_categories["vm_certification_blocked"] == 1


def test_vm_certification_defaults_post_reboot_user_from_profile(monkeypatch, tmp_path):
    iso = tmp_path / "arch.iso"
    profile = tmp_path / "profile.json"
    iso.write_text("fake iso", encoding="utf-8")
    profile.write_text(json.dumps({"linux_user": "alice"}), encoding="utf-8")
    calls = []

    def _fake_verify(*args, **kwargs):
        calls.append((args, kwargs))
        return VerifyResult(success=True, backend="virtualbox", exit_code=0, stages=[])

    monkeypatch.setattr("quickhatch.setup.certify.verify_install_script", _fake_verify)
    monkeypatch.setattr(
        "quickhatch.setup.certify._prepare_certification_key",
        lambda *a, **k: ("cert-key", "ssh-ed25519 AAAA quickhatch", ""),
    )

    result = run_vm_certification(
        family="arch",
        iso=iso,
        profile=profile,
        root=tmp_path / "certification",
        dry_run=False,
    )

    assert result.success
    assert calls[0][1]["post_reboot_user"] == "alice"


def test_vm_certification_defaults_post_reboot_user_to_quickhatch(monkeypatch, tmp_path):
    iso = tmp_path / "fedora.iso"
    profile = tmp_path / "profile.json"
    iso.write_text("fake iso", encoding="utf-8")
    profile.write_text(json.dumps({}), encoding="utf-8")
    calls = []

    def _fake_verify(*args, **kwargs):
        calls.append((args, kwargs))
        return VerifyResult(success=True, backend="virtualbox", exit_code=0, stages=[])

    monkeypatch.setattr("quickhatch.setup.certify.verify_install_script", _fake_verify)
    monkeypatch.setattr(
        "quickhatch.setup.certify._prepare_certification_key",
        lambda *a, **k: ("cert-key", "ssh-ed25519 AAAA quickhatch", ""),
    )

    result = run_vm_certification(
        family="fedora",
        iso=iso,
        profile=profile,
        root=tmp_path / "certification",
        dry_run=False,
    )

    assert result.success
    assert calls[0][1]["post_reboot_user"] == "quickhatch"


def test_vm_certification_live_backend_success_is_automated(monkeypatch, tmp_path):
    iso = tmp_path / "fedora.iso"
    profile = tmp_path / "profile.json"
    iso.write_text("fake iso", encoding="utf-8")
    profile.write_text(json.dumps({}), encoding="utf-8")
    calls = []

    def _fake_verify(*args, **kwargs):
        calls.append((args, kwargs))
        return VerifyResult(
            success=True,
            backend="virtualbox",
            exit_code=0,
            stages=["syntax-check", "vm-created", "vm-started", "ssh-up", "script-exit:0"],
        )

    monkeypatch.setattr("quickhatch.setup.certify.verify_install_script", _fake_verify)
    monkeypatch.setattr(
        "quickhatch.setup.certify._prepare_certification_key",
        lambda *a, **k: ("cert-key", "ssh-ed25519 AAAA quickhatch", ""),
    )

    result = run_vm_certification(
        family="fedora",
        iso=iso,
        profile=profile,
        root=tmp_path / "certification",
        dry_run=False,
    )

    assert result.success
    assert result.status == "passed"
    assert calls
    script_path = calls[0][0][0]
    verify_kwargs = calls[0][1]
    assert script_path.name == "certify-install.sh"
    assert "dnf --installroot" in script_path.read_text(encoding="utf-8")
    assert verify_kwargs["post_reboot_user"] == "quickhatch"
    assert verify_kwargs["require_post_reboot"] is True
    assert verify_kwargs["post_reboot_commands"]
    assert "authorized_keys" in verify_kwargs["live_bootstrap_command"]
    assert verify_kwargs["live_bootstrap_user"] == "liveuser"
    assert verify_kwargs["live_bootstrap_wait_seconds"] == 120


def test_vm_certification_ubuntu_live_backend_uses_graphical_tty_login(
    monkeypatch,
    tmp_path,
):
    iso = tmp_path / "ubuntu.iso"
    profile = tmp_path / "profile.json"
    iso.write_text("fake iso", encoding="utf-8")
    profile.write_text(json.dumps({}), encoding="utf-8")
    calls = []

    def _fake_verify(*args, **kwargs):
        calls.append((args, kwargs))
        return VerifyResult(success=True, backend="virtualbox", exit_code=0, stages=[])

    monkeypatch.setattr("quickhatch.setup.certify.verify_install_script", _fake_verify)
    monkeypatch.setattr(
        "quickhatch.setup.certify._prepare_certification_key",
        lambda *a, **k: ("cert-key", "ssh-ed25519 AAAA quickhatch", ""),
    )

    result = run_vm_certification(
        family="ubuntu",
        iso=iso,
        profile=profile,
        root=tmp_path / "certification",
        dry_run=False,
    )

    assert result.success
    verify_kwargs = calls[0][1]
    assert verify_kwargs["live_bootstrap_user"] == "ubuntu"
    assert verify_kwargs["live_bootstrap_wait_seconds"] == 120
    assert verify_kwargs["ssh_user"] == "root"


def test_vm_certification_debian_live_backend_uses_live_user_tty_login(
    monkeypatch,
    tmp_path,
):
    iso = tmp_path / "debian.iso"
    profile = tmp_path / "profile.json"
    iso.write_text("fake iso", encoding="utf-8")
    profile.write_text(json.dumps({}), encoding="utf-8")
    calls = []

    def _fake_verify(*args, **kwargs):
        calls.append((args, kwargs))
        return VerifyResult(success=True, backend="virtualbox", exit_code=0, stages=[])

    monkeypatch.setattr("quickhatch.setup.certify.verify_install_script", _fake_verify)
    monkeypatch.setattr(
        "quickhatch.setup.certify._prepare_certification_key",
        lambda *a, **k: ("cert-key", "ssh-ed25519 AAAA quickhatch", ""),
    )

    result = run_vm_certification(
        family="debian",
        iso=iso,
        profile=profile,
        root=tmp_path / "certification",
        dry_run=False,
    )

    assert result.success
    verify_kwargs = calls[0][1]
    assert verify_kwargs["ssh_user"] == "user"
    assert verify_kwargs["live_bootstrap_user"] == "user"
    assert verify_kwargs["live_bootstrap_wait_seconds"] == 120


def test_vm_certification_live_backend_failure_records_artifact(monkeypatch, tmp_path):
    iso = tmp_path / "arch.iso"
    profile = tmp_path / "profile.json"
    iso.write_text("fake iso", encoding="utf-8")
    profile.write_text(json.dumps({}), encoding="utf-8")

    monkeypatch.setattr(
        "quickhatch.setup.certify.verify_install_script",
        lambda *a, **k: VerifyResult(
            success=False,
            backend="virtualbox",
            exit_code=255,
            error="VM never answered on SSH",
            stdout_tail="last stdout line",
            stderr_tail="last stderr line",
            stages=["syntax-check", "vm-created"],
        ),
    )
    monkeypatch.setattr(
        "quickhatch.setup.certify._prepare_certification_key",
        lambda *a, **k: ("cert-key", "ssh-ed25519 AAAA quickhatch", ""),
    )

    result = run_vm_certification(
        family="arch",
        iso=iso,
        profile=profile,
        root=tmp_path / "certification",
        dry_run=False,
    )

    assert not result.success
    assert result.status == "blocked"
    assert "VM never answered on SSH" in result.blockers
    artifact = next((tmp_path / "certification" / "arch").iterdir())
    assert (artifact / "vm-result.json").exists()
    assert (artifact / "certification-run.jsonl").exists()
    events = [
        json.loads(line)
        for line in (artifact / "certification-run.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    finished = next(event for event in events if event["event_type"] == "vm_certification_finished")
    assert finished["stdout_tail"] == "last stdout line"
    assert finished["stderr_tail"] == "last stderr line"
