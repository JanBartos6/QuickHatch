from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from quickhatch.setup.types import FinalSetupStatus

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "app_partial_success_scenarios.yaml"


@dataclass(frozen=True)
class FakeCommandResult:
    app: str
    package: str
    rc: int
    stdout: str
    stderr: str


class FakeExecutor:
    """Replay scripted command and verification events without running commands."""

    def __init__(self) -> None:
        self.results: list[FakeCommandResult] = []

    def record(self, event: dict[str, Any]) -> FakeCommandResult:
        result = FakeCommandResult(
            app=str(event.get("app") or ""),
            package=str(event.get("package") or ""),
            rc=int(event.get("rc") or 0),
            stdout=str(event.get("stdout") or ""),
            stderr=str(event.get("stderr") or ""),
        )
        self.results.append(result)
        return result


class FakeFactsProvider:
    """Return before/after installed-app snapshots from the scenario facts profile."""

    def __init__(self, profile: dict[str, Any]) -> None:
        self.before = {_normalize(value) for value in profile["installed_apps_before"]}
        self.after = {_normalize(value) for value in profile["installed_apps_after"]}
        self.refreshes = 0

    def current_installed(self) -> set[str]:
        return self.before if self.refreshes == 0 else self.after

    def refresh_installed(self) -> set[str]:
        self.refreshes += 1
        return self.after


class FakeRepairProvider:
    """Capture repair/model responses and provider errors from the scenario script."""

    def __init__(self) -> None:
        self.responses: list[dict[str, str]] = []
        self.errors: list[dict[str, str]] = []

    def record_response(self, event: dict[str, Any]) -> None:
        self.responses.append(
            {
                "app": str(event.get("app") or ""),
                "package": str(event.get("package") or ""),
                "strategy": str(event.get("repair_strategy") or ""),
            }
        )

    def record_error(self, event: dict[str, Any]) -> None:
        self.errors.append(
            {
                "app": str(event.get("app") or ""),
                "package": str(event.get("package") or ""),
                "error": str(event.get("provider_error") or ""),
                "strategy": str(event.get("repair_strategy") or ""),
            }
        )


@dataclass
class FakeRunLog:
    events: list[dict[str, Any]] = field(default_factory=list)

    def record(self, event_type: str, **payload: Any) -> None:
        self.events.append({"event_type": event_type, **payload})


@dataclass
class ScenarioOutcome:
    completed_apps: set[str]
    manual_followup_reasons: dict[str, str]
    failed_apps: set[str]
    alternatives: list[str]
    final_status: FinalSetupStatus
    completed_history: list[set[str]]
    source_strategies: list[str]
    identical_repair_loops: list[str]
    provider_errors: list[str]
    runlog: FakeRunLog

    @property
    def manual_followup_apps(self) -> set[str]:
        return set(self.manual_followup_reasons)


def _load_fixture() -> dict[str, Any]:
    payload = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert isinstance(payload.get("scenarios"), list)
    return payload


FIXTURE = _load_fixture()
SCENARIOS = FIXTURE["scenarios"]
PROPERTY_SCENARIO_SEEDS = tuple(range(20260531, 20260579))
PROPERTY_SOURCES = ("repo", "flatpak", "snap", "aur", "unknown")
PROPERTY_FAMILY_PROFILES = (
    ("arch", "pacman", True, False, "yay"),
    ("ubuntu", "apt", True, True, "none"),
    ("debian", "apt", False, False, "none"),
    ("fedora", "dnf", True, False, "none"),
    ("generic", "generic", "unknown", "unknown", "unknown"),
)
PROPERTY_OUTCOMES = (
    "success",
    "failure",
    "repair_success",
    "provider_429",
    "provider_timeout",
    "repair_failure",
    "facts_refresh_success",
)


def _normalize(value: object) -> str:
    return str(value or "").strip().casefold()


def _requested_apps(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    return list(scenario["plan"]["requested_apps"])


def _requested_app_names(scenario: dict[str, Any]) -> set[str]:
    return {str(app["name"]) for app in _requested_apps(scenario)}


def _required_app_names(scenario: dict[str, Any]) -> set[str]:
    return {str(app["name"]) for app in _requested_apps(scenario) if app.get("required") is True}


def _optional_app_names(scenario: dict[str, Any]) -> set[str]:
    return {str(app["name"]) for app in _requested_apps(scenario) if app.get("required") is False}


def _find_requested_app(scenario: dict[str, Any], *, app: str, package: str) -> str:
    normalized_app = _normalize(app)
    normalized_package = _normalize(package)
    for requested in _requested_apps(scenario):
        if normalized_app and normalized_app == _normalize(requested["name"]):
            return str(requested["name"])
        if normalized_package and normalized_package == _normalize(requested.get("package")):
            return str(requested["name"])
    return ""


def _snapshot_completions(scenario: dict[str, Any], installed: set[str]) -> set[str]:
    completed: set[str] = set()
    for app in _requested_apps(scenario):
        app_name = str(app["name"])
        candidates = {_normalize(app_name), _normalize(app.get("package"))}
        if candidates & installed:
            completed.add(app_name)
    return completed


def _alternative_from_success(scenario: dict[str, Any], event: dict[str, Any]) -> str:
    strategy = str(event.get("repair_strategy") or "")
    event_app = str(event.get("app") or "")
    if not strategy.startswith("alternative"):
        return ""
    if event_app and event_app not in _requested_app_names(scenario):
        return event_app
    package = str(event.get("package") or "")
    return package


def _manual_followup_reason(app: dict[str, Any]) -> str:
    name = str(app["name"])
    package = str(app.get("package") or "unknown package")
    return f"{name}: package/app '{package}' remained unresolved after scripted repair."


def _compute_final_status(scenario: dict[str, Any], manual_followups: set[str]) -> FinalSetupStatus:
    if manual_followups & _required_app_names(scenario):
        return FinalSetupStatus.MANUAL_FOLLOWUP_REQUIRED
    if manual_followups:
        return FinalSetupStatus.SUCCESS_WITH_WARNINGS
    return FinalSetupStatus.SUCCESS


def _run_scenario(scenario: dict[str, Any]) -> ScenarioOutcome:
    executor = FakeExecutor()
    facts = FakeFactsProvider(scenario["facts_profile"])
    repairs = FakeRepairProvider()
    runlog = FakeRunLog()

    completed = _snapshot_completions(scenario, facts.current_installed())
    alternatives: list[str] = []
    source_strategies: list[str] = []
    identical_repair_loops: list[str] = []
    completed_history: list[set[str]] = [set(completed)]

    for event in scenario["simulated_events"]:
        event_type = str(event["type"])
        runlog.record(event_type, app=event.get("app"), package=event.get("package"))

        if event_type in {"command_success", "command_failure", "verification"}:
            result = executor.record(event)
            matched = _find_requested_app(
                scenario,
                app=result.app,
                package=result.package,
            )
            if event_type in {"command_success", "verification"} and result.rc == 0 and matched:
                completed.add(matched)
            alternative = _alternative_from_success(scenario, event)
            if event_type == "command_success" and alternative:
                alternatives.append(alternative)

        elif event_type == "facts_refresh":
            installed = facts.refresh_installed()
            completed.update(_snapshot_completions(scenario, installed))

        elif event_type == "repair_response":
            repairs.record_response(event)
            strategy = str(event.get("repair_strategy") or "")
            if strategy:
                source_strategies.append(strategy)
            if (
                "identical" in strategy
                or strategy.startswith("retry_")
                or strategy.startswith("repeat_")
            ):
                identical_repair_loops.append(strategy)

        elif event_type == "provider_error":
            repairs.record_error(event)
            provider_error = str(event.get("provider_error") or "")
            if provider_error:
                runlog.record("provider_error", provider_error=provider_error)

        completed_history.append(set(completed))

    completed.update(_snapshot_completions(scenario, facts.after))
    completed_history.append(set(completed))

    manual_followup_reasons = {
        str(app["name"]): _manual_followup_reason(app)
        for app in _requested_apps(scenario)
        if str(app["name"]) not in completed
    }
    final_status = _compute_final_status(scenario, set(manual_followup_reasons))
    provider_errors = [entry["error"] for entry in repairs.errors if entry["error"]]

    return ScenarioOutcome(
        completed_apps=completed,
        manual_followup_reasons=manual_followup_reasons,
        failed_apps=set(),
        alternatives=alternatives,
        final_status=final_status,
        completed_history=completed_history,
        source_strategies=source_strategies,
        identical_repair_loops=identical_repair_loops,
        provider_errors=provider_errors,
        runlog=runlog,
    )


def _expected_set(scenario: dict[str, Any], key: str) -> set[str]:
    return set(scenario["expected"][key])


def _expected_status(scenario: dict[str, Any]) -> FinalSetupStatus:
    return FinalSetupStatus(scenario["expected"]["final_status"])


def _format_outcome_mismatch(scenario: dict[str, Any], outcome: ScenarioOutcome) -> str:
    return (
        f"{scenario['id']} expected completed={scenario['expected']['completed_apps']} "
        f"manual={scenario['expected']['manual_followup_apps']} "
        f"alternatives={scenario['expected']['alternatives']} "
        f"status={scenario['expected']['final_status']}; got "
        f"completed={sorted(outcome.completed_apps)} "
        f"manual={sorted(outcome.manual_followup_apps)} "
        f"alternatives={sorted(outcome.alternatives)} "
        f"status={outcome.final_status.value}"
    )


def _generated_event(
    event_type: str,
    *,
    app: str,
    package: str,
    rc: int,
    stdout: str = "",
    stderr: str = "",
    repair_strategy: str = "",
    provider_error: str = "",
) -> dict[str, Any]:
    return {
        "type": event_type,
        "app": app,
        "package": package,
        "rc": rc,
        "stdout": stdout,
        "stderr": stderr,
        "repair_strategy": repair_strategy,
        "provider_error": provider_error,
    }


def _source_after_fallback(source: str, rng: random.Random) -> str:
    fallback_sources = [candidate for candidate in PROPERTY_SOURCES if candidate != source]
    return rng.choice(fallback_sources)


def _make_property_scenario(seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    family, package_manager, flatpak_available, snap_available, aur_helper = rng.choice(
        PROPERTY_FAMILY_PROFILES
    )
    app_count = rng.randint(1, 6)
    requested_apps: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    installed_before: set[str] = set()
    installed_after: set[str] = set()
    completed_apps: list[str] = []
    manual_followup_apps: list[str] = []

    for index in range(app_count):
        app_name = f"Seed {seed} App {index}"
        package = f"seed-{seed}-app-{index}"
        source = rng.choice(PROPERTY_SOURCES)
        required = rng.choice([True, False])
        requested_apps.append(
            {
                "name": app_name,
                "package": package,
                "source": source,
                "required": required,
            }
        )
        outcome = PROPERTY_OUTCOMES[(seed + index) % len(PROPERTY_OUTCOMES)]

        if rng.random() < 0.12:
            installed_before.add(package)
            installed_after.add(package)
            completed_apps.append(app_name)
            events.append(
                _generated_event(
                    "verification",
                    app=app_name,
                    package=package,
                    rc=0,
                    stdout="already installed",
                )
            )
            continue

        if outcome == "success":
            installed_after.add(package)
            completed_apps.append(app_name)
            events.append(
                _generated_event(
                    "command_success",
                    app=app_name,
                    package=package,
                    rc=0,
                    stdout="installed",
                )
            )
            continue

        if outcome == "repair_success":
            fallback_source = _source_after_fallback(source, rng)
            fallback_package = f"{package}-{fallback_source}"
            installed_after.add(fallback_package)
            completed_apps.append(app_name)
            events.extend(
                [
                    _generated_event(
                        "command_failure",
                        app=app_name,
                        package=package,
                        rc=1,
                        stderr="package unavailable",
                    ),
                    _generated_event(
                        "repair_response",
                        app=app_name,
                        package=fallback_package,
                        rc=0,
                        stdout=f"fallback to {fallback_source}",
                        repair_strategy=f"{source}_to_{fallback_source}",
                    ),
                    _generated_event(
                        "command_success",
                        app=app_name,
                        package=fallback_package,
                        rc=0,
                        stdout="fallback installed",
                        repair_strategy=f"{source}_to_{fallback_source}",
                    ),
                ]
            )
            continue

        if outcome == "facts_refresh_success":
            installed_after.add(package)
            completed_apps.append(app_name)
            events.extend(
                [
                    _generated_event(
                        "command_failure",
                        app=app_name,
                        package=package,
                        rc=1,
                        stderr="stale command output",
                    ),
                    _generated_event(
                        "facts_refresh",
                        app=app_name,
                        package=package,
                        rc=0,
                        stdout=f"INSTALLED: {package}",
                    ),
                ]
            )
            continue

        manual_followup_apps.append(app_name)
        events.append(
            _generated_event(
                "command_failure",
                app=app_name,
                package=package,
                rc=1,
                stderr="package unavailable",
            )
        )
        if outcome in {"provider_429", "provider_timeout"}:
            events.append(
                _generated_event(
                    "provider_error",
                    app=app_name,
                    package=package,
                    rc=0,
                    repair_strategy=f"{source}_repair",
                    provider_error="429" if outcome == "provider_429" else "timeout",
                )
            )
        elif outcome == "repair_failure":
            fallback_source = _source_after_fallback(source, rng)
            fallback_package = f"{package}-{fallback_source}"
            events.extend(
                [
                    _generated_event(
                        "repair_response",
                        app=app_name,
                        package=fallback_package,
                        rc=0,
                        stdout=f"fallback to {fallback_source}",
                        repair_strategy=f"{source}_to_{fallback_source}",
                    ),
                    _generated_event(
                        "command_failure",
                        app=app_name,
                        package=fallback_package,
                        rc=1,
                        stderr="fallback unavailable",
                        repair_strategy=f"{source}_to_{fallback_source}",
                    ),
                ]
            )

    events.append(
        _generated_event(
            "facts_refresh",
            app="",
            package="",
            rc=0,
            stdout=" ".join(sorted(installed_after)),
        )
    )
    manual_followup_set = set(manual_followup_apps)
    required_followups = {
        app["name"]
        for app in requested_apps
        if app["required"] is True and app["name"] in manual_followup_set
    }
    if required_followups:
        final_status = FinalSetupStatus.MANUAL_FOLLOWUP_REQUIRED
    elif manual_followup_set:
        final_status = FinalSetupStatus.SUCCESS_WITH_WARNINGS
    else:
        final_status = FinalSetupStatus.SUCCESS

    return {
        "id": f"property-seed-{seed}",
        "category": "property_generated",
        "facts_profile": {
            "family": family,
            "package_manager": package_manager,
            "installed_apps_before": sorted(installed_before),
            "installed_apps_after": sorted(installed_after),
            "flatpak_available": flatpak_available,
            "snap_available": snap_available,
            "aur_helper": aur_helper,
        },
        "plan": {
            "requested_apps": requested_apps,
            "command_groups": [
                {
                    "title": f"Property app group {seed}",
                    "packages": [app["package"] for app in requested_apps],
                    "commands": [f"install {app['package']}" for app in requested_apps],
                }
            ],
        },
        "simulated_events": events,
        "expected": {
            "completed_apps": completed_apps,
            "manual_followup_apps": manual_followup_apps,
            "failed_apps": [],
            "alternatives": [],
            "final_status": final_status.value,
            "must_not_block": True,
            "invariants": [
                "successful_apps_preserved",
                "failure_isolated",
                "provider_error_preserves_progress",
                "final_status_honest",
            ],
        },
        "rationale": "Seeded randomized app partial-success scenario.",
    }


def _assert_monotonic_completion(scenario: dict[str, Any], outcome: ScenarioOutcome) -> None:
    previous: set[str] = set()
    for snapshot in outcome.completed_history:
        assert previous <= snapshot, scenario["id"]
        previous = snapshot


def test_app_partial_success_fixture_summary_matches_cases() -> None:
    summary = FIXTURE["merge_summary"]

    assert summary["total_scenarios"] == len(SCENARIOS)
    assert summary["scenarios_by_category"] == {
        category: sum(1 for scenario in SCENARIOS if scenario["category"] == category)
        for category in summary["scenarios_by_category"]
    }
    assert FIXTURE["rejected_ambiguous_cases"] == []


@pytest.mark.parametrize("scenario", [pytest.param(case, id=case["id"]) for case in SCENARIOS])
def test_app_partial_success_scenario_expected_outcomes_are_deterministic(
    scenario: dict[str, Any],
) -> None:
    outcome = _run_scenario(scenario)

    assert outcome.completed_apps == _expected_set(scenario, "completed_apps"), (
        _format_outcome_mismatch(scenario, outcome)
    )
    assert outcome.manual_followup_apps == _expected_set(scenario, "manual_followup_apps"), (
        _format_outcome_mismatch(scenario, outcome)
    )
    assert outcome.failed_apps == _expected_set(scenario, "failed_apps"), _format_outcome_mismatch(
        scenario, outcome
    )
    assert set(outcome.alternatives) == _expected_set(scenario, "alternatives"), (
        _format_outcome_mismatch(scenario, outcome)
    )
    assert outcome.final_status == _expected_status(scenario), _format_outcome_mismatch(
        scenario, outcome
    )
    assert outcome.final_status != FinalSetupStatus.BLOCKED
    assert scenario["expected"]["must_not_block"] is True


def test_completed_apps_remain_completed_after_unrelated_later_failures() -> None:
    scenarios = [
        scenario
        for scenario in SCENARIOS
        if scenario["expected"]["completed_apps"] and scenario["expected"]["manual_followup_apps"]
    ]
    assert scenarios

    for scenario in scenarios:
        outcome = _run_scenario(scenario)
        for history in outcome.completed_history:
            assert history <= outcome.completed_apps, scenario["id"]
        assert outcome.completed_apps == _expected_set(scenario, "completed_apps")


def test_failed_or_missing_package_affects_only_mapped_apps() -> None:
    for scenario in SCENARIOS:
        outcome = _run_scenario(scenario)
        affected = outcome.manual_followup_apps | outcome.failed_apps
        expected = _expected_set(scenario, "manual_followup_apps") | _expected_set(
            scenario,
            "failed_apps",
        )

        assert affected == expected, _format_outcome_mismatch(scenario, outcome)


def test_optional_app_failure_does_not_block_setup() -> None:
    scenarios = [
        scenario
        for scenario in SCENARIOS
        if _expected_set(scenario, "manual_followup_apps")
        and _expected_set(scenario, "manual_followup_apps") <= _optional_app_names(scenario)
    ]
    assert scenarios

    for scenario in scenarios:
        outcome = _run_scenario(scenario)

        assert outcome.final_status == FinalSetupStatus.SUCCESS_WITH_WARNINGS, scenario["id"]
        assert outcome.final_status != FinalSetupStatus.BLOCKED


def test_required_app_failure_is_not_hidden_as_success() -> None:
    scenarios = [
        scenario
        for scenario in SCENARIOS
        if _expected_set(scenario, "manual_followup_apps") & _required_app_names(scenario)
    ]
    assert scenarios

    for scenario in scenarios:
        outcome = _run_scenario(scenario)

        assert outcome.final_status == FinalSetupStatus.MANUAL_FOLLOWUP_REQUIRED, scenario["id"]
        assert outcome.final_status != FinalSetupStatus.SUCCESS


def test_provider_429_and_timeout_do_not_erase_progress() -> None:
    scenarios = [
        scenario
        for scenario in SCENARIOS
        if any(event["type"] == "provider_error" for event in scenario["simulated_events"])
    ]
    assert scenarios

    for scenario in scenarios:
        outcome = _run_scenario(scenario)

        assert outcome.provider_errors, scenario["id"]
        assert outcome.completed_apps == _expected_set(scenario, "completed_apps")
        assert outcome.manual_followup_apps == _expected_set(scenario, "manual_followup_apps")


def test_fallback_source_changes_do_not_count_as_identical_repair_loops() -> None:
    scenarios = [
        scenario
        for scenario in SCENARIOS
        if any(
            event["type"] == "repair_response"
            and str(event.get("repair_strategy") or "")
            and "identical" not in str(event.get("repair_strategy") or "")
            and not str(event.get("repair_strategy") or "").startswith(("retry_", "repeat_"))
            for event in scenario["simulated_events"]
        )
    ]
    assert scenarios

    for scenario in scenarios:
        outcome = _run_scenario(scenario)

        assert outcome.source_strategies, scenario["id"]
        assert not outcome.identical_repair_loops, scenario["id"]
        assert outcome.final_status == _expected_status(scenario)


def test_identical_repair_loops_become_followup_without_blocking() -> None:
    scenarios = [
        scenario
        for scenario in SCENARIOS
        if any(
            "identical" in str(event.get("repair_strategy") or "")
            or str(event.get("repair_strategy") or "").startswith(("retry_", "repeat_"))
            for event in scenario["simulated_events"]
        )
    ]
    assert scenarios

    for scenario in scenarios:
        outcome = _run_scenario(scenario)

        assert outcome.identical_repair_loops, scenario["id"]
        assert outcome.manual_followup_apps == _expected_set(scenario, "manual_followup_apps")
        assert outcome.final_status != FinalSetupStatus.BLOCKED


def test_alternatives_are_recorded_as_alternatives_not_completed_original_apps() -> None:
    scenarios = [scenario for scenario in SCENARIOS if scenario["expected"]["alternatives"]]
    assert scenarios

    for scenario in scenarios:
        outcome = _run_scenario(scenario)
        original_apps = _requested_app_names(scenario)

        assert set(outcome.alternatives) == _expected_set(scenario, "alternatives")
        assert not (set(outcome.alternatives) & outcome.completed_apps)
        assert outcome.manual_followup_apps & original_apps


def test_final_status_is_honest_for_all_scenarios() -> None:
    for scenario in SCENARIOS:
        outcome = _run_scenario(scenario)

        assert outcome.final_status == _expected_status(scenario)
        if outcome.manual_followup_apps & _required_app_names(scenario):
            assert outcome.final_status == FinalSetupStatus.MANUAL_FOLLOWUP_REQUIRED
        elif outcome.manual_followup_apps:
            assert outcome.final_status == FinalSetupStatus.SUCCESS_WITH_WARNINGS
        else:
            assert outcome.final_status == FinalSetupStatus.SUCCESS


def test_manual_followup_reasons_are_app_specific() -> None:
    scenarios = [scenario for scenario in SCENARIOS if scenario["expected"]["manual_followup_apps"]]
    assert scenarios

    for scenario in scenarios:
        outcome = _run_scenario(scenario)

        for app in _expected_set(scenario, "manual_followup_apps"):
            reason = outcome.manual_followup_reasons[app]
            assert app in reason
            assert "unresolved" in reason


def test_facts_refresh_does_not_erase_manual_followup_decisions() -> None:
    scenarios = [
        scenario
        for scenario in SCENARIOS
        if any(event["type"] == "facts_refresh" for event in scenario["simulated_events"])
    ]
    assert scenarios

    for scenario in scenarios:
        outcome = _run_scenario(scenario)

        assert outcome.manual_followup_apps == _expected_set(scenario, "manual_followup_apps")
        assert outcome.completed_apps == _expected_set(scenario, "completed_apps")


def test_seeded_random_app_partial_success_invariants() -> None:
    for seed in PROPERTY_SCENARIO_SEEDS:
        scenario = _make_property_scenario(seed)
        outcome = _run_scenario(scenario)
        expected_manual = _expected_set(scenario, "manual_followup_apps")

        _assert_monotonic_completion(scenario, outcome)
        assert outcome.completed_apps == _expected_set(scenario, "completed_apps"), (
            _format_outcome_mismatch(scenario, outcome)
        )
        assert outcome.manual_followup_apps == expected_manual, _format_outcome_mismatch(
            scenario, outcome
        )
        for app, reason in outcome.manual_followup_reasons.items():
            assert app in reason
            assert "unresolved" in reason

        optional_followups = expected_manual <= _optional_app_names(scenario)
        if expected_manual and optional_followups:
            assert outcome.final_status == FinalSetupStatus.SUCCESS_WITH_WARNINGS, scenario["id"]
        if expected_manual & _required_app_names(scenario):
            assert outcome.final_status == FinalSetupStatus.MANUAL_FOLLOWUP_REQUIRED, scenario["id"]
        assert outcome.final_status == _expected_status(scenario), _format_outcome_mismatch(
            scenario, outcome
        )
        assert outcome.final_status != FinalSetupStatus.BLOCKED

        for index, event in enumerate(scenario["simulated_events"]):
            if event["type"] == "provider_error":
                assert outcome.completed_history[index] <= outcome.completed_history[index + 1], (
                    scenario["id"]
                )
                assert outcome.completed_history[index] <= outcome.completed_apps, scenario["id"]
