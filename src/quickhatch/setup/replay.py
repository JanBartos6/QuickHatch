"""Replay/debug helpers for setup run JSONL artifacts."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .failure import classify_failure


@dataclass
class ReplayDiagnostic:
    event_id: str
    event_type: str
    stage: str
    group_id: str
    category: str
    severity: str
    command: str = ""
    reason: str = ""
    suggested_next: str = ""


@dataclass
class RunReplaySummary:
    run_id: str = ""
    event_count: int = 0
    stages: list[str] = field(default_factory=list)
    command_count: int = 0
    failed_command_count: int = 0
    failed_verification_count: int = 0
    blocking_question_timeout_count: int = 0
    repair_budget_gate_count: int = 0
    model_failure_count: int = 0
    blockers: list[str] = field(default_factory=list)
    failure_categories: dict[str, int] = field(default_factory=dict)
    audit_findings: dict[str, int] = field(default_factory=dict)
    last_failure_event_id: str = ""
    last_failure_type: str = ""
    last_failure_command: str = ""
    diagnostics: list[ReplayDiagnostic] = field(default_factory=list)
    final_status: str = "unknown"


@dataclass
class RunlogScanSummary:
    root: str
    run_count: int = 0
    event_count: int = 0
    failure_categories: dict[str, int] = field(default_factory=dict)
    diagnostic_categories: dict[str, int] = field(default_factory=dict)
    runs: list[dict[str, Any]] = field(default_factory=list)


def load_run_events(path: Path | str) -> list[dict[str, Any]]:
    source = Path(path)
    events: list[dict[str, Any]] = []
    for line_no, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{source}:{line_no}: invalid JSONL event: {exc}") from exc
        if isinstance(event, dict):
            event.setdefault("event_id", f"{source.stem}:{line_no:06d}")
            events.append(event)
    return events


def load_run_events_tolerant(path: Path | str) -> list[dict[str, Any]]:
    source = Path(path)
    events: list[dict[str, Any]] = []
    for line_no, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            events.append(
                {
                    "event_id": f"{source.stem}:{line_no:06d}",
                    "event_type": "jsonl_decode_error",
                    "path": str(source),
                    "line_no": line_no,
                }
            )
            continue
        if isinstance(event, dict):
            event.setdefault("event_id", f"{source.stem}:{line_no:06d}")
            events.append(event)
    return events


def summarize_events(events: list[dict[str, Any]]) -> RunReplaySummary:
    summary = RunReplaySummary(event_count=len(events))
    seen_stages: set[str] = set()
    previous_event: dict[str, Any] | None = None
    for event in events:
        counted_category = ""
        summary.run_id = summary.run_id or str(event.get("run_id") or "")
        stage = str(event.get("stage") or "")
        if stage and stage not in seen_stages:
            seen_stages.add(stage)
            summary.stages.append(stage)
        event_type = str(event.get("event_type") or "")
        event_failed = False
        command = str(event.get("command") or event.get("visible_command") or "")
        if event_type == "command":
            summary.command_count += 1
            try:
                rc = int(event.get("rc"))
            except (TypeError, ValueError):
                rc = 0
            if rc != 0:
                summary.failed_command_count += 1
                classification = event.get("failure_classification")
                if isinstance(classification, dict):
                    category = str(classification.get("category") or "unknown")
                else:
                    category = classify_failure(
                        command=str(event.get("command") or ""),
                        rc=rc,
                        stdout=str(event.get("stdout_tail") or ""),
                        stderr=str(event.get("stderr_tail") or ""),
                    ).category.value
                counted_category = category
                summary.failure_categories[category] = (
                    summary.failure_categories.get(category, 0) + 1
                )
                event_failed = True
        elif event_type == "verification":
            try:
                rc = int(event.get("rc"))
            except (TypeError, ValueError):
                rc = 0
            if rc != 0:
                summary.failed_verification_count += 1
                category = _classification_category(event, rc=rc)
                counted_category = category
                summary.failure_categories[category] = (
                    summary.failure_categories.get(category, 0) + 1
                )
                event_failed = True
        elif event_type == "vm_certification_finished":
            if not bool(event.get("success")):
                event_failed = True
                category = "vm_certification_failed"
                counted_category = category
                summary.failure_categories[category] = (
                    summary.failure_categories.get(category, 0) + 1
                )
                detail = str(event.get("error") or event.get("exit_code") or "").strip()
                summary.blockers.append(
                    "VM certification failed" + (f": {detail}" if detail else ".")
                )
        elif event_type == "certification_blocked":
            event_failed = True
            category = "vm_certification_blocked"
            counted_category = category
            summary.failure_categories[category] = summary.failure_categories.get(category, 0) + 1
            blockers = event.get("blockers")
            if isinstance(blockers, list):
                summary.blockers.extend(str(item) for item in blockers)
            else:
                summary.blockers.append("VM certification blocked before execution.")
        elif event_type == "jsonl_decode_error":
            event_failed = True
            category = "runlog_jsonl_decode_error"
            counted_category = category
            summary.failure_categories[category] = summary.failure_categories.get(category, 0) + 1
            summary.blockers.append(
                f"Runlog contains malformed JSONL at line {event.get('line_no')}."
            )
        elif event_type == "command_preflight_decision":
            decisions = event.get("decisions")
            if isinstance(decisions, list):
                for decision in decisions:
                    if not isinstance(decision, dict):
                        continue
                    kind = str(decision.get("kind") or "")
                    if kind not in {"block", "shadow"}:
                        continue
                    category = str(decision.get("category") or "preflight")
                    counted_category = category
                    summary.failure_categories[category] = (
                        summary.failure_categories.get(category, 0) + 1
                    )
                    if kind == "block":
                        event_failed = True
                    break
        elif event_type == "blocking_question_timeout":
            summary.blocking_question_timeout_count += 1
            event_failed = True
            question_id = str(event.get("question_id") or "")
            summary.blockers.append(
                f"Blocking question timed out{f' ({question_id})' if question_id else ''}."
            )
        elif event_type == "repair_budget_gate_answer" and event.get("answer") in {None, ""}:
            summary.repair_budget_gate_count += 1
            event_failed = True
        elif event_type in {
            "model_call_failed",
            "planner_response_invalid",
            "repair_response_invalid",
        }:
            summary.model_failure_count += 1
            event_failed = True

        if event_type in {"final_audit", "stage_audit"}:
            result = event.get("result")
            findings = result.get("findings") if isinstance(result, dict) else None
            if isinstance(findings, list):
                for finding in findings:
                    if not isinstance(finding, dict):
                        continue
                    severity = str(finding.get("severity") or "").lower()
                    category = str(finding.get("category") or "audit")
                    key = f"{severity or 'unknown'}:{category}"
                    summary.audit_findings[key] = summary.audit_findings.get(key, 0) + 1
                    if severity in {"fatal", "repairable", "manual_followup"}:
                        event_failed = True
                        summary.failure_categories[key] = summary.failure_categories.get(key, 0) + 1

        diagnostic = diagnose_event(event, previous_event=previous_event)
        if diagnostic is not None:
            summary.diagnostics.append(diagnostic)
            if event_failed and counted_category == "unknown":
                remaining = summary.failure_categories.get("unknown", 0) - 1
                if remaining > 0:
                    summary.failure_categories["unknown"] = remaining
                else:
                    summary.failure_categories.pop("unknown", None)
                summary.failure_categories[diagnostic.category] = (
                    summary.failure_categories.get(diagnostic.category, 0) + 1
                )
            elif event_failed and diagnostic.category != counted_category:
                summary.failure_categories[diagnostic.category] = (
                    summary.failure_categories.get(diagnostic.category, 0) + 1
                )
        if event_failed:
            summary.last_failure_event_id = str(event.get("event_id") or "")
            summary.last_failure_type = event_type
            summary.last_failure_command = command
        if event_type in {
            "command_preflight_blocked",
            "stage_audit",
            "setup_blocked",
            "run_failed",
        }:
            reason = event.get("reason") or event.get("error")
            result = event.get("result")
            if not reason and isinstance(result, dict):
                blockers = result.get("blockers")
                if isinstance(blockers, list):
                    reason = "; ".join(str(item) for item in blockers)
            if reason:
                summary.blockers.append(str(reason))
                summary.last_failure_event_id = str(event.get("event_id") or "")
                summary.last_failure_type = event_type
                summary.last_failure_command = command
        if event_type == "final_audit":
            result = event.get("result")
            if isinstance(result, dict) and result.get("passed") is False:
                blockers = result.get("blockers")
                if isinstance(blockers, list):
                    for blocker in blockers:
                        summary.blockers.append(str(blocker))
                summary.last_failure_event_id = str(event.get("event_id") or "")
                summary.last_failure_type = event_type
                summary.last_failure_command = command
        if event_type in {"run_finished", "setup_run_finished", "final_audit"}:
            summary.final_status = str(event.get("status") or event.get("summary") or event_type)
        previous_event = event
    if not summary.run_id and events:
        summary.run_id = str(events[0].get("run_id") or "")
    return summary


def summarize_runlog(path: Path | str) -> RunReplaySummary:
    return summarize_events(load_run_events(path))


def _event_rc(event: dict[str, Any]) -> int:
    try:
        return int(event.get("rc"))
    except (TypeError, ValueError):
        return 0


def _event_text(event: dict[str, Any]) -> str:
    return " ".join(
        str(event.get(key) or "")
        for key in ("command", "visible_command", "stdout_tail", "stderr_tail", "reason", "error")
    )


def _diagnostic(
    event: dict[str, Any],
    *,
    category: str,
    severity: str,
    reason: str,
    suggested_next: str,
) -> ReplayDiagnostic:
    return ReplayDiagnostic(
        event_id=str(event.get("event_id") or ""),
        event_type=str(event.get("event_type") or ""),
        stage=str(event.get("stage") or ""),
        group_id=str(event.get("group_id") or ""),
        category=category,
        severity=severity,
        command=str(event.get("command") or event.get("visible_command") or ""),
        reason=reason,
        suggested_next=suggested_next,
    )


def _looks_like_driver_runtime_probe(command: str) -> bool:
    lowered = command.lower()
    return any(
        token in lowered
        for token in (
            "nvidia-smi",
            "lsmod",
            "modinfo nvidia",
            "/sys/module/nvidia",
            "nvidia_drm",
            "amdgpu",
            "i915",
            "nouveau",
            "vulkaninfo",
        )
    )


def _classification_category(event: dict[str, Any], *, rc: int) -> str:
    classification = event.get("failure_classification")
    if isinstance(classification, dict):
        return str(classification.get("category") or "unknown")
    return classify_failure(
        command=str(event.get("command") or event.get("visible_command") or ""),
        rc=rc,
        stdout=str(event.get("stdout_tail") or ""),
        stderr=str(event.get("stderr_tail") or ""),
        detail=str(event.get("reason") or event.get("error") or event.get("result") or ""),
    ).category.value


def _looks_like_standalone_assignment(command: str) -> str:
    match = re.match(
        r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(?:\$\(.*\)|`.*`|[^;&|]+)\s*(?:#.*)?$",
        command,
        flags=re.DOTALL,
    )
    return match.group(1) if match else ""


def diagnose_event(
    event: dict[str, Any],
    *,
    previous_event: dict[str, Any] | None = None,
) -> ReplayDiagnostic | None:
    """Classify runlog failures that should become fast local regressions."""

    event_type = str(event.get("event_type") or "")
    command = str(event.get("command") or event.get("visible_command") or "")
    lowered = _event_text(event).lower()
    stage = str(event.get("stage") or "")
    rc = _event_rc(event)

    if event_type == "command_preflight_decision":
        decisions = event.get("decisions")
        if isinstance(decisions, list):
            for decision in decisions:
                if not isinstance(decision, dict):
                    continue
                kind = str(decision.get("kind") or "")
                if kind not in {"block", "shadow"}:
                    continue
                category = str(decision.get("category") or "preflight")
                reason = str(decision.get("reason") or "Preflight decision recorded.")
                return _diagnostic(
                    event,
                    category=category,
                    severity="harness_fixable" if kind == "shadow" else "blocked",
                    reason=reason,
                    suggested_next=str(
                        decision.get("suggested_next")
                        or "Review whether this should be advisory, shadow, or a real safety block."
                    ),
                )

    if event_type == "verification" and rc != 0:
        if (
            ("not a mountpoint" in lowered and "/mnt/boot/efi" in lowered)
            or ("mountpoint" in lowered and "/mnt/boot/efi" in lowered)
            or ("device or resource busy" in lowered and "/mnt/boot/efi" in lowered)
        ):
            return _diagnostic(
                event,
                category="efi_mount_layout_conflict",
                severity="harness_fixable",
                reason=(
                    "The verification expected an EFI mountpoint that was not present, usually "
                    "after mixing /boot and /boot/efi layouts across attempts."
                ),
                suggested_next=(
                    "Use one family-specific EFI mount layout and verify it with findmnt before "
                    "bootloader work."
                ),
            )
        if "lsblk:" in lowered and "not a block device" in lowered:
            return _diagnostic(
                event,
                category="invalid_lsblk_mount_verification",
                severity="harness_fixable",
                reason="The verification passed a mountpoint path to lsblk as if it were a block device.",
                suggested_next=(
                    "Use `findmnt --target <mountpoint>` or `mountpoint -q` for mount checks; "
                    "use lsblk only on device paths."
                ),
            )
        if "/mnt/etc/fstab" in lowered and (
            "grep" in lowered or "awk" in lowered or "findmnt" in lowered
        ):
            return _diagnostic(
                event,
                category="fragile_fstab_verification",
                severity="harness_fixable",
                reason=(
                    "The verification is checking generated fstab text with a brittle "
                    "pipeline instead of asking whether an uncommented root mount exists."
                ),
                suggested_next=(
                    "Normalize install-root fstab probes to an awk check for an "
                    'uncommented `$2 == "/"` entry.'
                ),
            )
        if stage == "install_or_skip" and _looks_like_driver_runtime_probe(command):
            return _diagnostic(
                event,
                category="preboot_driver_runtime_probe",
                severity="harness_fixable",
                reason=(
                    "The install stage verified a runtime GPU module/tool before the "
                    "installed system had rebooted into its own kernel."
                ),
                suggested_next=(
                    "Defer runtime driver probes to postboot_driver/final_audit; keep "
                    "install_or_skip checks limited to packages, initramfs, bootloader, SSH."
                ),
            )
        if any(path in lowered for path in ("/usr/share/themes", "/usr/share/icons")):
            return _diagnostic(
                event,
                category="desktop_asset_path_probe",
                severity="repairable",
                reason=(
                    "The verification assumes an exact theme/icon asset path. Desktop packages "
                    "often install different directory names or only provide config fragments."
                ),
                suggested_next=(
                    "Verify the persisted desktop design contract or search installed theme/icon "
                    "directories case-insensitively instead of failing on one hardcoded path."
                ),
            )
        if "kreadconfig" in lowered and "unknown option" in lowered:
            return _diagnostic(
                event,
                category="quoted_shell_pipeline",
                severity="harness_fixable",
                reason=(
                    "A shell pipeline/operator appears to have been quoted as an argument, so "
                    "the verifier executed the wrong command."
                ),
                suggested_next=(
                    "Generate verification commands as shell snippets with operators outside "
                    "quoted arguments, or split the read and grep into separate commands."
                ),
            )
        if "parted" in lowered and "print" in lowered and "grep" in lowered:
            return _diagnostic(
                event,
                category="partition_layout_verification_failed",
                severity="repairable",
                reason="The partition layout verification did not find the expected partition signature.",
                suggested_next=(
                    "Refresh lsblk/blkid/parted facts for the selected disk and repair the "
                    "deterministic partition layout, rather than changing disks."
                ),
            )
        if "/mnt/boot/grub" in lowered and ("test -f" in lowered or "[ -f" in lowered):
            return _diagnostic(
                event,
                category="bootloader_artifact_missing",
                severity="harness_fixable",
                reason="The expected bootloader config artifact was missing in the target root.",
                suggested_next=(
                    "Run the family adapter bootloader hook with bind mounts and then verify "
                    "all acceptable bootloader artifact paths for that family."
                ),
            )
        if "systemctl" in lowered and ("inactive" in lowered or "disabled" in lowered):
            return _diagnostic(
                event,
                category="service_not_active_or_enabled",
                severity="repairable",
                reason="A service verification found the service inactive or disabled.",
                suggested_next=(
                    "Classify whether the service is required, optional, or VM-only; enable "
                    "required services and move optional/VM-only services to manual follow-up."
                ),
            )

    if event_type == "command" and rc != 0:
        category = _classification_category(event, rc=rc)
        if category == "package_missing":
            return _diagnostic(
                event,
                category="package_missing",
                severity="repairable",
                reason="The package manager rejected at least one requested package name.",
                suggested_next=(
                    "Treat this as a model/package-resolution repair, not a harness block; "
                    "drop or replace the missing target and continue required base setup."
                ),
            )
        if category == "package_conflict":
            return _diagnostic(
                event,
                category="package_conflict",
                severity="repairable",
                reason="The package manager reported conflicting packages or files.",
                suggested_next=(
                    "Isolate the conflicting package, keep already-installed packages, and move "
                    "non-required conflicting apps to manual_followup instead of retrying the "
                    "whole group."
                ),
            )
        if (
            "already mounted" in lowered
            or "not a mountpoint" in lowered
            or ("mountpoint" in lowered and "/mnt/boot/efi" in lowered)
            or ("device or resource busy" in lowered and "/mnt/boot/efi" in lowered)
        ):
            if "/mnt/boot/efi" in lowered or "/mnt/boot" in lowered:
                return _diagnostic(
                    event,
                    category="efi_mount_layout_conflict",
                    severity="harness_fixable",
                    reason=(
                        "The install flow reused or nested the EFI mountpoint incorrectly, "
                        "usually by mixing /boot and /boot/efi layouts across repair attempts."
                    ),
                    suggested_next=(
                        "Unmount the selected target tree recursively, recreate one family-specific "
                        "EFI mount layout, and verify it with findmnt before bootloader work."
                    ),
                )
            return _diagnostic(
                event,
                category="target_mount_state_conflict",
                severity="repairable",
                reason=(
                    "The target root or partition was already mounted from a previous attempt."
                ),
                suggested_next=(
                    "Refresh mount facts, unmount only the selected target tree/partitions, "
                    "then retry the same deterministic mount layout."
                ),
            )
        if "connection closed by" in lowered:
            return _diagnostic(
                event,
                category="ssh_connection_dropped",
                severity="workflow",
                reason="The SSH connection closed during a command.",
                suggested_next=(
                    "Refresh facts after reconnect and do not infer command success from the "
                    "interrupted SSH session."
                ),
            )
        if "partition(s)" in lowered and "being used" in lowered:
            return _diagnostic(
                event,
                category="target_disk_busy",
                severity="repairable",
                reason="The selected disk still had mounted/in-use partitions.",
                suggested_next=(
                    "Run a target-disk mount/holder cleanup for the selected disk before "
                    "partitioning; do not guess a different disk."
                ),
            )
        if "could not stat device" in lowered and "no such file or directory" in lowered:
            return _diagnostic(
                event,
                category="empty_or_invalid_target_disk",
                severity="harness_fixable",
                reason="The generated disk-selection shell produced an empty or invalid device path.",
                suggested_next=(
                    "Use normalized SetupFacts target-disk selection or ask the user; never let "
                    "a blank DISK variable reach partitioning commands."
                ),
            )
        if category == "bootloader" and any(
            needle in lowered
            for needle in (
                "failed to get canonical path of `airootfs'",
                "failed to get canonical path of 'airootfs'",
                "cannot find a device for /boot",
                "is /dev mounted",
                "guessing the root device failed",
            )
        ):
            return _diagnostic(
                event,
                category="bootloader_chroot_bind_mounts",
                severity="harness_fixable",
                reason=(
                    "Bootloader installation ran inside a target root without the required "
                    "/dev, /proc, /sys, or /run bind mounts/device context."
                ),
                suggested_next=(
                    "Use the deterministic family adapter bootloader hook: bind mount live "
                    "pseudo-filesystems before grub-install/grub2-install and verify fallback "
                    "EFI removable boot files."
                ),
            )
        if "cannot open /etc/passwd" in lowered or "invalid user:" in lowered:
            return _diagnostic(
                event,
                category="chroot_before_base_or_user",
                severity="harness_fixable",
                reason=(
                    "A chroot/user operation ran before the target base system or target user "
                    "existed."
                ),
                suggested_next=(
                    "Gate user/password/SSH ownership commands on a successful base install and "
                    "`test -f /mnt/etc/passwd`; create the user inside the target before chown."
                ),
            )
        if "chroot:" in lowered and "operation not permitted" in lowered:
            return _diagnostic(
                event,
                category="chroot_privilege_or_target_state",
                severity="harness_fixable",
                reason="A chroot command failed because privileges or target-root state were invalid.",
                suggested_next=(
                    "Run chroot operations through the family adapter with root privileges, "
                    "bind mounts, and a verified target root."
                ),
            )
        if "printf:" in lowered and "invalid format character" in lowered:
            return _diagnostic(
                event,
                category="shell_printf_format_bug",
                severity="harness_fixable",
                reason=(
                    "A generated installer/config file used shell printf with unescaped percent "
                    "characters from the payload."
                ),
                suggested_next=(
                    "Generate multi-line installer files with a quoted heredoc (`cat <<'EOF'`) "
                    "or `printf '%s\\n'` per literal line, never raw printf format strings."
                ),
            )
        if "mkfs.fat" in lowered and "command not found" in lowered:
            return _diagnostic(
                event,
                category="missing_filesystem_tool",
                severity="harness_fixable",
                reason="The live environment does not have the filesystem formatter required by the plan.",
                suggested_next=(
                    "Install or include the family-specific formatter package before formatting "
                    "(for example dosfstools for FAT/EFI), or choose an available filesystem."
                ),
            )
        if "flatpak system operation" in lowered and "not allowed for user" in lowered:
            return _diagnostic(
                event,
                category="flatpak_scope_permission",
                severity="repairable",
                reason="A system-scope Flatpak command was attempted without sufficient privileges.",
                suggested_next=(
                    "Use `sudo flatpak` for system installs or `flatpak --user` with user remotes, "
                    "then verify the same scope."
                ),
            )
        if "couldn't open temporary file" in lowered and "permission denied" in lowered:
            return _diagnostic(
                event,
                category="missing_privilege_for_system_edit",
                severity="repairable",
                reason="A system config edit was attempted without write privileges.",
                suggested_next=(
                    "Use sudo/root/chroot deliberately for root-owned files, or bootstrap "
                    "noninteractive sudo before running system edits."
                ),
            )
        if "are the same file" in lowered and "/etc/resolv.conf" in lowered:
            return _diagnostic(
                event,
                category="resolv_conf_bind_mount_noop",
                severity="repairable",
                reason="The target resolv.conf copy was a no-op because source and target resolved to the same file.",
                suggested_next=(
                    "Refresh target mount facts and only copy resolv.conf when source and target "
                    "are distinct regular files."
                ),
            )
        if "add-apt-repository" in lowered and "command not found" in lowered:
            return _diagnostic(
                event,
                category="missing_repository_tool",
                severity="repairable",
                reason="The requested repository helper is not installed in the target environment.",
                suggested_next=(
                    "Install the helper package first or write the repository/source file "
                    "directly through the family adapter."
                ),
            )
        if (
            "anaconda" in lowered
            or "liveinst" in lowered
            or "ubuntu-desktop-bootstrap" in lowered
            or "ubiquity" in lowered
            or "subiquity" in lowered
        ) and (
            category in {"display_session", "timeout"}
            or "dbus" in lowered
            or "timed out" in lowered
            or "connection refused" in lowered
        ):
            return _diagnostic(
                event,
                category="interactive_live_installer_over_ssh",
                severity="harness_fixable",
                reason=(
                    "A graphical/live installer path was attempted over SSH and failed or hung "
                    "on GUI/DBus/session assumptions."
                ),
                suggested_next=(
                    "Use deterministic family install spines for live installs. Treat graphical "
                    "installer flows as manual install mode, not automated SSH setup."
                ),
            )
        if "debconf" in lowered and (
            "unable to initialize frontend" in lowered or "falling back to frontend" in lowered
        ):
            return _diagnostic(
                event,
                category="apt_debconf_interactive",
                severity="harness_fixable",
                reason=(
                    "An apt/dpkg command reached an interactive debconf frontend in a non-TTY "
                    "SSH setup run."
                ),
                suggested_next=(
                    "Normalize apt/dpkg commands with `DEBIAN_FRONTEND=noninteractive`, dpkg "
                    "options, package lock handling, and explicit repair of interrupted dpkg."
                ),
            )
        if "resource temporarily unavailable" in lowered and (
            "dpkg" in lowered or "apt" in lowered
        ):
            return _diagnostic(
                event,
                category="package_database_lock",
                severity="repairable",
                reason="The package database was locked or another dpkg/apt process was active.",
                suggested_next=(
                    "Wait for package-manager locks or stop a specific stale service/PID; never "
                    "use broad pkill patterns against package managers."
                ),
            )
        if "dpkg" in lowered and (
            "not configured yet" in lowered or "dependency problems" in lowered
        ):
            return _diagnostic(
                event,
                category="apt_interrupted_dpkg_state",
                severity="repairable",
                reason="dpkg/apt is in an interrupted or partially configured state.",
                suggested_next=(
                    "Run noninteractive `dpkg --configure -a` and `apt-get -f install` with "
                    "lock handling before attempting new package groups."
                ),
            )
        if "package" in lowered and "was not found" in lowered:
            return _diagnostic(
                event,
                category="package_missing",
                severity="repairable",
                reason="A package query or install referenced a package that is not installed or available.",
                suggested_next=(
                    "Resolve the correct package identifier or treat the app/feature as "
                    "manual_followup; do not keep retrying the same package name."
                ),
            )
        if "release signed by unknown key" in lowered:
            return _diagnostic(
                event,
                category="bootstrap_keyring_missing",
                severity="harness_fixable",
                reason="The bootstrap source requires keyring/trust setup before debootstrap can verify it.",
                suggested_next=(
                    "Install/use the family keyring or explicitly select a trusted bootstrap "
                    "source; only use no-check-gpg as a deliberate fallback."
                ),
            )
        if "opening repository /var/lib/flatpak/repo" in lowered:
            return _diagnostic(
                event,
                category="flatpak_repo_not_initialized",
                severity="repairable",
                reason="Flatpak verification queried a system repo that is not initialized.",
                suggested_next=(
                    "Initialize the same Flatpak scope used for install, or verify user-scope "
                    "Flatpaks with `flatpak --user list`."
                ),
            )
        if "lsblk:" in lowered and ("unknown column" in lowered or "unknown columns" in lowered):
            return _diagnostic(
                event,
                category="stale_lsblk_column",
                severity="harness_fixable",
                reason="The command used an lsblk column name unavailable on the target ISO.",
                suggested_next=(
                    "Prefer stable `lsblk -dnpo NAME,TYPE,SIZE,RM,ROTA` probes and tolerate "
                    "missing optional columns."
                ),
            )
        if "lsblk:" in lowered and "not a block device" in lowered:
            return _diagnostic(
                event,
                category="invalid_lsblk_mount_verification",
                severity="harness_fixable",
                reason="The verification passed a mountpoint path to lsblk as if it were a block device.",
                suggested_next=(
                    "Use `findmnt --target <mountpoint>` or `mountpoint -q` for mount checks; "
                    "use lsblk only on device paths."
                ),
            )
        if previous_event is not None:
            previous_command = str(
                previous_event.get("command") or previous_event.get("visible_command") or ""
            )
            variable = _looks_like_standalone_assignment(previous_command)
            if variable and re.search(rf"\${{{variable}}}|\${variable}\b", command):
                return _diagnostic(
                    event,
                    category="stateful_shell_prelude",
                    severity="harness_fixable",
                    reason=(
                        "A variable was assigned in one SSH command and consumed in the next; "
                        "separate SSH executions do not preserve shell state."
                    ),
                    suggested_next=(
                        "Combine standalone assignment preludes with the next command that "
                        "uses the variable."
                    ),
                )

    if event_type == "blocking_question_timeout":
        return _diagnostic(
            event,
            category="blocking_question_timeout",
            severity="workflow",
            reason="The run waited for a user answer and timed out.",
            suggested_next=(
                "Make timeout behavior visible in the UI and ensure optional/noncritical "
                "questions default to manual follow-up instead of stopping core setup."
            ),
        )

    if event_type == "repair_budget_gate_answer" and event.get("answer") in {None, ""}:
        return _diagnostic(
            event,
            category="repair_budget_gate_no_answer",
            severity="workflow",
            reason="Repair budget was exhausted and the extension question had no answer.",
            suggested_next=(
                "Replay the preceding failed command/verification locally and decide whether "
                "the gate was valid or a harness false positive."
            ),
        )

    return None


def scan_runlogs(
    root: Path | str | None = None,
    *,
    limit: int = 50,
) -> RunlogScanSummary:
    target_root = Path(root) if root else Path.home() / ".quickhatch" / "setup-runs"
    result = RunlogScanSummary(root=str(target_root))
    if not target_root.exists():
        return result
    paths = sorted(
        (path for path in target_root.rglob("*.jsonl") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in paths:
        try:
            summary = summarize_events(load_run_events_tolerant(path))
        except ValueError:
            continue
        if (
            summary.command_count == 0
            and summary.failed_verification_count == 0
            and not summary.blockers
            and summary.event_count < 20
        ):
            continue
        result.run_count += 1
        result.event_count += summary.event_count
        for category, count in summary.failure_categories.items():
            result.failure_categories[category] = result.failure_categories.get(category, 0) + count
        for diagnostic in summary.diagnostics:
            result.diagnostic_categories[diagnostic.category] = (
                result.diagnostic_categories.get(diagnostic.category, 0) + 1
            )
        if len(result.runs) < limit:
            result.runs.append(
                {
                    "path": str(path),
                    "run_id": summary.run_id,
                    "event_count": summary.event_count,
                    "command_count": summary.command_count,
                    "failed_command_count": summary.failed_command_count,
                    "failed_verification_count": summary.failed_verification_count,
                    "blocking_question_timeout_count": summary.blocking_question_timeout_count,
                    "last_failure_event_id": summary.last_failure_event_id,
                    "last_failure_type": summary.last_failure_type,
                    "last_failure_command": summary.last_failure_command,
                    "diagnostics": [asdict(item) for item in summary.diagnostics[:10]],
                    "blockers": summary.blockers[:10],
                }
            )
    return result


def find_latest_runlog(
    root: Path | str | None = None,
    *,
    prefer_real_runs: bool = True,
) -> Path | None:
    target_root = Path(root) if root else Path.home() / ".quickhatch" / "setup-runs"
    if not target_root.exists():
        return None
    candidates = [path for path in target_root.rglob("*.jsonl") if path.is_file()]
    if not candidates:
        return None
    if not prefer_real_runs:
        return max(candidates, key=lambda path: path.stat().st_mtime)

    scored: list[tuple[int, float, Path]] = []
    for path in candidates:
        events = load_run_events_tolerant(path)
        summary = summarize_events(events)
        event_types = {str(event.get("event_type") or "") for event in events}
        real_run_score = 0
        if {"run_started", "connect_and_probe", "planner_prompt"} & event_types:
            real_run_score += 4
        if summary.command_count or summary.failed_verification_count:
            real_run_score += 2
        if summary.event_count >= 20:
            real_run_score += 1
        scored.append((real_run_score, path.stat().st_mtime, path))
    if not scored:
        return max(candidates, key=lambda path: path.stat().st_mtime)
    return max(scored, key=lambda item: (item[0], item[1]))[2]


def stage_events(
    path: Path | str,
    *,
    stage: str,
    group_id: str | None = None,
) -> list[dict[str, Any]]:
    events = load_run_events(path)
    selected: list[dict[str, Any]] = []
    for event in events:
        if str(event.get("stage") or "") != stage:
            continue
        if group_id and str(event.get("group_id") or "") != group_id:
            continue
        selected.append(event)
    return selected


def summarize_stage(path: Path | str, *, stage: str, group_id: str | None = None) -> dict[str, Any]:
    selected = stage_events(path, stage=stage, group_id=group_id)
    return {
        "stage": stage,
        "group_id": group_id or "",
        "events": len(selected),
        "summary": asdict(summarize_events(selected)),
        "last_events": selected[-5:],
    }


def find_event(path: Path | str, event_id: str) -> dict[str, Any]:
    for event in load_run_events(path):
        if str(event.get("event_id") or "") == event_id:
            return event
    raise KeyError(f"event id not found: {event_id}")


def _safe_test_name(name: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_]+", "_", name.strip())
    text = re.sub(r"_+", "_", text).strip("_").lower()
    if not text:
        text = "setup_regression"
    if text[0].isdigit():
        text = f"regression_{text}"
    return text


def build_regression_fixture(path: Path | str, *, event_id: str, name: str) -> str:
    event = find_event(path, event_id)
    test_name = _safe_test_name(name)
    command = str(event.get("command") or event.get("visible_command") or "")
    try:
        rc = int(event.get("rc"))
    except (TypeError, ValueError):
        rc = 1
    stdout = str(event.get("stdout_tail") or "")
    stderr = str(event.get("stderr_tail") or "")
    classification = classify_failure(command=command, rc=rc, stdout=stdout, stderr=stderr)
    payload = {
        "event": event,
        "expected_classifier": classification.category.value,
    }
    return (
        "from __future__ import annotations\n\n"
        "from quickhatch.setup.failure import classify_failure\n\n\n"
        f"def test_{test_name}():\n"
        f"    payload = {payload!r}\n"
        "    event = payload['event']\n"
        "    result = classify_failure(\n"
        "        command=str(event.get('command') or ''),\n"
        "        rc=int(event.get('rc') or 1),\n"
        "        stdout=str(event.get('stdout_tail') or ''),\n"
        "        stderr=str(event.get('stderr_tail') or ''),\n"
        "    )\n"
        "    assert result.category.value == payload['expected_classifier']\n"
    )


def promote_regression_fixture(
    path: Path | str,
    *,
    event_id: str,
    name: str,
    output_dir: Path | str = "tests/generated",
) -> Path:
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"test_{_safe_test_name(name)}.py"
    target.write_text(
        build_regression_fixture(path, event_id=event_id, name=name), encoding="utf-8"
    )
    return target
