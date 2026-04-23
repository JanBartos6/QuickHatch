"""Stage-driven remote setup orchestration."""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from quickhatch.bridge.server import BridgeState
from quickhatch.config import AIConfig, UserProfile

from .audit import audit_stage, final_audit
from .facts import describe_fact_changes
from .planner import ResearchPlanValidationError, build_research_prompt, extract_research_plan
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


class SetupOrchestrator:
    """Execute staged remote setup with typed plans and audits."""

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
                summary = f"Setup blocked: {exc}"
                self.bridge.add_message("agent", f"⚠️ {summary}")
                return summary
        finally:
            self.bridge.update_setup_run(status="idle", waiting_for_input=False)

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
        self.ctx.facts = self.runtime.probe_facts(
            fallback=self.ctx.facts,
            is_live=self.ctx.facts.is_live if self.ctx.facts else False,
            live_marker=self.ctx.facts.live_marker if self.ctx.facts else "",
            pending_question_id=self.bridge.snapshot_setup_run().get("active_question_id", ""),
            pending_question_blocking=self.bridge.snapshot_setup_run().get("waiting_for_input", False),
        )
        change_text = describe_fact_changes(before, self.ctx.facts)
        if change_text:
            self.bridge.add_message("agent", f"Refreshed setup facts: {change_text}")
        self.runlog.record("facts_refresh", stage=stage.value, facts=asdict(self.ctx.facts))
        return self.ctx.facts

    def _run_connect_and_probe(self) -> None:
        self.bridge.update_setup_run(stage=SetupStage.CONNECT_AND_PROBE.value, status="running")
        self.bridge.add_message(
            "agent", f"Connecting to {self.runtime.user}@{self.runtime.host}:{self.runtime.ssh_port}…"
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
                "agent", "✓ Pre-reboot guard self-test passed — checks will run correctly at reboot time."
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
        prompt = build_research_prompt(
            self.profile,
            self.ctx.facts or SetupFacts(),
            system_info=self.ctx.system_info,
            migration_plan_md=self.runtime.migration_plan_md,
            distro_primer_md=self.runtime.distro_primer_md,
            native_search_active=self.runtime.native_search_active,
        )
        self.runlog.record("planner_prompt", stage=SetupStage.RESEARCH_AND_PLAN.value, prompt=prompt)
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
                    self.ctx.plan = extract_research_plan(json.dumps(payload.get("plan", {})), self.profile)
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

    def _run_stage_groups(self, stage: SetupStage) -> None:
        if self.ctx.facts is None or self.ctx.plan is None:
            raise RuntimeError("Setup orchestration missing facts or plan.")
        self.bridge.update_setup_run(stage=stage.value, status="running")
        stage_state = self.ctx.stage_state.setdefault(stage.value, {})
        for group in self._iter_stage_groups(stage):
            if self.bridge.stop_requested:
                raise RuntimeError("Agent stopped by user.")
            if group.optional_slow:
                answer = self._ask_optional_slow_confirmation(group)
                if answer is None:
                    group.status = PlanItemStatus.MANUAL_FOLLOWUP
                    self._mark_optional_apps_manual_followup(group)
                    continue
            group.status = PlanItemStatus.IN_PROGRESS
            self.bridge.add_message("agent", f"[{stage.value}] {group.title}")
            self.runlog.record(
                "command_group_started",
                stage=stage.value,
                group=asdict(group),
            )
            for command in group.commands:
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
                    group.status = PlanItemStatus.FAILED
                    stage_state.setdefault("failed_commands", []).append(command)
                    raise RuntimeError(
                        f"Stage {stage.value} failed in '{group.title}' with command: {command}\n{stderr.strip() or stdout.strip()}"
                    )
                self._update_from_command(command, stdout, stderr, stage=stage)
            failed_verifications: list[str] = []
            for verification in list(group.verification) + list(
                self.ctx.plan.verification_commands.get(stage.value, [])
            ):
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
                    failed_verifications.append(
                        f"Verification failed for {group.title}: {verification}"
                    )
            if failed_verifications:
                stage_state.setdefault("failed_verifications", []).extend(failed_verifications)
                group.status = PlanItemStatus.FAILED
                raise RuntimeError("; ".join(failed_verifications))
            group.status = PlanItemStatus.COMPLETED
            self._mark_group_completion(group, stage=stage)
            self._refresh_facts(stage=stage)
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
        self._run_stage_groups(SetupStage.INSTALL_OR_SKIP)
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
        result = audit_stage(
            SetupStage.INSTALL_OR_SKIP,
            self.ctx.facts,
            plan=self.ctx.plan,
            stage_state=self.ctx.stage_state[SetupStage.INSTALL_OR_SKIP.value],
        )
        self._record_stage_result(result)

    def _run_postboot_driver(self) -> None:
        self._run_stage_groups(SetupStage.POSTBOOT_DRIVER)

    def _run_desktop(self) -> None:
        self._run_stage_groups(SetupStage.DESKTOP)

    def _run_apps(self) -> None:
        self._run_stage_groups(SetupStage.APPS)

    def _run_final_audit(self) -> AuditResult:
        self.bridge.update_setup_run(stage=SetupStage.FINAL_AUDIT.value, status="running")
        if self.ctx.plan is None:
            result = AuditResult(
                stage=SetupStage.FINAL_AUDIT,
                passed=False,
                blockers=["No research plan exists for final audit."],
            )
        else:
            result = final_audit(self.ctx.facts or SetupFacts(), self.ctx.plan, self.ctx.stage_results)
        self.ctx.stage_results.append(result)
        self.runlog.record("final_audit", result=asdict(result))
        self.bridge.update_setup_run(
            blockers=list(result.blockers),
            status="blocked" if not result.passed else "done",
            stage=SetupStage.FINAL_AUDIT.value,
        )
        return result

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
        answered = pending.answered.wait(timeout=timeout)
        self.bridge.update_setup_run(
            waiting_for_input=False,
            active_question_id="",
            blockers=[],
            status="running",
        )
        if not answered:
            self.runlog.record(
                "blocking_question_timeout", stage=stage.value, question_id=pending.id
            )
            return None
        self.runlog.record(
            "blocking_question_answer",
            stage=stage.value,
            question_id=pending.id,
            answer=pending.answer,
        )
        return pending.id if return_qid else (pending.answer or "")

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

    def _mark_group_completion(self, group: CommandGroup, *, stage: SetupStage) -> None:
        if not self.ctx.plan:
            return
        if stage == SetupStage.DESKTOP:
            self.ctx.stage_state.setdefault(stage.value, {})["persisted_config_written"] = True
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
            parts = command.split()
            if parts:
                self.ctx.created_user = parts[-1].strip("'\"")
        if stage == SetupStage.DESKTOP:
            state = self.ctx.stage_state.setdefault(stage.value, {})
            if any(token in command for token in ("kwriteconfig", "plasma-apply", "gsettings", "wallpaper")):
                state["persisted_config_written"] = True
            if "plasma-apply" in command and stdout.strip() == "":
                if self.ctx.facts and self.ctx.facts.session_type:
                    self.ctx.facts.desktop_runtime_state = DesktopRuntimeState.LIVE_APPLY_SUCCEEDED

    def _command_timeout(self, command: str) -> int:
        command_lower = command.lower()
        if any(token in command_lower for token in ("yay -s", "makepkg", "pacstrap", "debootstrap")):
            return 1800
        if "reboot" in command_lower:
            return 60
        return 300

    def _build_summary(self) -> str:
        plan = self.ctx.plan
        apps = plan.apps if plan else []
        installed = [app.name for app in apps if app.status in {PlanItemStatus.COMPLETED, PlanItemStatus.INSTALLED}]
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
