from __future__ import annotations

import json

from quickhatch.setup.replay import (
    build_regression_fixture,
    find_latest_runlog,
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


def test_replay_diagnoses_failed_fstab_verification(tmp_path):
    path = tmp_path / "run.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(event)
            for event in [
                {
                    "run_id": "abc",
                    "event_type": "verification",
                    "event_id": "abc:000001",
                    "stage": "install_or_skip",
                    "group_id": "base",
                    "command": "awk '$2 == \"/\"' /mnt/etc/fstab | grep -q ' / '",
                    "rc": 1,
                },
                {
                    "run_id": "abc",
                    "event_type": "blocking_question_timeout",
                    "event_id": "abc:000002",
                    "stage": "install_or_skip",
                    "question_id": "q1",
                },
            ]
        ),
        encoding="utf-8",
    )

    summary = summarize_runlog(path)

    assert summary.failed_verification_count == 1
    assert summary.blocking_question_timeout_count == 1
    assert summary.last_failure_event_id == "abc:000002"
    assert any(
        diagnostic.category == "fragile_fstab_verification"
        and diagnostic.severity == "harness_fixable"
        for diagnostic in summary.diagnostics
    )
    assert any(
        diagnostic.category == "blocking_question_timeout" for diagnostic in summary.diagnostics
    )


def test_replay_diagnoses_preboot_driver_runtime_probe(tmp_path):
    path = tmp_path / "run.jsonl"
    path.write_text(
        json.dumps(
            {
                "run_id": "abc",
                "event_type": "verification",
                "event_id": "abc:000010",
                "stage": "install_or_skip",
                "group_id": "drivers",
                "command": "arch-chroot /mnt modinfo nvidia_uvm",
                "rc": 1,
                "stderr_tail": "modinfo: ERROR: Module nvidia_uvm not found.",
            }
        ),
        encoding="utf-8",
    )

    summary = summarize_runlog(path)

    assert summary.failed_verification_count == 1
    assert any(
        diagnostic.category == "preboot_driver_runtime_probe" for diagnostic in summary.diagnostics
    )


def test_latest_runlog_prefers_real_setup_run_over_tiny_health_artifact(tmp_path):
    tiny = tmp_path / "tiny.jsonl"
    tiny.write_text(
        json.dumps({"event_type": "command", "run_id": "tiny", "rc": 0}),
        encoding="utf-8",
    )
    real = tmp_path / "real.jsonl"
    real.write_text(
        "\n".join(
            json.dumps(event)
            for event in [
                {"event_type": "run_started", "run_id": "real"},
                {"event_type": "connect_and_probe", "run_id": "real"},
                {"event_type": "planner_prompt", "run_id": "real"},
                {"event_type": "command", "run_id": "real", "rc": 0},
            ]
        ),
        encoding="utf-8",
    )

    assert find_latest_runlog(tmp_path) == real
