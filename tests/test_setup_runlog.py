from __future__ import annotations

import json

from quickhatch.setup.runlog import SetupRunLogger
from quickhatch.setup.runlog import write_executed_commands_script
from quickhatch.setup.runlog import redact_sensitive_text


def test_runlog_writes_jsonl_entries(tmp_path):
    logger = SetupRunLogger("run123", root=tmp_path)
    logger.record("planner_prompt", stage="research_and_plan", prompt="{}")
    logger.record("command", stage="apps", command="sudo pacman -S steam", rc=0)

    lines = logger.path.read_text(encoding="utf-8").splitlines()

    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["run_id"] == "run123"
    assert first["event_type"] == "planner_prompt"
    assert second["command"] == "sudo pacman -S steam"


def test_runlog_redacts_sensitive_values(tmp_path):
    logger = SetupRunLogger("run-secret", root=tmp_path)
    logger.record(
        "planner_prompt",
        profile={"api_key": "sk-secret", "default_account_password": "password"},
        prompt=(
            '{"default_account_password": "hunter2"}\n'
            "password: hunter2\n"
            "echo 'jan:hunter2' | chpasswd\n"
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKE honz.bartos@example.test"
        ),
    )

    entry = json.loads(logger.path.read_text(encoding="utf-8").splitlines()[0])

    serialized = json.dumps(entry)
    assert "sk-secret" not in serialized
    assert "hunter2" not in serialized
    assert "AAAAC3" not in serialized
    assert "honz.bartos@example.test" not in serialized
    assert entry["profile"]["api_key"] == "***REDACTED***"
    assert entry["profile"]["default_account_password"] == "***REDACTED***"


def test_redact_sensitive_text_removes_ssh_public_key_comment():
    text = (
        "grep -qxF 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKE "
        "honz.bartos@example.test' ~/.ssh/authorized_keys"
    )

    redacted = redact_sensitive_text(text)

    assert "AAAAC3" not in redacted
    assert "honz.bartos@example.test" not in redacted
    assert "ssh-key ***REDACTED***" in redacted


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
