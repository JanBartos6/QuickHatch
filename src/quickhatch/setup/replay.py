"""Replay/debug helpers for setup run JSONL artifacts."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .failure import classify_failure


@dataclass
class RunReplaySummary:
    run_id: str = ""
    event_count: int = 0
    stages: list[str] = field(default_factory=list)
    command_count: int = 0
    failed_command_count: int = 0
    blockers: list[str] = field(default_factory=list)
    failure_categories: dict[str, int] = field(default_factory=dict)
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
    for event in events:
        summary.run_id = summary.run_id or str(event.get("run_id") or "")
        stage = str(event.get("stage") or "")
        if stage and stage not in seen_stages:
            seen_stages.add(stage)
            summary.stages.append(stage)
        event_type = str(event.get("event_type") or "")
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
                summary.failure_categories[category] = (
                    summary.failure_categories.get(category, 0) + 1
                )
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
        if event_type in {"run_finished", "setup_run_finished", "final_audit"}:
            summary.final_status = str(event.get("status") or event.get("summary") or event_type)
    if not summary.run_id and events:
        summary.run_id = str(events[0].get("run_id") or "")
    return summary


def summarize_runlog(path: Path | str) -> RunReplaySummary:
    return summarize_events(load_run_events(path))


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
