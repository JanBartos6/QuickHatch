from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from quickhatch.setup.support_bundle import export_support_bundle


def _write_jsonl(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )


def _zip_texts(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as zf:
        return {
            name: zf.read(name).decode("utf-8")
            for name in zf.namelist()
            if name.endswith((".json", ".jsonl", ".sh", ".txt"))
        }


def _zip_json(path: Path, name: str) -> Any:
    return json.loads(_zip_texts(path)[name])


def test_support_bundle_redacts_secret_like_strings_and_private_keys(tmp_path):
    runlog = tmp_path / "runs" / "run-secret.jsonl"
    _write_jsonl(
        runlog,
        [
            {
                "event_type": "command",
                "run_id": "run-secret",
                "stage": "apps",
                "command": (
                    "OPENAI_API_KEY=sk-live-secret-token-1234567890 "
                    "curl --password cli-secret-password --token=cli-token-secret "
                    "https://example.test"
                ),
                "stdout_tail": "Authorization: Bearer provider-token-abcdef123456",
                "stderr_tail": "password=hunter2\nAWS_SECRET_ACCESS_KEY=aws-private-token",
                "private_key": (
                    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
                    "PRIVATE-KEY-DATA-DO-NOT-LEAK\n"
                    "-----END OPENSSH PRIVATE KEY-----"
                ),
            },
            {
                "event_type": "planner_prompt",
                "run_id": "run-secret",
                "prompt": (
                    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKE user@example.test\n"
                    '{"default_setup_password": "default-password"}'
                ),
                "profile": {
                    "provider_token": "model-token",
                    "default_setup_password": "default-password",
                },
            },
        ],
    )

    result = export_support_bundle(runlog, out=tmp_path / "support.zip")
    combined = "\n".join(_zip_texts(Path(result.path)).values())

    assert "sk-live-secret-token" not in combined
    assert "provider-token-abcdef" not in combined
    assert "cli-secret-password" not in combined
    assert "cli-token-secret" not in combined
    assert "aws-private-token" not in combined
    assert "hunter2" not in combined
    assert "model-token" not in combined
    assert "default-password" not in combined
    assert "AAAAC3" not in combined
    assert "user@example.test" not in combined
    assert "BEGIN OPENSSH PRIVATE KEY" not in combined
    assert "PRIVATE-KEY-DATA-DO-NOT-LEAK" not in combined


def test_support_bundle_handles_missing_optional_artifacts(tmp_path):
    result = export_support_bundle(root=tmp_path / "empty-runs", out=tmp_path / "missing.zip")

    assert result.status == "ok"
    assert Path(result.path).exists()
    manifest = _zip_json(Path(result.path), "manifest.json")
    summary = _zip_json(Path(result.path), "support-summary.json")

    assert "setup_runlog" in manifest["skipped"]
    assert "final_audit" in manifest["skipped"]
    assert "app_verification" in manifest["skipped"]
    assert summary["final_status"] == "unknown"


def test_support_bundle_auto_names_do_not_collide_for_same_runlog_stem(tmp_path):
    first = tmp_path / "certification" / "arch-run" / "certification-run.jsonl"
    second = tmp_path / "certification" / "ubuntu-run" / "certification-run.jsonl"
    _write_jsonl(first, [{"event_type": "certification_finished", "status": "passed"}])
    _write_jsonl(second, [{"event_type": "certification_finished", "status": "passed"}])
    output_dir = tmp_path / "support"

    first_result = export_support_bundle(first, output_dir=output_dir)
    second_result = export_support_bundle(second, output_dir=output_dir)

    assert first_result.path != second_result.path
    assert Path(first_result.path).exists()
    assert Path(second_result.path).exists()
    assert "arch-run-certification-run" in Path(first_result.path).name
    assert "ubuntu-run-certification-run" in Path(second_result.path).name


def test_support_bundle_normalizes_enum_style_final_status(tmp_path):
    runlog = tmp_path / "runs" / "run-status.jsonl"
    _write_jsonl(
        runlog,
        [
            {
                "event_type": "final_audit",
                "run_id": "run-status",
                "result": {
                    "passed": True,
                    "final_status": "FinalSetupStatus.SUCCESS_WITH_WARNINGS",
                    "findings": [
                        {
                            "severity": "warning",
                            "category": "apps",
                            "summary": "Alternative installed for requested app.",
                        }
                    ],
                },
            }
        ],
    )

    result = export_support_bundle(runlog, out=tmp_path / "status.zip")

    summary = _zip_json(Path(result.path), "support-summary.json")
    final_audit = _zip_json(Path(result.path), "final-audit.json")
    assert summary["final_status"] == "success_with_warnings"
    assert final_audit["status"] == "success_with_warnings"


def test_support_bundle_includes_release_readiness_diagnostics(tmp_path):
    runlog = tmp_path / "runs" / "run-ready.jsonl"
    _write_jsonl(
        runlog,
        [
            {
                "event_type": "planner_prompt",
                "run_id": "run-ready",
                "profile": {
                    "install_profile": "configure_existing",
                    "remote_setup_mode": "wipe_and_reinstall",
                    "install_encryption": "none",
                    "install_filesystem": "btrfs",
                    "install_partition_notes": "Use only the explicitly confirmed VM disk.",
                    "preferences": {
                        "another_machine": "yes",
                        "distro_preference": "arch",
                        "hardware_search_opt_in": True,
                    },
                    "apps_to_install": ["Brave", "Firefox"],
                    "ai_config": {"api_key": "sk-profile-secret-1234567890"},
                    "completed_steps": ["welcome", "scan"],
                },
            },
            {
                "event_type": "facts_refresh",
                "run_id": "run-ready",
                "facts": {
                    "distro_family": "arch",
                    "package_manager": "pacman",
                    "current_user": "jan",
                    "gpu_vendors": ["nvidia"],
                    "nvidia_smi_state": "ok",
                    "hostname": "private-hostname",
                },
            },
            {
                "event_type": "dangerous_operation_preview",
                "run_id": "run-ready",
                "preview": {
                    "operation": {"kind": "write_fstab", "target": "/mnt/etc/fstab"},
                    "rendered_commands": ["printf 'token=secret-preview' > /mnt/etc/fstab"],
                },
            },
            {
                "event_type": "capability_granted",
                "run_id": "run-ready",
                "capability": {"kind": "write_fstab", "target": "/mnt/etc/fstab"},
            },
            {
                "event_type": "command_preflight_decision",
                "run_id": "run-ready",
                "command": "sudo pacman -S brave-bin",
                "decisions": [{"kind": "advisory", "category": "package", "reason": "ok"}],
            },
            {
                "event_type": "final_audit",
                "run_id": "run-ready",
                "status": "manual_followup_required",
                "result": {
                    "passed": True,
                    "final_status": "manual_followup_required",
                    "blockers": [],
                    "findings": [
                        {
                            "severity": "manual_followup",
                            "category": "apps",
                            "summary": "Manual follow-up app remains: Brave",
                        }
                    ],
                    "details": {
                        "remote_final_verification": {
                            "app_verification_signals": {
                                "Brave": {"signal": "native_package", "target": "brave-bin"}
                            },
                            "verified_installed_apps": ["Firefox"],
                            "unverified_installed_apps": ["Brave"],
                            "app_verification_manual_followups": [
                                {"app": "Brave", "reason": "missing launcher"}
                            ],
                        },
                        "final_audit_repairs": [
                            {
                                "action": "manual_followup",
                                "reason": "Leave Brave install to the user",
                                "findings": ["Brave missing"],
                            }
                        ],
                    },
                },
            },
            {
                "event_type": "certification_finished",
                "run_id": "run-ready",
                "status": "dry_run",
                "success": True,
            },
        ],
    )

    result = export_support_bundle(runlog, out=tmp_path / "ready.zip")
    bundle = Path(result.path)

    names = set(_zip_texts(bundle))
    assert {
        "setup-runlog-redacted.jsonl",
        "support-summary.json",
        "final-audit.json",
        "setup-facts-summary.json",
        "profile-summary.json",
        "capability-events.json",
        "preflight-events.json",
        "dangerous-operation-previews.json",
        "app-verification.json",
        "manual-followups.json",
        "certification-summary.json",
    } <= names

    summary = _zip_json(bundle, "support-summary.json")
    assert summary["final_status"] == "manual_followup_required"
    assert summary["install_profile"] == "configure_existing"
    assert summary["remote_setup_mode"] == "wipe_and_reinstall"
    assert summary["physical_setup_mode"] == "remote_setup"
    assert summary["install_encryption"] == "none"
    assert summary["install_filesystem"] == "btrfs"

    final_audit = _zip_json(bundle, "final-audit.json")
    assert final_audit["findings"][0]["category"] == "apps"
    assert final_audit["details"]["remote_final_verification"]["unverified_installed_apps"] == [
        "Brave"
    ]

    facts = _zip_json(bundle, "setup-facts-summary.json")
    assert facts["package_manager"] == "pacman"
    assert facts["gpu_vendors"] == ["nvidia"]
    assert "hostname" not in facts

    profile = _zip_json(bundle, "profile-summary.json")
    assert profile["install_profile"] == "configure_existing"
    assert profile["remote_setup_mode"] == "wipe_and_reinstall"
    assert profile["physical_setup_mode"] == "remote_setup"
    assert profile["install_encryption"] == "none"
    assert profile["install_filesystem"] == "btrfs"
    assert profile["install_partition_notes"] == "Use only the explicitly confirmed VM disk."
    assert profile["apps_to_install_count"] == 2
    assert profile["preferences"]["distro_preference"] == "arch"
    assert "ai_config" not in profile

    app_signals = _zip_json(bundle, "app-verification.json")
    assert app_signals["app_verification_signals"]["Brave"]["target"] == "brave-bin"
    assert app_signals["unverified_installed_apps"] == ["Brave"]

    assert _zip_json(bundle, "capability-events.json")[0]["event_type"] == "capability_granted"
    assert _zip_json(bundle, "preflight-events.json")[0]["event_type"] == (
        "command_preflight_decision"
    )
    assert _zip_json(bundle, "dangerous-operation-previews.json")[0]["event_type"] == (
        "dangerous_operation_preview"
    )
    assert any(
        item["source"] == "app_verification" for item in _zip_json(bundle, "manual-followups.json")
    )
    assert _zip_json(bundle, "certification-summary.json")["events"][0]["success"] is True

    combined = "\n".join(_zip_texts(bundle).values())
    assert "sk-profile-secret" not in combined
