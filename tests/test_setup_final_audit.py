from __future__ import annotations

from quickhatch.setup.audit import final_audit
from quickhatch.setup.types import (
    AuditFindingSeverity,
    AuditResult,
    FinalSetupStatus,
    PlanItemStatus,
    ResearchPlan,
    SetupFacts,
    SetupStage,
    StrategyDecision,
)


def _plan() -> ResearchPlan:
    return ResearchPlan(
        target_summary="test target",
        distro_family="arch",
        package_manager="pacman",
        driver_strategy=StrategyDecision(strategy="none"),
        desktop_strategy=StrategyDecision(strategy="none"),
        command_groups=[],
        apps=[],
        unresolved_questions=[],
        verification_commands={},
        required_items=[],
        optional_items=[],
    )


def test_final_audit_keeps_fstab_bootloader_findings_fatal() -> None:
    result = final_audit(
        SetupFacts(package_manager="pacman", current_user="jan"),
        _plan(),
        [
            AuditResult(
                stage=SetupStage.INSTALL_OR_SKIP,
                passed=False,
                blockers=["/etc/fstab missing root entry after fresh install."],
            )
        ],
    )

    assert result.final_status == FinalSetupStatus.BLOCKED
    assert result.blockers
    assert result.findings[0].severity == AuditFindingSeverity.FATAL
    assert result.findings[0].category == "fstab"


def test_final_audit_turns_optional_app_issues_into_manual_followup() -> None:
    result = final_audit(
        SetupFacts(package_manager="pacman", current_user="jan"),
        _plan(),
        [
            AuditResult(
                stage=SetupStage.APPS,
                passed=False,
                blockers=["Optional slow apps still need user confirmation: Codex"],
            )
        ],
    )

    assert result.passed is True
    assert result.blockers == []
    assert result.final_status == FinalSetupStatus.MANUAL_FOLLOWUP_REQUIRED
    assert result.findings[0].severity == AuditFindingSeverity.MANUAL_FOLLOWUP
    assert result.findings[0].category == "apps"


def test_final_audit_marks_secure_boot_nvidia_as_unsupported_safe() -> None:
    result = final_audit(
        SetupFacts(package_manager="pacman", current_user="jan"),
        _plan(),
        [
            AuditResult(
                stage=SetupStage.POSTBOOT_DRIVER,
                passed=False,
                blockers=[
                    "Secure Boot is enabled; NVIDIA modules need MOK signing or manual follow-up."
                ],
            )
        ],
    )

    assert result.passed is True
    assert result.final_status == FinalSetupStatus.UNSUPPORTED_SAFE
    assert result.findings[0].severity == AuditFindingSeverity.UNSUPPORTED_SAFE


def test_final_audit_blocks_unanswered_questions() -> None:
    facts = SetupFacts(package_manager="pacman", current_user="jan")
    facts.waiting_for_input = True

    result = final_audit(facts, _plan(), [])

    assert result.final_status == FinalSetupStatus.BLOCKED
    assert any(finding.category == "blocking_question" for finding in result.findings)


def test_final_audit_can_report_success_with_warnings() -> None:
    plan = _plan()
    plan.apps = []
    result = final_audit(
        SetupFacts(package_manager="pacman", current_user="jan"),
        plan,
        [
            AuditResult(
                stage=SetupStage.APPS,
                passed=True,
                blockers=[],
                details={"status": PlanItemStatus.EXPLICITLY_SKIPPED.value},
            )
        ],
    )

    assert result.final_status == FinalSetupStatus.SUCCESS
