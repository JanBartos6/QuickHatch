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
    last_failure_event_id: str = ""
    last_failure_type: str = ""
    last_failure_command: str = ""
    diagnostics: list[ReplayDiagnostic] = field(default_factory=list)
    final_status: str = "unknown"


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
                event_failed = True
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

        diagnostic = diagnose_event(event, previous_event=previous_event)
        if diagnostic is not None:
            summary.diagnostics.append(diagnostic)
            if event_failed and diagnostic.category != counted_category:
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

    if event_type == "verification" and rc != 0:
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

    if event_type == "command" and rc != 0:
        classification = classify_failure(
            command=command,
            rc=rc,
            stdout=str(event.get("stdout_tail") or ""),
            stderr=str(event.get("stderr_tail") or ""),
        )
        if classification.category.value == "package_missing":
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


def find_latest_runlog(
    root: Path | str | None = None,
    *,
    prefer_real_runs: bool = True,
) -> Path | None:
    target_root = Path(root) if root else Path.home() / ".quickhatch" / "setup-runs"
    if not target_root.exists():
        return None
    candidates = [path for path in target_root.glob("*.jsonl") if path.is_file()]
    if not candidates:
        return None
    if not prefer_real_runs:
        return max(candidates, key=lambda path: path.stat().st_mtime)

    scored: list[tuple[int, float, Path]] = []
    for path in candidates:
        try:
            events = load_run_events(path)
        except ValueError:
            continue
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
