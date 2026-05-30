from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from quickhatch.setup.capabilities import (
    CAPABILITY_KINDS,
    CapabilityStore,
    dangerous_command_requirements,
)
from quickhatch.setup.preflight import preflight_setup_command, preflight_setup_decisions
from quickhatch.setup.types import SetupFacts

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "preflight_command_corpus.yaml"
RUN_ID = "corpus-preflight-run"


@dataclass(frozen=True)
class CorpusCaseResult:
    case_id: str
    expected: str
    outcome: str
    error: str = ""


def _load_cases() -> list[dict[str, Any]]:
    payload = yaml.safe_load(FIXTURE_PATH.read_text())
    assert isinstance(payload, dict)
    cases = payload.get("cases")
    assert isinstance(cases, list)
    return cases


CORPUS_CASES = _load_cases()
ACTIONABLE_CASES = [case for case in CORPUS_CASES if case["expected"] != "manual_review"]
SAFE_CASES = [case for case in CORPUS_CASES if case["expected"] in {"allow", "allow_with_advisory"}]
MANUAL_REVIEW_CASES = [case for case in CORPUS_CASES if case["expected"] == "manual_review"]


def _facts_for_case(case: dict[str, Any]) -> SetupFacts:
    family = case["facts_profile"]
    package_manager = {
        "arch": "pacman",
        "debian": "apt",
        "ubuntu": "apt",
        "fedora": "dnf",
        "generic": "unknown",
    }[family]
    distro_family = "unknown" if family == "generic" else family
    common = {
        "distro_family": distro_family,
        "package_manager": package_manager,
        "community_helper": "yay" if family == "arch" else "none",
        "init_system": "systemd",
        "current_user": "root",
        "login_user": "root",
        "is_root": True,
        "sudo_nopasswd": True,
    }
    if case["mode"] == "configure_existing":
        return SetupFacts(
            **{
                **common,
                "current_user": "quickhatch",
                "login_user": "quickhatch",
                "is_root": False,
                "is_live": False,
            }
        )
    if case["mode"] == "fresh_install":
        return SetupFacts(**{**common, "is_live": True, "target_root_mounted": True})
    return SetupFacts(**{**common, "is_live": True})


def _install_profile_for_case(case: dict[str, Any]) -> str:
    if case["mode"] == "configure_existing":
        return "configure_existing"
    return "full_disk_wipe"


def _store_with_capability(case: dict[str, Any], *, target: str | None = None) -> CapabilityStore:
    kind = case["expected_capability_kind"]
    grant_target = case["expected_target"] if target is None else target
    store = CapabilityStore(run_id=RUN_ID, install_profile=_install_profile_for_case(case))
    store.grant(
        kind=kind,
        target=grant_target,
        confirmation_text=f"CONFIRM {kind} {grant_target}".strip(),
        confirmation_id=f"preview-{case['id']}",
        reason=f"corpus test grant for {case['id']}",
        source="harness_generated",
        one_time_use=False,
        preview_id=f"preview-{case['id']}",
    )
    return store


def _mismatched_target(target: str) -> str:
    if target.startswith("/dev/"):
        return "/dev/quickhatch-mismatch"
    if target.startswith("/"):
        return "/quickhatch-mismatch"
    if target:
        return f"{target}-mismatch"
    return "/quickhatch-mismatch"


def _hard_block(
    case: dict[str, Any], *, capability_store: CapabilityStore | None = None
) -> str | None:
    return preflight_setup_command(
        case["command"],
        _facts_for_case(case),
        capability_store=capability_store,
    )


def _evaluate_case(case: dict[str, Any]) -> CorpusCaseResult:
    error = _hard_block(case)
    expected = case["expected"]
    if expected in {"allow", "allow_with_advisory"}:
        if error:
            return CorpusCaseResult(case["id"], expected, "false_positive", error)
        return CorpusCaseResult(case["id"], expected, "pass")
    if expected == "block_without_capability":
        if not error:
            return CorpusCaseResult(case["id"], expected, "false_negative")
        return CorpusCaseResult(case["id"], expected, "pass")
    if expected == "allow_only_with_capability":
        if not error:
            return CorpusCaseResult(
                case["id"],
                expected,
                "false_negative",
                "allowed without required capability",
            )
        if not case["expected_capability_kind"] or not case["expected_target"]:
            return CorpusCaseResult(case["id"], expected, "pass")
        if case["expected_capability_kind"] not in CAPABILITY_KINDS:
            return CorpusCaseResult(
                case["id"],
                expected,
                "capability_mismatch",
                f"unsupported capability kind: {case['expected_capability_kind']}",
            )
        matching_error = _hard_block(case, capability_store=_store_with_capability(case))
        if matching_error:
            return CorpusCaseResult(
                case["id"],
                expected,
                "capability_mismatch",
                f"matching capability did not pass: {matching_error}",
            )
        mismatch_store = _store_with_capability(
            case, target=_mismatched_target(case["expected_target"])
        )
        mismatch_error = _hard_block(case, capability_store=mismatch_store)
        if not mismatch_error:
            return CorpusCaseResult(
                case["id"],
                expected,
                "capability_mismatch",
                "mismatched target capability unexpectedly passed",
            )
        return CorpusCaseResult(case["id"], expected, "pass")
    raise AssertionError(f"Unhandled corpus expectation: {expected}")


def _format_results(results: list[CorpusCaseResult]) -> str:
    passing = sum(result.outcome == "pass" for result in results)
    failures = [result for result in results if result.outcome != "pass"]
    false_positives = [result for result in failures if result.outcome == "false_positive"]
    false_negatives = [result for result in failures if result.outcome == "false_negative"]
    capability_mismatches = [
        result for result in failures if result.outcome == "capability_mismatch"
    ]
    lines = [
        "preflight command corpus mismatches",
        f"cases loaded: {len(CORPUS_CASES)}",
        f"manual_review skipped: {len(MANUAL_REVIEW_CASES)}",
        f"actionable cases: {len(results)}",
        f"passing cases: {passing}",
        f"failing cases: {len(failures)}",
        f"false positives: {len(false_positives)}",
        f"false negatives: {len(false_negatives)}",
        f"capability mismatches: {len(capability_mismatches)}",
    ]
    for label, bucket in (
        ("false positives", false_positives),
        ("false negatives", false_negatives),
        ("capability mismatches", capability_mismatches),
    ):
        if not bucket:
            continue
        lines.append(f"\n{label}:")
        for result in bucket:
            suffix = f" :: {result.error}" if result.error else ""
            lines.append(f"- {result.case_id} ({result.expected}){suffix}")
    return "\n".join(lines)


def _likely_false_positive_detector(case: dict[str, Any], error: str) -> str:
    requirements = dangerous_command_requirements(case["command"])
    if requirements:
        requirement_summary = ", ".join(
            f"{item.get('kind', '<unknown>')}:{item.get('target', '<none>')}"
            for item in requirements
        )
        return (
            f"quickhatch.setup.capabilities.dangerous_command_requirements ({requirement_summary})"
        )
    if "changes system packages and needs root privileges" in error:
        return (
            "quickhatch.setup.preflight._package_tool_mutates_system via "
            "preflight_setup_command root privilege check"
        )
    return "quickhatch.setup.preflight.preflight_setup_command"


def _recommended_false_positive_fix(case: dict[str, Any], error: str) -> str:
    command = case["command"]
    if "changes system packages and needs root privileges" in error:
        return (
            "Treat package-manager planning flags such as apt `-s`/`--simulate`/"
            "`--just-print` and dnf `--assumeno` as non-mutating before the root "
            "privilege gate, while still blocking real package mutations."
        )
    requirements = dangerous_command_requirements(command)
    if requirements:
        if command.strip().startswith(("echo ", "printf ")):
            return (
                "Classify executable shell words instead of matching dangerous tokens "
                "inside inert echo/printf string arguments."
            )
        if "#" in command:
            return (
                "Strip or parse shell comments before dangerous-effect regex matching so "
                "comment text is not treated as an executable operation."
            )
        if command.startswith("python3 -c") and "print(" in command:
            return (
                "Do not treat interpreter string literals as executed shell unless the "
                "wrapper invokes a shell bridge such as os.system/subprocess shell text."
            )
        if command.startswith("sh -c"):
            return (
                "For shell `-c`, inspect only the command payload argument, not unused "
                "positional arguments after the payload."
            )
        return (
            "Narrow dangerous_command_requirements to ignore inert string/comment contexts "
            "while preserving detection for actually executed disk/boot mutations."
        )
    return "Narrow the blocking detector to the command effect that actually mutates state."


def _format_safe_case_results(results: list[CorpusCaseResult]) -> str:
    passing = sum(result.outcome == "pass" for result in results)
    false_positives = [result for result in results if result.outcome == "false_positive"]
    lines = [
        "safe preflight false-positive mismatches",
        f"total safe cases checked: {len(results)}",
        f"passing safe cases: {passing}",
        f"false positives found: {len(false_positives)}",
    ]
    if not false_positives:
        return "\n".join(lines)

    cases_by_id = {case["id"]: case for case in SAFE_CASES}
    lines.append("\nfalse positives:")
    for result in false_positives:
        case = cases_by_id[result.case_id]
        lines.extend(
            [
                f"- {result.case_id} ({result.expected})",
                f"  command: {case['command']}",
                f"  blocking reason: {result.error}",
                f"  likely detector: {_likely_false_positive_detector(case, result.error)}",
                f"  recommended narrow fix: {_recommended_false_positive_fix(case, result.error)}",
            ]
        )
    return "\n".join(lines)


def test_preflight_command_corpus_matches_current_harness() -> None:
    results = [_evaluate_case(case) for case in ACTIONABLE_CASES]
    failures = [result for result in results if result.outcome != "pass"]

    assert not failures, _format_results(results)


def test_safe_preflight_corpus_cases_do_not_hard_block() -> None:
    results = [_evaluate_case(case) for case in SAFE_CASES]
    failures = [result for result in results if result.outcome != "pass"]

    assert not failures, _format_safe_case_results(results)


@pytest.mark.parametrize(
    ("command", "facts_profile", "mode"),
    [
        ("apt-get install gnome-tweaks", "ubuntu", "configure_existing"),
        ("dnf install vlc", "fedora", "configure_existing"),
        ("sudo mkfs.ext4 /dev/sda1", "generic", "any"),
        ("sudo wipefs -a /dev/sda", "generic", "any"),
        ('cmd="sudo wipefs -a /dev/sda"; sh -c "$cmd"', "generic", "any"),
        (
            'python3 -c "import os; os.system(\\"sudo dd if=/dev/zero of=/dev/sda\\")"',
            "generic",
            "any",
        ),
        ('sh -c "sudo wipefs -a /dev/sda"', "generic", "any"),
    ],
)
def test_safe_false_positive_dangerous_counterparts_still_block(
    command: str,
    facts_profile: str,
    mode: str,
) -> None:
    case = {
        "id": "dangerous-counterpart",
        "command": command,
        "facts_profile": facts_profile,
        "mode": mode,
    }

    assert preflight_setup_command(command, _facts_for_case(case))


def test_preflight_command_corpus_has_no_manual_review_cases() -> None:
    assert MANUAL_REVIEW_CASES == []


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(case, id=case["id"])
        for case in CORPUS_CASES
        if case["expected"] == "allow_with_advisory"
        and (
            case.get("expected_capability_kind") == "external_shell_payload"
            or case["id"] == "obf-manual-review-generated-command"
        )
    ],
)
def test_external_payload_advisory_cases_are_tagged(case: dict[str, Any]) -> None:
    decisions = preflight_setup_decisions(case["command"], _facts_for_case(case))

    assert any("external_shell_payload_observed" in decision.reason for decision in decisions)
