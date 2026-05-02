from __future__ import annotations

import json

from quickhatch.setup.replay import (
    build_regression_fixture,
    load_run_events,
    promote_regression_fixture,
    summarize_runlog,
    summarize_stage,
)


def test_replay_loader_reconstructs_summary(tmp_path):
    path = tmp_path / "run.jsonl"
    events = [
        {"run_id": "abc", "event_type": "facts_refresh", "stage": "connect_and_probe"},
        {
            "run_id": "abc",
            "event_type": "command",
            "stage": "apps",
            "group_id": "browser",
            "command": "sudo pacman -S brave-bin",
            "rc": 1,
            "stderr_tail": "error: target not found: brave-bin",
        },
        {
            "run_id": "abc",
            "event_type": "command_preflight_blocked",
            "stage": "apps",
            "reason": "blocked for test",
        },
    ]
    path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")

    loaded = load_run_events(path)
    summary = summarize_runlog(path)

    assert loaded[0]["event_id"] == "run:000001"
    assert summary.run_id == "abc"
    assert summary.command_count == 1
    assert summary.failed_command_count == 1
    assert summary.failure_categories["package_missing"] == 1
    assert summary.blockers == ["blocked for test"]


def test_stage_replay_filters_group(tmp_path):
    path = tmp_path / "run.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(event)
            for event in [
                {"event_type": "command", "stage": "apps", "group_id": "one", "rc": 0},
                {"event_type": "command", "stage": "apps", "group_id": "two", "rc": 0},
                {"event_type": "command", "stage": "desktop", "group_id": "one", "rc": 0},
            ]
        ),
        encoding="utf-8",
    )

    result = summarize_stage(path, stage="apps", group_id="two")

    assert result["events"] == 1
    assert result["last_events"][0]["group_id"] == "two"


def test_promote_regression_generates_pytest_fixture(tmp_path):
    path = tmp_path / "run.jsonl"
    path.write_text(
        json.dumps(
            {
                "event_id": "run123:000004",
                "event_type": "command",
                "command": "sudo apt-get install nope",
                "rc": 100,
                "stderr_tail": "Unable to locate package nope",
            }
        ),
        encoding="utf-8",
    )

    fixture = build_regression_fixture(path, event_id="run123:000004", name="apt missing")
    output = promote_regression_fixture(
        path,
        event_id="run123:000004",
        name="apt missing",
        output_dir=tmp_path / "generated",
    )

    assert "test_apt_missing" in fixture
    assert "package_missing" in fixture
    assert output.exists()
