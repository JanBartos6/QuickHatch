from __future__ import annotations

import json
import zipfile

from quickhatch.setup.runlog import (
    SetupRunLogger,
    export_support_bundle,
    redact_sensitive_text,
    write_executed_commands_script,
)


def test_runlog_writes_jsonl_entries(tmp_path):
    logger = SetupRunLogger("run123", root=tmp_path)
    logger.record("planner_prompt", stage="research_and_plan", prompt="{}")
    logger.record("command", stage="apps", command="sudo pacman -S steam", rc=0)

    lines = logger.path.read_text(encoding="utf-8").splitlines()

    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["run_id"] == "run123"
    assert first["event_id"] == "run123:000001"
    assert first["event_type"] == "planner_prompt"
    assert second["event_id"] == "run123:000002"
    assert second["command"] == "sudo pacman -S steam"


def test_runlog_redacts_sensitive_values(tmp_path):
    logger = SetupRunLogger("run-secret", root=tmp_path)
    logger.record(
        "planner_prompt",
        profile={"api_key": "sk-redact", "default_account_password": "password"},
        prompt=(
            '{"default_account_password": "hunter2"}\n'
            "password: hunter2\n"
            "echo 'jan:hunter2' | chpasswd\n"
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKE user@example.test"
        ),
    )

    entry = json.loads(logger.path.read_text(encoding="utf-8").splitlines()[0])

    serialized = json.dumps(entry)
    assert "sk-redact" not in serialized
    assert "hunter2" not in serialized
    assert "AAAAC3" not in serialized
    assert "user@example.test" not in serialized
    assert entry["profile"]["api_key"] == "***REDACTED***"
    assert entry["profile"]["default_account_password"] == "***REDACTED***"


def test_redact_sensitive_text_removes_ssh_public_key_comment():
    text = (
        "grep -qxF 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKE "
        "user@example.test' ~/.ssh/authorized_keys"
    )

    redacted = redact_sensitive_text(text)

    assert "AAAAC3" not in redacted
    assert "user@example.test" not in redacted
    assert "ssh-key ***REDACTED***" in redacted


def test_redact_sensitive_text_removes_chroot_chpasswd_variables():
    text = 'qh_pw=hunter2; printf \'%s\\n\' "$qh_pw" | $qh_sudo chroot "$qh_root" chpasswd'

    redacted = redact_sensitive_text(text)

    assert "hunter2" not in redacted
    assert "qh_pw=***REDACTED***" in redacted
    assert "echo '***REDACTED***' | chpasswd" in redacted


def test_executed_commands_script_contains_successful_commands_only(tmp_path):
    logger = SetupRunLogger("run-replay", root=tmp_path)
    logger.record("command", stage="apps", group_id="browser", command="sudo pacman -S brave", rc=0)
    logger.record(
        "verification",
        stage="apps",
        group_id="browser",
        command="pacman -Q brave-bin",
        rc=0,
    )
    logger.record("command", stage="apps", group_id="broken", command="sudo pacman -S nope", rc=1)
    logger.record(
        "command",
        stage="install_or_skip",
        group_id="user",
        command="echo 'jan:hunter2' | chpasswd",
        rc=0,
    )

    script_path = write_executed_commands_script(
        logger.path,
        output_dir=tmp_path / "bundle",
        status="complete",
    )

    assert script_path is not None
    text = script_path.read_text(encoding="utf-8")
    assert "sudo pacman -S brave" in text
    assert "# VERIFY rc=0: pacman -Q brave-bin" in text
    assert "# SKIPPED failed command rc=1: sudo pacman -S nope" in text
    assert "hunter2" not in text
    assert "# EDIT REQUIRED redacted command rc=0: echo '***REDACTED***' | chpasswd" in text


def test_export_support_bundle_writes_redacted_diagnostics_zip(tmp_path):
    logger = SetupRunLogger("run-support", root=tmp_path / "runs")
    logger.record(
        "planner_prompt",
        profile={"api_key": "sk-redact", "default_account_password": "hunter2"},
        prompt=(
            '{"default_account_password": "hunter2"}\n'
            "password: hunter2\n"
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKE user@example.test"
        ),
    )
    logger.record("run_started", host="127.0.0.1", user="jan")
    logger.record("command", stage="apps", group_id="browser", command="sudo pacman -S brave", rc=0)
    logger.record(
        "command",
        stage="install_or_skip",
        group_id="user",
        command="echo 'jan:hunter2' | chpasswd",
        rc=0,
    )

    bundle = export_support_bundle(logger.path, output_dir=tmp_path / "support")

    assert bundle.exists()
    with zipfile.ZipFile(bundle) as zf:
        names = set(zf.namelist())
        assert {
            "manifest.json",
            "README.txt",
            "support-metadata.json",
            "support-summary.json",
            "replay-summary.json",
            "runlog-scan-summary.json",
            "setup-runlog-redacted.jsonl",
            "executed-commands.sh",
        } <= names
        combined = "\n".join(
            zf.read(name).decode("utf-8")
            for name in sorted(names)
            if name.endswith((".json", ".jsonl", ".sh", ".txt"))
        )

    assert "sudo pacman -S brave" in combined
    assert "sk-redact" not in combined
    assert "hunter2" not in combined
    assert "AAAAC3" not in combined
    assert "user@example.test" not in combined
    assert ".quickhatch-checkpoint" not in names
    assert "quickhatch-migration-bundle.zip" not in names
