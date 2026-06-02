from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import yaml

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "model_reliability_scenarios.yaml"


def _load_fixture() -> dict[str, Any]:
    payload = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert isinstance(payload.get("metadata"), dict)
    assert isinstance(payload.get("scenarios"), list)
    return payload


def test_model_reliability_fixture_schema_and_counts() -> None:
    fixture = _load_fixture()
    metadata = fixture["metadata"]
    scenarios = fixture["scenarios"]
    required_fields = set(metadata["schema"]["required_fields"])
    coverage_status_values = set(metadata["schema"]["coverage_status_values"])
    outcome_values = set(metadata["schema"]["outcome_values"])

    assert metadata["total_scenarios"] == len(scenarios)
    assert metadata["total_scenarios"] >= 50
    assert metadata["scenarios_by_category"] == dict(
        sorted(Counter(scenario["category"] for scenario in scenarios).items())
    )
    assert metadata["coverage_statuses"] == dict(
        sorted(Counter(scenario["coverage"]["status"] for scenario in scenarios).items())
    )

    ids: set[str] = set()
    for scenario in scenarios:
        assert required_fields.issubset(scenario), scenario.get("id")
        assert scenario["id"] not in ids
        ids.add(scenario["id"])
        assert scenario["coverage"]["status"] in coverage_status_values
        assert scenario["expected_harness_response"]["outcome"] in outcome_values
        assert scenario["risk"] in {"low", "medium", "high"}
        assert scenario["model_behavior"]["pattern"]
        assert isinstance(scenario["expected_harness_response"]["must_not_execute"], list)
        assert isinstance(scenario["expected_harness_response"]["runlog_events"], list)


def test_model_reliability_fixture_links_existing_coverage() -> None:
    fixture = _load_fixture()
    scenarios = fixture["scenarios"]
    root = FIXTURE_PATH.parent.parent.parent

    covered_or_partial = [
        scenario
        for scenario in scenarios
        if scenario["coverage"]["status"] in {"covered", "partial"}
    ]
    assert covered_or_partial
    for scenario in covered_or_partial:
        coverage = scenario["coverage"]
        assert coverage["existing_tests"], scenario["id"]
        assert coverage["existing_fixtures"], scenario["id"]
        for rel_path in coverage["existing_tests"] + coverage["existing_fixtures"]:
            assert (root / rel_path).exists(), f"{scenario['id']} references missing {rel_path}"


def test_model_reliability_fixture_covers_core_model_failure_families() -> None:
    fixture = _load_fixture()
    metadata = fixture["metadata"]

    assert set(metadata["scenarios_by_category"]) == {
        "final_status_honesty",
        "json_contract",
        "output_noise",
        "planning_mode",
        "provider_diversity",
        "repair_quality",
        "research_grounding",
        "retry_loop_budget",
        "safety_contract",
        "uncertainty_followup",
    }
    assert metadata["expected_behaviors"]["reject_malformed_or_unsafe_model_output"] >= 10
    assert metadata["expected_behaviors"]["retry_or_correct_before_execution"] >= 10
    assert metadata["expected_behaviors"]["preserve_dangerous_operation_gates"] >= 5
    assert metadata["expected_behaviors"]["keep_final_status_honest"] >= 4
