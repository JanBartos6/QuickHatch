from __future__ import annotations

import json

from quickhatch.setup.replay import (
    build_regression_fixture,
    find_latest_runlog,
    load_run_events,
    promote_regression_fixture,
    scan_runlogs,
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


def test_latest_runlog_prefers_newer_malformed_real_run(tmp_path):
    old_path = tmp_path / "old.jsonl"
    old_path.write_text(
        json.dumps({"event_type": "run_started", "run_id": "old"}) + "\n",
        encoding="utf-8",
    )
    new_path = tmp_path / "new.jsonl"
    new_path.write_text(
        json.dumps({"event_type": "run_started", "run_id": "new"}) + "\n{bad",
        encoding="utf-8",
    )

    assert find_latest_runlog(tmp_path) == new_path
    scanned = scan_runlogs(tmp_path)
    assert scanned.failure_categories["runlog_jsonl_decode_error"] == 1


def test_replay_classifies_failed_verification_and_shadow_preflight():
    summary = summarize_runlog_from_events(
        [
            {
                "event_type": "verification",
                "stage": "postboot_driver",
                "command": "nvidia-smi",
                "rc": 1,
                "stderr_tail": (
                    "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver"
                ),
            },
            {
                "event_type": "command_preflight_decision",
                "stage": "apps",
                "decisions": [
                    {
                        "kind": "shadow",
                        "category": "package_manager_mismatch",
                        "reason": "wrong package manager",
                    }
                ],
            },
        ]
    )

    assert summary.failed_verification_count == 1
    assert summary.failure_categories
    assert "package_manager_mismatch" in summary.failure_categories


def test_replay_classifies_structured_audit_findings():
    summary = summarize_runlog_from_events(
        [
            {
                "event_type": "final_audit",
                "status": "manual_followup_required",
                "result": {
                    "passed": True,
                    "findings": [
                        {
                            "id": "apps-1",
                            "severity": "manual_followup",
                            "category": "apps",
                            "summary": "Optional app unavailable.",
                        }
                    ],
                },
            }
        ]
    )

    assert summary.audit_findings["manual_followup:apps"] == 1
    assert summary.failure_categories["manual_followup:apps"] == 1
    assert summary.final_status == "manual_followup_required"


def test_replay_uses_nested_final_audit_final_status():
    summary = summarize_runlog_from_events(
        [
            {
                "event_type": "final_audit",
                "result": {
                    "passed": True,
                    "final_status": "FinalSetupStatus.SUCCESS_WITH_WARNINGS",
                    "findings": [],
                },
            }
        ]
    )

    assert summary.final_status == "success_with_warnings"


def summarize_runlog_from_events(events):
    from quickhatch.setup.replay import summarize_events

    return summarize_events(events)


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


def test_replay_diagnoses_bootloader_chroot_bind_mount_failure(tmp_path):
    path = tmp_path / "run.jsonl"
    path.write_text(
        json.dumps(
            {
                "run_id": "abc",
                "event_type": "command",
                "event_id": "abc:000011",
                "stage": "install_or_skip",
                "group_id": "bootloader",
                "command": "grub-install --target=x86_64-efi --efi-directory=/boot",
                "rc": 1,
                "stderr_tail": "grub-install: error: failed to get canonical path of `airootfs'.",
            }
        ),
        encoding="utf-8",
    )

    summary = summarize_runlog(path)

    assert any(
        diagnostic.category == "bootloader_chroot_bind_mounts" for diagnostic in summary.diagnostics
    )


def test_replay_diagnoses_interactive_live_installer_over_ssh(tmp_path):
    path = tmp_path / "run.jsonl"
    path.write_text(
        json.dumps(
            {
                "run_id": "abc",
                "event_type": "command",
                "event_id": "abc:000012",
                "stage": "install_or_skip",
                "group_id": "anaconda",
                "command": "sudo dbus-run-session anaconda --kickstart=/tmp/qh.ks --liveinst",
                "rc": 124,
                "stderr_tail": "Command timed out",
            }
        ),
        encoding="utf-8",
    )

    summary = summarize_runlog(path)

    assert any(
        diagnostic.category == "interactive_live_installer_over_ssh"
        for diagnostic in summary.diagnostics
    )


def test_replay_diagnoses_shell_printf_format_bug(tmp_path):
    path = tmp_path / "run.jsonl"
    path.write_text(
        json.dumps(
            {
                "run_id": "abc",
                "event_type": "command",
                "event_id": "abc:000013",
                "stage": "install_or_skip",
                "group_id": "kickstart",
                "command": "printf 'url --mirrorlist=%s' > /tmp/qh.ks",
                "rc": 1,
                "stderr_tail": "bash: line 1: printf: `p': invalid format character",
            }
        ),
        encoding="utf-8",
    )

    summary = summarize_runlog(path)

    assert any(
        diagnostic.category == "shell_printf_format_bug" for diagnostic in summary.diagnostics
    )


def test_replay_diagnoses_efi_mount_layout_conflict(tmp_path):
    path = tmp_path / "run.jsonl"
    path.write_text(
        json.dumps(
            {
                "run_id": "abc",
                "event_type": "command",
                "event_id": "abc:000014",
                "stage": "install_or_skip",
                "group_id": "mounts",
                "command": "sudo mount /dev/sda1 /mnt/boot/efi/efi",
                "rc": 32,
                "stderr_tail": "mount: /mnt/boot/efi/efi: /dev/sda1 already mounted on /mnt/boot/efi/efi.",
            }
        ),
        encoding="utf-8",
    )

    summary = summarize_runlog(path)

    assert any(
        diagnostic.category == "efi_mount_layout_conflict" for diagnostic in summary.diagnostics
    )


def test_replay_diagnoses_invalid_disk_selection(tmp_path):
    path = tmp_path / "run.jsonl"
    path.write_text(
        json.dumps(
            {
                "run_id": "abc",
                "event_type": "command",
                "event_id": "abc:000015",
                "stage": "install_or_skip",
                "group_id": "partition",
                "command": 'parted -s "$DISK" mklabel gpt',
                "rc": 1,
                "stderr_tail": "Error: Could not stat device  - No such file or directory.",
            }
        ),
        encoding="utf-8",
    )

    summary = summarize_runlog(path)

    assert any(
        diagnostic.category == "empty_or_invalid_target_disk" for diagnostic in summary.diagnostics
    )


def test_replay_diagnoses_chroot_before_base_or_user(tmp_path):
    path = tmp_path / "run.jsonl"
    path.write_text(
        json.dumps(
            {
                "run_id": "abc",
                "event_type": "command",
                "event_id": "abc:000016",
                "stage": "install_or_skip",
                "group_id": "ssh",
                "command": 'sudo chroot /mnt /bin/bash -c "echo x | chpasswd"',
                "rc": 1,
                "stderr_tail": "chpasswd: cannot open /etc/passwd",
            }
        ),
        encoding="utf-8",
    )

    summary = summarize_runlog(path)

    assert any(
        diagnostic.category == "chroot_before_base_or_user" for diagnostic in summary.diagnostics
    )


def test_replay_diagnoses_invalid_lsblk_mount_verification(tmp_path):
    path = tmp_path / "run.jsonl"
    path.write_text(
        json.dumps(
            {
                "run_id": "abc",
                "event_type": "command",
                "event_id": "abc:000017",
                "stage": "install_or_skip",
                "group_id": "verify",
                "command": "lsblk /mnt",
                "rc": 32,
                "stderr_tail": "lsblk: /mnt: not a block device",
            }
        ),
        encoding="utf-8",
    )

    summary = summarize_runlog(path)

    assert any(
        diagnostic.category == "invalid_lsblk_mount_verification"
        for diagnostic in summary.diagnostics
    )


def test_replay_diagnoses_common_verification_false_paths(tmp_path):
    cases = [
        (
            "mountpoint /mnt/boot/efi",
            "/mnt/boot/efi is not a mountpoint",
            "efi_mount_layout_conflict",
        ),
        (
            "kreadconfig6 --file kdeglobals --group Icons --key Theme '|' grep -q Papirus-Dark",
            "kreadconfig6: Unknown option 'q'.",
            "quoted_shell_pipeline",
        ),
        (
            "parted -ms /dev/sda print | grep -q '^1:.*:fat32:'",
            "",
            "partition_layout_verification_failed",
        ),
        (
            "test -f /mnt/boot/grub2/grub.cfg",
            "",
            "bootloader_artifact_missing",
        ),
    ]
    for index, (command, stderr, category) in enumerate(cases, start=1):
        path = tmp_path / f"run-{index}.jsonl"
        path.write_text(
            json.dumps(
                {
                    "run_id": f"abc-{index}",
                    "event_type": "verification",
                    "event_id": f"abc:{index:06d}",
                    "stage": "install_or_skip",
                    "group_id": "verify",
                    "command": command,
                    "rc": 1,
                    "stderr_tail": stderr,
                }
            ),
            encoding="utf-8",
        )

        summary = summarize_runlog(path)

        assert any(diagnostic.category == category for diagnostic in summary.diagnostics)


def test_replay_diagnoses_common_command_failures_from_mined_runs(tmp_path):
    cases = [
        (
            "mountpoint -q /mnt/boot/efi",
            "",
            "efi_mount_layout_conflict",
        ),
        (
            "swapoff -a || true",
            "Connection closed by 127.0.0.1 port 2225",
            "ssh_connection_dropped",
        ),
        (
            "sh -c 'sed -i s/main/main contrib/ /etc/apt/sources.list'",
            "sed: couldn't open temporary file /etc/apt/sedYBKNj0: Permission denied",
            "missing_privilege_for_system_edit",
        ),
        (
            "sudo add-apt-repository -y universe",
            "sudo: 'add-apt-repository': command not found",
            "missing_repository_tool",
        ),
        (
            "command -v flatpak && flatpak list | grep -q VLC",
            "error: While opening repository /var/lib/flatpak/repo: opening repo: opendir(/var/lib/flatpak/repo): No such file or directory",
            "flatpak_repo_not_initialized",
        ),
    ]
    for index, (command, stderr, category) in enumerate(cases, start=1):
        path = tmp_path / f"cmd-{index}.jsonl"
        path.write_text(
            json.dumps(
                {
                    "run_id": f"cmd-{index}",
                    "event_type": "command",
                    "event_id": f"cmd:{index:06d}",
                    "stage": "install_or_skip",
                    "group_id": "mined",
                    "command": command,
                    "rc": 1,
                    "stderr_tail": stderr,
                }
            ),
            encoding="utf-8",
        )

        summary = summarize_runlog(path)

        assert any(diagnostic.category == category for diagnostic in summary.diagnostics)


def test_scan_runlogs_aggregates_diagnostics(tmp_path):
    nested = tmp_path / "certification" / "arch" / "latest"
    nested.mkdir(parents=True)
    run = nested / "real.jsonl"
    run.write_text(
        "\n".join(
            json.dumps(event)
            for event in [
                {"event_type": "run_started", "run_id": "real"},
                {
                    "event_type": "verification",
                    "event_id": "real:000002",
                    "run_id": "real",
                    "stage": "install_or_skip",
                    "command": "grep -q ' / ' /mnt/etc/fstab",
                    "rc": 1,
                },
            ]
        ),
        encoding="utf-8",
    )

    summary = scan_runlogs(tmp_path)

    assert summary.run_count == 1
    assert summary.diagnostic_categories["fragile_fstab_verification"] == 1
    assert summary.runs[0]["path"] == str(run)


def test_scan_runlogs_keeps_failed_vm_certification_artifacts(tmp_path):
    nested = tmp_path / "certification" / "arch" / "latest"
    nested.mkdir(parents=True)
    run = nested / "certification-run.jsonl"
    run.write_text(
        json.dumps(
            {
                "event_type": "vm_certification_finished",
                "event_id": "cert:000001",
                "run_id": "cert",
                "success": False,
                "exit_code": 1,
                "error": "installer failed",
            }
        ),
        encoding="utf-8",
    )

    summary = scan_runlogs(tmp_path)

    assert summary.run_count == 1
    assert summary.failure_categories["vm_certification_failed"] == 1
    assert "VM certification failed: installer failed" in summary.runs[0]["blockers"]


def test_scan_runlogs_surfaces_malformed_jsonl_instead_of_dropping_run(tmp_path):
    nested = tmp_path / "setup-runs" / "bad"
    nested.mkdir(parents=True)
    run = nested / "run.jsonl"
    run.write_text('{"event_type": "run_started"}\n{"event_type": ', encoding="utf-8")

    summary = scan_runlogs(tmp_path)

    assert summary.run_count == 1
    assert summary.failure_categories["runlog_jsonl_decode_error"] == 1
    assert any("malformed JSONL" in blocker for blocker in summary.runs[0]["blockers"])


def test_replay_summary_surfaces_failed_final_audit_blockers(tmp_path):
    run = tmp_path / "failed-final.jsonl"
    run.write_text(
        json.dumps(
            {
                "event_type": "final_audit",
                "event_id": "final:000001",
                "run_id": "final",
                "result": {
                    "passed": False,
                    "blockers": ["Required app verification failed"],
                },
            }
        ),
        encoding="utf-8",
    )

    summary = summarize_runlog(run)

    assert "Required app verification failed" in summary.blockers
    assert summary.last_failure_type == "final_audit"
