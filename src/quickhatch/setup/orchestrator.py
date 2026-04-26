"""Stage-driven remote setup orchestration."""

from __future__ import annotations

import copy
import json
import re
import shlex
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from quickhatch.bridge.server import BridgeState
from quickhatch.config import AIConfig, UserProfile

from .audit import audit_stage, final_audit
from .facts import describe_fact_changes
from .planner import (
    RepairActionValidationError,
    ResearchPlanValidationError,
    build_repair_prompt,
    build_research_prompt,
    extract_repair_action,
    extract_research_plan,
)
from .preflight import preflight_setup_command
from .runlog import SetupRunLogger
from .types import (
    AuditResult,
    CommandGroup,
    DesktopRuntimeState,
    PlanItemStatus,
    ResearchPlan,
    SetupFacts,
    SetupStage,
)


@dataclass
class SetupRuntime:
    profile: UserProfile
    bridge: BridgeState
    config: AIConfig
    host: str
    user: str
    ssh_port: int
    key_path: str | None
    system_info_fn: Callable[[], str]
    call_model_text: Callable[[AIConfig, list[dict[str, str]]], str]
    probe_facts: Callable[..., SetupFacts]
    detect_live_env: Callable[[], tuple[bool, str]]
    ssh_exec: Callable[[str, int], tuple[int, str, str]]
    repo_search: Callable[[str], str]
    community_search: Callable[[str], str]
    web_search: Callable[[str, int], str]
    wait_for_reconnect: Callable[[str], tuple[bool, str, str]]
    run_pre_reboot_checks: Callable[[], tuple[bool, list[str], str]]
    smoke_test_preboot_script: Callable[[], str | None]
    bootstrap_nopasswd_sudo: Callable[[SetupFacts], SetupFacts]
    runtime_prompt_block: Callable[[SetupFacts, bool], str]
    migration_plan_md: str = ""
    distro_primer_md: str = ""
    target_distro_family: str = ""
    native_search_active: bool = False
    results_log_limit: int = 100


@dataclass
class SetupExecutionContext:
    run_id: str
    facts: SetupFacts | None = None
    system_info: str = ""
    plan: ResearchPlan | None = None
    stage_results: list[AuditResult] = field(default_factory=list)
    stage_state: dict[str, dict[str, Any]] = field(default_factory=dict)
    results_log: list[str] = field(default_factory=list)
    created_user: str | None = None


@dataclass
class GroupFailure:
    failure_type: str
    command: str
    stdout: str
    stderr: str
    detail: str


class SetupOrchestrator:
    """Execute staged remote setup with typed plans and audits."""

    MAX_GROUP_REPAIRS = 2
    MAX_REPAIR_TURNS = 8

    def __init__(self, runtime: SetupRuntime) -> None:
        self.runtime = runtime
        self.bridge = runtime.bridge
        self.profile = runtime.profile
        self.ctx = SetupExecutionContext(run_id=str(uuid.uuid4())[:8])
        self.runlog = SetupRunLogger(self.ctx.run_id)

    def run(self) -> str:
        self._start_run_state()
        try:
            try:
                self._run_connect_and_probe()
                self._run_research_and_plan()
                self._run_install_or_skip()
                self._run_postboot_driver()
                self._run_desktop()
                self._run_apps()
                final = self._run_final_audit()
                if not final.passed:
                    summary = "Setup stopped during final audit: " + "; ".join(final.blockers)
                    self.bridge.add_message("agent", f"⚠️ {summary}")
                    return summary
                summary = self._build_summary()
                self.bridge.add_message("agent", f"Finished: {summary}")
                return summary
            except RuntimeError as exc:
                stopped = self.bridge.stop_requested
                summary = str(exc) if stopped else f"Setup blocked: {exc}"
                if stopped and not summary:
                    summary = "Agent stopped by user."
                self.bridge.update_setup_run(
                    status="stopped" if stopped else "blocked",
                    waiting_for_input=False,
                    active_question_id="",
                    blockers=[] if stopped else [str(exc)],
                )
                self.bridge.add_message(
                    "agent",
                    f"{'Stopped.' if stopped else '⚠️ ' + summary}",
                )
                return summary
        finally:
            self.bridge.update_setup_run(waiting_for_input=False, active_question_id="")

    def _start_run_state(self) -> None:
        self.bridge.update_setup_run(
            run_id=self.ctx.run_id,
            stage=SetupStage.CONNECT_AND_PROBE.value,
            status="running",
            blockers=[],
            waiting_for_input=False,
            active_question_id="",
            planning_complete=False,
        )
        self.runlog.record("run_started", host=self.runtime.host, user=self.runtime.user)

    def _record_stage_result(self, result: AuditResult) -> None:
        self.ctx.stage_results.append(result)
        self.bridge.update_setup_run(
            blockers=list(result.blockers),
            stage=result.stage.value,
            status="blocked" if not result.passed else "running",
        )
        self.runlog.record("stage_audit", stage=result.stage.value, result=asdict(result))
        if not result.passed:
            raise RuntimeError("; ".join(result.blockers))

    def _refresh_facts(self, *, stage: SetupStage) -> SetupFacts:
        before = self.ctx.facts
        is_live = self.ctx.facts.is_live if self.ctx.facts else False
        live_marker = self.ctx.facts.live_marker if self.ctx.facts else ""
        try:
            is_live, live_marker = self.runtime.detect_live_env()
        except Exception as exc:
            self.runlog.record(
                "live_env_refresh_failed",
                stage=stage.value,
                error=str(exc),
            )
        self.ctx.facts = self.runtime.probe_facts(
            fallback=self.ctx.facts,
            is_live=is_live,
            live_marker=live_marker,
            pending_question_id=self.bridge.snapshot_setup_run().get("active_question_id", ""),
            pending_question_blocking=self.bridge.snapshot_setup_run().get(
                "waiting_for_input", False
            ),
        )
        change_text = describe_fact_changes(before, self.ctx.facts)
        if change_text:
            self.bridge.add_message("agent", f"Refreshed setup facts: {change_text}")
        self.runlog.record("facts_refresh", stage=stage.value, facts=asdict(self.ctx.facts))
        return self.ctx.facts

    def _run_connect_and_probe(self) -> None:
        self.bridge.update_setup_run(stage=SetupStage.CONNECT_AND_PROBE.value, status="running")
        self.bridge.add_message(
            "agent",
            f"Connecting to {self.runtime.user}@{self.runtime.host}:{self.runtime.ssh_port}…",
        )
        rc, out, err = self.runtime.ssh_exec("echo 'QuickHatch connected!' && uname -a", 60)
        if rc != 0:
            raise RuntimeError(f"SSH connection failed: {err.strip()}")
        self.bridge.add_message("agent", f"Connected — {out.strip()}")
        preboot_err = self.runtime.smoke_test_preboot_script()
        if preboot_err:
            self.bridge.add_message("agent", f"⚠️ Pre-reboot guard self-test failed: {preboot_err}")
        else:
            self.bridge.add_message(
                "agent",
                "✓ Pre-reboot guard self-test passed — checks will run correctly at reboot time.",
            )
        self.ctx.system_info = self.runtime.system_info_fn()
        is_live, live_marker = self.runtime.detect_live_env()
        self.ctx.facts = self.runtime.probe_facts(
            fallback=None,
            is_live=is_live,
            live_marker=live_marker,
            pending_question_id="",
            pending_question_blocking=False,
        )
        if not is_live:
            self.ctx.facts = self.runtime.bootstrap_nopasswd_sudo(self.ctx.facts)
        self.runlog.record(
            "connect_and_probe",
            system_info=self.ctx.system_info,
            facts=asdict(self.ctx.facts),
        )
        result = audit_stage(SetupStage.CONNECT_AND_PROBE, self.ctx.facts)
        self._record_stage_result(result)

    def _planner_model_call(self, messages: list[dict[str, str]]) -> str:
        planning_config = copy.copy(self.runtime.config)
        planning_config.native_search = self.runtime.native_search_active
        return self.runtime.call_model_text(planning_config, messages)

    def _parse_planner_json(self, text: str) -> dict[str, Any]:
        raw = (text or "").strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(lines[1:-1] if len(lines) >= 2 else lines)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise RuntimeError("Planner response must be a single JSON object.")
        return data

    def _run_research_and_plan(self) -> None:
        self.bridge.update_setup_run(stage=SetupStage.RESEARCH_AND_PLAN.value, status="running")
        self.bridge.add_message(
            "agent",
            "Researching package names, driver strategy, and stage plan before running commands…",
        )
        facts = self.ctx.facts or SetupFacts()
        runtime_guardrails = self.runtime.runtime_prompt_block(facts, facts.is_live)
        prompt = build_research_prompt(
            self.profile,
            facts,
            system_info=self.ctx.system_info,
            migration_plan_md=self.runtime.migration_plan_md,
            distro_primer_md=(
                self.runtime.distro_primer_md
                + (
                    "\n\n== SETUP RUNTIME GUARDRAILS ==\n" + runtime_guardrails
                    if runtime_guardrails
                    else ""
                )
            ),
            native_search_active=self.runtime.native_search_active,
            target_distro_family=self.runtime.target_distro_family,
        )
        self.runlog.record(
            "planner_prompt", stage=SetupStage.RESEARCH_AND_PLAN.value, prompt=prompt
        )
        if self.runtime.native_search_active:
            response_text = self._planner_model_call([{"role": "user", "content": prompt}])
            self.runlog.record(
                "planner_response", stage=SetupStage.RESEARCH_AND_PLAN.value, text=response_text
            )
            try:
                self.ctx.plan = extract_research_plan(response_text, self.profile)
            except ResearchPlanValidationError as exc:
                raise RuntimeError(f"Research plan validation failed: {exc}") from exc
        else:
            messages = [{"role": "user", "content": prompt}]
            self.ctx.plan = None
            for _ in range(8):
                response_text = self._planner_model_call(messages)
                self.runlog.record(
                    "planner_response",
                    stage=SetupStage.RESEARCH_AND_PLAN.value,
                    text=response_text,
                )
                try:
                    payload = self._parse_planner_json(response_text)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"Planner returned invalid JSON: {exc}") from exc
                action = str(payload.get("action", "")).strip()
                if not action:
                    self.ctx.plan = extract_research_plan(response_text, self.profile)
                    break
                if action == "plan":
                    self.ctx.plan = extract_research_plan(
                        json.dumps(payload.get("plan", {})), self.profile
                    )
                    break
                if action == "repo_search":
                    result = self.runtime.repo_search(str(payload.get("query", "")).strip())
                elif action == "community_search":
                    result = self.runtime.community_search(str(payload.get("query", "")).strip())
                elif action == "web_search":
                    result = self.runtime.web_search(
                        str(payload.get("query", "")).strip(),
                        int(payload.get("max_results", 5) or 5),
                    )
                elif action == "question":
                    answer = self._ask_blocking_question(
                        str(payload.get("question", "")).strip(),
                        choices=payload.get("choices"),
                        stage=SetupStage.RESEARCH_AND_PLAN,
                        context={"planner_question": True},
                        timeout=float(payload.get("timeout_seconds", 600) or 600),
                    )
                    result = f"[question answer]\n{answer or 'no answer'}"
                else:
                    raise RuntimeError(f"Unsupported planner action: {action}")
                messages = [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": response_text},
                    {"role": "user", "content": result},
                ]
            if self.ctx.plan is None:
                raise RuntimeError("Planner did not produce a final research plan.")
        self.bridge.update_setup_run(planning_complete=True)
        for question in self.ctx.plan.unresolved_questions:
            if not bool(question.get("required", True)):
                continue
            answer = self._ask_blocking_question(
                str(question.get("question", "")).strip(),
                choices=question.get("choices"),
                stage=SetupStage.RESEARCH_AND_PLAN,
                context={"question": question},
                timeout=float(question.get("timeout_seconds", 600) or 600),
            )
            question["answer"] = answer
        result = audit_stage(
            SetupStage.RESEARCH_AND_PLAN,
            self.ctx.facts or SetupFacts(),
            plan=self.ctx.plan,
        )
        self._record_stage_result(result)

    def _iter_stage_groups(self, stage: SetupStage) -> list[CommandGroup]:
        if not self.ctx.plan:
            return []
        return [group for group in self.ctx.plan.command_groups if group.stage == stage]

    def _run_stage_groups(self, stage: SetupStage, *, audit_after: bool = True) -> None:
        if self.ctx.facts is None or self.ctx.plan is None:
            raise RuntimeError("Setup orchestration missing facts or plan.")
        self.bridge.update_setup_run(stage=stage.value, status="running")
        stage_state = self.ctx.stage_state.setdefault(stage.value, {})
        for group in self._iter_stage_groups(stage):
            if self.bridge.stop_requested:
                raise RuntimeError("Agent stopped by user.")
            current_group = group
            repair_attempts = 0
            while True:
                if current_group.optional_slow:
                    answer = self._ask_optional_slow_confirmation(current_group)
                    if answer is None:
                        current_group.status = PlanItemStatus.MANUAL_FOLLOWUP
                        self._mark_optional_apps_manual_followup(current_group)
                        self.runlog.record(
                            "group_manual_followup",
                            stage=stage.value,
                            group_id=current_group.id,
                            reason="User skipped optional slow install during setup.",
                        )
                        break
                failure = self._execute_group(stage, current_group, stage_state)
                if failure is None:
                    current_group.status = PlanItemStatus.COMPLETED
                    self._mark_group_completion(current_group, stage=stage)
                    self._refresh_facts(stage=stage)
                    break
                repair_attempts += 1
                if repair_attempts > self.MAX_GROUP_REPAIRS:
                    raise RuntimeError(
                        f"Stage {stage.value} exhausted repair attempts for '{current_group.title}': {failure.detail}"
                    )
                current_group.status = PlanItemStatus.FAILED
                repair_outcome = self._repair_failed_group(
                    stage,
                    current_group,
                    failure,
                    repair_attempt=repair_attempts,
                )
                action = str(repair_outcome.get("action", ""))
                if action == "retry_group":
                    current_group = repair_outcome["group"]
                    continue
                if action == "manual_followup":
                    current_group.status = PlanItemStatus.MANUAL_FOLLOWUP
                    reason = str(repair_outcome.get("reason", "") or failure.detail)
                    self._mark_group_manual_followup(
                        current_group,
                        stage=stage,
                        reason=reason,
                        app_names=list(repair_outcome.get("app_names", []) or []),
                    )
                    self.runlog.record(
                        "group_manual_followup",
                        stage=stage.value,
                        group_id=current_group.id,
                        reason=reason,
                    )
                    break
                raise RuntimeError(
                    f"Unsupported repair outcome for {current_group.title}: {action}"
                )
        if audit_after:
            result = audit_stage(stage, self.ctx.facts, plan=self.ctx.plan, stage_state=stage_state)
            self._record_stage_result(result)

    def _run_install_or_skip(self) -> None:
        if self.ctx.facts and not self.ctx.facts.is_live:
            self.ctx.stage_state[SetupStage.INSTALL_OR_SKIP.value] = {
                "pre_reboot_checks_passed": True,
                "reconnected_after_install": True,
                "skipped": True,
            }
            result = audit_stage(
                SetupStage.INSTALL_OR_SKIP,
                self.ctx.facts,
                plan=self.ctx.plan,
                stage_state=self.ctx.stage_state[SetupStage.INSTALL_OR_SKIP.value],
            )
            self._record_stage_result(result)
            return
        self._run_stage_groups(SetupStage.INSTALL_OR_SKIP, audit_after=False)
        ok, failures, summary = self.runtime.run_pre_reboot_checks()
        self.ctx.stage_state.setdefault(SetupStage.INSTALL_OR_SKIP.value, {})[
            "pre_reboot_checks_passed"
        ] = ok
        self.runlog.record(
            "pre_reboot_checks",
            passed=ok,
            failures=failures,
            summary=summary,
        )
        if not ok:
            raise RuntimeError(summary or "; ".join(failures))
        self._issue_install_reboot()
        reconnect_user = self.ctx.created_user or self.runtime.user
        reconnected, fresh_info, new_user = self.runtime.wait_for_reconnect(reconnect_user)
        self.ctx.stage_state.setdefault(SetupStage.INSTALL_OR_SKIP.value, {})[
            "reconnected_after_install"
        ] = reconnected
        if not reconnected:
            raise RuntimeError(fresh_info or "Target did not reconnect after install reboot.")
        if new_user:
            self.runtime.user = new_user
        self.ctx.system_info = fresh_info
        self._refresh_facts(stage=SetupStage.INSTALL_OR_SKIP)
        if self.ctx.facts and self.ctx.facts.is_live:
            raise RuntimeError(
                "Target reconnected, but it is still reporting a live environment after install reboot."
            )
        result = audit_stage(
            SetupStage.INSTALL_OR_SKIP,
            self.ctx.facts,
            plan=self.ctx.plan,
            stage_state=self.ctx.stage_state[SetupStage.INSTALL_OR_SKIP.value],
        )
        self._record_stage_result(result)

    def _issue_install_reboot(self) -> None:
        command = (
            "(umount -R /mnt 2>/dev/null || true); (umount -R /target 2>/dev/null || true); reboot"
        )
        self.runlog.record(
            "install_reboot",
            stage=SetupStage.INSTALL_OR_SKIP.value,
            command=command,
        )
        rc, stdout, stderr = self.runtime.ssh_exec(command, 60)
        self.runlog.record(
            "install_reboot_result",
            stage=SetupStage.INSTALL_OR_SKIP.value,
            rc=rc,
            stdout_tail=(stdout or "")[-500:],
            stderr_tail=(stderr or "")[-500:],
        )

    def _run_postboot_driver(self) -> None:
        self._run_stage_groups(SetupStage.POSTBOOT_DRIVER)

    def _run_desktop(self) -> None:
        self._run_stage_groups(SetupStage.DESKTOP)

    def _run_apps(self) -> None:
        self._run_stage_groups(SetupStage.APPS)

    def _run_final_audit(self) -> AuditResult:
        self.bridge.update_setup_run(stage=SetupStage.FINAL_AUDIT.value, status="running")
        if self.ctx.facts is not None:
            self._refresh_facts(stage=SetupStage.FINAL_AUDIT)
        if self.ctx.plan is None:
            result = AuditResult(
                stage=SetupStage.FINAL_AUDIT,
                passed=False,
                blockers=["No research plan exists for final audit."],
            )
        else:
            result = final_audit(
                self.ctx.facts or SetupFacts(), self.ctx.plan, self.ctx.stage_results
            )
            remote_blockers = self._remote_final_verification()
            if remote_blockers:
                result.blockers.extend(remote_blockers)
                result.passed = False
                result.details["remote_final_verification"] = remote_blockers
        self.ctx.stage_results.append(result)
        self.runlog.record("final_audit", result=asdict(result))
        self.bridge.update_setup_run(
            blockers=list(result.blockers),
            status="blocked" if not result.passed else "done",
            stage=SetupStage.FINAL_AUDIT.value,
        )
        return result

    def _remote_final_verification(self) -> list[str]:
        if self.ctx.plan is None or self.ctx.facts is None:
            return []
        blockers: list[str] = []
        blockers.extend(self._verify_planned_packages_installed())
        blockers.extend(self._verify_systemd_health())
        return blockers

    def _verify_planned_packages_installed(self) -> list[str]:
        if self.ctx.plan is None or self.ctx.facts is None:
            return []
        package_sources = {
            "repo",
            "official",
            "official-repo",
            "community",
            "aur",
            "aur-binary",
            "aur-source",
        }
        candidates = [
            app
            for app in self.ctx.plan.apps
            if app.status in {PlanItemStatus.INSTALLED, PlanItemStatus.COMPLETED}
            and (app.source or "").lower() in package_sources
        ]
        if not candidates:
            return []
        query_cmds = {
            "pacman": "pacman -Qq",
            "apt": "dpkg-query -W -f='${Package}\\n'",
            "dnf": "rpm -qa --queryformat '%{NAME}\\n'",
            "zypper": "rpm -qa --queryformat '%{NAME}\\n'",
            "apk": "apk info",
            "xbps": "xbps-query -m | sed 's/-[0-9][^-]*$//'",
            "emerge": "qlist -IC 2>/dev/null || true",
        }
        query = query_cmds.get(self.ctx.facts.package_manager)
        if not query:
            return []
        rc, stdout, stderr = self.runtime.ssh_exec(query, 45)
        self.runlog.record(
            "final_package_verification",
            package_manager=self.ctx.facts.package_manager,
            command=query,
            rc=rc,
            stdout_tail=(stdout or "")[-800:],
            stderr_tail=(stderr or "")[-800:],
        )
        if rc != 0:
            return [
                "Could not verify installed packages with "
                f"{self.ctx.facts.package_manager}: {(stderr or stdout or '').strip()[:200]}"
            ]
        installed = {line.strip().lower() for line in (stdout or "").splitlines() if line.strip()}
        missing: list[str] = []
        for app in candidates:
            variants = self._package_name_variants(app.name, app.package_name)
            if not variants:
                missing.append(app.name)
                continue
            if not any(variant in installed for variant in variants):
                missing.append(app.name)
        if not missing:
            return []
        return [
            "Final package verification failed for installed app(s): "
            + ", ".join(sorted(missing))
            + ". Re-check exact package names or mark them manual_followup before done."
        ]

    @staticmethod
    def _package_name_variants(app_name: str, package_name: str) -> set[str]:
        raw = {app_name, package_name}
        variants: set[str] = set()
        for value in raw:
            normalized = (value or "").strip().lower()
            if not normalized:
                continue
            dashed = normalized.replace(" ", "-")
            compact = normalized.replace(" ", "")
            variants.update(
                {
                    normalized,
                    dashed,
                    compact,
                    f"{dashed}-bin",
                    f"{dashed}-git",
                }
            )
        return {variant for variant in variants if variant}

    def _verify_systemd_health(self) -> list[str]:
        if self.ctx.facts is None or self.ctx.facts.init_system != "systemd":
            return []
        rc, stdout, stderr = self.runtime.ssh_exec(
            "systemctl --failed --no-legend --plain 2>/dev/null | awk '{print $1}' | head -20",
            20,
        )
        self.runlog.record(
            "final_systemd_verification",
            rc=rc,
            stdout_tail=(stdout or "")[-800:],
            stderr_tail=(stderr or "")[-800:],
        )
        if rc != 0:
            return []
        ignore_failed = {"systemd-modules-load.service", "systemd-resolved.service"}
        failed_units = [
            line.strip()
            for line in (stdout or "").splitlines()
            if line.strip() and line.strip() not in ignore_failed
        ]
        if not failed_units:
            return []
        return [
            "systemctl reports failed unit(s): "
            + ", ".join(failed_units)
            + ". Investigate them before declaring setup complete."
        ]

    def _ask_optional_slow_confirmation(self, group: CommandGroup) -> str | None:
        qid = self._ask_blocking_question(
            f"Install slow optional group '{group.title}' now?",
            choices=["install now", "manual follow-up"],
            stage=group.stage,
            context={"group_id": group.id, "optional_slow": True},
            timeout=300,
            return_qid=False,
        )
        if not qid:
            return None
        if qid.strip().lower() not in {"install now", "yes", "y"}:
            return None
        return qid

    def _ask_blocking_question(
        self,
        question: str,
        *,
        choices: list[str] | None,
        stage: SetupStage,
        context: dict[str, Any],
        timeout: float,
        return_qid: bool = False,
    ) -> str | None:
        pending = self.bridge.add_question(
            question,
            choices,
            blocking=True,
            phase="setup",
            context={"stage": stage.value, **context},
        )
        self.bridge.update_setup_run(
            stage=stage.value,
            status="waiting_for_input",
            waiting_for_input=True,
            active_question_id=pending.id,
            blockers=[question],
        )
        self.runlog.record(
            "blocking_question",
            stage=stage.value,
            question_id=pending.id,
            question=question,
            choices=choices,
        )
        deadline = time.monotonic() + max(0.0, timeout)
        answered = False
        while time.monotonic() < deadline:
            if pending.answered.wait(timeout=min(2.0, max(0.0, deadline - time.monotonic()))):
                answered = True
                break
            if self.bridge.stop_requested:
                break
        if self.bridge.stop_requested and not answered:
            self.bridge.answer_question(pending.id, "[stopped before answer]")
            self.bridge.update_setup_run(
                waiting_for_input=False,
                active_question_id="",
                blockers=[],
                status="stopped",
            )
            self.runlog.record(
                "blocking_question_stopped", stage=stage.value, question_id=pending.id
            )
            raise RuntimeError("Agent stopped by user while waiting for an answer.")
        if not answered:
            self.bridge.answer_question(pending.id, "[timed out waiting for answer]")
            self.bridge.update_setup_run(
                waiting_for_input=False,
                active_question_id="",
                blockers=[],
                status="running",
            )
            self.runlog.record(
                "blocking_question_timeout", stage=stage.value, question_id=pending.id
            )
            return None
        self.bridge.update_setup_run(
            waiting_for_input=False,
            active_question_id="",
            blockers=[],
            status="running",
        )
        self.runlog.record(
            "blocking_question_answer",
            stage=stage.value,
            question_id=pending.id,
            answer=pending.answer,
        )
        return pending.id if return_qid else (pending.answer or "")

    def _execute_group(
        self,
        stage: SetupStage,
        group: CommandGroup,
        stage_state: dict[str, Any],
    ) -> GroupFailure | None:
        group.status = PlanItemStatus.IN_PROGRESS
        stage_state["failed_verifications"] = []
        self.bridge.add_message("agent", f"[{stage.value}] {group.title}")
        self.runlog.record(
            "command_group_started",
            stage=stage.value,
            group=asdict(group),
        )
        for command in group.commands:
            if stage == SetupStage.INSTALL_OR_SKIP and self._is_reboot_command(command):
                stage_state["deferred_reboot_command"] = command
                self.runlog.record(
                    "command_deferred_until_guarded_reboot",
                    stage=stage.value,
                    group_id=group.id,
                    command=command,
                )
                continue
            lint_error = preflight_setup_command(command, self.ctx.facts or SetupFacts())
            if lint_error:
                self.bridge.add_message(
                    "agent",
                    f"[harness] 🛑 Command blocked: `{command}`\n{lint_error}",
                )
                self.runlog.record(
                    "command_preflight_blocked",
                    stage=stage.value,
                    group_id=group.id,
                    command=command,
                    reason=lint_error,
                )
                return GroupFailure(
                    failure_type="preflight",
                    command=command,
                    stdout="",
                    stderr="",
                    detail=lint_error,
                )
            rc, stdout, stderr = self.runtime.ssh_exec(command, self._command_timeout(command))
            self.ctx.results_log.append(f"$ {command}\n[rc={rc}]\n{stdout}\n{stderr}".strip())
            self.ctx.results_log[:] = self.ctx.results_log[-self.runtime.results_log_limit :]
            self.runlog.record(
                "command",
                stage=stage.value,
                group_id=group.id,
                command=command,
                rc=rc,
                stdout_tail=(stdout or "")[-800:],
                stderr_tail=(stderr or "")[-800:],
            )
            if rc != 0:
                stage_state.setdefault("failed_commands", []).append(command)
                detail = stderr.strip() or stdout.strip() or "command failed"
                self.bridge.add_message(
                    "agent",
                    self._format_group_failure_message(
                        group.title,
                        "Command failed",
                        command,
                        rc,
                        stdout,
                        stderr,
                    ),
                )
                return GroupFailure(
                    failure_type="command",
                    command=command,
                    stdout=stdout,
                    stderr=stderr,
                    detail=detail,
                )
            self._update_from_command(command, stdout, stderr, stage=stage)
        for verification in group.verification:
            rc, stdout, stderr = self.runtime.ssh_exec(verification, 120)
            self.runlog.record(
                "verification",
                stage=stage.value,
                group_id=group.id,
                command=verification,
                rc=rc,
                stdout_tail=(stdout or "")[-500:],
                stderr_tail=(stderr or "")[-500:],
            )
            if rc != 0:
                detail = (
                    f"Verification failed for {group.title}: {verification} "
                    f"(rc={rc}; {(stderr or stdout or '').strip()[:200]})"
                )
                stage_state.setdefault("failed_verifications", []).append(detail)
                self.bridge.add_message(
                    "agent",
                    self._format_group_failure_message(
                        group.title,
                        "Verification failed",
                        verification,
                        rc,
                        stdout,
                        stderr,
                    ),
                )
                return GroupFailure(
                    failure_type="verification",
                    command=verification,
                    stdout=stdout,
                    stderr=stderr,
                    detail=detail,
                )
        return None

    @staticmethod
    def _format_group_failure_message(
        title: str,
        label: str,
        command: str,
        rc: int,
        stdout: str,
        stderr: str,
    ) -> str:
        tail = (stderr or stdout or "").strip()
        if len(tail) > 500:
            tail = tail[-500:]
        if not tail:
            tail = "No output."
        return f"⚠️ {label} in '{title}' (rc={rc}): `{command}`\n{tail}"

    def _repair_model_call(self, messages: list[dict[str, str]]) -> str:
        repair_config = copy.copy(self.runtime.config)
        repair_config.native_search = False
        return self.runtime.call_model_text(repair_config, messages)

    def _repair_search_result(self, action: dict[str, Any]) -> str:
        query = str(action.get("query", "")).strip()
        name = str(action.get("action", ""))
        if name == "repo_search":
            result = self.runtime.repo_search(query)
        elif name == "community_search":
            result = self.runtime.community_search(query)
        elif name == "web_search":
            result = self.runtime.web_search(query, int(action.get("max_results", 5) or 5))
        else:
            raise RuntimeError(f"Unsupported repair search action: {name}")
        return f"[{name}]\nquery={query}\n{result}"

    def _repair_failed_group(
        self,
        stage: SetupStage,
        failed_group: CommandGroup,
        failure: GroupFailure,
        *,
        repair_attempt: int,
    ) -> dict[str, Any]:
        prompt = build_repair_prompt(
            stage=stage,
            failed_group=failed_group,
            failure_type=failure.failure_type,
            failed_command=failure.command,
            stdout_tail=failure.stdout,
            stderr_tail=failure.stderr,
            facts=self.ctx.facts or SetupFacts(),
            recent_results=self.ctx.results_log,
        )
        self.runlog.record(
            "repair_prompt",
            stage=stage.value,
            group_id=failed_group.id,
            repair_attempt=repair_attempt,
            prompt=prompt,
        )
        messages = [{"role": "user", "content": prompt}]
        for turn in range(1, self.MAX_REPAIR_TURNS + 1):
            response_text = self._repair_model_call(messages)
            self.runlog.record(
                "repair_response",
                stage=stage.value,
                group_id=failed_group.id,
                repair_attempt=repair_attempt,
                repair_turn=turn,
                text=response_text,
            )
            try:
                action = extract_repair_action(
                    response_text,
                    fallback_stage=stage,
                    failed_group=failed_group,
                )
            except RepairActionValidationError as exc:
                self.runlog.record(
                    "repair_response_invalid",
                    stage=stage.value,
                    group_id=failed_group.id,
                    repair_attempt=repair_attempt,
                    repair_turn=turn,
                    error=str(exc),
                )
                messages = [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": response_text},
                    {
                        "role": "user",
                        "content": (
                            "[quickhatch repair parser]\n"
                            f"Your previous repair response was invalid JSON: {exc}\n"
                            "Return exactly one complete JSON object. Do not use Markdown fences, "
                            "comments, prose, or truncated strings. Use action retry_group, "
                            "repo_search, community_search, web_search, question, or manual_followup."
                        ),
                    },
                ]
                continue
            self.runlog.record(
                "repair_action",
                stage=stage.value,
                group_id=failed_group.id,
                repair_attempt=repair_attempt,
                repair_turn=turn,
                action=action["action"],
            )
            if action["action"] in {"repo_search", "community_search", "web_search"}:
                result = self._repair_search_result(action)
                messages = [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": response_text},
                    {"role": "user", "content": result},
                ]
                continue
            if action["action"] == "question":
                answer = self._ask_blocking_question(
                    action["question"],
                    choices=action.get("choices"),
                    stage=stage,
                    context={
                        "repair": True,
                        "group_id": failed_group.id,
                        "failure_type": failure.failure_type,
                    },
                    timeout=float(action.get("timeout_seconds", 600) or 600),
                )
                if not answer:
                    if not bool(action.get("required", True)):
                        return {
                            "action": "manual_followup",
                            "reason": "Repair question timed out for a non-required item.",
                        }
                    raise RuntimeError(
                        f"Repair question timed out for required item in '{failed_group.title}'."
                    )
                answer_text = f"[question answer]\n{answer or 'no answer'}"
                messages = [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": response_text},
                    {"role": "user", "content": answer_text},
                ]
                continue
            return action
        raise RuntimeError(f"Repair loop exhausted for '{failed_group.title}'.")

    def _mark_optional_apps_manual_followup(self, group: CommandGroup) -> None:
        if not self.ctx.plan:
            return
        group_pkgs = {pkg.lower() for pkg in group.packages}
        for app in self.ctx.plan.apps:
            if app.optional_slow and (
                app.package_name.lower() in group_pkgs or app.name.lower() in group.title.lower()
            ):
                app.status = PlanItemStatus.MANUAL_FOLLOWUP
                app.reason = "User skipped optional slow install during setup."

    def _mark_group_manual_followup(
        self,
        group: CommandGroup,
        *,
        stage: SetupStage,
        reason: str,
        app_names: list[str],
    ) -> None:
        if not self.ctx.plan:
            return
        if stage != SetupStage.APPS:
            self.ctx.stage_state.setdefault(stage.value, {}).setdefault(
                "manual_followups", []
            ).append({"group_id": group.id, "reason": reason})
            return
        app_names_lower = {name.lower() for name in app_names if name}
        group_pkgs = {pkg.lower() for pkg in group.packages}
        title = group.title.lower()
        matched = False
        for app in self.ctx.plan.apps:
            if app.package_name.lower() in group_pkgs:
                app.status = PlanItemStatus.MANUAL_FOLLOWUP
                app.reason = reason
                matched = True
                continue
            if app.name.lower() in app_names_lower or app.name.lower() in title:
                app.status = PlanItemStatus.MANUAL_FOLLOWUP
                app.reason = reason
                matched = True
        if not matched:
            self.ctx.stage_state.setdefault(stage.value, {}).setdefault(
                "manual_followups", []
            ).append({"group_id": group.id, "reason": reason})

    def _mark_group_completion(self, group: CommandGroup, *, stage: SetupStage) -> None:
        if not self.ctx.plan:
            return
        if stage != SetupStage.APPS:
            return
        group_pkgs = {pkg.lower() for pkg in group.packages}
        title = group.title.lower()
        for app in self.ctx.plan.apps:
            app_pkg = app.package_name.lower()
            if app_pkg and app_pkg in group_pkgs:
                app.status = PlanItemStatus.INSTALLED
                continue
            if app.name.lower() in title and app.status == PlanItemStatus.PENDING:
                app.status = PlanItemStatus.INSTALLED

    def _update_from_command(
        self, command: str, stdout: str, stderr: str, *, stage: SetupStage
    ) -> None:
        del stderr
        if stage == SetupStage.INSTALL_OR_SKIP and "useradd " in command:
            created_user = self._extract_created_user(command)
            if created_user:
                self.ctx.created_user = created_user
        if stage == SetupStage.DESKTOP:
            state = self.ctx.stage_state.setdefault(stage.value, {})
            if any(
                token in command
                for token in ("kwriteconfig", "plasma-apply", "gsettings", "wallpaper")
            ):
                state["persisted_config_written"] = True
            if "plasma-apply" in command and stdout.strip() == "":
                if self.ctx.facts and self.ctx.facts.session_type:
                    self.ctx.facts.desktop_runtime_state = DesktopRuntimeState.LIVE_APPLY_SUCCEEDED

    def _command_timeout(self, command: str) -> int:
        command_lower = command.lower()
        if any(
            token in command_lower for token in ("yay -s", "makepkg", "pacstrap", "debootstrap")
        ):
            return 1800
        if "reboot" in command_lower:
            return 60
        return 300

    @staticmethod
    def _is_reboot_command(command: str) -> bool:
        return bool(
            re.search(
                r"(^|[;&|]\s*)(?:sudo\s+)?(?:systemctl\s+)?reboot\b"
                r"|(^|[;&|]\s*)(?:sudo\s+)?shutdown\s+-r\b"
                r"|(^|[;&|]\s*)(?:sudo\s+)?(?:telinit|init)\s+6\b"
                r"|(^|[;&|]\s*)(?:sudo\s+)?kexec\s+-e\b"
                r"|(^|[;&|]\s*)(?:sudo\s+)?sh\s+-c\s+['\"]?echo\s+[a-z]*b\s*>\s*/proc/sysrq-trigger",
                command,
            )
        )

    @staticmethod
    def _extract_created_user(command: str) -> str | None:
        flags_with_values = {
            "-c",
            "--comment",
            "-d",
            "--home-dir",
            "-e",
            "--expiredate",
            "-f",
            "--inactive",
            "-g",
            "--gid",
            "--ingroup",
            "-G",
            "--groups",
            "-k",
            "--skel",
            "-p",
            "--password",
            "-s",
            "--shell",
            "-u",
            "--uid",
            "-Z",
            "--selinux-user",
            "-K",
            "--key",
        }
        for line in command.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = re.search(
                r"(?:^|[;&|]\s*)(?:sudo\s+)?(?:(?:arch-chroot|chroot)\s+\S+\s+)?"
                r"(?:useradd|adduser)\b(?P<tail>[^;&|]*)",
                line,
            )
            if not match:
                continue
            try:
                tokens = shlex.split(match.group("tail"))
            except ValueError:
                tokens = match.group("tail").split()
            for idx in range(len(tokens) - 1, -1, -1):
                token = tokens[idx].strip("\"'`")
                if not token or token.startswith("-"):
                    continue
                if idx > 0 and tokens[idx - 1] in flags_with_values:
                    continue
                if re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", token):
                    return token
        return None

    def _build_summary(self) -> str:
        plan = self.ctx.plan
        apps = plan.apps if plan else []
        installed = [
            app.name
            for app in apps
            if app.status in {PlanItemStatus.COMPLETED, PlanItemStatus.INSTALLED}
        ]
        followup = [
            app.name
            for app in apps
            if app.status in {PlanItemStatus.MANUAL_FOLLOWUP, PlanItemStatus.EXPLICITLY_SKIPPED}
        ]
        parts = [plan.target_summary if plan else "Remote setup completed."]
        if installed:
            parts.append("Installed/configured: " + ", ".join(sorted(installed)))
        if followup:
            parts.append("Manual follow-up: " + ", ".join(sorted(followup)))
        return ". ".join(part for part in parts if part).strip()
