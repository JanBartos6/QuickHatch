"""Stage-driven remote setup orchestration."""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
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
from .preflight import arch_community_helpers_in_command, preflight_setup_command
from .resolver import build_install_command, nvidia_open_kernel_supported
from .runlog import SetupRunLogger
from .types import (
    AppPlanItem,
    AuditResult,
    CommandGroup,
    DesktopRuntimeState,
    PlanItemStatus,
    ResearchPlan,
    SetupFacts,
    SetupStage,
)

_LIVE_DISK_PREP_BASE_COMMANDS = (
    "swapoff -a || true",
    "mountpoint -q /mnt && umount -R /mnt || true",
)

_LIVE_DISK_DESTRUCTIVE_PATTERNS = (
    re.compile(r"\bparted\b.*\bmklabel\b", re.IGNORECASE),
    re.compile(r"\bparted\b.*\bmkpart\b", re.IGNORECASE),
    re.compile(r"\bmkfs(?:\.[\w+-]+)?\b", re.IGNORECASE),
    re.compile(r"\bwipefs\b", re.IGNORECASE),
    re.compile(r"\bsgdisk\b", re.IGNORECASE),
    re.compile(r"\bfdisk\b", re.IGNORECASE),
)
_ARCH_NVIDIA_INSTALL_RE = re.compile(
    r"\b(?:sudo\s+)?pacman\s+-S(?:y|u|yu|yyu)?\b.*\bnvidia\b",
    re.IGNORECASE,
)
_ARCH_NVIDIA_OPEN_PACKAGES = (
    "linux-headers",
    "nvidia-open",
    "nvidia-utils",
    "lib32-nvidia-utils",
    "nvidia-settings",
)
_ARCH_NVIDIA_LTS_OPEN_PACKAGES = (
    "linux-lts-headers",
    "nvidia-lts-open",
    "nvidia-utils",
    "lib32-nvidia-utils",
    "nvidia-settings",
)
_ARCH_NVIDIA_ZEN_OPEN_PACKAGES = (
    "linux-zen-headers",
    "nvidia-open-dkms",
    "nvidia-utils",
    "lib32-nvidia-utils",
    "nvidia-settings",
)
_ARCH_NVIDIA_HARDENED_OPEN_PACKAGES = (
    "linux-hardened-headers",
    "nvidia-open-dkms",
    "nvidia-utils",
    "lib32-nvidia-utils",
    "nvidia-settings",
)
_ARCH_NVIDIA_BOOT_CONFIG_COMMANDS = (
    "printf '%s\\n' 'blacklist nouveau' 'options nouveau modeset=0' | sudo tee /etc/modprobe.d/blacklist-nouveau.conf >/dev/null",
    "printf '%s\\n' 'options nvidia-drm modeset=1' | sudo tee /etc/modprobe.d/nvidia.conf >/dev/null",
    "sudo sed -i 's/^MODULES=.*/MODULES=(nvidia nvidia_modeset nvidia_uvm nvidia_drm)/' /etc/mkinitcpio.conf",
    'if [ -f /etc/default/grub ] && ! grep -q \'nvidia-drm.modeset=1\' /etc/default/grub; then sudo sed -i \'s/GRUB_CMDLINE_LINUX_DEFAULT="\\([^"]*\\)"/GRUB_CMDLINE_LINUX_DEFAULT="\\1 nvidia-drm.modeset=1"/\' /etc/default/grub; fi',
    'if [ -f /etc/default/grub ] && ! grep -q \'modprobe.blacklist=nouveau\' /etc/default/grub; then sudo sed -i \'s/GRUB_CMDLINE_LINUX_DEFAULT="\\([^"]*\\)"/GRUB_CMDLINE_LINUX_DEFAULT="\\1 modprobe.blacklist=nouveau"/\' /etc/default/grub; fi',
    "sudo mkinitcpio -P",
    "if command -v grub-mkconfig >/dev/null 2>&1; then sudo grub-mkconfig -o /boot/grub/grub.cfg; fi",
)
_ARCH_AUR_BUILD_USER = "quickhatch-build"
_ARCH_YAY_BOOTSTRAP_COMMAND = (
    "(sudo timedatectl set-ntp true >/dev/null 2>&1 && sleep 5 || true) && "
    "sudo pacman -S --needed --noconfirm base-devel git && "
    "rm -rf /tmp/quickhatch-yay-bin && "
    "git clone https://aur.archlinux.org/yay-bin.git /tmp/quickhatch-yay-bin && "
    "cd /tmp/quickhatch-yay-bin && "
    "makepkg -s --noconfirm && "
    "sudo pacman -U --noconfirm yay-bin-*.pkg.tar.zst"
)
_ARCH_ROOT_YAY_BOOTSTRAP_COMMAND = (
    "set -e; "
    "(timedatectl set-ntp true >/dev/null 2>&1 && sleep 5 || true); "
    "pacman -S --needed --noconfirm base-devel git sudo && "
    f"id -u {_ARCH_AUR_BUILD_USER} >/dev/null 2>&1 || "
    f"useradd -m -G wheel -s /bin/bash {_ARCH_AUR_BUILD_USER}; "
    f"printf '%s\\n' '{_ARCH_AUR_BUILD_USER} ALL=(ALL:ALL) NOPASSWD: /usr/bin/pacman' "
    f"> /etc/sudoers.d/99-quickhatch-aur-build && "
    "chmod 440 /etc/sudoers.d/99-quickhatch-aur-build && "
    "rm -rf /tmp/quickhatch-yay-bin && "
    f"sudo -u {_ARCH_AUR_BUILD_USER} bash -lc "
    "'git clone https://aur.archlinux.org/yay-bin.git /tmp/quickhatch-yay-bin && "
    "cd /tmp/quickhatch-yay-bin && makepkg --noconfirm' && "
    "pacman -U --noconfirm /tmp/quickhatch-yay-bin/yay-bin-*.pkg.tar.zst"
)
_ARCH_LIVE_VCONSOLE_PREP_COMMAND = (
    "mkdir -p /mnt/etc && { test -f /mnt/etc/vconsole.conf || "
    "printf 'KEYMAP=us\\n' > /mnt/etc/vconsole.conf; }"
)
_ARCH_PACSTRAP_MNT_RE = re.compile(r"\bpacstrap\b.*\s/mnt(?:\s|$)", re.IGNORECASE)
_SETUP_MODEL_CALL_TIMEOUT_SECONDS = max(
    30, int(os.environ.get("QUICKHATCH_SETUP_MODEL_TIMEOUT", "90") or "90")
)
_NON_BLOCKING_FAILED_SYSTEMD_UNITS = {
    "systemd-modules-load.service",
    "systemd-resolved.service",
}
_NON_BLOCKING_FAILED_SYSTEMD_UNIT_PREFIXES = (
    "drkonqi-coredump-processor@",
    "systemd-coredump@",
)
_DESKTOP_RUNTIME_COMMAND_RE = re.compile(
    r"\b(?:plasma-apply-[\w-]+|kscreen-doctor|xrandr|qdbus6?|gsettings|lookandfeeltool)\b",
    re.IGNORECASE,
)


def is_non_blocking_failed_systemd_unit(unit: str) -> bool:
    """Return true for noisy/transient failed units that should not block setup.

    Crash-report/coredump helpers can be left failed by a previous user-session
    crash even when the installed OS is otherwise bootable and configured. Real
    service failures still remain hard blockers.
    """

    normalized = (unit or "").strip()
    return normalized in _NON_BLOCKING_FAILED_SYSTEMD_UNITS or normalized.startswith(
        _NON_BLOCKING_FAILED_SYSTEMD_UNIT_PREFIXES
    )


_STANDALONE_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*=\$\(.+\)\s*$"
    r"|^\s*(?:export\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s*)+\s*$"
)
_PACMAN_QI_SINGLE_RE = re.compile(
    r"^(?:sudo\s+)?pacman\s+-Qi\s+(?P<name>[A-Za-z0-9@._+:-]+)\s*$",
    re.IGNORECASE,
)
_PARTED_PRE_FORMAT_FS_CHECK_RE = re.compile(
    r"\bparted\s+-ms\s+(?P<disk>/\S+)\s+print\s*\|\s*grep\s+-q\s+"
    r"(?P<quote>['\"])\^(?P<partition>\d+):\.\*:(?:fat32|ext[234]|btrfs|xfs):(?P=quote)",
    re.IGNORECASE,
)
_SYSFS_MODULE_PARAMETER_CAT_RE = re.compile(
    r"^(?P<prefix>\s*)cat\s+(?P<path>/sys/module/[A-Za-z0-9_+-]+/parameters/[A-Za-z0-9_+-]+)"
    r"(?P<rest>(?:\s|$).*)",
    re.IGNORECASE | re.DOTALL,
)

_REPO_APP_SOURCES = {
    "repo",
    "official",
    "official-repo",
    "official_repo",
    "official_docs",
    "repo_search",
    "selected_app_catalog",
    "distro_primer",
    "package_manager",
    "package-manager",
    "pacman",
    "apt",
    "apt-get",
    "apt_get",
    "dnf",
    "zypper",
    "apk",
    "xbps",
    "xbps-install",
    "xbps_install",
    "emerge",
    "core",
    "extra",
    "multilib",
}
_COMMUNITY_APP_SOURCES = {
    "community",
    "aur",
    "aur-binary",
    "aur_binary",
    "aur-source",
    "aur_source",
    "community_search",
    "yay",
    "paru",
}

_APP_PACKAGE_ALIASES: dict[str, set[str]] = {
    "brave": {"brave", "brave-bin", "brave-browser", "brave-browser-bin"},
    "brave browser": {"brave", "brave-bin", "brave-browser", "brave-browser-bin"},
    "cursor": {"cursor", "cursor-bin", "visual-studio-code-bin"},
    "github desktop": {"github-desktop", "github-desktop-bin"},
    "lm studio": {"lm-studio", "lm-studio-bin", "lmstudio", "lmstudio-bin"},
    "mullvad": {"mullvad", "mullvad-vpn", "mullvad-vpn-bin", "mullvad-vpn-daemon-bin"},
    "mullvad vpn": {
        "mullvad",
        "mullvad-vpn",
        "mullvad-vpn-bin",
        "mullvad-vpn-daemon-bin",
    },
    "localsend": {"localsend", "localsend-bin"},
    "local send": {"localsend", "localsend-bin"},
    "chatgpt": {"chatgpt", "chatgpt-desktop-bin", "chatgpt-desktop-unix-git", "pake-chatgpt"},
    "claude": {"claude", "claude-desktop-bin", "claude-desktop"},
    "codex": {"codex", "openai-codex", "openai-codex-desktop", "codex-desktop-bin"},
}

_INSTALLED_NONROOT_PRIVILEGED_STAGES = {
    SetupStage.POSTBOOT_DRIVER,
    SetupStage.DESKTOP,
    SetupStage.APPS,
}

_PRIVILEGED_COMMAND_RE = re.compile(
    r"^(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*"
    r"(?:"
    r"pacman|apt|apt-get|dnf|zypper|apk|xbps-install|emerge|"
    r"systemctl|service|rc-update|rc-service|"
    r"grub-mkconfig|grub2-mkconfig|grub-install|grub2-install|bootctl|"
    r"mkinitcpio|dracut|update-initramfs|update-grub|"
    r"modprobe|rmmod|depmod|"
    r"useradd|adduser|usermod|groupadd|groupmod|chpasswd|passwd|"
    r"timedatectl|hwclock"
    r")\b",
    re.IGNORECASE,
)

_PRIVILEGED_PATH_RE = re.compile(r"(?:^|\s)/(?:etc|boot|usr|var|opt)(?:/|\s|$)")
_PRIVILEGED_PATH_WRITE_RE = re.compile(
    r"^(?:"
    r"sed\s+-i|tee\b|install\b|mkdir\b|chmod\b|chown\b|ln\b|rm\b|cp\b|mv\b|touch\b"
    r")",
    re.IGNORECASE,
)
_INSTALLED_CHROOT_PREFIX_RE = re.compile(
    r"^(?:sudo\s+)?(?:arch-chroot|chroot)\s+/(?:mnt|target)\s+(?P<inner>.+)$",
    re.IGNORECASE | re.DOTALL,
)
_PRIVILEGED_REDIRECT_PREFIXES = ("/etc/", "/boot/", "/usr/", "/var/lib/", "/opt/")
_PRIVILEGED_REDIRECT_EXACT_PATHS = {"/etc", "/boot", "/usr", "/opt"}


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
    call_model_text: Callable[..., str]
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
    copy_local_file_to_target: Callable[[Path, str, int], tuple[int, str, str]] | None = None


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

    DEFAULT_GROUP_REPAIRS = 3
    GROUP_REPAIR_BUDGETS = {
        SetupStage.INSTALL_OR_SKIP: 3,
        SetupStage.POSTBOOT_DRIVER: 6,
        SetupStage.DESKTOP: 10,
        SetupStage.APPS: 10,
    }
    REPAIR_BUDGET_EXTENSION_ATTEMPTS = 3
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
        self._maybe_run_transfer_smoke_test()
        result = audit_stage(SetupStage.CONNECT_AND_PROBE, self.ctx.facts)
        self._record_stage_result(result)

    def _maybe_run_transfer_smoke_test(self) -> None:
        method = (self.profile.data_transfer_method or "").strip().lower()
        if method != "keep_windows":
            return
        state = self.ctx.stage_state.setdefault(SetupStage.CONNECT_AND_PROBE.value, {})
        target_path = f"/tmp/quickhatch-transfer-smoke-{self.ctx.run_id}.txt"
        if self.runtime.copy_local_file_to_target is None:
            reason = "host-side copy helper is unavailable"
            state["transfer_smoke_test"] = {"status": "skipped", "reason": reason}
            self.bridge.add_message(
                "agent",
                "⚠️ Network file-transfer smoke test skipped: "
                f"{reason}. Data migration must stay manual follow-up.",
            )
            self.runlog.record(
                "transfer_smoke_test",
                stage=SetupStage.CONNECT_AND_PROBE.value,
                status="skipped",
                reason=reason,
            )
            return

        payload = f"quickhatch-transfer-smoke {self.ctx.run_id}"
        local_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                newline="\n",
                prefix="quickhatch-transfer-smoke-",
                suffix=".txt",
                delete=False,
            ) as handle:
                handle.write(payload + "\n")
                local_path = Path(handle.name)
            rc, stdout, stderr = self.runtime.copy_local_file_to_target(local_path, target_path, 60)
            self.runlog.record(
                "transfer_smoke_copy",
                stage=SetupStage.CONNECT_AND_PROBE.value,
                target_path=target_path,
                rc=rc,
                stdout_tail=(stdout or "")[-500:],
                stderr_tail=(stderr or "")[-500:],
            )
            if rc != 0:
                detail = (stderr or stdout or "copy failed").strip()[:240]
                state["transfer_smoke_test"] = {"status": "failed", "detail": detail}
                self.bridge.add_message(
                    "agent",
                    "⚠️ Network file-transfer smoke test failed. "
                    "The install can continue, but Windows data transfer should be manual "
                    f"follow-up. {detail}",
                )
                return

            verify = (
                f"grep -qxF {shlex.quote(payload)} {shlex.quote(target_path)} "
                f"&& rm -f {shlex.quote(target_path)}"
            )
            verify_rc, verify_out, verify_err = self.runtime.ssh_exec(verify, 30)
            self.runlog.record(
                "transfer_smoke_verify",
                stage=SetupStage.CONNECT_AND_PROBE.value,
                command=verify,
                rc=verify_rc,
                stdout_tail=(verify_out or "")[-500:],
                stderr_tail=(verify_err or "")[-500:],
            )
            if verify_rc != 0:
                detail = (verify_err or verify_out or "verification failed").strip()[:240]
                state["transfer_smoke_test"] = {"status": "failed", "detail": detail}
                self.bridge.add_message(
                    "agent",
                    "⚠️ Network file-transfer smoke test copied a file, but remote "
                    f"verification failed. Treat data transfer as manual follow-up. {detail}",
                )
                return
            state["transfer_smoke_test"] = {"status": "passed", "target_path": target_path}
            self.bridge.add_message(
                "agent",
                "✓ Host-to-target file-transfer smoke test passed with a tiny dummy file.",
            )
        finally:
            if local_path is not None:
                try:
                    local_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _planner_model_call(self, messages: list[dict[str, str]]) -> str:
        planning_config = copy.copy(self.runtime.config)
        planning_config.native_search = self.runtime.native_search_active
        return self._call_model_with_retries(planning_config, messages, context="planning")

    def _parse_planner_json(self, text: str) -> dict[str, Any]:
        raw = (text or "").strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(lines[1:-1] if len(lines) >= 2 else lines)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            start = raw.find("{")
            end = raw.rfind("}")
            if start < 0 or end <= start:
                raise
            data = json.loads(raw[start : end + 1])
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
        messages = [{"role": "user", "content": prompt}]
        self.ctx.plan = None
        last_planner_error = ""
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
                last_planner_error = f"Planner returned invalid JSON: {exc}"
                self.runlog.record(
                    "planner_response_invalid",
                    stage=SetupStage.RESEARCH_AND_PLAN.value,
                    error=last_planner_error,
                )
                messages = self._planner_retry_messages(
                    prompt,
                    response_text,
                    (
                        "[quickhatch planner parser]\n"
                        f"{last_planner_error}\n"
                        "Return exactly one complete JSON object. Do not use Markdown fences, "
                        "comments, prose before/after JSON, or truncated strings."
                    ),
                )
                continue
            action = str(payload.get("action", "")).strip()
            if not action:
                try:
                    self.ctx.plan = extract_research_plan(response_text, self.profile)
                except ResearchPlanValidationError as exc:
                    last_planner_error = f"Research plan validation failed: {exc}"
                    self.runlog.record(
                        "planner_response_invalid",
                        stage=SetupStage.RESEARCH_AND_PLAN.value,
                        error=last_planner_error,
                    )
                    messages = self._planner_retry_messages(
                        prompt,
                        response_text,
                        (
                            "[quickhatch planner validator]\n"
                            f"{last_planner_error}\n"
                            "Fix the same plan and return JSON only. Keep all command groups "
                            "inside canonical stages and make every command/verification field "
                            "a shell command or list of shell commands."
                        ),
                    )
                    continue
                break
            if action == "plan":
                try:
                    self.ctx.plan = extract_research_plan(
                        json.dumps(payload.get("plan", {})), self.profile
                    )
                except ResearchPlanValidationError as exc:
                    last_planner_error = f"Research plan validation failed: {exc}"
                    self.runlog.record(
                        "planner_response_invalid",
                        stage=SetupStage.RESEARCH_AND_PLAN.value,
                        error=last_planner_error,
                    )
                    messages = self._planner_retry_messages(
                        prompt,
                        response_text,
                        (
                            "[quickhatch planner validator]\n"
                            f"{last_planner_error}\n"
                            "Fix the plan object and return JSON only. Use action=plan with a "
                            "valid plan, or return the valid plan object directly."
                        ),
                    )
                    continue
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
                last_planner_error = f"Unsupported planner action: {action}"
                messages = self._planner_retry_messages(
                    prompt,
                    response_text,
                    (
                        "[quickhatch planner parser]\n"
                        f"{last_planner_error}\n"
                        "Allowed actions are repo_search, community_search, web_search, "
                        "question, or plan. Return one valid JSON object."
                    ),
                )
                continue
            messages = [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": response_text},
                {"role": "user", "content": result},
            ]
        if self.ctx.plan is None:
            raise RuntimeError(
                last_planner_error or "Planner did not produce a final research plan."
            )
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

    @staticmethod
    def _planner_retry_messages(
        prompt: str, response_text: str, correction: str
    ) -> list[dict[str, str]]:
        return [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": response_text},
            {"role": "user", "content": correction},
        ]

    def _iter_stage_groups(self, stage: SetupStage) -> list[CommandGroup]:
        if not self.ctx.plan:
            return []
        return [group for group in self.ctx.plan.command_groups if group.stage == stage]

    def _group_repair_budget(self, stage: SetupStage) -> int:
        return int(self.GROUP_REPAIR_BUDGETS.get(stage, self.DEFAULT_GROUP_REPAIRS))

    def _ask_repair_budget_extension(
        self,
        stage: SetupStage,
        group: CommandGroup,
        failure: GroupFailure,
        *,
        attempts_used: int,
        current_budget: int,
    ) -> str:
        extension = self.REPAIR_BUDGET_EXTENSION_ATTEMPTS
        choices = [f"try {extension} more repairs", "stop setup"]
        if stage not in {SetupStage.INSTALL_OR_SKIP}:
            choices.insert(1, "manual follow-up")
        answer = self._ask_blocking_question(
            (
                f"'{group.title}' has failed after {attempts_used} repair attempt(s) "
                f"in stage '{stage.value}'.\n\n"
                f"Last failure: {failure.detail}\n\n"
                f"Current repair budget: {current_budget}. Continue only if you want "
                "QuickHatch to spend more attempts on this exact group."
            ),
            choices=choices,
            stage=stage,
            context={
                "group_id": group.id,
                "repair_budget_exhausted": True,
                "attempts_used": attempts_used,
                "current_budget": current_budget,
            },
            timeout=1800,
            return_qid=False,
        )
        normalized = (answer or "").strip().lower()
        self.runlog.record(
            "repair_budget_gate_answer",
            stage=stage.value,
            group_id=group.id,
            attempts_used=attempts_used,
            current_budget=current_budget,
            answer=answer,
        )
        if normalized.startswith(("try", "continue", "yes", "y")):
            return "extend"
        if normalized.startswith("manual") and "manual follow-up" in choices:
            return "manual_followup"
        return "stop"

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
            repair_budget = self._group_repair_budget(stage)
            while True:
                if stage == SetupStage.DESKTOP and not self._desktop_runtime_ready_or_followup(
                    current_group, stage_state
                ):
                    current_group.status = PlanItemStatus.MANUAL_FOLLOWUP
                    reason = stage_state.get(
                        "display_manual_followup_reason",
                        "Display-dependent desktop work needs an active target monitor/session.",
                    )
                    self._mark_group_manual_followup(
                        current_group,
                        stage=stage,
                        reason=str(reason),
                        app_names=[],
                    )
                    self.runlog.record(
                        "group_manual_followup",
                        stage=stage.value,
                        group_id=current_group.id,
                        reason=reason,
                    )
                    break
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
                auto_repaired = self._auto_repair_missing_repo_targets(
                    stage, current_group, failure
                )
                if auto_repaired is not None:
                    current_group = auto_repaired
                    continue
                repair_attempts += 1
                if repair_attempts > repair_budget:
                    decision = self._ask_repair_budget_extension(
                        stage,
                        current_group,
                        failure,
                        attempts_used=repair_attempts - 1,
                        current_budget=repair_budget,
                    )
                    if decision == "extend":
                        repair_budget += self.REPAIR_BUDGET_EXTENSION_ATTEMPTS
                        self.runlog.record(
                            "repair_budget_extended",
                            stage=stage.value,
                            group_id=current_group.id,
                            repair_attempts=repair_attempts,
                            new_budget=repair_budget,
                        )
                    elif decision == "manual_followup":
                        current_group.status = PlanItemStatus.MANUAL_FOLLOWUP
                        self._mark_group_manual_followup(
                            current_group,
                            stage=stage,
                            reason=(
                                f"User chose manual follow-up after {repair_attempts - 1} "
                                f"failed repair attempt(s): {failure.detail}"
                            ),
                            app_names=[],
                        )
                        break
                    else:
                        raise RuntimeError(
                            f"Stage {stage.value} exhausted repair attempts for "
                            f"'{current_group.title}': {failure.detail}"
                        )
                if repair_attempts > repair_budget:
                    raise RuntimeError(
                        f"Stage {stage.value} exhausted repair attempts for '{current_group.title}': {failure.detail}"
                    )
                current_group.status = PlanItemStatus.FAILED
                self.bridge.add_message(
                    "agent",
                    (
                        f"Repairing '{current_group.title}' after {failure.failure_type} "
                        f"failure (attempt {repair_attempts}/{repair_budget})…"
                    ),
                )
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
            if stage == SetupStage.DESKTOP:
                self._update_desktop_display_audit_state(stage_state)
            if stage == SetupStage.APPS:
                self._mark_unanswered_optional_slow_apps_manual_followup()
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
        self._ensure_reconnect_ssh_key()
        ok, failures, summary = self._run_pre_reboot_checks_with_retries()
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
        if self.ctx.facts is not None:
            self.ctx.facts = self.runtime.bootstrap_nopasswd_sudo(self.ctx.facts)
        result = audit_stage(
            SetupStage.INSTALL_OR_SKIP,
            self.ctx.facts,
            plan=self.ctx.plan,
            stage_state=self.ctx.stage_state[SetupStage.INSTALL_OR_SKIP.value],
        )
        self._record_stage_result(result)

    def _run_pre_reboot_checks_with_retries(self) -> tuple[bool, list[str], str]:
        last_result: tuple[bool, list[str], str] = (False, [], "")
        max_attempts = 8
        for attempt in range(1, max_attempts + 1):
            ok, failures, summary = self.runtime.run_pre_reboot_checks()
            last_result = (ok, failures, summary)
            self.runlog.record(
                "pre_reboot_check_attempt",
                stage=SetupStage.INSTALL_OR_SKIP.value,
                attempt=attempt,
                passed=ok,
                failures=failures,
                summary=summary,
            )
            if ok or not self._looks_like_transient_ssh_disconnect(summary, *failures):
                return last_result
            if attempt >= max_attempts:
                return last_result
            if self.bridge.stop_requested:
                raise RuntimeError("Agent stopped by user.")
            delay = min(3 * attempt, 20)
            self.bridge.add_message(
                "agent",
                "Pre-reboot safety check hit a transient SSH disconnect; "
                f"retrying ({attempt + 1}/{max_attempts}) after {delay}s…",
            )
            time.sleep(delay)
        return last_result

    def _ensure_reconnect_ssh_key(self) -> None:
        reconnect_user = self.ctx.created_user or self.runtime.user
        if not reconnect_user or reconnect_user == "root":
            return
        public_key = self._resolve_public_key_text()
        if not public_key:
            raise RuntimeError(
                "Cannot verify SSH continuity before reboot: QuickHatch public key is missing."
            )

        user_q = shlex.quote(reconnect_user)
        key_q = shlex.quote(public_key)
        command = (
            f"set -e; "
            f"qh_user={user_q}; "
            f'qh_home="/mnt/home/$qh_user"; '
            f'test -d "$qh_home"; '
            f"qh_owner=$(stat -c '%u:%g' \"$qh_home\"); "
            f'install -d -m 700 "$qh_home/.ssh"; '
            f'touch "$qh_home/.ssh/authorized_keys"; '
            f'grep -qxF {key_q} "$qh_home/.ssh/authorized_keys" '
            f"|| printf '%s\\n' {key_q} >> \"$qh_home/.ssh/authorized_keys\"; "
            f'chown "$qh_owner" "$qh_home/.ssh" "$qh_home/.ssh/authorized_keys"; '
            f'chmod 700 "$qh_home/.ssh"; '
            f'chmod 600 "$qh_home/.ssh/authorized_keys"; '
            f'grep -qxF {key_q} "$qh_home/.ssh/authorized_keys"'
        )
        self.bridge.add_message("agent", f"`{command}` — ensure SSH reconnect key")
        last_output = ""
        success = False
        max_attempts = 8
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                delay = min(2 * attempt, 15)
                self.bridge.add_message(
                    "agent",
                    "Retrying SSH reconnect key verification "
                    f"({attempt}/{max_attempts}) after {delay}s…",
                )
                time.sleep(delay)
            rc, stdout, stderr = self.runtime.ssh_exec(command, 60)
            last_output = (stderr or stdout or "").strip()
            self.ctx.results_log.append(f"$ {command}\n[rc={rc}]\n{stdout}\n{stderr}".strip())
            self.ctx.results_log[:] = self.ctx.results_log[-self.runtime.results_log_limit :]
            self.runlog.record(
                "ssh_continuity_key",
                stage=SetupStage.INSTALL_OR_SKIP.value,
                user=reconnect_user,
                attempt=attempt,
                rc=rc,
                stdout_tail=(stdout or "")[-500:],
                stderr_tail=(stderr or "")[-500:],
            )
            if rc == 0:
                success = True
                break
            if self._verify_reconnect_ssh_key(reconnect_user, public_key):
                self.bridge.add_message(
                    "agent",
                    "SSH reconnect key is present despite a transient SSH command failure.",
                )
                success = True
                break
            if attempt >= 3 and not self._looks_like_transient_ssh_disconnect(
                stdout, stderr, last_output
            ):
                break
        if not success:
            raise RuntimeError(
                "Cannot verify SSH continuity before reboot: failed to install "
                f"QuickHatch public key for '{reconnect_user}'. "
                f"{last_output[:240]}"
            )
        self.ctx.stage_state.setdefault(SetupStage.INSTALL_OR_SKIP.value, {})[
            "ssh_continuity_key_installed"
        ] = True

    def _verify_reconnect_ssh_key(self, reconnect_user: str, public_key: str) -> bool:
        user_q = shlex.quote(reconnect_user)
        key_q = shlex.quote(public_key)
        command = (
            f"qh_home=/mnt/home/{user_q}; "
            f'test -d "$qh_home" && '
            f'test -f "$qh_home/.ssh/authorized_keys" && '
            f'grep -qxF {key_q} "$qh_home/.ssh/authorized_keys"'
        )
        rc, stdout, stderr = self.runtime.ssh_exec(command, 30)
        self.runlog.record(
            "ssh_continuity_key_verify",
            stage=SetupStage.INSTALL_OR_SKIP.value,
            user=reconnect_user,
            rc=rc,
            stdout_tail=(stdout or "")[-500:],
            stderr_tail=(stderr or "")[-500:],
        )
        return rc == 0

    @staticmethod
    def _looks_like_transient_ssh_disconnect(*texts: str) -> bool:
        text = "\n".join(str(item or "") for item in texts).lower()
        return any(
            marker in text
            for marker in (
                "rc=255",
                "connection closed",
                "connection reset",
                "kex_exchange_identification",
                "connection timed out",
            )
        )

    def _resolve_public_key_text(self) -> str:
        candidates: list[Path] = []
        raw_profile_path = getattr(self.runtime.profile, "ssh_public_key_path", "") or ""
        if raw_profile_path:
            candidates.append(Path(raw_profile_path))
        if self.runtime.key_path:
            candidates.append(Path(self.runtime.key_path).with_suffix(".pub"))
        candidates.append(Path.home() / ".ssh" / "quickhatch_ed25519.pub")

        for candidate in candidates:
            try:
                text = candidate.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if text.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")):
                return text
        return ""

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
        self._run_stage_groups(SetupStage.DESKTOP, audit_after=False)
        self._ensure_display_manager_started()
        stage_state = self.ctx.stage_state.setdefault(SetupStage.DESKTOP.value, {})
        self._update_desktop_display_audit_state(stage_state)
        result = audit_stage(
            SetupStage.DESKTOP,
            self.ctx.facts or SetupFacts(),
            plan=self.ctx.plan,
            stage_state=stage_state,
        )
        if not result.passed and self._resolve_display_preference_blockers(stage_state):
            result = audit_stage(
                SetupStage.DESKTOP,
                self.ctx.facts or SetupFacts(),
                plan=self.ctx.plan,
                stage_state=stage_state,
            )
        self._record_stage_result(result)

    def _ensure_display_manager_started(self) -> None:
        facts = self.ctx.facts
        if (
            facts is None
            or facts.init_system != "systemd"
            or not facts.display_manager_enabled
            or facts.display_manager_active
        ):
            return
        command = (
            'if [ "$(id -u)" = "0" ]; then '
            "systemctl start display-manager.service; "
            "else sudo systemctl start display-manager.service; fi"
        )
        self.bridge.add_message(
            "agent",
            "Starting the enabled display manager so the target reaches a graphical login…",
        )
        rc, stdout, stderr = self._ssh_exec_with_transient_retries(
            command,
            60,
            stage=SetupStage.DESKTOP,
            group_id="quickhatch-start-display-manager",
            label="display-manager-start",
        )
        self.runlog.record(
            "display_manager_start",
            stage=SetupStage.DESKTOP.value,
            command=command,
            rc=rc,
            stdout_tail=(stdout or "")[-500:],
            stderr_tail=(stderr or "")[-500:],
        )
        self._refresh_facts(stage=SetupStage.DESKTOP)

    def _update_desktop_display_audit_state(self, stage_state: dict[str, Any]) -> None:
        prefs = self._source_display_preferences()
        stage_state["display_preferences"] = prefs
        facts = self.ctx.facts
        if (
            prefs
            and facts is not None
            and not facts.displays
            and facts.desktop_runtime_state == DesktopRuntimeState.PENDING_RELOGIN
        ):
            target = next((pref for pref in prefs if pref.get("primary")), prefs[0])
            mode = str(target.get("mode") or "").strip()
            refresh = self._as_float(target.get("refresh_hz"))
            label = mode + (f"@{refresh:g}Hz" if refresh is not None and mode else "")
            stage_state["display_manual_followup"] = True
            stage_state["display_manual_followup_reason"] = (
                "Display preferences"
                + (f" ({label})" if label else "")
                + " could not be runtime-verified before graphical login; display manager "
                "is enabled, so treat display tuning as post-login/manual follow-up."
            )
        if stage_state.get("display_manual_followup"):
            stage_state["display_preference_blockers"] = []
        else:
            stage_state["display_preference_blockers"] = self._display_preference_blockers(prefs)

    def _resolve_display_preference_blockers(self, stage_state: dict[str, Any]) -> bool:
        blockers = [str(item) for item in (stage_state.get("display_preference_blockers") or [])]
        if not blockers:
            return False
        answer = self._ask_blocking_question(
            (
                "QuickHatch detected a target display mismatch after desktop setup.\n\n"
                + "\n".join(f"- {blocker}" for blocker in blockers)
                + "\n\nChoose retry if the monitor was asleep, on another input, or the "
                "graphical session just started. Choose manual follow-up if this monitor "
                "cannot expose the requested mode right now or you want to finish setup "
                "and adjust display settings later."
            ),
            choices=["retry display probe", "manual follow-up"],
            stage=SetupStage.DESKTOP,
            context={"display_preference_blockers": blockers},
            timeout=600,
            return_qid=False,
        )
        if answer and answer.strip().lower().startswith("retry"):
            self._refresh_facts(stage=SetupStage.DESKTOP)
            self._update_desktop_display_audit_state(stage_state)
            blockers = [
                str(item) for item in (stage_state.get("display_preference_blockers") or [])
            ]
            if not blockers:
                return True

        reason = "Display tuning left for manual follow-up: " + " | ".join(blockers)
        stage_state["display_manual_followup"] = True
        stage_state["display_manual_followup_reason"] = reason
        stage_state["display_preference_blockers"] = []
        stage_state.setdefault("manual_followups", []).append({"reason": reason})
        self.runlog.record(
            "display_preference_manual_followup",
            stage=SetupStage.DESKTOP.value,
            reason=reason,
        )
        self.bridge.add_message("agent", f"⚠️ {reason}")
        return True

    def _desktop_runtime_ready_or_followup(
        self, group: CommandGroup, stage_state: dict[str, Any]
    ) -> bool:
        if not self._desktop_group_needs_runtime_display(group):
            return True
        if self.ctx.facts and self.ctx.facts.displays:
            return True
        self._refresh_facts(stage=SetupStage.DESKTOP)
        if self.ctx.facts and self.ctx.facts.displays:
            return True

        answer = self._ask_blocking_question(
            (
                "QuickHatch cannot read an active target display before running "
                f"desktop live-apply group '{group.title}'. If the monitor is asleep, "
                "on another input, or not logged into Plasma/SDDM, switch it to this "
                "Linux machine and choose retry. Otherwise leave wallpaper/resolution/"
                "live theme apply as manual follow-up."
            ),
            choices=["retry display probe", "manual follow-up"],
            stage=SetupStage.DESKTOP,
            context={"group_id": group.id, "display_runtime_probe": True},
            timeout=600,
            return_qid=False,
        )
        if answer and answer.strip().lower().startswith("retry"):
            self._refresh_facts(stage=SetupStage.DESKTOP)
            if self.ctx.facts and self.ctx.facts.displays:
                return True

        reason = (
            f"Skipped display-dependent desktop group '{group.title}' because no "
            "active target display facts were available over the real GUI session."
        )
        stage_state["display_manual_followup"] = True
        stage_state["display_manual_followup_reason"] = reason
        self.bridge.add_message("agent", f"⚠️ {reason}")
        return False

    @staticmethod
    def _desktop_group_needs_runtime_display(group: CommandGroup) -> bool:
        return any(
            SetupOrchestrator._command_needs_desktop_runtime_env(command)
            for command in [*group.commands, *group.verification]
        )

    def _source_display_preferences(self) -> list[dict[str, Any]]:
        displays = getattr(getattr(self.profile, "hardware", None), "displays", []) or []
        prefs: list[dict[str, Any]] = []
        for display in displays:
            width = int(getattr(display, "width_px", 0) or 0)
            height = int(getattr(display, "height_px", 0) or 0)
            if width <= 0 or height <= 0:
                continue
            prefs.append(
                {
                    "name": getattr(display, "name", "") or "display",
                    "mode": f"{width}x{height}",
                    "refresh_hz": getattr(display, "refresh_hz", None),
                    "primary": bool(getattr(display, "primary", False)),
                }
            )
        return prefs

    def _display_preference_blockers(self, prefs: list[dict[str, Any]]) -> list[str]:
        if not prefs or self.ctx.facts is None:
            return []
        preferred = next((pref for pref in prefs if pref.get("primary")), prefs[0])
        preferred_mode = str(preferred.get("mode") or "")
        preferred_refresh = self._as_float(preferred.get("refresh_hz"))
        target_label = preferred_mode
        if preferred_refresh is not None:
            target_label += f"@{preferred_refresh:g}Hz"
        displays = list(self.ctx.facts.displays or [])
        if not displays:
            return [
                "Display preferences could not be runtime-verified"
                + (f" ({target_label})" if target_label else "")
                + "; no active target display facts were available. Log into the graphical session, "
                "rerun desktop verification, or leave display tuning as manual follow-up."
            ]
        matching_mode = [
            display
            for display in displays
            if not preferred_mode or display.current_mode == preferred_mode
        ]
        if preferred_mode and not matching_mode:
            seen = ", ".join(
                f"{display.name}:{display.current_mode or 'unknown'}" for display in displays
            )
            return [
                f"Display mode mismatch: preferred {target_label}, target reports {seen}. "
                "Adjust KDE/kscreen display configuration before declaring desktop complete."
            ]
        if preferred_refresh is not None and matching_mode:
            known_refresh = [
                display.refresh_hz for display in matching_mode if display.refresh_hz is not None
            ]
            if not known_refresh:
                return [
                    f"Display refresh could not be verified for preferred {target_label}. "
                    "Verify the active monitor refresh rate before declaring desktop complete."
                ]
            if not any(
                abs(float(refresh) - float(preferred_refresh)) <= 1.0 for refresh in known_refresh
            ):
                seen = ", ".join(f"{refresh:g}Hz" for refresh in known_refresh)
                return [
                    f"Display refresh mismatch: preferred {target_label}, target reports {seen}. "
                    "Adjust KDE/kscreen refresh rate before declaring desktop complete."
                ]
        return []

    @staticmethod
    def _as_float(value: Any) -> float | None:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _run_apps(self) -> None:
        self._synthesize_missing_app_command_groups()
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
        final_state = self.ctx.stage_state.setdefault(SetupStage.FINAL_AUDIT.value, {})
        final_state["package_verification_ran"] = False
        final_state["verified_installed_apps"] = []
        final_state["unverified_installed_apps"] = []
        package_sources = {
            "repo",
            "official",
            "official-repo",
            "community",
            "aur",
            "aur-binary",
            "aur-source",
        }
        installed_like = [
            app
            for app in self.ctx.plan.apps
            if app.status in {PlanItemStatus.INSTALLED, PlanItemStatus.COMPLETED}
        ]
        candidates = [
            app
            for app in installed_like
            if (app.source or "").lower() in package_sources and (app.package_name or "").strip()
        ]
        unverifiable = [app.name for app in installed_like if app not in candidates]
        final_state["unverified_installed_apps"] = sorted(unverifiable)
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
        final_state["package_verification_ran"] = True
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
            final_state["unverified_installed_apps"] = sorted(app.name for app in installed_like)
            return [
                "Could not verify installed packages with "
                f"{self.ctx.facts.package_manager}: {(stderr or stdout or '').strip()[:200]}"
            ]
        installed = {line.strip().lower() for line in (stdout or "").splitlines() if line.strip()}
        missing: list[str] = []
        verified: list[str] = []
        for app in candidates:
            variants = self._package_name_variants(app.name, app.package_name)
            if not variants:
                missing.append(app.name)
                continue
            if not any(variant in installed for variant in variants):
                missing.append(app.name)
                continue
            verified.append(app.name)
        final_state["verified_installed_apps"] = sorted(verified)
        final_state["unverified_installed_apps"] = sorted({*unverifiable, *missing})
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
        for key in {app_name.strip().lower(), package_name.strip().lower()}:
            variants.update(_APP_PACKAGE_ALIASES.get(key, set()))
        return {variant for variant in variants if variant}

    @staticmethod
    def _variant_appears_in_command_text(variant: str, text: str) -> bool:
        if not variant or not text:
            return False
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_.+-]){re.escape(variant.lower())}(?![A-Za-z0-9_.+-])"
        )
        return bool(pattern.search(text.lower()))

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
        failed_units = [
            line.strip()
            for line in (stdout or "").splitlines()
            if line.strip() and not is_non_blocking_failed_systemd_unit(line)
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
        commands = self._commands_for_group(stage, group)
        for command in commands:
            command = self._rewrite_arch_community_helper_command(command, stage=stage, group=group)
            visible_command = command
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
                bootstrap_failure = self._try_bootstrap_arch_aur_helper_for_command(
                    command,
                    stage=stage,
                    group=group,
                    stage_state=stage_state,
                )
                if bootstrap_failure is not None:
                    return bootstrap_failure
                command = self._rewrite_arch_community_helper_command(
                    command, stage=stage, group=group
                )
                if self._arch_aur_helper_available() and self._uses_arch_community_helper_command(
                    command
                ):
                    lint_error = preflight_setup_command(command, self.ctx.facts or SetupFacts())
                if not lint_error:
                    self.runlog.record(
                        "command_preflight_recovered",
                        stage=stage.value,
                        group_id=group.id,
                        command=command,
                        recovery="arch_aur_helper_bootstrap",
                    )
                else:
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
                        command=visible_command,
                        stdout="",
                        stderr="",
                        detail=lint_error,
                    )
            command = self._wrap_desktop_runtime_command(command, stage=stage)
            if command != visible_command:
                self.bridge.add_message(
                    "agent",
                    f"`{visible_command}` — using active GUI session environment",
                )
            else:
                self.bridge.add_message("agent", f"`{visible_command}`")
            rc, stdout, stderr = self._ssh_exec_with_transient_retries(
                command,
                self._command_timeout(command),
                stage=stage,
                group_id=group.id,
                label="command",
            )
            self.ctx.results_log.append(f"$ {command}\n[rc={rc}]\n{stdout}\n{stderr}".strip())
            self.ctx.results_log[:] = self.ctx.results_log[-self.runtime.results_log_limit :]
            self.runlog.record(
                "command",
                stage=stage.value,
                group_id=group.id,
                command=command,
                visible_command=visible_command,
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
                        visible_command,
                        rc,
                        stdout,
                        stderr,
                    ),
                )
                return GroupFailure(
                    failure_type="command",
                    command=visible_command,
                    stdout=stdout,
                    stderr=stderr,
                    detail=detail,
                )
            self._update_from_command(visible_command, stdout, stderr, stage=stage)
        self._maybe_reboot_for_nvidia_driver(stage, group, stage_state)
        for verification in group.verification:
            verification = self._normalize_installed_nonroot_privileged_command(
                verification, stage=stage
            )
            verification = self._normalize_package_verification_command(verification, stage=stage)
            normalized_verification = self._normalize_sysfs_module_parameter_verification(
                verification, stage=stage
            )
            if normalized_verification != verification:
                self.runlog.record(
                    "verification_normalized",
                    stage=stage.value,
                    group_id=group.id,
                    original=verification,
                    normalized=normalized_verification,
                    reason="kernel module parameter files may be root-readable only",
                )
                verification = normalized_verification
            normalized_verification = self._normalize_pre_format_partition_verification(
                verification, group=group
            )
            if normalized_verification != verification:
                self.runlog.record(
                    "verification_normalized",
                    stage=stage.value,
                    group_id=group.id,
                    original=verification,
                    normalized=normalized_verification,
                    reason="partition groups should verify partition nodes before mkfs, not filesystem labels",
                )
                verification = normalized_verification
            normalized_verification = self._normalize_fstab_mount_verification(verification)
            if normalized_verification != verification:
                self.runlog.record(
                    "verification_normalized",
                    stage=stage.value,
                    group_id=group.id,
                    original=verification,
                    normalized=normalized_verification,
                    reason="fstab mount checks must match fields, not whitespace-sensitive text",
                )
                verification = normalized_verification
            self.bridge.add_message("agent", f"`{verification}` — verify")
            rc, stdout, stderr = self._ssh_exec_with_transient_retries(
                verification,
                120,
                stage=stage,
                group_id=group.id,
                label="verification",
            )
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
                if self._soft_fail_desktop_cosmetic_verification(
                    stage=stage,
                    group=group,
                    verification=verification,
                    rc=rc,
                    stdout=stdout,
                    stderr=stderr,
                    stage_state=stage_state,
                ):
                    continue
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

    def _normalize_sysfs_module_parameter_verification(
        self, command: str, *, stage: SetupStage
    ) -> str:
        facts = self.ctx.facts or SetupFacts()
        if (
            stage not in _INSTALLED_NONROOT_PRIVILEGED_STAGES
            or facts.is_live
            or facts.is_root
            or self._has_privileged_command_wrapper(command.strip())
        ):
            return command
        match = _SYSFS_MODULE_PARAMETER_CAT_RE.match(command or "")
        if not match:
            return command
        return (
            f"{match.group('prefix')}sudo cat "
            f"{shlex.quote(match.group('path'))}{match.group('rest')}"
        )

    @staticmethod
    def _normalize_pre_format_partition_verification(command: str, *, group: CommandGroup) -> str:
        if any("mkfs" in planned.lower() for planned in group.commands):
            return command
        match = _PARTED_PRE_FORMAT_FS_CHECK_RE.search(command or "")
        if not match:
            return command
        disk = match.group("disk").strip("'\"")
        partition = match.group("partition")
        suffix = "p" if disk and disk[-1].isdigit() else ""
        return f"test -b {shlex.quote(disk + suffix + partition)}"

    @staticmethod
    def _normalize_fstab_mount_verification(command: str) -> str:
        """Replace brittle `grep ' / ' fstab` checks with field-aware checks."""

        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            return command
        tool = (tokens[0] or "").strip().split("/")[-1].lower()
        if len(tokens) < 3 or tool != "grep":
            return command
        args = tokens[1:]
        if args and args[0] == "-q":
            args = args[1:]
        if len(args) != 2:
            return command
        mountpoint = args[0].strip()
        path = args[1]
        if mountpoint not in {"/", "/boot"} or not path.endswith("/etc/fstab"):
            return command
        return (
            "awk "
            + shlex.quote(f'$2 == "{mountpoint}" {{ found=1 }} END {{ exit(found ? 0 : 1) }}')
            + " "
            + shlex.quote(path)
        )

    def _synthesize_missing_app_command_groups(self) -> None:
        """Turn resolved app plan rows into executable app groups if the model omitted them."""

        if self.ctx.plan is None or self.ctx.facts is None:
            return
        pending = [
            app
            for app in self.ctx.plan.apps
            if app.status
            in {
                PlanItemStatus.PENDING,
                PlanItemStatus.IN_PROGRESS,
                PlanItemStatus.OPTIONAL_SLOW_PENDING_CONFIRMATION,
            }
            and not self._app_is_covered_by_app_group(app)
        ]
        if not pending:
            return

        repo_packages: list[str] = []
        community_packages: list[str] = []
        repo_app_names: list[str] = []
        community_app_names: list[str] = []
        for app in pending:
            source = self._normalized_app_source(app.source)
            package_name = (app.package_name or "").strip()
            if app.status == PlanItemStatus.OPTIONAL_SLOW_PENDING_CONFIRMATION:
                # Optional slow apps need an explicit user decision. Without a
                # planned group there is nothing safe to ask/execute, so keep
                # setup moving with a visible manual follow-up instead.
                app.status = PlanItemStatus.MANUAL_FOLLOWUP
                app.reason = app.reason or "Optional slow app was left for manual follow-up."
                self._record_optional_slow_apps_manual_followup([app.name])
                continue
            if source == "repo" and package_name:
                repo_packages.append(package_name)
                repo_app_names.append(app.name)
                continue
            if source == "community" and package_name:
                if self.ctx.facts.package_manager == "pacman":
                    community_packages.append(package_name)
                    community_app_names.append(app.name)
                    continue
                app.status = PlanItemStatus.MANUAL_FOLLOWUP
                app.reason = (
                    app.reason
                    or "Community package source is not supported automatically for this distro."
                )
                continue
            app.status = PlanItemStatus.MANUAL_FOLLOWUP
            app.reason = app.reason or "Planner did not provide a resolvable package source."

        groups: list[CommandGroup] = []
        if repo_packages:
            command = build_install_command(repo_packages, "repo", self.ctx.facts)
            if command:
                groups.append(
                    CommandGroup(
                        id="quickhatch-synth-repo-apps",
                        stage=SetupStage.APPS,
                        title="Install resolved repository applications",
                        commands=[command],
                        verification=[
                            self._package_verification_command(repo_packages, self.ctx.facts)
                        ],
                        rationale=(
                            "QuickHatch synthesized this group because the planner resolved "
                            "required repository apps but omitted an executable apps command group."
                        ),
                        source="repo",
                        packages=repo_packages,
                    )
                )
            else:
                self._mark_apps_manual_followup_by_names(
                    repo_app_names,
                    "No typed repository install command is available for this package manager.",
                )
        if community_packages:
            command = self._community_install_command(community_packages, self.ctx.facts)
            if command:
                groups.append(
                    CommandGroup(
                        id="quickhatch-synth-community-apps",
                        stage=SetupStage.APPS,
                        title="Install resolved community applications",
                        commands=[command],
                        verification=[
                            self._package_verification_command(community_packages, self.ctx.facts)
                        ],
                        rationale=(
                            "QuickHatch synthesized this group because the planner resolved "
                            "required community apps but omitted an executable apps command group."
                        ),
                        source="community",
                        packages=community_packages,
                    )
                )
            else:
                self._mark_apps_manual_followup_by_names(
                    community_app_names,
                    "No supported community-package helper is available for this target.",
                )
        if not groups:
            return
        self.ctx.plan.command_groups.extend(groups)
        self.bridge.add_message(
            "agent",
            "[harness] Added missing app install group(s) from the validated app plan: "
            + ", ".join(group.title for group in groups),
        )
        self.runlog.record(
            "synthesized_app_command_groups",
            stage=SetupStage.APPS.value,
            groups=[asdict(group) for group in groups],
        )

    def _app_is_covered_by_app_group(self, app: AppPlanItem) -> bool:
        if self.ctx.plan is None:
            return False
        variants = self._package_name_variants(app.name, app.package_name)
        for group in self.ctx.plan.command_groups:
            if group.stage != SetupStage.APPS:
                continue
            group_packages = {pkg.lower() for pkg in group.packages}
            if variants and variants.intersection(group_packages):
                return True
            command_text = " ".join([group.title, *group.commands, *group.verification])
            if any(
                self._variant_appears_in_command_text(variant, command_text) for variant in variants
            ):
                return True
        return False

    @staticmethod
    def _normalized_app_source(source: str) -> str:
        normalized = (source or "").strip().lower().replace(" ", "_")
        if normalized in _REPO_APP_SOURCES:
            return "repo"
        if normalized in _COMMUNITY_APP_SOURCES:
            return "community"
        return normalized or "manual_followup"

    @staticmethod
    def _community_install_command(packages: list[str], facts: SetupFacts) -> str:
        if facts.package_manager != "pacman":
            return ""
        helper = facts.community_helper if facts.community_helper in {"yay", "paru"} else "yay"
        quoted = " ".join(shlex.quote(package) for package in packages if package)
        if not quoted:
            return ""
        if facts.is_root and facts.login_user and facts.login_user != "root":
            return f"sudo -u {shlex.quote(facts.login_user)} {helper} -S --noconfirm {quoted}"
        return f"{helper} -S --noconfirm {quoted}"

    @staticmethod
    def _package_verification_command(packages: list[str], facts: SetupFacts) -> str:
        quoted = " ".join(shlex.quote(package) for package in packages if package)
        if not quoted:
            return "true"
        pm = facts.package_manager
        if pm == "pacman":
            return f"pacman -Q {quoted}"
        if pm == "apt":
            return f"dpkg-query -W {quoted}"
        if pm in {"dnf", "zypper"}:
            return f"rpm -q {quoted}"
        if pm == "apk":
            return f"apk info -e {quoted}"
        if pm == "xbps":
            return " && ".join(f"xbps-query {shlex.quote(package)}" for package in packages)
        if pm == "emerge":
            return " && ".join(f"qlist -IC {shlex.quote(package)}" for package in packages)
        return "true"

    def _mark_apps_manual_followup_by_names(self, names: list[str], reason: str) -> None:
        if self.ctx.plan is None:
            return
        wanted = {name.lower() for name in names if name}
        for app in self.ctx.plan.apps:
            if app.name.lower() in wanted:
                app.status = PlanItemStatus.MANUAL_FOLLOWUP
                app.reason = reason

    def _normalize_package_verification_command(self, command: str, *, stage: SetupStage) -> str:
        facts = self.ctx.facts or SetupFacts()
        if facts.package_manager != "pacman" or stage not in _INSTALLED_NONROOT_PRIVILEGED_STAGES:
            return command
        match = _PACMAN_QI_SINGLE_RE.match((command or "").strip())
        if not match:
            return command
        name = shlex.quote(match.group("name"))
        return f"(pacman -Qi {name} >/dev/null 2>&1 || pacman -Qg {name} >/dev/null 2>&1)"

    def _soft_fail_desktop_cosmetic_verification(
        self,
        *,
        stage: SetupStage,
        group: CommandGroup,
        verification: str,
        rc: int,
        stdout: str,
        stderr: str,
        stage_state: dict[str, Any],
    ) -> bool:
        if stage != SetupStage.DESKTOP:
            return False
        if not self._is_cosmetic_desktop_verification(verification):
            return False
        reason = (
            f"Cosmetic desktop verification did not pass for '{group.title}' "
            f"({verification}). Leaving this theme/icon/font check as manual follow-up."
        )
        stage_state.setdefault("manual_followups", []).append(
            {
                "group_id": group.id,
                "reason": reason,
                "verification": verification,
            }
        )
        self.runlog.record(
            "desktop_cosmetic_verification_manual_followup",
            stage=stage.value,
            group_id=group.id,
            command=verification,
            rc=rc,
            stdout_tail=(stdout or "")[-500:],
            stderr_tail=(stderr or "")[-500:],
            reason=reason,
        )
        self.bridge.add_message("agent", f"[harness] {reason}")
        return True

    @staticmethod
    def _is_cosmetic_desktop_verification(command: str) -> bool:
        lowered = (command or "").lower()
        if any(
            core in lowered
            for core in (
                "plasma-desktop",
                "plasma-meta",
                "plasma-workspace",
                "kde-applications",
                "sddm",
                "gdm",
                "lightdm",
                "xorg",
                "wayland",
                "xdg-desktop-portal",
                "networkmanager",
                "pipewire",
                "dolphin",
                "konsole",
            )
        ):
            return False
        has_cosmetic_hint = any(
            hint in lowered
            for hint in (
                "look-and-feel",
                "lookandfeel",
                "theme",
                "icon",
                "cursor",
                "wallpaper",
                "font",
                "ttf-",
                "otf-",
                "noto",
                "inter",
                "kvantum",
                "catppuccin",
                "sweet",
                "candy",
                "papirus",
                "bibata",
            )
        )
        if not has_cosmetic_hint:
            return False
        if re.search(r"\b(?:pacman|dpkg-query|rpm|apk|xbps-query|qlist)\b", lowered):
            return True
        return any(
            path in lowered
            for path in (
                "/usr/share/plasma/look-and-feel",
                "/usr/share/icons",
                "/usr/share/themes",
                "/usr/share/wallpapers",
                "/usr/share/fonts",
                "/usr/local/share/fonts",
                "/.local/share/icons",
                "/.local/share/themes",
                "/.local/share/wallpapers",
                "/.config/gtk-",
                "kdeglobals",
                "kwinrc",
            )
        )

    def _auto_repair_missing_repo_targets(
        self,
        stage: SetupStage,
        group: CommandGroup,
        failure: GroupFailure,
    ) -> CommandGroup | None:
        if stage == SetupStage.APPS or failure.failure_type != "command":
            return None
        missing = self._missing_repo_targets_from_output(failure.stdout, failure.stderr)
        if not missing:
            return None
        repaired_commands: list[str] = []
        changed = False
        for command in group.commands:
            if command == failure.command:
                repaired = self._drop_missing_targets_from_install_command(command, missing)
                if repaired and repaired != command:
                    repaired_commands.append(repaired)
                    changed = True
                    continue
            repaired_commands.append(command)
        if not changed:
            return None
        repaired_group = copy.deepcopy(group)
        repaired_group.id = f"{group.id}-without-missing-targets"
        repaired_group.title = f"{group.title} (without unavailable repo targets)"
        repaired_group.commands = repaired_commands
        repaired_group.packages = [pkg for pkg in group.packages if pkg not in missing]
        repaired_group.rationale = (
            f"{group.rationale}\nQuickHatch removed unavailable repository target(s) "
            f"{', '.join(sorted(missing))} from this non-app stage so remaining "
            "validated packages can continue."
        ).strip()
        self.bridge.add_message(
            "agent",
            "[harness] Removed unavailable non-app repo target(s) "
            f"{', '.join(sorted(missing))} and will retry the group.",
        )
        self.runlog.record(
            "auto_repair_missing_repo_targets",
            stage=stage.value,
            group_id=group.id,
            missing=sorted(missing),
            commands=repaired_commands,
        )
        self.ctx.stage_state.setdefault(stage.value, {}).setdefault("manual_followups", []).append(
            {
                "group_id": group.id,
                "reason": (
                    "Unavailable repository target(s) were dropped from "
                    f"'{group.title}': {', '.join(sorted(missing))}. "
                    "Review or choose alternatives after setup."
                ),
            }
        )
        return repaired_group

    @staticmethod
    def _missing_repo_targets_from_output(*texts: str) -> set[str]:
        text = "\n".join(str(item or "") for item in texts)
        return set(re.findall(r"target not found:\s*([A-Za-z0-9@._+:-]+)", text))

    @staticmethod
    def _drop_missing_targets_from_install_command(command: str, missing: set[str]) -> str | None:
        if any(token in command for token in ("&&", "||", ";", "|", "\n")):
            return None
        try:
            tokens = shlex.split(command)
        except ValueError:
            return None
        if not tokens or not any(tool in tokens for tool in ("pacman", "pacstrap")):
            return None
        filtered = [token for token in tokens if token not in missing]
        if filtered == tokens:
            return None
        payload = SetupOrchestrator._install_payload_tokens(filtered)
        if not payload:
            return None
        return shlex.join(filtered)

    @staticmethod
    def _install_payload_tokens(tokens: list[str]) -> list[str]:
        if "pacman" in tokens:
            for idx, token in enumerate(tokens):
                if token == "-S" or token.startswith("-S"):
                    return [item for item in tokens[idx + 1 :] if not item.startswith("-")]
        if "pacstrap" in tokens:
            mount_idx = next(
                (idx for idx, token in enumerate(tokens) if token in {"/mnt", "/target"}),
                -1,
            )
            if mount_idx >= 0:
                return [
                    item
                    for item in tokens[mount_idx + 1 :]
                    if not item.startswith("-") and not item.startswith("$")
                ]
        return []

    def _ssh_exec_with_transient_retries(
        self,
        command: str,
        timeout: int,
        *,
        stage: SetupStage,
        group_id: str,
        label: str,
    ) -> tuple[int, str, str]:
        max_attempts = 5
        last_result = (1, "", "command did not run")
        for attempt in range(1, max_attempts + 1):
            rc, stdout, stderr = self.runtime.ssh_exec(command, timeout)
            last_result = (rc, stdout, stderr)
            if rc == 0:
                return last_result
            if not self._looks_like_transient_ssh_disconnect(stdout, stderr):
                return last_result
            if attempt == max_attempts:
                return last_result
            delay = min(2 * attempt, 10)
            self.bridge.add_message(
                "agent",
                "Transient SSH transport failure during "
                f"{label}; retrying the same command "
                f"({attempt + 1}/{max_attempts}) after {delay}s…",
            )
            self.runlog.record(
                "ssh_transport_retry",
                stage=stage.value,
                group_id=group_id,
                label=label,
                command=command,
                attempt=attempt,
                rc=rc,
                stdout_tail=(stdout or "")[-500:],
                stderr_tail=(stderr or "")[-500:],
            )
            time.sleep(delay)
        return last_result

    def _commands_for_group(self, stage: SetupStage, group: CommandGroup) -> list[str]:
        commands = self._drop_live_install_harness_owned_commands(stage, group)
        commands = self._combine_standalone_assignments(commands)
        commands = self._inject_arch_live_vconsole_before_pacstrap(stage, group, commands)
        if self._is_arch_nvidia_driver_group(stage, group, commands):
            commands = self._normalize_arch_nvidia_commands(commands)
        if self._needs_arch_aur_helper_bootstrap(commands):
            bootstrap_command = self._arch_yay_bootstrap_command()
            commands = [bootstrap_command, *commands]
            self.runlog.record(
                "arch_aur_helper_bootstrap_injected",
                stage=stage.value,
                group_id=group.id,
                command=bootstrap_command,
            )
        if not self._needs_live_disk_prep(stage, commands):
            return [
                self._normalize_installed_nonroot_privileged_command(command, stage=stage)
                for command in commands
            ]

        first_destructive = self._first_live_disk_destructive_index(commands)
        commands_before_destructive = [
            command.strip().lower() for command in commands[:first_destructive]
        ]
        has_unmount = any(
            "umount" in command and "/mnt" in command for command in commands_before_destructive
        )
        has_swapoff = any("swapoff" in command for command in commands_before_destructive)
        if has_unmount and has_swapoff:
            return commands

        prep_commands = self._live_disk_prep_commands(commands)
        patched_commands = prep_commands + commands
        self.runlog.record(
            "live_disk_prep_injected",
            stage=stage.value,
            group_id=group.id,
            commands=prep_commands,
        )
        patched_commands = self._combine_standalone_assignments(patched_commands)
        return [
            self._normalize_installed_nonroot_privileged_command(command, stage=stage)
            for command in patched_commands
        ]

    @staticmethod
    def _combine_standalone_assignments(commands: list[str]) -> list[str]:
        combined: list[str] = []
        pending_assignment = ""
        for command in commands:
            stripped = (command or "").strip()
            if pending_assignment:
                combined.append(f"{pending_assignment} && {stripped}")
                pending_assignment = ""
                continue
            if _STANDALONE_ASSIGNMENT_RE.fullmatch(stripped):
                pending_assignment = stripped
                continue
            combined.append(command)
        if pending_assignment:
            combined.append(pending_assignment)
        return combined

    def _wrap_desktop_runtime_command(self, command: str, *, stage: SetupStage) -> str:
        if stage != SetupStage.DESKTOP or not self._command_needs_desktop_runtime_env(command):
            return command
        facts = self.ctx.facts or SetupFacts()
        gui_user = (
            str(facts.raw.get("QH_GUI_USER", "") or "").strip()
            or facts.login_user
            or facts.current_user
            or self.runtime.user
        )
        current_user = facts.current_user or self.runtime.user
        user_q = shlex.quote(gui_user)
        inner = (
            f"qh_pid=$(pgrep -u {user_q} -n plasmashell 2>/dev/null "
            f"|| pgrep -u {user_q} -n kwin_wayland 2>/dev/null "
            f"|| pgrep -u {user_q} -n kwin_x11 2>/dev/null || true); "
            'if [ -n "$qh_pid" ] && [ -r "/proc/$qh_pid/environ" ]; then '
            'while IFS= read -r qh_env; do case "$qh_env" in '
            "DISPLAY=*|WAYLAND_DISPLAY=*|XAUTHORITY=*|XDG_RUNTIME_DIR=*|"
            "DBUS_SESSION_BUS_ADDRESS=*|XDG_CURRENT_DESKTOP=*|XDG_SESSION_TYPE=*) "
            'export "$qh_env";; esac; done < <(tr "\\000" "\\n" < "/proc/$qh_pid/environ"); '
            "fi; "
            'export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-wayland}"; '
            f"{command}"
        )
        wrapped = f"bash -lc {shlex.quote(inner)}"
        if current_user and gui_user and current_user != gui_user and not facts.is_root:
            return wrapped
        if facts.is_root and gui_user and gui_user != "root":
            return f"sudo -u {user_q} {wrapped}"
        return wrapped

    @staticmethod
    def _command_needs_desktop_runtime_env(command: str) -> bool:
        return bool(_DESKTOP_RUNTIME_COMMAND_RE.search(command or ""))

    def _drop_live_install_harness_owned_commands(
        self, stage: SetupStage, group: CommandGroup
    ) -> list[str]:
        commands: list[str] = []
        facts = self.ctx.facts or SetupFacts()
        for command in group.commands:
            if (
                stage == SetupStage.INSTALL_OR_SKIP
                and facts.is_live
                and (
                    self._is_reboot_command(command)
                    or self._is_live_target_unmount_command(command)
                )
            ):
                self.runlog.record(
                    "command_deferred_until_guarded_reboot",
                    stage=stage.value,
                    group_id=group.id,
                    command=command,
                    reason="live install unmount/reboot is harness-owned",
                )
                continue
            commands.append(command)
        return commands

    def _inject_arch_live_vconsole_before_pacstrap(
        self, stage: SetupStage, group: CommandGroup, commands: list[str]
    ) -> list[str]:
        facts = self.ctx.facts or SetupFacts()
        if (
            stage != SetupStage.INSTALL_OR_SKIP
            or not facts.is_live
            or facts.distro_family != "arch"
            or any("vconsole.conf" in command for command in commands)
        ):
            return commands

        patched: list[str] = []
        injected = False
        for command in commands:
            if not injected and _ARCH_PACSTRAP_MNT_RE.search(command):
                patched.append(_ARCH_LIVE_VCONSOLE_PREP_COMMAND)
                injected = True
                self.runlog.record(
                    "arch_live_vconsole_prep_injected",
                    stage=stage.value,
                    group_id=group.id,
                    command=_ARCH_LIVE_VCONSOLE_PREP_COMMAND,
                )
            patched.append(command)
        return patched

    def _normalize_installed_nonroot_privileged_command(
        self, command: str, *, stage: SetupStage
    ) -> str:
        facts = self.ctx.facts or SetupFacts()
        if stage not in _INSTALLED_NONROOT_PRIVILEGED_STAGES or facts.is_live:
            return command
        stripped = command.strip()
        chroot_stripped = self._strip_installed_chroot_prefix(stripped)
        if chroot_stripped != stripped:
            self.runlog.record(
                "command_installed_chroot_stripped",
                stage=stage.value,
                original=command,
                rewritten=chroot_stripped,
            )
            stripped = chroot_stripped
        if facts.is_root:
            return stripped if stripped != command.strip() else command
        same_user_stripped = self._strip_same_user_execution_wrapper(stripped)
        if same_user_stripped != stripped:
            self.runlog.record(
                "command_same_user_wrapper_stripped",
                stage=stage.value,
                original=command,
                rewritten=same_user_stripped,
            )
            stripped = same_user_stripped
        rewritten = self._sudo_tee_for_privileged_redirection(stripped)
        if rewritten != stripped:
            self.runlog.record(
                "command_privilege_normalized",
                stage=stage.value,
                original=command,
                rewritten=rewritten,
                reason="root-owned shell redirection",
            )
            return rewritten
        if not stripped or self._has_privileged_command_wrapper(stripped):
            return stripped if stripped != command.strip() else command
        if self._uses_arch_community_helper_command(stripped):
            return stripped if stripped != command.strip() else command

        rewritten = self._sudo_tee_for_privileged_pipeline(stripped)
        if rewritten != stripped:
            self.runlog.record(
                "command_privilege_normalized",
                stage=stage.value,
                original=command,
                rewritten=rewritten,
                reason="privileged tee pipeline",
            )
            return rewritten

        if _PRIVILEGED_COMMAND_RE.search(stripped) or self._writes_privileged_path(stripped):
            rewritten = f"sudo {stripped}"
            self.runlog.record(
                "command_privilege_normalized",
                stage=stage.value,
                original=command,
                rewritten=rewritten,
                reason="installed non-root privileged command",
            )
            return rewritten
        return stripped if stripped != command.strip() else command

    @staticmethod
    def _strip_installed_chroot_prefix(command: str) -> str:
        match = _INSTALLED_CHROOT_PREFIX_RE.match(command.strip())
        if not match:
            return command
        inner = match.group("inner").strip()
        if inner.startswith("/bin/bash "):
            inner = "bash " + inner[len("/bin/bash ") :]
        return inner

    @staticmethod
    def _has_privileged_command_wrapper(command: str) -> bool:
        return bool(
            re.match(r"^(?:sudo|doas|su|runuser|arch-chroot|chroot)\b", command)
            or re.search(r"(?:^|[;&|]\s*)(?:sudo|doas)\s+", command)
        )

    def _strip_same_user_execution_wrapper(self, command: str) -> str:
        facts = self.ctx.facts or SetupFacts()
        current_user = (facts.current_user or facts.login_user or self.runtime.user or "").strip()
        if not current_user:
            return command
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            return command
        if not tokens:
            return command
        tool = tokens[0]
        if tool == "runuser":
            idx = 1
            target_user = ""
            if idx < len(tokens) and tokens[idx] in {"-u", "--user"}:
                if idx + 1 >= len(tokens):
                    return command
                target_user = tokens[idx + 1]
                idx += 2
            elif idx < len(tokens) and tokens[idx].startswith("--user="):
                target_user = tokens[idx].split("=", 1)[1]
                idx += 1
            else:
                return command
            if idx < len(tokens) and tokens[idx] == "--":
                idx += 1
            if target_user == current_user and idx < len(tokens):
                return shlex.join(tokens[idx:])
            return command
        if tool == "sudo":
            idx = 1
            target_user = "root"
            while idx < len(tokens):
                token = tokens[idx]
                if token in {"-u", "--user"}:
                    if idx + 1 >= len(tokens):
                        return command
                    target_user = tokens[idx + 1]
                    idx += 2
                    continue
                if token.startswith("--user="):
                    target_user = token.split("=", 1)[1]
                    idx += 1
                    continue
                if token in {"-g", "-h", "-D", "-p", "-r", "-t", "-T", "-C", "-R"}:
                    idx += 2
                    continue
                if token.startswith("-"):
                    idx += 1
                    continue
                break
            if target_user == current_user and idx < len(tokens):
                return shlex.join(tokens[idx:])
        return command

    @staticmethod
    def _writes_privileged_path(command: str) -> bool:
        return bool(
            _PRIVILEGED_PATH_WRITE_RE.search(command) and _PRIVILEGED_PATH_RE.search(command)
        )

    @staticmethod
    def _sudo_tee_for_privileged_pipeline(command: str) -> str:
        if re.search(r"\|\s*sudo\s+tee\b", command):
            return command
        if not re.search(
            r"\|\s*tee\b(?:\s+-[A-Za-z]+)*\s+/(?:etc|boot|usr|var|opt)(?:/|\s|$)",
            command,
        ):
            return command
        return re.sub(r"\|\s*tee\b", "| sudo tee", command, count=1)

    @staticmethod
    def _is_privileged_redirect_target(path: str) -> bool:
        stripped = (path or "").strip().strip("'\"")
        return stripped in _PRIVILEGED_REDIRECT_EXACT_PATHS or any(
            stripped.startswith(prefix) for prefix in _PRIVILEGED_REDIRECT_PREFIXES
        )

    @staticmethod
    def _split_shell_redirection(command: str) -> tuple[str, str, str] | None:
        quote: str | None = None
        escaped = False
        idx = 0
        while idx < len(command):
            char = command[idx]
            if escaped:
                escaped = False
                idx += 1
                continue
            if char == "\\":
                escaped = True
                idx += 1
                continue
            if quote:
                if char == quote:
                    quote = None
                idx += 1
                continue
            if char in {"'", '"'}:
                quote = char
                idx += 1
                continue
            if char == ">":
                op = ">>" if idx + 1 < len(command) and command[idx + 1] == ">" else ">"
                rest = command[idx + len(op) :].strip()
                if not rest or rest.startswith("&"):
                    idx += len(op)
                    continue
                try:
                    target = shlex.split(rest, posix=True)[0]
                except (IndexError, ValueError):
                    target = rest.split(maxsplit=1)[0] if rest else ""
                if target:
                    return command[:idx].strip(), op, target
                idx += len(op)
                continue
            idx += 1
        return None

    @staticmethod
    def _strip_leading_sudo_for_redirection(tokens: list[str]) -> list[str]:
        if not tokens or tokens[0] != "sudo":
            return tokens
        idx = 1
        while idx < len(tokens):
            token = tokens[idx]
            if token in {"-u", "-g", "-h", "-D", "-p", "-r", "-t", "-T", "-C", "-R"}:
                idx += 2
                continue
            if token.startswith("-"):
                idx += 1
                continue
            break
        return tokens[idx:]

    def _sudo_tee_for_privileged_redirection(self, command: str) -> str:
        split = self._split_shell_redirection(command)
        if split is None:
            return command
        left, op, target = split
        if not self._is_privileged_redirect_target(target):
            return command
        if any(separator in left for separator in ("|", "&&", "||", ";")):
            return command
        try:
            tokens = shlex.split(left, posix=True)
        except ValueError:
            return command
        tokens = self._strip_leading_sudo_for_redirection(tokens)
        if not tokens:
            return command
        tool = tokens[0].split("/")[-1].lower()
        if tool not in {"echo", "printf"}:
            return command
        writer = shlex.join(tokens)
        tee = "tee -a" if op == ">>" else "tee"
        return f"{writer} | sudo {tee} {shlex.quote(target)} >/dev/null"

    def _is_arch_nvidia_driver_group(
        self, stage: SetupStage, group: CommandGroup, commands: list[str]
    ) -> bool:
        facts = self.ctx.facts or SetupFacts()
        if stage != SetupStage.POSTBOOT_DRIVER:
            return False
        if facts.package_manager != "pacman" or "nvidia" not in facts.gpu_vendors:
            return False
        text = " ".join([group.title, *group.packages, *commands]).lower()
        return "nvidia" in text or "nouveau" in text

    def _normalize_arch_nvidia_commands(self, commands: list[str]) -> list[str]:
        if self._arch_nvidia_requires_manual_package_resolution():
            return commands
        normalized: list[str] = []
        inserted_install = False
        for command in commands:
            stripped = command.strip()
            lower = stripped.lower()
            if not stripped:
                continue
            if "export kernel_pkg" in lower or "export nv_pkg" in lower:
                continue
            if "$nv_pkg" in lower:
                if not inserted_install:
                    normalized.append(self._arch_nvidia_install_command())
                    inserted_install = True
                continue
            if _ARCH_NVIDIA_INSTALL_RE.search(stripped):
                if not inserted_install:
                    normalized.append(self._arch_nvidia_install_command())
                    inserted_install = True
                continue
            if "modprobe.d/nvidia.conf" in lower or "blacklist-nouveau" in lower:
                continue
            if "mkinitcpio" in lower and " -p" in lower:
                continue
            normalized.append(stripped)
        if not inserted_install and any("nvidia" in command.lower() for command in commands):
            normalized.insert(0, self._arch_nvidia_install_command())
        for command in _ARCH_NVIDIA_BOOT_CONFIG_COMMANDS:
            if command not in normalized:
                normalized.append(command)
        return normalized

    def _needs_arch_aur_helper_bootstrap(self, commands: list[str]) -> bool:
        facts = self.ctx.facts or SetupFacts()
        if facts.package_manager != "pacman" or facts.community_helper in {"yay", "paru"}:
            return False
        return any(self._uses_arch_community_helper_command(command) for command in commands)

    def _arch_yay_bootstrap_command(self) -> str:
        facts = self.ctx.facts or SetupFacts()
        return _ARCH_ROOT_YAY_BOOTSTRAP_COMMAND if facts.is_root else _ARCH_YAY_BOOTSTRAP_COMMAND

    @staticmethod
    def _is_arch_yay_bootstrap_command(command: str) -> bool:
        lower = command.lower()
        return "quickhatch-yay-bin" in lower and "yay-bin.git" in lower and "makepkg" in lower

    def _arch_aur_helper_available(self) -> bool:
        facts = self.ctx.facts or SetupFacts()
        return facts.package_manager == "pacman" and facts.community_helper in {"yay", "paru"}

    def _rewrite_arch_community_helper_command(
        self,
        command: str,
        *,
        stage: SetupStage,
        group: CommandGroup,
    ) -> str:
        facts = self.ctx.facts or SetupFacts()
        helper = facts.community_helper
        if facts.package_manager != "pacman" or helper not in {"yay", "paru"}:
            return command
        helpers = arch_community_helpers_in_command(command)
        if not helpers:
            return command
        if helpers == {helper}:
            return (
                self._wrap_root_arch_community_helper_command(command) if facts.is_root else command
            )
        rewritten = command
        for other in {"yay", "paru"} - {helper}:
            rewritten = re.sub(rf"(?<![\w/-]){other}(?=\s)", helper, rewritten)
            rewritten = re.sub(
                rf"(?<![\w-])/usr/bin/{other}(?=\s)", f"/usr/bin/{helper}", rewritten
            )
        if rewritten != command:
            self.bridge.add_message(
                "agent",
                f"[harness] Rewriting AUR helper command to use detected `{helper}`.",
            )
            self.runlog.record(
                "arch_aur_helper_command_rewritten",
                stage=stage.value,
                group_id=group.id,
                original=command,
                rewritten=rewritten,
                helper=helper,
            )
        if facts.is_root and self._uses_arch_community_helper_command(rewritten):
            rewritten = self._wrap_root_arch_community_helper_command(rewritten)
        return rewritten

    def _wrap_root_arch_community_helper_command(self, command: str) -> str:
        if self._has_explicit_non_root_user_wrapper(command):
            return command
        wrapped = f"sudo -u {_ARCH_AUR_BUILD_USER} bash -lc {shlex.quote(command)}"
        self.bridge.add_message(
            "agent",
            f"[harness] Running AUR helper as `{_ARCH_AUR_BUILD_USER}` because makepkg/yay must not run as root.",
        )
        self.runlog.record(
            "arch_aur_helper_wrapped_root_command",
            original=command,
            rewritten=wrapped,
            build_user=_ARCH_AUR_BUILD_USER,
        )
        return wrapped

    @staticmethod
    def _has_explicit_non_root_user_wrapper(command: str) -> bool:
        return bool(
            re.search(r"\b(?:sudo|runuser)\s+(?:-[^\s]+\s+)*-u\s+(?!root\b)\S+", command)
            or re.search(r"\bsu\s+-?\s*(?!root\b)[A-Za-z_][A-Za-z0-9_-]*\b.*\s-c\b", command)
        )

    def _try_bootstrap_arch_aur_helper_for_command(
        self,
        command: str,
        *,
        stage: SetupStage,
        group: CommandGroup,
        stage_state: dict[str, Any],
    ) -> GroupFailure | None:
        facts = self.ctx.facts or SetupFacts()
        if (
            facts.package_manager != "pacman"
            or facts.community_helper in {"yay", "paru"}
            or not self._uses_arch_community_helper_command(command)
        ):
            return None
        if stage_state.get("arch_aur_helper_bootstrap_attempted"):
            return None
        stage_state["arch_aur_helper_bootstrap_attempted"] = True
        bootstrap_command = self._arch_yay_bootstrap_command()
        self.bridge.add_message(
            "agent",
            "AUR helper is missing; installing `yay` before retrying the planned community command.",
        )
        self.bridge.add_message("agent", f"`{bootstrap_command}`")
        self.runlog.record(
            "arch_aur_helper_bootstrap_started",
            stage=stage.value,
            group_id=group.id,
            command=bootstrap_command,
            blocked_command=command,
        )
        rc, stdout, stderr = self.runtime.ssh_exec(
            bootstrap_command,
            self._command_timeout(bootstrap_command),
        )
        self.ctx.results_log.append(f"$ {bootstrap_command}\n[rc={rc}]\n{stdout}\n{stderr}".strip())
        self.ctx.results_log[:] = self.ctx.results_log[-self.runtime.results_log_limit :]
        self.runlog.record(
            "arch_aur_helper_bootstrap_result",
            stage=stage.value,
            group_id=group.id,
            rc=rc,
            stdout_tail=(stdout or "")[-800:],
            stderr_tail=(stderr or "")[-800:],
        )
        if rc != 0:
            detail = stderr.strip() or stdout.strip() or "failed to install yay"
            self.bridge.add_message(
                "agent",
                self._format_group_failure_message(
                    group.title,
                    "AUR helper bootstrap failed",
                    bootstrap_command,
                    rc,
                    stdout,
                    stderr,
                ),
            )
            return GroupFailure(
                failure_type="command",
                command=bootstrap_command,
                stdout=stdout,
                stderr=stderr,
                detail=detail,
            )
        self._update_from_command(bootstrap_command, stdout, stderr, stage=stage)
        facts = self._refresh_facts(stage=stage)
        if facts.package_manager == "pacman" and facts.community_helper not in {"yay", "paru"}:
            facts.community_helper = "yay"
        self.bridge.add_message("agent", "✓ AUR helper installed — retrying planned command.")
        return None

    @staticmethod
    def _uses_arch_community_helper_command(command: str) -> bool:
        return bool(arch_community_helpers_in_command(command))

    def _arch_nvidia_install_command(self) -> str:
        packages = " ".join(self._arch_nvidia_packages())
        return f"sudo pacman -Syu --needed --noconfirm {packages}"

    def _arch_nvidia_packages(self) -> tuple[str, ...]:
        facts = self.ctx.facts or SetupFacts()
        kernel_release = (facts.kernel_release or "").lower()
        if "-lts" in kernel_release:
            return _ARCH_NVIDIA_LTS_OPEN_PACKAGES
        if "-zen" in kernel_release:
            return _ARCH_NVIDIA_ZEN_OPEN_PACKAGES
        if "-hardened" in kernel_release:
            return _ARCH_NVIDIA_HARDENED_OPEN_PACKAGES
        return _ARCH_NVIDIA_OPEN_PACKAGES

    def _arch_nvidia_requires_manual_package_resolution(self) -> bool:
        facts = self.ctx.facts or SetupFacts()
        return bool(facts.gpu_inventory and not nvidia_open_kernel_supported(facts))

    def _maybe_reboot_for_nvidia_driver(
        self,
        stage: SetupStage,
        group: CommandGroup,
        stage_state: dict[str, Any],
    ) -> None:
        commands = [*group.commands, *group.verification]
        if not self._is_arch_nvidia_driver_group(stage, group, commands):
            return
        if stage_state.get("nvidia_reboot_attempted"):
            return
        facts = self._refresh_facts(stage=stage)
        if facts.nvidia_smi_state == "ok" and not facts.nvidia_nouveau_bound:
            return
        if not facts.nvidia_nouveau_bound and facts.nvidia_smi_state not in {
            "present_but_failing",
            "missing",
        }:
            return
        stage_state["nvidia_reboot_attempted"] = True
        command = "sudo systemctl enable sshd NetworkManager || true; sudo reboot"
        self.bridge.add_message(
            "agent",
            "NVIDIA driver changes need a controlled reboot before verification.",
        )
        self.bridge.add_message("agent", f"`{command}`")
        self.runlog.record(
            "nvidia_driver_reboot",
            stage=stage.value,
            group_id=group.id,
            facts=asdict(facts),
            command=command,
        )
        rc, stdout, stderr = self.runtime.ssh_exec(command, 60)
        self.ctx.results_log.append(f"$ {command}\n[rc={rc}]\n{stdout}\n{stderr}".strip())
        self.ctx.results_log[:] = self.ctx.results_log[-self.runtime.results_log_limit :]
        if rc != 0:
            raise RuntimeError(
                "Failed to reboot after NVIDIA driver configuration: "
                f"{(stderr or stdout or '').strip()[:240]}"
            )
        reconnected, fresh_info, new_user = self.runtime.wait_for_reconnect(self.runtime.user)
        self.runlog.record(
            "nvidia_driver_reconnect",
            stage=stage.value,
            group_id=group.id,
            reconnected=reconnected,
            fresh_info=fresh_info,
            new_user=new_user,
        )
        if not reconnected:
            raise RuntimeError(fresh_info or "Target did not reconnect after NVIDIA driver reboot.")
        if new_user:
            self.runtime.user = new_user
        self.ctx.system_info = fresh_info or self.ctx.system_info
        self._refresh_facts(stage=stage)

    def _needs_live_disk_prep(self, stage: SetupStage, commands: list[str]) -> bool:
        facts = self.ctx.facts or SetupFacts()
        if stage != SetupStage.INSTALL_OR_SKIP or not facts.is_live:
            return False
        return any(
            pattern.search(command)
            for command in commands
            for pattern in _LIVE_DISK_DESTRUCTIVE_PATTERNS
        )

    @staticmethod
    def _live_disk_prep_commands(commands: list[str]) -> list[str]:
        prep = list(_LIVE_DISK_PREP_BASE_COMMANDS)
        for disk in SetupOrchestrator._extract_destructive_target_disks(commands):
            disk_q = shlex.quote(disk)
            prep.append(
                f"for p in $(lsblk -lnpo NAME {disk_q} 2>/dev/null | tail -n +2 | tac); "
                'do umount "$p" 2>/dev/null || true; done'
            )
        return prep

    @staticmethod
    def _extract_destructive_target_disks(commands: list[str]) -> list[str]:
        disks: list[str] = []
        seen: set[str] = set()
        disk_re = re.compile(
            r"(/dev/(?:nvme\d+n\d+|mmcblk\d+|sd[a-z]+|vd[a-z]+|xvd[a-z]+))(?:p?\d+)?\b"
        )
        for command in commands:
            if not any(pattern.search(command) for pattern in _LIVE_DISK_DESTRUCTIVE_PATTERNS):
                continue
            for match in disk_re.finditer(command):
                disk = match.group(1)
                if disk not in seen:
                    seen.add(disk)
                    disks.append(disk)
        return disks

    @staticmethod
    def _first_live_disk_destructive_index(commands: list[str]) -> int:
        for index, command in enumerate(commands):
            if any(pattern.search(command) for pattern in _LIVE_DISK_DESTRUCTIVE_PATTERNS):
                return index
        return len(commands)

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
        repair_config.native_search = self.runtime.native_search_active
        return self._call_model_with_retries(repair_config, messages, context="repair")

    def _call_model_with_retries(
        self,
        model_config: AIConfig,
        messages: list[dict[str, str]],
        *,
        context: str,
    ) -> str:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            started = time.monotonic()
            try:
                return self._call_runtime_model_text(model_config, messages)
            except Exception as caught:
                error = caught
            elapsed = max(0.0, time.monotonic() - started)
            last_error = error
            self.runlog.record(
                "model_call_failed",
                context=context,
                attempt=attempt,
                elapsed_seconds=round(elapsed, 1),
                timeout_seconds=_SETUP_MODEL_CALL_TIMEOUT_SECONDS,
                error=str(error),
            )
            if attempt >= 3:
                break
            delay = min(3 * attempt, 12)
            self.bridge.add_message(
                "agent",
                f"⚠️ {context.capitalize()} LLM call failed ({attempt}/3; "
                f"attempt took {elapsed:.0f}s, timeout {_SETUP_MODEL_CALL_TIMEOUT_SECONDS}s): "
                f"{str(error)[:220]}. Retrying in {delay}s…",
            )
            time.sleep(delay)
        raise RuntimeError(f"{context.capitalize()} LLM call failed after 3 attempts: {last_error}")

    def _call_runtime_model_text(
        self, model_config: AIConfig, messages: list[dict[str, str]]
    ) -> str:
        try:
            return self.runtime.call_model_text(
                model_config,
                messages,
                _SETUP_MODEL_CALL_TIMEOUT_SECONDS,
            )
        except TypeError as exc:
            if not self._looks_like_timeout_callback_mismatch(exc):
                raise
            return self.runtime.call_model_text(model_config, messages)

    @staticmethod
    def _looks_like_timeout_callback_mismatch(exc: TypeError) -> bool:
        message = str(exc)
        return (
            "positional" in message
            or "required positional" in message
            or "unexpected keyword" in message
            or "takes" in message
            or "argument" in message
        )

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
            native_search_active=self.runtime.native_search_active,
        )
        self.runlog.record(
            "repair_prompt",
            stage=stage.value,
            group_id=failed_group.id,
            repair_attempt=repair_attempt,
            prompt=prompt,
        )
        messages = [{"role": "user", "content": prompt}]
        search_performed = False
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
                search_performed = True
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
            if (
                action["action"] == "retry_group"
                and self._repair_requires_research(stage, failed_group, failure)
                and not search_performed
                and not action["group"].evidence
            ):
                self.runlog.record(
                    "repair_retry_rejected_missing_evidence",
                    stage=stage.value,
                    group_id=failed_group.id,
                    repair_attempt=repair_attempt,
                    repair_turn=turn,
                )
                messages = [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": response_text},
                    {
                        "role": "user",
                        "content": (
                            "[quickhatch repair policy]\n"
                            "Do not retry this package/driver/desktop/app failure without fresh evidence. "
                            "First return repo_search, community_search, or web_search; or return retry_group "
                            "with group.evidence explaining the current source-backed basis for each changed command."
                        ),
                    },
                ]
                continue
            return action
        raise RuntimeError(f"Repair loop exhausted for '{failed_group.title}'.")

    @staticmethod
    def _repair_requires_research(
        stage: SetupStage, failed_group: CommandGroup, failure: GroupFailure
    ) -> bool:
        text = " ".join(
            [
                stage.value,
                failed_group.title,
                failed_group.source,
                failure.command,
                failure.detail,
                " ".join(failed_group.packages),
            ]
        ).lower()
        if stage in {SetupStage.POSTBOOT_DRIVER, SetupStage.DESKTOP, SetupStage.APPS}:
            return True
        return any(
            marker in text
            for marker in (
                "pacman",
                "apt",
                "dnf",
                "zypper",
                "apk",
                "xbps",
                "emerge",
                "package",
                "driver",
                "nvidia",
                "theme",
                "desktop",
                "aur",
                "yay",
                "paru",
            )
        )

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

    def _mark_unanswered_optional_slow_apps_manual_followup(self) -> None:
        if not self.ctx.plan:
            return
        names: list[str] = []
        for app in self.ctx.plan.apps:
            if app.status != PlanItemStatus.OPTIONAL_SLOW_PENDING_CONFIRMATION:
                continue
            app.status = PlanItemStatus.MANUAL_FOLLOWUP
            app.reason = (
                app.reason
                or "Optional slow app was not explicitly confirmed during setup; left for manual follow-up."
            )
            names.append(app.name)
        if not names:
            return
        self._record_optional_slow_apps_manual_followup(names)

    def _record_optional_slow_apps_manual_followup(self, names: list[str]) -> None:
        if not names:
            return
        reason = "Optional slow app(s) left for manual follow-up: " + ", ".join(sorted(names))
        self.ctx.stage_state.setdefault(SetupStage.APPS.value, {}).setdefault(
            "manual_followups", []
        ).append({"reason": reason, "apps": sorted(names)})
        self.runlog.record(
            "optional_slow_manual_followup",
            stage=SetupStage.APPS.value,
            apps=sorted(names),
            reason=reason,
        )
        self.bridge.add_message("agent", reason)

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
        command_text = " ".join([*group.commands, *group.verification, group.title]).lower()
        for app in self.ctx.plan.apps:
            variants = self._package_name_variants(app.name, app.package_name)
            if variants and variants.intersection(group_pkgs):
                app.status = PlanItemStatus.INSTALLED
                continue
            if any(
                self._variant_appears_in_command_text(variant, command_text) for variant in variants
            ):
                app.status = PlanItemStatus.INSTALLED
                continue
            if app.name.lower() in title and app.status == PlanItemStatus.PENDING:
                app.status = PlanItemStatus.INSTALLED

    def _update_from_command(
        self, command: str, stdout: str, stderr: str, *, stage: SetupStage
    ) -> None:
        del stderr
        if stage == SetupStage.INSTALL_OR_SKIP and re.search(r"\b(?:useradd|adduser)\b", command):
            created_user = self._extract_created_user(command)
            if created_user:
                self.ctx.created_user = created_user
        if self._is_arch_yay_bootstrap_command(command) and self.ctx.facts is not None:
            self.ctx.facts.community_helper = "yay"
        if stage == SetupStage.DESKTOP:
            state = self.ctx.stage_state.setdefault(stage.value, {})
            if self._command_persists_desktop_config(command):
                state["persisted_config_written"] = True
            if self._command_attempts_desktop_design(command):
                state["desktop_design_attempted"] = True
            if self._command_attempts_wallpaper(command):
                state["wallpaper_attempted"] = True
            if "plasma-apply" in command and stdout.strip() == "":
                if self.ctx.facts and self.ctx.facts.displays:
                    self.ctx.facts.desktop_runtime_state = DesktopRuntimeState.LIVE_APPLY_SUCCEEDED

    @staticmethod
    def _command_persists_desktop_config(command: str) -> bool:
        lowered = command.lower()
        return any(
            token in lowered
            for token in (
                "kwriteconfig",
                "gsettings",
                "xfconf-query",
                "systemctl enable gdm",
                "systemctl enable --now gdm",
                "systemctl enable sddm",
                "systemctl enable --now sddm",
                "systemctl enable lightdm",
                "systemctl enable --now lightdm",
                ".dmrc",
                "/.config/",
                "/etc/sddm.conf",
                "/etc/sddm.conf.d/",
                "/etc/lightdm/",
                "kscreen",
                "kdeglobals",
            )
        )

    @staticmethod
    def _command_attempts_desktop_design(command: str) -> bool:
        lowered = command.lower()
        return any(
            token in lowered
            for token in (
                "plasma-apply-lookandfeel",
                "plasma-apply-colorscheme",
                "plasma-apply-desktoptheme",
                "plasma-apply-wallpaperimage",
                "lookandfeeltool",
                "gsettings set org.gnome.desktop.interface gtk-theme",
                "gsettings set org.gnome.desktop.interface color-scheme",
                "gsettings set org.gnome.desktop.background",
                "dconf write",
                "xfconf-query",
                "swww ",
                "hyprpaper",
                "nitrogen",
                "feh --bg",
                "colorscheme",
                "lookandfeel",
                "wallpaper",
            )
        )

    @staticmethod
    def _command_attempts_wallpaper(command: str) -> bool:
        lowered = command.lower()
        return any(
            token in lowered
            for token in (
                "wallpaper",
                "picture-uri",
                "plasma-apply-wallpaperimage",
                "swww img",
                "hyprpaper",
                "nitrogen --set",
                "feh --bg",
            )
        )

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
    def _is_live_target_unmount_command(command: str) -> bool:
        return bool(
            re.search(
                r"(^|[;&|]\s*)(?:sudo\s+)?umount\s+(?:-[A-Za-z]+\s+)*(?:/mnt|/target)\b",
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
            matches = []
            chroot_match = re.search(
                r"(?:^|[;&|]\s*)(?:sudo\s+)?(?:(?:arch-chroot|chroot)\s+\S+\s+)?"
                r"(?:useradd|adduser)\b(?P<tail>[^;&|]*)",
                line,
            )
            if chroot_match:
                matches.append(chroot_match.group("tail"))
            matches.extend(
                match.group("tail")
                for match in re.finditer(
                    r"(?:^|[;&|]\s*|['\"])(?:sudo\s+)?(?:useradd|adduser)\b(?P<tail>[^;&|'\"]*)",
                    line,
                )
            )
            for tail in matches:
                user = SetupOrchestrator._extract_created_user_from_tail(
                    tail,
                    flags_with_values=flags_with_values,
                )
                if user:
                    return user
        return None

    @staticmethod
    def _extract_created_user_from_tail(tail: str, *, flags_with_values: set[str]) -> str | None:
        try:
            tokens = shlex.split(tail)
        except ValueError:
            tokens = tail.split()
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
        final_state = self.ctx.stage_state.get(SetupStage.FINAL_AUDIT.value, {})
        verified_names = {
            str(name).strip()
            for name in final_state.get("verified_installed_apps", []) or []
            if str(name).strip()
        }
        unverified_names = {
            str(name).strip()
            for name in final_state.get("unverified_installed_apps", []) or []
            if str(name).strip()
        }
        verified_installed = sorted(verified_names)
        manual_followup: dict[str, str] = {}
        skipped: list[str] = []
        for app in apps:
            if app.status == PlanItemStatus.EXPLICITLY_SKIPPED:
                skipped.append(app.name)
                continue
            if app.status == PlanItemStatus.MANUAL_FOLLOWUP:
                manual_followup[app.name] = app.reason or "manual follow-up"
                continue
            if app.status in {PlanItemStatus.COMPLETED, PlanItemStatus.INSTALLED}:
                if app.name in verified_names:
                    continue
                reason = app.reason or "not verified by final package-manager check"
                if app.name in unverified_names:
                    reason = app.reason or "not verified by final package-manager check"
                manual_followup[app.name] = reason
        stage_followup = []
        for stage_state in self.ctx.stage_state.values():
            for item in stage_state.get("manual_followups", []) or []:
                reason = str(item.get("reason", "") or "").strip()
                if reason:
                    stage_followup.append(reason)
        parts = [plan.target_summary if plan else "Remote setup completed."]
        if verified_installed:
            parts.append("Verified installed: " + ", ".join(verified_installed))
        if manual_followup:
            parts.append("Manual follow-up: " + ", ".join(sorted(manual_followup)))
        if skipped:
            parts.append("Skipped: " + ", ".join(sorted(skipped)))
        if stage_followup:
            parts.append("Manual follow-up: " + " | ".join(sorted(set(stage_followup))))
        return ". ".join(part for part in parts if part).strip()
