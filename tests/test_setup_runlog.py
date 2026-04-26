from __future__ import annotations

import json

from quickhatch.setup.runlog import SetupRunLogger


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
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKE quickhatch"
        ),
    )

    entry = json.loads(logger.path.read_text(encoding="utf-8").splitlines()[0])

    serialized = json.dumps(entry)
    assert "sk-secret" not in serialized
    assert "hunter2" not in serialized
    assert "AAAAC3" not in serialized
    assert entry["profile"]["api_key"] == "***REDACTED***"
    assert entry["profile"]["default_account_password"] == "***REDACTED***"
