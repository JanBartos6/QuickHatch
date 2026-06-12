from __future__ import annotations

import json
import zipfile

import pytest

from quickhatch import cli
from quickhatch.config import UserProfile


def _write_profile(path, profile: UserProfile | None = None):
    profile = profile or UserProfile()
    path.write_text(json.dumps(cli._redacted_profile_dict(profile)), encoding="utf-8")
    return path


def test_cli_help_exposes_setup_debug_certification_and_support(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    top_help = capsys.readouterr().out
    for token in ("debug", "setup", "profile", "models", "certify"):
        assert token in top_help

    with pytest.raises(SystemExit) as exc:
        cli.main(["debug", "--help"])
    assert exc.value.code == 0
    debug_help = capsys.readouterr().out
    for token in ("support-bundle", "release-smoke", "catalog-health", "harness-health"):
        assert token in debug_help

    with pytest.raises(SystemExit) as exc:
        cli.main(["setup", "run", "--help"])
    assert exc.value.code == 0
    setup_help = capsys.readouterr().out
    for token in (
        "--install-profile",
        "--remote-setup-mode",
        "--install-encryption",
        "--dangerous-mode",
    ):
        assert token in setup_help


def test_cli_profile_write_preserves_install_fields_and_redacts_api_key(tmp_path, monkeypatch):
    source = _write_profile(tmp_path / "source.json")
    out = tmp_path / "profile.json"
    monkeypatch.setenv("QH_TEST_API_KEY", "sk-redact")
    captured = []
    monkeypatch.setattr(cli, "_print_json", captured.append)

    cli.main(
        [
            "profile",
            "write",
            "--from-profile",
            str(source),
            "--out",
            str(out),
            "--ai-provider",
            "api_gemini",
            "--ai-model",
            "gemini-3.1-flash-lite",
            "--api-key-env",
            "QH_TEST_API_KEY",
            "--remote-setup-mode",
            "configure_only",
            "--install-profile",
            "configure_existing",
            "--install-encryption",
            "luks",
            "--install-filesystem",
            "btrfs",
            "--linux-host",
            "192.0.2.10:2222",
            "--linux-user",
            "quickhatch",
            "--data-transfer-method",
            "keep_windows",
            "--data-transfer-path",
            "C:\\Users\\User\\Documents,C:\\Users\\User\\Desktop",
            "--dangerous-mode",
        ]
    )

    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["ai_config"]["api_key"] is None
    assert saved["ai_config"]["provider"] == "api_gemini"
    assert saved["ai_config"]["model_name"] == "gemini-3.1-flash-lite"
    assert saved["remote_setup_mode"] == "configure_only"
    assert saved["install_profile"] == "configure_existing"
    assert saved["install_encryption"] == "luks"
    assert saved["install_filesystem"] == "btrfs"
    assert saved["data_transfer_method"] == "keep_windows"
    assert saved["data_transfer_paths"] == [
        "C:\\Users\\User\\Documents",
        "C:\\Users\\User\\Desktop",
    ]
    assert saved["dangerous_mode"] is True
    assert "sk-redact" not in json.dumps(captured)


def test_cli_configure_existing_setup_launch_preserves_non_destructive_mode(tmp_path, monkeypatch):
    source = _write_profile(tmp_path / "profile.json")
    captured_profile = {}
    captured_payload = []

    def fake_run(profile):
        captured_profile["remote_setup_mode"] = profile.remote_setup_mode
        captured_profile["install_profile"] = profile.install_profile
        return cli._setup_payload(
            profile=profile,
            bridge_setup_run={
                "status": "manual_followup_required",
                "final_status": "manual_followup_required",
                "blockers": [],
            },
            summary="Manual follow-up remains.",
        )

    monkeypatch.setattr(cli, "_run_setup_agent_cli", fake_run)
    monkeypatch.setattr(cli, "_print_json", captured_payload.append)

    cli.main(
        [
            "setup",
            "run",
            "--from-profile",
            str(source),
            "--linux-host",
            "192.0.2.10",
            "--linux-user",
            "quickhatch",
            "--remote-setup-mode",
            "configure_only",
            "--install-profile",
            "configure_existing",
        ]
    )

    assert captured_profile == {
        "remote_setup_mode": "configure_only",
        "install_profile": "configure_existing",
    }
    assert captured_payload[-1]["status"] == "manual_followup_required"
    assert captured_payload[-1]["final_status"] == "manual_followup_required"


def test_cli_destructive_setup_requires_explicit_confirmation(tmp_path, monkeypatch):
    source = _write_profile(tmp_path / "profile.json")
    monkeypatch.setattr(
        cli,
        "_run_setup_agent_cli",
        lambda _profile: pytest.fail("setup should not launch without confirmation"),
    )

    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "setup",
                "run",
                "--from-profile",
                str(source),
                "--linux-host",
                "192.0.2.10",
                "--linux-user",
                "quickhatch",
                "--remote-setup-mode",
                "wipe_and_reinstall",
                "--install-profile",
                "full_disk_wipe",
            ]
        )

    assert "requires explicit CLI confirmation" in str(exc.value)


def test_cli_dangerous_mode_bypasses_destructive_setup_confirmation(tmp_path, monkeypatch):
    source = _write_profile(tmp_path / "profile.json")
    captured_profile = {}
    captured_payload = []

    def fake_run(profile):
        captured_profile["remote_setup_mode"] = profile.remote_setup_mode
        captured_profile["install_profile"] = profile.install_profile
        captured_profile["dangerous_mode"] = profile.dangerous_mode
        return cli._setup_payload(
            profile=profile,
            bridge_setup_run={"status": "success", "final_status": "success"},
            summary="Setup completed.",
        )

    monkeypatch.setattr(cli, "_run_setup_agent_cli", fake_run)
    monkeypatch.setattr(cli, "_print_json", captured_payload.append)

    cli.main(
        [
            "setup",
            "run",
            "--from-profile",
            str(source),
            "--linux-host",
            "192.0.2.10",
            "--linux-user",
            "quickhatch",
            "--remote-setup-mode",
            "wipe_and_reinstall",
            "--install-profile",
            "full_disk_wipe",
            "--dangerous-mode",
        ]
    )

    assert captured_profile == {
        "remote_setup_mode": "wipe_and_reinstall",
        "install_profile": "full_disk_wipe",
        "dangerous_mode": True,
    }
    assert captured_payload[-1]["dangerous_mode"] is True


def test_cli_setup_payload_distinguishes_blocked_final_status():
    profile = UserProfile()

    payload = cli._setup_payload(
        profile=profile,
        bridge_setup_run={
            "status": "blocked",
            "final_status": "blocked",
            "blockers": ["fstab check failed"],
        },
        summary="Setup blocked.",
    )

    assert payload["status"] == "blocked"
    assert payload["final_status"] == "blocked"
    assert payload["blockers"] == ["fstab check failed"]


def test_cli_support_bundle_command_exports_redacted_bundle(tmp_path, monkeypatch):
    runlog = tmp_path / "run.jsonl"
    runlog.write_text(
        json.dumps(
            {
                "event_type": "profile",
                "profile": {
                    "ai_config": {"api_key": "sk-redact"},
                    "default_account_password": "setup-password",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    bundle = tmp_path / "support.zip"
    captured = []
    monkeypatch.setattr(cli, "_print_json", captured.append)

    cli.main(["debug", "support-bundle", "--runlog", str(runlog), "--out", str(bundle)])

    assert captured[-1]["status"] == "ok"
    with zipfile.ZipFile(bundle) as zf:
        combined = "\n".join(
            zf.read(name).decode("utf-8", errors="replace") for name in zf.namelist()
        )
    assert "sk-redact" not in combined
    assert "setup-password" not in combined


def test_cli_release_smoke_command_routes_to_payload(monkeypatch):
    from quickhatch.setup import debugtools

    captured = []
    monkeypatch.setattr(cli, "_print_json", captured.append)
    monkeypatch.setattr(
        debugtools,
        "release_smoke_payload",
        lambda **kwargs: {"success": True, "received": kwargs},
    )

    cli.main(["debug", "release-smoke", "--full-pytest", "--timeout", "123"])

    assert captured[-1]["success"] is True
    assert captured[-1]["received"]["full_pytest"] is True
    assert captured[-1]["received"]["timeout"] == 123


def test_cli_gemini_model_resolution_prefers_flash_lite_without_leaking_key(
    monkeypatch,
):
    from quickhatch.ai import model_list

    captured = []
    monkeypatch.setattr(cli, "_print_json", captured.append)
    monkeypatch.setattr(
        model_list,
        "fetch",
        lambda kind, endpoint, api_key: [
            "gemini-2.5-pro",
            "gemini-2.5-flash-lite",
            "gemini-3.1-flash-lite",
        ],
    )

    cli.main(
        [
            "models",
            "list",
            "--provider",
            "api_gemini",
            "--api-key",
            "AIza-secret-not-printed",
            "--prefer-gemini-flash-lite",
        ]
    )

    payload = captured[-1]
    assert payload["selected_model"] == "gemini-3.1-flash-lite"
    assert payload["api_key_present"] is True
    assert "AIza-secret-not-printed" not in json.dumps(payload)


def test_cli_gemini_model_resolution_falls_back_to_closest_flash(monkeypatch):
    from quickhatch import secrets
    from quickhatch.ai import model_list

    captured = []
    monkeypatch.setattr(cli, "_print_json", captured.append)
    monkeypatch.setattr(secrets, "load_api_key", lambda provider, endpoint: "stored-secret-key")

    seen = {}

    def fake_fetch(kind, endpoint, api_key):
        seen["api_key"] = api_key
        return ["gemini-2.5-pro", "gemini-2.5-flash-lite"]

    monkeypatch.setattr(
        model_list,
        "fetch",
        fake_fetch,
    )

    cli.main(["models", "list", "--provider", "api_gemini", "--prefer-gemini-flash-lite"])

    assert seen["api_key"] == "stored-secret-key"
    assert captured[-1]["selected_model"] == "gemini-2.5-flash-lite"
    assert "closest Flash Lite/Flash" in captured[-1]["selection_reason"]
    assert "stored-secret-key" not in json.dumps(captured[-1])


def test_cli_gemini_model_resolution_prefers_exact_model_over_lite_variant(monkeypatch):
    from quickhatch.ai import model_list

    captured = []
    monkeypatch.setattr(cli, "_print_json", captured.append)
    monkeypatch.setattr(
        model_list,
        "fetch",
        lambda kind, endpoint, api_key: [
            "gemini-2.5-flash-lite",
            "gemini-2.5-flash",
        ],
    )

    cli.main(
        [
            "models",
            "list",
            "--provider",
            "api_gemini",
            "--api-key",
            "AIza-secret-not-printed",
            "--prefer",
            "gemini-2.5-flash",
        ]
    )

    assert captured[-1]["selected_model"] == "gemini-2.5-flash"
    assert captured[-1]["selection_reason"] == "selected exact fetched model"
    assert "AIza-secret-not-printed" not in json.dumps(captured[-1])
