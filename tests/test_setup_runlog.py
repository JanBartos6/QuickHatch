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
