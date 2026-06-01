from __future__ import annotations

import json
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from quickhatch.setup.support_bundle import export_support_bundle

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "support_bundle_diagnostics_scenarios.yaml"


def _fixture() -> dict[str, Any]:
    return yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))


SCENARIOS = _fixture()["scenarios"]


def _case_id(scenario: dict[str, Any]) -> str:
    return str(scenario["id"])


def _write_runlog(tmp_path: Path, scenario: dict[str, Any]) -> Path:
    run_id = str(scenario["id"])
    runlog = tmp_path / "runs" / f"{run_id}.jsonl"
    runlog.parent.mkdir(parents=True, exist_ok=True)
    events: list[dict[str, Any]] = []
    for idx, raw_event in enumerate(scenario.get("representative_runlog_events") or [], start=1):
        event = dict(raw_event.get("payload") or {})
        event["event_type"] = raw_event["event_type"]
        event.setdefault("run_id", run_id)
        event.setdefault("event_id", f"{run_id}:{idx:06d}")
        events.append(event)
    runlog.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    return runlog


def _bundle_texts(bundle: Path) -> dict[str, str]:
    with zipfile.ZipFile(bundle) as zf:
        return {
            name: zf.read(name).decode("utf-8")
            for name in zf.namelist()
            if name.endswith((".json", ".jsonl", ".sh", ".txt"))
        }


def _bundle_json(bundle: Path, name: str) -> Any:
    return json.loads(_bundle_texts(bundle)[name])


def _export_scenario(tmp_path: Path, scenario: dict[str, Any]) -> Path:
    runlog = _write_runlog(tmp_path, scenario)
    result = export_support_bundle(runlog, out=tmp_path / f"{scenario['id']}.zip")
    return Path(result.path)


def _has_no_likely_gap(scenario: dict[str, Any]) -> bool:
    return str(scenario.get("current_likely_gap") or "").strip().lower() == "none"


def _literal_redaction_markers(scenario: dict[str, Any]) -> list[str]:
    markers: list[str] = []
    for raw in scenario.get("redaction_expectations") or []:
        marker = str(raw or "").strip()
        if not marker or marker.lower() == "none":
            continue
        lowered = marker.lower()
        if " may remain" in lowered or " should remain" in lowered:
            continue
        for separator in (
            " must be redacted",
            " should be redacted",
            " must redact",
            " should redact",
        ):
            if separator in lowered:
                marker = marker[: lowered.index(separator)].strip()
                lowered = marker.lower()
                break
        if lowered.startswith(("confirmation token", "ssh private key text", "secret-like text")):
            continue
        if lowered.startswith(("failed command output", "any command tail", "fstab command")):
            continue
        if not any(
            token in lowered
            for token in (
                "api",
                "bearer",
                "chpasswd",
                "credential",
                "key",
                "password",
                "secret",
                "sk-",
                "ssh-",
                "token",
            )
        ):
            continue
        markers.append(marker)
    return markers


def _has_status_source(scenario: dict[str, Any]) -> bool:
    for raw_event in scenario.get("representative_runlog_events") or []:
        event_type = str(raw_event.get("event_type") or "")
        payload = raw_event.get("payload") if isinstance(raw_event.get("payload"), dict) else {}
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        if event_type == "final_audit":
            return True
        if event_type in {"run_finished", "setup_run_finished", "run_failed", "setup_blocked"}:
            return bool(payload.get("final_status") or payload.get("status"))
        if result.get("final_status") or result.get("status"):
            return True
    return False


def test_support_bundle_diagnostics_fixture_schema_and_coverage() -> None:
    data = _fixture()
    scenarios = data["scenarios"]
    summary = data["merge_summary"]
    required = {
        "id",
        "category",
        "status",
        "representative_runlog_events",
        "expected_bundle_artifacts",
        "required_evidence",
        "redaction_expectations",
        "current_likely_gap",
        "rationale",
        "risk",
        "source_agents",
    }

    assert summary["total_scenarios"] == 93
    assert len(scenarios) == summary["total_scenarios"]
    assert not summary["rejected_ambiguous_cases"]
    assert not summary["omitted_duplicate_cases"]
    assert not [scenario["id"] for scenario in scenarios if required - set(scenario)]

    categories = Counter(scenario["category"] for scenario in scenarios)
    statuses = Counter(scenario["status"] for scenario in scenarios)
    assert categories["blocked_run"] >= 1
    assert categories["preflight_block"] >= 1
    assert categories["capability_mismatch"] >= 1
    assert categories["provider_error"] >= 1
    assert categories["repair_loop_exhaustion"] >= 1
    assert categories["redaction"] >= 1
    assert categories["status_summary_agreement"] >= 1
    assert {
        "blocked",
        "manual_followup_required",
        "success_with_warnings",
        "unsupported_safe",
    } <= set(statuses)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=_case_id)
def test_support_bundle_exports_every_scenario_and_redacts_markers(
    tmp_path: Path, scenario: dict[str, Any]
) -> None:
    bundle = _export_scenario(tmp_path, scenario)
    texts = _bundle_texts(bundle)
    combined = "\n".join(texts.values())

    assert "manifest.json" in texts
    assert "support-summary.json" in texts
    assert "setup-runlog-redacted.jsonl" in texts
    assert "README.txt" in texts
    for marker in _literal_redaction_markers(scenario):
        assert marker not in combined


@pytest.mark.parametrize(
    "scenario",
    [scenario for scenario in SCENARIOS if _has_no_likely_gap(scenario)],
    ids=_case_id,
)
def test_no_gap_scenarios_include_expected_artifacts(
    tmp_path: Path, scenario: dict[str, Any]
) -> None:
    bundle = _export_scenario(tmp_path, scenario)
    names = set(_bundle_texts(bundle))

    assert set(scenario["expected_bundle_artifacts"]) <= names


@pytest.mark.parametrize(
    "scenario",
    [
        scenario
        for scenario in SCENARIOS
        if _has_no_likely_gap(scenario)
        and scenario["status"] != "unknown"
        and _has_status_source(scenario)
    ],
    ids=_case_id,
)
def test_no_gap_scenarios_preserve_final_status(tmp_path: Path, scenario: dict[str, Any]) -> None:
    bundle = _export_scenario(tmp_path, scenario)
    support_summary = _bundle_json(bundle, "support-summary.json")

    assert support_summary["final_status"] == scenario["status"]


@pytest.mark.parametrize(
    "scenario",
    [
        scenario
        for scenario in SCENARIOS
        if _has_no_likely_gap(scenario)
        and "manual-followups.json" in scenario["expected_bundle_artifacts"]
    ],
    ids=_case_id,
)
def test_no_gap_manual_followup_artifact_has_reason(
    tmp_path: Path, scenario: dict[str, Any]
) -> None:
    bundle = _export_scenario(tmp_path, scenario)
    manual_followups = _bundle_json(bundle, "manual-followups.json")
    serialized = json.dumps(manual_followups).lower()

    assert manual_followups
    assert any(
        token in serialized for token in ("reason", "summary", "manual", "follow-up", "followup")
    )


def test_profile_summary_minimizes_non_diagnostic_profile_details(tmp_path: Path) -> None:
    scenario = next(
        scenario
        for scenario in SCENARIOS
        if scenario["id"] == "profile-summary-minimizes-ai-config"
    )
    bundle = _export_scenario(tmp_path, scenario)
    profile = _bundle_json(bundle, "profile-summary.json")
    runlog_text = _bundle_texts(bundle)["setup-runlog-redacted.jsonl"]

    assert profile["install_profile"] == "configure_existing"
    assert profile["apps_to_install_count"] == 2
    assert "ai_config" not in profile
    assert "personal_note" not in json.dumps(profile)
    assert "Private Employer" not in json.dumps(profile)
    assert "sk-profile-ai-secret" not in runlog_text


def test_facts_summary_minimizes_host_identity_details(tmp_path: Path) -> None:
    scenario = next(
        scenario
        for scenario in SCENARIOS
        if scenario["id"] == "facts-summary-minimizes-host-identity"
    )
    bundle = _export_scenario(tmp_path, scenario)
    facts = _bundle_json(bundle, "setup-facts-summary.json")
    serialized = json.dumps(facts)

    assert facts["distro_family"] == "ubuntu"
    assert facts["package_manager"] == "apt"
    assert facts["gpu_vendors"] == ["intel"]
    assert "hostname" not in facts
    assert "fqdn" not in facts
    assert "ip_addresses" not in facts
    assert "mac_addresses" not in facts
    assert "jan-private-host" not in serialized
    assert "192.168.55.42" not in serialized
    assert "de:ad:be:ef:00:01" not in serialized
