from __future__ import annotations

from quickhatch.setup.audit import audit_stage, final_audit
from quickhatch.setup.types import (
    AppPlanItem,
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


def test_final_audit_keeps_generated_secure_boot_driver_text_nonfatal() -> None:
    result = final_audit(
        SetupFacts(package_manager="pacman", current_user="jan"),
        _plan(),
        [
            AuditResult(
                stage=SetupStage.POSTBOOT_DRIVER,
                passed=False,
                blockers=[
                    "Secure Boot is enabled; NVIDIA kernel modules may need MOK signing, "
                    "an enrolled key, or Secure Boot disabled before nvidia-smi can pass."
                ],
            )
        ],
    )

    assert result.passed is True
    assert result.final_status == FinalSetupStatus.UNSUPPORTED_SAFE
    assert result.findings[0].severity == AuditFindingSeverity.UNSUPPORTED_SAFE
    assert result.findings[0].category == "driver"


def test_final_audit_keeps_generated_nouveau_driver_text_nonfatal() -> None:
    result = final_audit(
        SetupFacts(package_manager="pacman", current_user="jan"),
        _plan(),
        [
            AuditResult(
                stage=SetupStage.POSTBOOT_DRIVER,
                passed=False,
                blockers=[
                    "NVIDIA GPU is still bound to nouveau; blacklist nouveau, "
                    "regenerate initramfs, reboot, then verify nvidia-smi."
                ],
            )
        ],
    )

    assert result.passed is True
    assert result.final_status == FinalSetupStatus.MANUAL_FOLLOWUP_REQUIRED
    assert result.findings[0].severity == AuditFindingSeverity.MANUAL_FOLLOWUP
    assert result.findings[0].category == "driver"


def test_final_audit_treats_nvidia_manual_group_as_manual_followup() -> None:
    result = final_audit(
        SetupFacts(package_manager="pacman", current_user="jan"),
        _plan(),
        [
            AuditResult(
                stage=SetupStage.FINAL_AUDIT,
                passed=False,
                blockers=["Manual follow-up setup item(s) remain: NVIDIA Driver"],
            )
        ],
    )

    assert result.passed is True
    assert result.final_status == FinalSetupStatus.MANUAL_FOLLOWUP_REQUIRED
    assert result.findings[0].severity == AuditFindingSeverity.MANUAL_FOLLOWUP
    assert result.findings[0].category == "driver"


def test_final_audit_preserves_nvidia_driver_manual_followup() -> None:
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        current_user="jan",
        gpu_vendors=["nvidia"],
        gpu_inventory=["NVIDIA GeForce GTX 1080"],
        nvidia_smi_state="missing",
    )
    plan = _plan()
    plan.driver_strategy = StrategyDecision(
        strategy="nvidia_legacy_manual_followup",
        confidence="manual_followup_required",
        source="manual_followup",
        packages=[],
        notes=(
            "Detected pre-Turing NVIDIA hardware. Resolve the current legacy/proprietary "
            "package path before installing."
        ),
    )
    postboot = audit_stage(SetupStage.POSTBOOT_DRIVER, facts, plan=plan)

    result = final_audit(facts, plan, [postboot])

    assert postboot.passed is True
    assert result.passed is True
    assert result.final_status == FinalSetupStatus.MANUAL_FOLLOWUP_REQUIRED
    assert result.blockers == []
    assert result.findings[0].severity == AuditFindingSeverity.MANUAL_FOLLOWUP
    assert result.findings[0].category == "driver"
    assert "NVIDIA driver strategy requires explicit user follow-up" in result.findings[0].summary


def test_final_audit_blocks_unanswered_questions() -> None:
    facts = SetupFacts(package_manager="pacman", current_user="jan")
    facts.waiting_for_input = True

    result = final_audit(facts, _plan(), [])

    assert result.final_status == FinalSetupStatus.BLOCKED
    assert any(finding.category == "blocking_question" for finding in result.findings)


def test_final_audit_blocks_live_environment_success() -> None:
    result = final_audit(
        SetupFacts(package_manager="pacman", current_user="root", is_live=True),
        _plan(),
        [
            AuditResult(stage=SetupStage.INSTALL_OR_SKIP, passed=True),
            AuditResult(stage=SetupStage.POSTBOOT_DRIVER, passed=True),
        ],
    )

    assert result.passed is False
    assert result.final_status == FinalSetupStatus.BLOCKED
    assert any("live environment" in blocker for blocker in result.blockers)


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


def test_final_audit_plan_manual_followup_items_change_status() -> None:
    plan = _plan()
    plan.apps = [
        AppPlanItem(
            name="Spotify",
            status=PlanItemStatus.MANUAL_FOLLOWUP,
            reason="Package unavailable on this distro.",
        )
    ]

    result = final_audit(
        SetupFacts(package_manager="pacman", current_user="jan"),
        plan,
        [],
    )

    assert result.passed is True
    assert result.final_status == FinalSetupStatus.MANUAL_FOLLOWUP_REQUIRED
    assert any(
        finding.severity == AuditFindingSeverity.MANUAL_FOLLOWUP and finding.category == "apps"
        for finding in result.findings
    )


def test_final_audit_required_failed_app_becomes_manual_followup() -> None:
    plan = _plan()
    plan.apps = [
        AppPlanItem(
            name="Required Tool",
            required=True,
            status=PlanItemStatus.FAILED,
            reason="Required package unavailable.",
        )
    ]

    result = final_audit(
        SetupFacts(package_manager="pacman", current_user="jan"),
        plan,
        [],
    )

    assert result.passed is True
    assert result.final_status == FinalSetupStatus.MANUAL_FOLLOWUP_REQUIRED
    assert any(
        finding.severity == AuditFindingSeverity.MANUAL_FOLLOWUP for finding in result.findings
    )


def test_final_audit_alternative_installed_is_warning() -> None:
    plan = _plan()
    plan.apps = [
        AppPlanItem(
            name="Adobe Acrobat Reader",
            package_name="okular",
            source="alternative",
            required=True,
            status=PlanItemStatus.INSTALLED,
        )
    ]
    result = audit_stage(
        SetupStage.FINAL_AUDIT,
        SetupFacts(package_manager="pacman", current_user="jan"),
        plan=plan,
        stage_state={
            "verified_alternative_apps": [
                {
                    "app": "Adobe Acrobat Reader",
                    "alternative": "okular",
                    "signal": "native_package",
                    "target": "okular",
                }
            ]
        },
    )

    assert result.final_status == FinalSetupStatus.SUCCESS_WITH_WARNINGS
    assert result.findings[0].severity == AuditFindingSeverity.WARNING


def test_final_audit_status_matrix_preserves_all_terminal_states() -> None:
    success = final_audit(
        SetupFacts(package_manager="pacman", current_user="jan"),
        _plan(),
        [],
    )
    warnings = audit_stage(
        SetupStage.FINAL_AUDIT,
        SetupFacts(package_manager="pacman", current_user="jan"),
        plan=_plan(),
        stage_state={
            "verified_alternative_apps": [{"app": "Adobe Acrobat Reader", "alternative": "okular"}]
        },
    )
    manual = final_audit(
        SetupFacts(package_manager="pacman", current_user="jan"),
        _plan(),
        [
            AuditResult(
                stage=SetupStage.APPS,
                passed=False,
                blockers=["Manual follow-up app(s) remain: Brave"],
            )
        ],
    )
    unsupported = final_audit(
        SetupFacts(package_manager="pacman", current_user="jan"),
        _plan(),
        [
            AuditResult(
                stage=SetupStage.POSTBOOT_DRIVER,
                passed=False,
                blockers=["Secure Boot is enabled; NVIDIA modules need manual follow-up."],
            )
        ],
    )
    blocked = final_audit(
        SetupFacts(package_manager="pacman", current_user="root", is_live=True),
        _plan(),
        [],
    )

    assert success.final_status == FinalSetupStatus.SUCCESS
    assert warnings.final_status == FinalSetupStatus.SUCCESS_WITH_WARNINGS
    assert manual.final_status == FinalSetupStatus.MANUAL_FOLLOWUP_REQUIRED
    assert unsupported.final_status == FinalSetupStatus.UNSUPPORTED_SAFE
    assert blocked.final_status == FinalSetupStatus.BLOCKED
    assert blocked.passed is False
