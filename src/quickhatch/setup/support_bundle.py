"""Redacted support bundle export for setup diagnostics."""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from quickhatch import __version__
from quickhatch.analysis.app_catalog import validate_runtime_catalog

from .runlog import build_executed_commands_script, redact_sensitive_text


@dataclass
class SupportBundleResult:
    path: str
    status: str = "ok"
    included: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    selected_runlog: str = ""


_FACT_SUMMARY_KEYS = {
    "distro_family",
    "os_id",
    "os_like",
    "package_manager",
    "init_system",
    "current_user",
    "login_user",
    "is_live",
    "is_root",
    "sudo_nopasswd",
    "community_helper",
    "gpu_vendors",
    "gpu_inventory",
    "kernel_release",
    "session_type",
    "desktop_runtime_state",
    "display_manager_active",
    "display_manager_enabled",
    "nvidia_smi_state",
    "nvidia_kernel_driver",
    "nvidia_nouveau_bound",
    "secure_boot_state",
    "fact_probe_ok",
}
_PROFILE_PREFERENCE_KEYS = {
    "migration_type",
    "distro_preference",
    "desktop_look",
    "icon_style",
    "wallpaper_style",
    "another_machine",
    "data_transfer_method",
    "hardware_search_opt_in",
}
_APP_VERIFICATION_KEYS = {
    "app_verification_signals",
    "app_verification_manual_followups",
    "verified_installed_apps",
    "verified_alternative_apps",
    "unverified_installed_apps",
    "installed_flatpaks",
    "installed_snaps",
    "installed_binaries",
    "installed_desktop_entries",
}
_FINAL_STATUS_VALUES = {
    "success",
    "success_with_warnings",
    "manual_followup_required",
    "unsupported_safe",
    "blocked",
}


def export_support_bundle(
    runlog_path: Path | str | None = None,
    *,
    root: Path | str | None = None,
    output_dir: Path | str | None = None,
    out: Path | str | None = None,
) -> SupportBundleResult:
    from .replay import find_latest_runlog, load_run_events_tolerant, scan_runlogs, summarize_events

    scan_root = Path(root) if root else None
    selected_runlog = Path(runlog_path) if runlog_path else find_latest_runlog(scan_root)
    target = _resolve_output_path(
        out=out,
        output_dir=output_dir,
        runlog=selected_runlog,
        root=scan_root,
    )
    target.parent.mkdir(parents=True, exist_ok=True)

    included: list[str] = []
    skipped: dict[str, str] = {}
    entries: dict[str, str | bytes] = {}
    events: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}

    if selected_runlog is None:
        skipped["setup_runlog"] = "No setup runlog found."
    elif not selected_runlog.exists() or not selected_runlog.is_file():
        skipped["setup_runlog"] = f"Runlog not found: {selected_runlog}"
    else:
        events = load_run_events_tolerant(selected_runlog)
        summary = asdict(summarize_events(events))
        _add_text(
            entries,
            included,
            "setup-runlog-redacted.jsonl",
            _read_redacted_text(selected_runlog),
        )
        _add_text(
            entries,
            included,
            "executed-commands.sh",
            build_executed_commands_script(
                selected_runlog,
                status=str(summary.get("final_status") or "unknown"),
            ),
        )

    if summary:
        _add_json(entries, included, "replay-summary.json", summary)
    else:
        skipped["replay_summary"] = "No runlog events available."

    if scan_root and scan_root.exists():
        _add_json(entries, included, "runlog-scan-summary.json", asdict(scan_runlogs(scan_root)))
    elif selected_runlog and selected_runlog.exists():
        _add_json(
            entries,
            included,
            "runlog-scan-summary.json",
            asdict(scan_runlogs(selected_runlog.parent)),
        )
    else:
        skipped["runlog_scan_summary"] = "No existing runlog root available."

    diagnostics = _build_diagnostics(events, summary)
    _add_json(entries, included, "support-summary.json", diagnostics["support_summary"])
    _add_optional_json(entries, included, skipped, "final-audit.json", diagnostics["final_audit"])
    _add_optional_json(
        entries,
        included,
        skipped,
        "setup-facts-summary.json",
        diagnostics["setup_facts_summary"],
    )
    _add_optional_json(entries, included, skipped, "profile-summary.json", diagnostics["profile"])
    _add_collection_json(
        entries,
        included,
        skipped,
        "capability-events.json",
        diagnostics["capability_events"],
    )
    _add_collection_json(
        entries,
        included,
        skipped,
        "preflight-events.json",
        diagnostics["preflight_events"],
    )
    _add_collection_json(
        entries,
        included,
        skipped,
        "dangerous-operation-previews.json",
        diagnostics["dangerous_operation_previews"],
    )
    _add_optional_json(
        entries,
        included,
        skipped,
        "app-verification.json",
        diagnostics["app_verification"],
    )
    _add_collection_json(
        entries,
        included,
        skipped,
        "manual-followups.json",
        diagnostics["manual_followups"],
    )
    _add_optional_json(
        entries,
        included,
        skipped,
        "certification-summary.json",
        _certification_summary(events, selected_runlog),
    )
    try:
        _add_json(entries, included, "catalog-health-summary.json", validate_runtime_catalog())
    except Exception as exc:
        skipped["catalog_health_summary"] = f"catalog validation failed: {exc}"
    skipped["harness_health_summary"] = (
        "Not collected automatically; run `quickhatch debug harness-health` separately if needed."
    )
    _add_json(entries, included, "support-metadata.json", environment_metadata())
    _add_text(entries, included, "README.txt", _readme_text())

    created_at = int(time.time())
    final_included = sorted({*included, "manifest.json"})
    manifest = {
        "schema": "quickhatch-support-bundle-v1",
        "created_at": created_at,
        "quickhatch_version": __version__,
        "selected_runlog": str(selected_runlog or ""),
        "scan_root": str(scan_root or ""),
        "included": final_included,
        "skipped": skipped,
        "privacy": (
            "Credentials, passwords, bearer tokens, private/public SSH key material, and known "
            "secret fields are redacted. The bundle does not collect checkpoints, SSH key files, "
            "or migration export ZIPs."
        ),
    }
    entries["manifest.json"] = _redacted_json(manifest)

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in sorted(entries.items()):
            zf.writestr(name, content)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return SupportBundleResult(
        path=str(target),
        included=sorted({*included, "manifest.json"}),
        skipped=skipped,
        selected_runlog=str(selected_runlog or ""),
    )


def _resolve_output_path(
    *,
    out: Path | str | None,
    output_dir: Path | str | None,
    runlog: Path | None,
    root: Path | None,
) -> Path:
    base_name = _bundle_name(_bundle_run_id(runlog), int(time.time()))
    if out:
        path = Path(out)
        return path if path.suffix.lower() == ".zip" else _dedupe_path(path / base_name)
    if output_dir:
        return _dedupe_path(Path(output_dir) / base_name)
    if runlog:
        return _dedupe_path(runlog.parent / base_name)
    if root:
        return _dedupe_path(root / base_name)
    return _dedupe_path(Path.cwd() / base_name)


def _bundle_run_id(runlog: Path | None) -> str:
    if runlog is None:
        return "no-runlog"
    parent = _slug(runlog.parent.name)
    stem = _slug(runlog.stem)
    return f"{parent}-{stem}" if parent else stem


def _dedupe_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for idx in range(2, 1000):
        candidate = path.with_name(f"{stem}-{idx}{suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Could not choose a unique support bundle path near {path}")


def _bundle_name(run_id: str, created_at: int) -> str:
    return f"quickhatch-support-{_slug(run_id)}-{created_at}.zip"


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip(".-")
    return slug or "run"


def _redacted_json(payload: Any) -> str:
    return redact_sensitive_text(json.dumps(payload, indent=2, sort_keys=True, default=str)) + "\n"


def _read_redacted_text(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    text = redact_sensitive_text(text)
    return text if not text or text.endswith("\n") else text + "\n"


def _add_text(
    entries: dict[str, str | bytes],
    included: list[str],
    name: str,
    text: str,
) -> None:
    entries[name] = redact_sensitive_text(text)
    included.append(name)


def _add_json(
    entries: dict[str, str | bytes],
    included: list[str],
    name: str,
    payload: Any,
) -> None:
    entries[name] = _redacted_json(payload)
    included.append(name)


def _add_optional_json(
    entries: dict[str, str | bytes],
    included: list[str],
    skipped: dict[str, str],
    name: str,
    payload: Any,
) -> None:
    if payload:
        _add_json(entries, included, name, payload)
    else:
        skipped[name.removesuffix(".json").replace("-", "_")] = "No matching data found."


def _add_collection_json(
    entries: dict[str, str | bytes],
    included: list[str],
    skipped: dict[str, str],
    name: str,
    payload: list[Any],
) -> None:
    if payload:
        _add_json(entries, included, name, payload)
    else:
        skipped[name.removesuffix(".json").replace("-", "_")] = "No matching events found."


def _build_diagnostics(events: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
    final_event = _latest_event(events, "final_audit")
    final_result = final_event.get("result") if isinstance(final_event.get("result"), dict) else {}
    final_details = (
        final_result.get("details") if isinstance(final_result.get("details"), dict) else {}
    )
    final_findings = (
        final_result.get("findings") if isinstance(final_result.get("findings"), list) else []
    )
    facts_summary = _facts_summary(events)
    profile = _profile_summary(events)
    app_verification = _app_verification_payload(final_details)
    manual_followups = _manual_followups(final_findings, final_details, events)
    final_status = _normalize_final_status(
        final_result.get("final_status")
        or final_event.get("status")
        or (
            summary.get("final_status")
            if str(summary.get("final_status") or "") != "final_audit"
            else ""
        )
        or "unknown"
    )
    return {
        "support_summary": {
            "run_id": summary.get("run_id") or "",
            "final_status": final_status,
            "failed_command_count": summary.get("failed_command_count", 0),
            "failed_verification_count": summary.get("failed_verification_count", 0),
            "blocking_question_timeout_count": summary.get("blocking_question_timeout_count", 0),
            "install_profile": profile.get("install_profile", ""),
            "remote_setup_mode": profile.get("remote_setup_mode", ""),
            "physical_setup_mode": profile.get("physical_setup_mode", ""),
            "install_encryption": profile.get("install_encryption", ""),
            "install_filesystem": profile.get("install_filesystem", ""),
            "manual_followup_count": len(manual_followups),
            "audit_finding_count": len(final_findings),
        },
        "final_audit": _clean_payload(
            {
                "status": final_status,
                "passed": final_result.get("passed"),
                "blockers": final_result.get("blockers", []),
                "findings": final_findings,
                "details": final_details,
            }
        )
        if final_event
        else {},
        "setup_facts_summary": facts_summary,
        "profile": profile,
        "capability_events": _matching_events(
            events, lambda event: _event_type(event).startswith("capability_")
        ),
        "preflight_events": _matching_events(
            events, lambda event: "preflight" in _event_type(event)
        ),
        "dangerous_operation_previews": _matching_events(
            events, lambda event: _event_type(event) == "dangerous_operation_preview"
        ),
        "app_verification": app_verification,
        "manual_followups": manual_followups,
    }


def _latest_event(events: list[dict[str, Any]], event_type: str) -> dict[str, Any]:
    for event in reversed(events):
        if _event_type(event) == event_type:
            return event
    return {}


def _event_type(event: dict[str, Any]) -> str:
    return str(event.get("event_type") or "")


def _normalize_final_status(value: Any) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if "." in lowered:
        lowered = lowered.rsplit(".", 1)[-1]
    return lowered if lowered in _FINAL_STATUS_VALUES else text


def _matching_events(
    events: list[dict[str, Any]],
    predicate: Any,
) -> list[dict[str, Any]]:
    return [_clean_payload(event) for event in events if predicate(event)]


def _facts_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for event in events:
        facts = event.get("facts")
        if isinstance(facts, dict):
            latest = facts
    return _clean_payload(
        {key: latest.get(key) for key in sorted(_FACT_SUMMARY_KEYS) if key in latest}
    )


def _profile_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    profile: dict[str, Any] = {}
    for event in events:
        raw = event.get("profile")
        if isinstance(raw, dict):
            profile = raw
    prefs = profile.get("preferences") if isinstance(profile.get("preferences"), dict) else {}
    another_machine = str(prefs.get("another_machine") or "").lower()
    physical_setup_mode = ""
    if another_machine == "yes":
        physical_setup_mode = "remote_setup"
    elif another_machine == "no":
        physical_setup_mode = "single_machine_export"
    return _clean_payload(
        {
            "install_profile": profile.get("install_profile", ""),
            "remote_setup_mode": profile.get("remote_setup_mode", ""),
            "physical_setup_mode": physical_setup_mode,
            "install_encryption": profile.get("install_encryption", ""),
            "install_filesystem": profile.get("install_filesystem", ""),
            "install_partition_notes": profile.get("install_partition_notes", ""),
            "preferences": {
                key: prefs.get(key) for key in sorted(_PROFILE_PREFERENCE_KEYS) if key in prefs
            },
            "apps_to_install_count": len(profile.get("apps_to_install") or []),
            "completed_steps": list(profile.get("completed_steps") or []),
        }
    )


def _app_verification_payload(final_details: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in sorted(_APP_VERIFICATION_KEYS):
        values = _collect_key(final_details, key)
        if values:
            payload[key] = values[-1] if len(values) == 1 else values
    return _clean_payload(payload)


def _collect_key(value: Any, wanted: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == wanted:
                found.append(item)
            found.extend(_collect_key(item, wanted))
    elif isinstance(value, list):
        for item in value:
            found.extend(_collect_key(item, wanted))
    return found


def _manual_followups(
    findings: list[Any],
    final_details: dict[str, Any],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        if str(finding.get("severity") or "") == "manual_followup":
            items.append(
                {
                    "source": "final_audit_finding",
                    "category": finding.get("category", ""),
                    "summary": finding.get("summary", ""),
                }
            )
    for item in _collect_key(final_details, "app_verification_manual_followups"):
        if isinstance(item, list):
            for followup in item:
                if isinstance(followup, dict):
                    items.append({"source": "app_verification", **followup})
    for repair in _collect_key(final_details, "final_audit_repairs"):
        if isinstance(repair, list):
            for entry in repair:
                if isinstance(entry, dict) and entry.get("action") == "manual_followup":
                    items.append(
                        {
                            "source": "final_audit_repair",
                            "reason": entry.get("reason", ""),
                            "findings": entry.get("findings", []),
                        }
                    )
    for event in events:
        if _event_type(event) == "final_audit_repair_manual_followup":
            items.append({"source": "final_audit_repair_event", **_clean_payload(event)})
    return [_clean_payload(item) for item in items]


def _certification_summary(
    events: list[dict[str, Any]],
    selected_runlog: Path | None,
) -> dict[str, Any]:
    cert_events = [
        _clean_payload(event)
        for event in events
        if _event_type(event)
        in {"vm_certification_finished", "certification_finished", "certification_blocked"}
    ]
    adjacent_summary: Any = None
    if selected_runlog:
        summary_path = selected_runlog.parent / "summary.json"
        if summary_path.exists() and summary_path.is_file():
            try:
                adjacent_summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                adjacent_summary = None
    if not cert_events and adjacent_summary is None:
        return {}
    return _clean_payload({"events": cert_events, "artifact_summary": adjacent_summary or {}})


def _clean_payload(value: Any) -> Any:
    return json.loads(_redacted_json(value))


def _readme_text() -> str:
    return (
        "QuickHatch support bundle\n"
        "\n"
        "This ZIP contains redacted diagnostic artifacts for troubleshooting one setup "
        "run. It intentionally avoids checkpoints, SSH key files, migration export ZIPs, "
        "and raw credential material.\n"
    )


def environment_metadata() -> dict[str, Any]:
    return {
        "quickhatch_version": __version__,
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
    }
