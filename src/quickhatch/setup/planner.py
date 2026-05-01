"""Research-plan prompt and validation helpers."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import Any

from quickhatch.analysis.app_catalog import selected_app_catalog_context
from quickhatch.config import UserProfile

from .types import (
    AppPlanItem,
    CommandGroup,
    PlanEvidence,
    PlanItemStatus,
    ResearchPlan,
    SetupFacts,
    SetupStage,
    StrategyDecision,
)

_OPTIONAL_SLOW_PACKAGES = {
    "pake-chatgpt",
    "chatgpt-desktop-bin",
    "chatgpt-desktop-unix-git",
    "claude-desktop-bin",
    "openai-codex-desktop",
    "codex-desktop-bin",
}

_STAGE_ALIASES = {
    "probe": SetupStage.CONNECT_AND_PROBE,
    "connect": SetupStage.CONNECT_AND_PROBE,
    "detect": SetupStage.CONNECT_AND_PROBE,
    "planning": SetupStage.RESEARCH_AND_PLAN,
    "research": SetupStage.RESEARCH_AND_PLAN,
    "plan": SetupStage.RESEARCH_AND_PLAN,
    "disk": SetupStage.INSTALL_OR_SKIP,
    "disk_prepare": SetupStage.INSTALL_OR_SKIP,
    "partition": SetupStage.INSTALL_OR_SKIP,
    "partitioning": SetupStage.INSTALL_OR_SKIP,
    "format": SetupStage.INSTALL_OR_SKIP,
    "formatting": SetupStage.INSTALL_OR_SKIP,
    "mount": SetupStage.INSTALL_OR_SKIP,
    "mounting": SetupStage.INSTALL_OR_SKIP,
    "base_install": SetupStage.INSTALL_OR_SKIP,
    "bootstrap": SetupStage.INSTALL_OR_SKIP,
    "pacstrap": SetupStage.INSTALL_OR_SKIP,
    "debootstrap": SetupStage.INSTALL_OR_SKIP,
    "chroot": SetupStage.INSTALL_OR_SKIP,
    "bootloader": SetupStage.INSTALL_OR_SKIP,
    "boot_loader": SetupStage.INSTALL_OR_SKIP,
    "initramfs": SetupStage.INSTALL_OR_SKIP,
    "reboot": SetupStage.INSTALL_OR_SKIP,
    "driver": SetupStage.POSTBOOT_DRIVER,
    "drivers": SetupStage.POSTBOOT_DRIVER,
    "gpu": SetupStage.POSTBOOT_DRIVER,
    "graphics": SetupStage.POSTBOOT_DRIVER,
    "nvidia": SetupStage.POSTBOOT_DRIVER,
    "postboot_drivers": SetupStage.POSTBOOT_DRIVER,
    "desktop_environment": SetupStage.DESKTOP,
    "display": SetupStage.DESKTOP,
    "display_manager": SetupStage.DESKTOP,
    "theme": SetupStage.DESKTOP,
    "theming": SetupStage.DESKTOP,
    "applications": SetupStage.APPS,
    "packages": SetupStage.APPS,
    "app_install": SetupStage.APPS,
    "final": SetupStage.FINAL_AUDIT,
    "audit": SetupStage.FINAL_AUDIT,
    "verify": SetupStage.FINAL_AUDIT,
    "verification": SetupStage.FINAL_AUDIT,
}

_PLACEHOLDER_COMMANDS = {
    "check",
    "verify",
    "validate",
    "test",
    "todo",
    "tbd",
    "n/a",
    "na",
    "none",
    "noop",
    "pass",
}

_PROSE_COMMAND_MARKERS = (
    " should ",
    " must ",
    " needs to ",
    " verify that ",
    " ensure that ",
    " confirm that ",
    " expected ",
)

_HEREDOC_OPERATOR_RE = re.compile(r"<<-?\s*['\"]?[A-Za-z_][A-Za-z0-9_]*['\"]?")
_HEREDOC_TERMINATOR_RE = re.compile(r"(?:[A-Za-z_]*EOF[A-Za-z0-9_]*|CHROOT_EOF|INSTALL_EOF)")
_FSTAB_ROOT_GREP_RE = re.compile(
    r"^grep\s+-q(?:e|E)?\s+['\"](?:\\s|\s)*/(?:\\s|\s)*['\"]\s+/mnt/etc/fstab$",
    re.IGNORECASE,
)

_EXECUTABLE_COMMAND_GROUP_STAGES = {
    SetupStage.INSTALL_OR_SKIP,
    SetupStage.POSTBOOT_DRIVER,
    SetupStage.DESKTOP,
    SetupStage.APPS,
}

_REPO_SOURCE_ALIASES = {
    "repo",
    "repos",
    "official",
    "official_repo",
    "official-repo",
    "official_docs",
    "repo_search",
    "selected_app_catalog",
    "distro_primer",
    "distribution_repo",
    "distribution-repo",
    "distro_repo",
    "distro-repo",
    "vendor_repo",
    "vendor-repo",
    "external_repo",
    "external-repo",
    "third_party_repo",
    "third-party-repo",
    "thirdparty_repo",
    "thirdparty-repo",
    "repo_external",
    "repo-external",
    "brave_repo",
    "brave-repo",
    "rpmfusion",
    "rpm_fusion",
    "rpm-fusion",
    "repo_rpmfusion",
    "repo-rpmfusion",
    "rpmfusion_repo",
    "rpmfusion-repo",
    "rpm_fusion_repo",
    "rpm-fusion-repo",
    "package_manager",
    "package-manager",
    "pacman",
    "apt",
    "apt_get",
    "apt-get",
    "dnf",
    "zypper",
    "apk",
    "xbps",
    "xbps_install",
    "xbps-install",
    "emerge",
    "portage",
    "core",
    "extra",
    "multilib",
}

_COMMUNITY_SOURCE_ALIASES = {
    "community",
    "aur",
    "aur_binary",
    "aur-binary",
    "aur_source",
    "aur-source",
    "community_search",
    "yay",
    "paru",
}


class ResearchPlanValidationError(ValueError):
    """Raised when planner output is malformed or incomplete."""


class RepairActionValidationError(ValueError):
    """Raised when a repair action is malformed or incomplete."""


def _coerce_stage(value: str) -> SetupStage:
    normalized = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in _STAGE_ALIASES:
        return _STAGE_ALIASES[normalized]
    try:
        return SetupStage(normalized)
    except ValueError as exc:
        raise ResearchPlanValidationError(f"Unknown setup stage: {value}") from exc


def _coerce_status(value: str | None) -> PlanItemStatus:
    if not value:
        return PlanItemStatus.PENDING
    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "ready": PlanItemStatus.PENDING,
        "planned": PlanItemStatus.PENDING,
        "plan": PlanItemStatus.PENDING,
        "queued": PlanItemStatus.PENDING,
        "todo": PlanItemStatus.PENDING,
        "to_do": PlanItemStatus.PENDING,
        "needed": PlanItemStatus.PENDING,
        "required": PlanItemStatus.PENDING,
        "install": PlanItemStatus.PENDING,
        "to_install": PlanItemStatus.PENDING,
        "install_required": PlanItemStatus.PENDING,
        "needs_install": PlanItemStatus.PENDING,
        "resolved": PlanItemStatus.PENDING,
        "found": PlanItemStatus.PENDING,
        "uninstalled": PlanItemStatus.PENDING,
        "not_installed": PlanItemStatus.PENDING,
        "not_yet_installed": PlanItemStatus.PENDING,
        "missing": PlanItemStatus.PENDING,
        "absent": PlanItemStatus.PENDING,
        "unresolved": PlanItemStatus.PENDING,
        "available": PlanItemStatus.PENDING,
        "native": PlanItemStatus.PENDING,
        "native_available": PlanItemStatus.PENDING,
        "ok": PlanItemStatus.COMPLETED,
        "success": PlanItemStatus.COMPLETED,
        "succeeded": PlanItemStatus.COMPLETED,
        "done": PlanItemStatus.COMPLETED,
        "complete": PlanItemStatus.COMPLETED,
        "installed_successfully": PlanItemStatus.INSTALLED,
        "skipped": PlanItemStatus.EXPLICITLY_SKIPPED,
        "skip": PlanItemStatus.EXPLICITLY_SKIPPED,
        "not_applicable": PlanItemStatus.EXPLICITLY_SKIPPED,
        "n/a": PlanItemStatus.EXPLICITLY_SKIPPED,
        "na": PlanItemStatus.EXPLICITLY_SKIPPED,
        "manual": PlanItemStatus.MANUAL_FOLLOWUP,
        "manual_follow_up": PlanItemStatus.MANUAL_FOLLOWUP,
        "followup": PlanItemStatus.MANUAL_FOLLOWUP,
        "follow_up": PlanItemStatus.MANUAL_FOLLOWUP,
        "needs_user": PlanItemStatus.MANUAL_FOLLOWUP,
    }
    if normalized in aliases:
        return aliases[normalized]
    try:
        return PlanItemStatus(normalized)
    except ValueError as exc:
        raise ResearchPlanValidationError(f"Unknown plan item status: {value}") from exc


def _normalize_package_source(value: str | None) -> str:
    normalized = str(value or "").strip().lower().replace(" ", "_")
    if not normalized:
        return "manual_followup"
    if normalized in _REPO_SOURCE_ALIASES:
        return "repo"
    if normalized in _COMMUNITY_SOURCE_ALIASES:
        return "community"
    if normalized in {
        "manual",
        "manual_follow_up",
        "followup",
        "follow_up",
        "unsupported",
        "unknown",
        "none",
    }:
        return "manual_followup"
    return str(value or "manual_followup").strip()


def _extract_json_payload(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if len(lines) >= 2 else lines)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                raise ResearchPlanValidationError(
                    f"Planner did not return valid JSON: {exc}"
                ) from exc
        else:
            raise ResearchPlanValidationError(f"Planner did not return valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ResearchPlanValidationError("Planner output must be a single JSON object.")
    return data


def _parse_strategy(raw: Any, field_name: str) -> StrategyDecision:
    if not isinstance(raw, dict):
        raise ResearchPlanValidationError(f"{field_name} must be an object.")
    strategy = str(raw.get("strategy", "")).strip()
    if not strategy:
        raise ResearchPlanValidationError(f"{field_name}.strategy is required.")
    return StrategyDecision(
        strategy=strategy,
        confidence=str(raw.get("confidence", "manual_followup_required")),
        source=_normalize_package_source(str(raw.get("source", "manual_followup"))),
        packages=list(raw.get("packages", []) or []),
        services=list(raw.get("services", []) or []),
        notes=str(raw.get("notes", "") or ""),
        evidence=_parse_evidence(raw.get("evidence", []), field_name=f"{field_name}.evidence"),
    )


def _parse_evidence(raw: Any, *, field_name: str) -> list[PlanEvidence]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [{"source_type": "note", "summary": raw}]
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise ResearchPlanValidationError(f"{field_name} must be a list of evidence objects.")
    evidence: list[PlanEvidence] = []
    for index, item in enumerate(raw):
        if isinstance(item, str):
            item = {"source_type": "note", "summary": item}
        if not isinstance(item, dict):
            raise ResearchPlanValidationError(f"{field_name}[{index}] must be an evidence object.")
        source_type = str(item.get("source_type", item.get("type", "")) or "").strip()
        summary = str(item.get("summary", item.get("result", "")) or "").strip()
        if not source_type or not summary:
            raise ResearchPlanValidationError(
                f"{field_name}[{index}] needs source_type and summary."
            )
        evidence.append(
            PlanEvidence(
                source_type=source_type,
                summary=summary,
                query=str(item.get("query", "") or ""),
                url=str(item.get("url", "") or ""),
                title=str(item.get("title", "") or ""),
                confidence=str(item.get("confidence", "medium") or "medium"),
            )
        )
    return evidence


def _shell_command_items(raw: Any, *, field_name: str) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        for key in ("commands", "command", "checks", "check", "verification", "shell"):
            if key in raw:
                return _shell_command_items(raw[key], field_name=field_name)
        raise ResearchPlanValidationError(f"{field_name} must contain shell command(s).")
    if isinstance(raw, str):
        return [raw]
    if not isinstance(raw, list):
        raise ResearchPlanValidationError(f"{field_name} must be a list of shell commands.")
    items: list[Any] = []
    for item in raw:
        if isinstance(item, dict):
            items.extend(_shell_command_items(item, field_name=field_name))
        else:
            items.append(item)
    return items


def _normalize_shell_command(command: str) -> str:
    stripped = command.strip()
    normalized = " ".join(stripped.lower().split())
    if normalized in {"lsblk /mnt", "lsblk --fs /mnt", "lsblk -f /mnt"}:
        return "mountpoint -q /mnt"
    if normalized in {"lsblk /mnt/boot", "lsblk --fs /mnt/boot", "lsblk -f /mnt/boot"}:
        return "mountpoint -q /mnt/boot"
    if _FSTAB_ROOT_GREP_RE.match(stripped):
        return "awk '$2 == \"/\" { found=1 } END { exit found ? 0 : 1 }' /mnt/etc/fstab"
    return stripped


def _clean_shell_commands(raw: Any, *, field_name: str) -> list[str]:
    raw_items = _shell_command_items(raw, field_name=field_name)
    commands: list[str] = []
    for index, item in enumerate(raw_items):
        command = _normalize_shell_command(str(item or ""))
        if not command:
            continue
        normalized = command.lower()
        if (
            len(command) < 3
            or (len(command) == 1 and command.isalpha())
            or normalized in _PLACEHOLDER_COMMANDS
            or any(marker in f" {normalized} " for marker in _PROSE_COMMAND_MARKERS)
        ):
            raise ResearchPlanValidationError(
                f"{field_name}[{index}] is not a real shell command: {command!r}."
            )
        if _HEREDOC_OPERATOR_RE.search(command) or _HEREDOC_TERMINATOR_RE.fullmatch(
            command.strip()
        ):
            raise ResearchPlanValidationError(
                f"{field_name}[{index}] uses heredoc syntax: {command!r}. "
                "Each command runs in a separate SSH session; use a single quoted "
                "`bash -lc`/`sh -c` command or direct file edits instead."
            )
        commands.append(command)
    return commands


def _parse_command_group(item: Any) -> CommandGroup:
    if not isinstance(item, dict):
        raise ResearchPlanValidationError("Each command group must be an object.")
    gid = str(item.get("id", "")).strip()
    title = str(item.get("title", "")).strip()
    stage = _coerce_stage(str(item.get("stage", "")).strip())
    if stage not in _EXECUTABLE_COMMAND_GROUP_STAGES:
        raise ResearchPlanValidationError(
            f"command_groups[{gid or title or '?'}] uses harness-owned stage "
            f"{stage.value}; executable command groups must use install_or_skip, "
            "postboot_driver, desktop, or apps."
        )
    cleaned_commands = _clean_shell_commands(
        item.get("commands", []), field_name=f"command_groups[{gid or title or '?'}].commands"
    )
    if not gid or not title or not cleaned_commands:
        raise ResearchPlanValidationError(
            "Each command group needs id, title, stage, and at least one command."
        )
    optional = bool(item.get("optional", False))
    optional_slow = bool(item.get("optional_slow", False)) and optional
    return CommandGroup(
        id=gid,
        stage=stage,
        title=title,
        commands=cleaned_commands,
        verification=_clean_shell_commands(
            item.get("verification", []),
            field_name=f"command_groups[{gid or title or '?'}].verification",
        ),
        rationale=str(item.get("rationale", "") or ""),
        source=_normalize_package_source(str(item.get("source", "repo") or "repo")),
        packages=[str(pkg) for pkg in (item.get("packages", []) or []) if str(pkg).strip()],
        optional=optional,
        optional_slow=optional_slow,
        status=_coerce_status(item.get("status")),
        evidence=_parse_evidence(
            item.get("evidence", []),
            field_name=f"command_groups[{gid or title or '?'}].evidence",
        ),
    )


def _parse_command_groups(raw: Any) -> list[CommandGroup]:
    if not isinstance(raw, list) or not raw:
        raise ResearchPlanValidationError("command_groups must be a non-empty list.")
    groups: list[CommandGroup] = []
    for item in raw:
        groups.append(_parse_command_group(item))
    return groups


def _normalize_app_items(raw: Any, requested: list[str]) -> list[AppPlanItem]:
    items: list[AppPlanItem] = []
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ResearchPlanValidationError("apps must be a list.")
    for item in raw:
        if not isinstance(item, dict):
            raise ResearchPlanValidationError("Each app plan item must be an object.")
        name = str(item.get("name", "")).strip()
        if not name:
            raise ResearchPlanValidationError("Each app plan item needs a name.")
        package_name = str(item.get("package_name", item.get("package", "")) or "").strip()
        source = _normalize_package_source(
            str(item.get("source", "manual_followup") or "manual_followup")
        )
        optional_slow = (
            bool(item.get("optional_slow", False)) or package_name in _OPTIONAL_SLOW_PACKAGES
        )
        status = _coerce_status(item.get("status"))
        if status in {
            PlanItemStatus.INSTALLED,
            PlanItemStatus.COMPLETED,
            PlanItemStatus.IN_PROGRESS,
        }:
            # The planner is a plan, not evidence. Execution/audit code
            # must prove installed/completed state from commands or remote
            # package verification before final success can rely on it.
            status = PlanItemStatus.PENDING
        if optional_slow and status == PlanItemStatus.PENDING:
            status = PlanItemStatus.OPTIONAL_SLOW_PENDING_CONFIRMATION
        items.append(
            AppPlanItem(
                name=name,
                package_name=package_name,
                source=source,
                status=status,
                required=bool(item.get("required", name in requested)),
                optional_slow=optional_slow,
                reason=str(item.get("reason", "") or ""),
                evidence=_parse_evidence(
                    item.get("evidence", []),
                    field_name=f"apps[{name}].evidence",
                ),
            )
        )
    requested_lower = {name.lower(): name for name in requested}
    known = {key for item in items for key in {item.name.lower(), item.package_name.lower()} if key}
    for lower, original in requested_lower.items():
        if lower in known:
            continue
        items.append(
            AppPlanItem(
                name=original,
                source="manual_followup",
                status=PlanItemStatus.MANUAL_FOLLOWUP,
                required=True,
                reason="Planner did not resolve this requested app yet.",
            )
        )
    return items


def extract_research_plan(text: str, profile: UserProfile) -> ResearchPlan:
    payload = _extract_json_payload(text)
    target_summary = str(payload.get("target_summary", "")).strip()
    distro_family = str(payload.get("distro_family", "")).strip()
    package_manager = str(payload.get("package_manager", "")).strip()
    if not target_summary or not distro_family or not package_manager:
        raise ResearchPlanValidationError(
            "Planner output must include target_summary, distro_family, and package_manager."
        )
    driver = _parse_strategy(payload.get("driver_strategy"), "driver_strategy")
    desktop = _parse_strategy(payload.get("desktop_strategy"), "desktop_strategy")
    command_groups = _parse_command_groups(payload.get("command_groups"))
    apps = _normalize_app_items(payload.get("apps"), list(profile.apps_to_install or []))
    questions = list(payload.get("unresolved_questions", []) or [])
    for question in questions:
        if not isinstance(question, dict) or not str(question.get("question", "")).strip():
            raise ResearchPlanValidationError(
                "Each unresolved question must contain question text."
            )
        question.setdefault("required", True)
        question.setdefault("choices", None)
    verification_payload = payload.get("verification_commands", {}) or {}
    if not isinstance(verification_payload, dict):
        raise ResearchPlanValidationError("verification_commands must be an object keyed by stage.")
    verification_commands: dict[str, list[str]] = {}
    for stage_name, commands in verification_payload.items():
        stage = _coerce_stage(str(stage_name))
        verification_commands[stage.value] = _clean_shell_commands(
            commands,
            field_name=f"verification_commands.{stage.value}",
        )
    return ResearchPlan(
        target_summary=target_summary,
        distro_family=distro_family,
        package_manager=package_manager,
        driver_strategy=driver,
        desktop_strategy=desktop,
        command_groups=command_groups,
        apps=apps,
        unresolved_questions=questions,
        verification_commands=verification_commands,
        required_items=[
            str(item) for item in (payload.get("required_items", []) or []) if str(item).strip()
        ],
        optional_items=[
            str(item) for item in (payload.get("optional_items", []) or []) if str(item).strip()
        ],
        planner_revision=int(payload.get("planner_revision", 1) or 1),
    )


def build_research_prompt(
    profile: UserProfile,
    facts: SetupFacts,
    *,
    system_info: str,
    migration_plan_md: str,
    distro_primer_md: str,
    native_search_active: bool,
    target_distro_family: str = "",
) -> str:
    profile_payload = asdict(profile)
    if profile_payload.get("ai_config", {}).get("api_key"):
        profile_payload["ai_config"]["api_key"] = "***REDACTED***"
    profile_payload["ssh_key_status"] = {
        "public_key_path_configured": bool(getattr(profile, "ssh_public_key_path", None)),
        "harness_owns_authorized_keys_continuity": True,
        "planner_should_request_raw_key_material": False,
    }
    selected_app_catalog = selected_app_catalog_context(
        list(profile.apps_to_install or []),
        list(getattr(profile, "apps", []) or []),
        distro_family=target_distro_family or facts.distro_family,
    )
    if selected_app_catalog:
        profile_payload["selected_app_catalog"] = selected_app_catalog
    display_preferences = [
        asdict(display)
        for display in getattr(getattr(profile, "hardware", None), "displays", []) or []
    ]
    search_block = (
        "Provider-native search is ENABLED for this planning pass. Use it heavily for current package names, driver families, and distro-specific instructions.\n"
        "Harness planning actions are also available for auditable target-specific repo/community/web lookups when exact package facts need to be fed back into the next turn.\n"
        if native_search_active
        else "Provider-native search is NOT available here. Use the harness planning actions when exact package names or distro-specific steps are uncertain.\n"
    )
    response_contract = (
        "You are generating a staged remote-setup research plan. "
        "Return JSON only, with no markdown fences or prose. You may either:\n"
        '- emit one action object: {"action":"repo_search"|"community_search"|"web_search","query":"...","reason":"..."}\n'
        '- emit one action object: {"action":"question","question":"...","choices":[...],"required":true}\n'
        '- emit one final action object: {"action":"plan","plan":{...full plan schema below...}}\n'
        "- or emit the full plan schema directly as one JSON object.\n\n"
    )
    return (
        response_contract + "The plan schema MUST contain:\n"
        "- target_summary (string)\n"
        "- distro_family (string)\n"
        "- package_manager (string)\n"
        "- driver_strategy (object: strategy, confidence, source, packages, services, notes, evidence)\n"
        "- desktop_strategy (object: strategy, confidence, source, packages, services, notes, evidence)\n"
        "- command_groups (non-empty list; each item: id, stage, title, commands, verification, rationale, source, packages, optional, optional_slow, evidence)\n"
        "- apps (list; each item: name, package_name, source, status, required, optional_slow, reason, evidence)\n"
        "- evidence entries are objects: {source_type, summary, query, url, title, confidence}. Use source_type values such as repo_search, community_search, web_search, provider_native_search, official_docs, distro_primer, selected_app_catalog, local_facts, or user_profile.\n"
        "- unresolved_questions (list of objects with question, required, choices)\n"
        "- verification_commands (object keyed by stage)\n"
        "- required_items (list)\n"
        "- optional_items (list)\n"
        "- planner_revision (integer)\n\n"
        "Rules:\n"
        "- Executable command_groups may ONLY use these stages: install_or_skip, postboot_driver, desktop, or apps.\n"
        "- connect_and_probe, research_and_plan, and final_audit are harness-owned stages. Do not create command groups for them; rely on normalized facts or place checks in the verification for the relevant executable stage.\n"
        "- Put disk partitioning, formatting, mounting, base install, chroot configuration, initramfs, bootloader, and install reboot work under install_or_skip. Do not invent stages such as disk_prepare, partitioning, chroot, bootloader, or base_install.\n"
        "- Do not include final `umount /mnt`, `umount /target`, or `reboot` commands in command_groups. QuickHatch owns the guarded pre-reboot checks, SSH key continuity, unmount, reboot, and reconnect sequence after install_or_skip succeeds.\n"
        "- Do not ask the user for raw SSH key content. If ssh_key_status.public_key_path_configured is true, QuickHatch already has the public key path and will enforce authorized_keys continuity during guarded reboot. Your plan should create the target user and enable sshd, but QuickHatch handles final key copying/validation.\n"
        "- Put driver/GPU work under postboot_driver, display manager/theme/runtime desktop work under desktop, and requested application installs under apps.\n"
        "- No mutating work can happen before research_and_plan finishes.\n"
        "- Do not optimize for or replay any prior QuickHatch run. Treat the current normalized facts, user profile, migration plan, and distro primer as the only target. The target may be a VM, laptop, desktop, non-NVIDIA GPU, no discrete GPU, no reachable GUI session, a different package manager, or an already-installed configure-only system.\n"
        "- For VM guest drivers, trust virtualization/system_vendor/product_name facts over generic GPU adapter labels. Example: VirtualBox with VMSVGA may report a `VMware SVGA` display adapter; if VM integration is needed, use distro-provided VirtualBox guest utility packages, not VMware tools.\n"
        "- VM guest additions are optional unless the user explicitly requested VM integration. Do not install `virtualbox-guest-additions-iso`, do not run or recommend `VBoxLinuxAdditions.run`, and do not compile Guest Additions from the ISO during unattended setup; that path is kernel-fragile and can leave failed `vboxadd.service` units. If distro-provided guest packages are unavailable or fail against the current kernel, mark VM guest integration as manual_followup and continue with the generic display stack.\n"
        "- Never assume Arch, KDE, NVIDIA, AUR, a specific theme, a specific monitor layout, or a specific app set unless the current facts/profile/primer establish it. If an example or previous run conflicts with live facts, live facts win.\n"
        "- `commands`, command-group `verification`, and `verification_commands` must contain executable shell commands only. Do not write prose like `lsblk should show ...`; use commands such as `mountpoint /mnt` or `lsblk /dev/sda | grep -q '/mnt'`.\n"
        "- Each command item runs as its own SSH execution. Do NOT use heredoc openers/terminators such as `<<EOF` and `EOF_CHROOT`; they will be split incorrectly. For chroot batches, use explicit commands like `arch-chroot /mnt sh -c 'cmd1; cmd2'`, or edit files under `/mnt` directly.\n"
        '- Do not put standalone shell variable assignments in their own command item. Variables do not persist across SSH executions; combine assignment and use in the same command, e.g. `UCODE=$(...) && pacstrap ... "$UCODE"`.\n'
        "- On Arch, do not add `--noconfirm` to `pacstrap`; `pacstrap` rejects that flag. Use `pacstrap -K /mnt ...` and avoid provider prompts by naming concrete provider packages where needed.\n"
        "- On Ubuntu/Desktop or Debian-family live targets, full live install is supported through the debootstrap path. Prefer debootstrap over trying to drive graphical installers over SSH: install debootstrap in the live env, partition/mount the target, debootstrap the current release codename into `/mnt`, bind-mount dev/dev/pts/proc/sys/run before chroot commands, install `linux-generic`, `systemd-sysv`, `sudo`, `openssh-server`, `network-manager`, GRUB packages, and the selected desktop metapackage inside the chroot, create the target user, enable SSH/NetworkManager, write fstab, install GRUB for the detected boot mode, run `update-initramfs -u -k all` and `update-grub`, then let QuickHatch own guarded unmount/reboot/reconnect. Use separate executable commands; do not use heredocs.\n"
        "- For Ubuntu/Debian debootstrap from a live ISO, check whether `/cdrom/dists/<codename>` exists. If it does, prefer `file:///cdrom` or use it as the first fallback before slow internet mirrors; otherwise pick an evidence-backed fast HTTP(S) mirror instead of hardcoding one global mirror forever.\n"
        "- For Ubuntu/Debian desktop setup after debootstrap, do NOT install `ubuntu-desktop` or `ubuntu-desktop-minimal` in this remote harness. Those metapackages can pull Firefox/Snap transitional packages that block dpkg noninteractively. Install explicit GNOME core packages instead: `gnome-shell`, `gdm3`, `ubuntu-session`, `gnome-terminal`, `nautilus`, `gnome-control-center`, `yaru-theme-gtk`, `yaru-theme-icon`, `xdg-desktop-portal-gnome`, and VM/display packages as facts require. Do not add Mozilla/Firefox external repos, install Firefox, or force Snap unless the user explicitly requested Firefox/Snap.\n"
        "- Do not launch Ubuntu Desktop's graphical installer (`ubiquity`, `ubuntu-desktop-bootstrap`, or similar Flutter/bootstrap UI) over SSH. If you intentionally use Ubuntu autoinstall instead of debootstrap, it must be a current, noninteractive, evidence-backed Subiquity/autoinstall invocation; otherwise use debootstrap.\n"
        "- On Fedora-family live Workstation ISOs, do not attempt a full unattended OS install over SSH in this tranche. Do not run Anaconda/Kickstart as a normal SSH command, and do not emit experimental `dnf --installroot` disk-install groups. Ask the user to run the graphical installer manually, reboot, rerun quickstart.sh, and then continue configure-only on the installed Fedora system.\n"
        "- For fstab/root verification, avoid whitespace-sensitive checks like `grep -q ' / ' /mnt/etc/fstab`; fstab is tab/space delimited. Prefer `findmnt -R /mnt`, `mountpoint /mnt/boot`, or `awk '$2 == \"/\" {found=1} END{exit !found}' /mnt/etc/fstab`.\n"
        "- Every driver_strategy, desktop_strategy, command group, and resolved app SHOULD include evidence. If you used provider-native search, cite it as provider_native_search with a query/summary. If you used harness search results, cite repo_search/community_search/web_search. If an item only comes from local facts/user preference/catalog, cite local_facts/user_profile/selected_app_catalog and keep confidence honest.\n"
        "- Do not invent source URLs. If only native search was used and you do not have a URL, leave url empty and include query + summary.\n"
        "- Use official repos by default; only use community sources when needed and explicit.\n"
        "- On Arch, do not ask the user solely because `yay`/`paru` is absent. Prefer `yay` for AUR packages when no helper is detected; the harness can bootstrap it before execution. Avoid `paru` unless facts show it is installed.\n"
        "- Classify slow desktop wrappers (ChatGPT/Claude/Codex desktop wrappers, Electron bundle wrappers, etc.) as optional_slow only when the command group is optional. Never mark required base install, bootloader, driver, or core desktop groups as optional_slow.\n"
        "- Use selected_app_catalog as curated prior knowledge for selected apps. Prefer high-confidence source_hint values and active_install_hint when its family matches the target distro.\n"
        "- Treat active_install_hint as advisory package-resolution data, not proof of installation success. Verify package names with repo/community search when confidence is not high, source is manual_followup/web/alternative, or packages are empty.\n"
        "- If an app is unresolved, mark it manual_followup or ask a blocking question; do not hallucinate a package.\n"
        "- QuickHatch remote setup commands run on the Linux target, not on the Windows host. They cannot read arbitrary Windows-local paths. For data_transfer_method='keep_windows', QuickHatch performs a tiny host-to-target transfer smoke test during connect; use that only as evidence that SSH/scp transport works, not as evidence that user files were copied. For external_drive/cloud transfer methods, prepare Linux-side destinations/tools and write clear user instructions; do not claim Windows files were copied unless a command actually proves it on the target.\n"
        "- Use the scanned display info only as preference input; remote runtime facts are truth.\n"
        "- Split desktop work into separate groups: core desktop/display-manager package install, "
        "persistent user config writes, and live display/theme/wallpaper apply. Do not combine "
        "AUR package installs with `plasma-apply-*`, `kscreen-doctor`, `xrandr`, or `qdbus` in the "
        "same command group; the harness may skip live-apply groups when no target monitor is active.\n"
        "- Treat desktop design as a contract, not a vibe. desktop_strategy.notes should name the "
        "intended DE/session, global theme or color scheme, icon theme, cursor theme, wallpaper "
        "source/path, panel/dock decisions, and any effects. If the user did not request a custom "
        "look, say that defaults are intentional instead of inventing a rice.\n"
        "- Installing a theme/icon package is not the same as applying it. Design command groups "
        "must contain persistent config writes and verification commands that read the resulting "
        "state (`kreadconfig6`/config-file reads for KDE, `gsettings get` for GNOME, or the "
        "DE/window-manager's native config files for other desktops).\n"
        "- Desktop/theme groups must handle monitor reality, not just packages: write persistent "
        "theme/wallpaper/display configuration, then verify active outputs, resolution, and refresh "
        "with kscreen-doctor/xrandr when a GUI session is reachable. If no GUI session is reachable, "
        "state pending_relogin/manual_followup instead of claiming live theme, wallpaper, resolution, "
        "or refresh success.\n"
        "- Do not mark the run as complete. This is planning only.\n\n"
        + search_block
        + "\n== NORMALIZED SETUP FACTS ==\n"
        + json.dumps(asdict(facts), indent=2, default=str)
        + "\n\n== REMOTE SYSTEM INFO ==\n"
        + (system_info or "").strip()
        + "\n\n== DISPLAY PREFERENCES FROM SOURCE MACHINE ==\n"
        + json.dumps(display_preferences, indent=2)
        + "\n\n== DISTRO PRIMER ==\n"
        + (distro_primer_md or "").strip()
        + "\n\n== MIGRATION PLAN ==\n"
        + (migration_plan_md or "").strip()
        + "\n\n== USER PROFILE ==\n"
        + json.dumps(profile_payload, indent=2, default=str)
    )


def extract_repair_action(
    text: str,
    *,
    fallback_stage: SetupStage,
    failed_group: CommandGroup,
) -> dict[str, Any]:
    try:
        payload = _extract_json_payload(text)
    except ResearchPlanValidationError as exc:
        raise RepairActionValidationError(str(exc)) from exc
    action = str(payload.get("action", "")).strip()
    if action not in {
        "repo_search",
        "community_search",
        "web_search",
        "question",
        "retry_group",
        "manual_followup",
    }:
        raise RepairActionValidationError(f"Unsupported repair action: {action or '<empty>'}")
    if action in {"repo_search", "community_search", "web_search"}:
        query = str(payload.get("query", "")).strip()
        if not query:
            raise RepairActionValidationError(f"{action} requires a non-empty query.")
        return {
            "action": action,
            "query": query,
            "reason": str(payload.get("reason", "") or ""),
            "max_results": int(payload.get("max_results", 5) or 5),
        }
    if action == "question":
        question = str(payload.get("question", "")).strip()
        if not question:
            raise RepairActionValidationError("question action requires question text.")
        return {
            "action": action,
            "question": question,
            "choices": payload.get("choices"),
            "required": bool(payload.get("required", True)),
            "timeout_seconds": float(payload.get("timeout_seconds", 600) or 600),
        }
    if action == "manual_followup":
        return {
            "action": action,
            "reason": str(
                payload.get("reason", "") or "Model deferred this item to manual follow-up."
            ),
            "app_names": [
                str(name) for name in (payload.get("app_names", []) or []) if str(name).strip()
            ],
        }
    group_payload = payload.get("group")
    if not isinstance(group_payload, dict):
        raise RepairActionValidationError("retry_group requires a group object.")
    normalized_group = dict(group_payload)
    normalized_group.setdefault("id", f"{failed_group.id}-repair")
    normalized_group.setdefault("stage", fallback_stage.value)
    normalized_group.setdefault("title", f"{failed_group.title} (repair)")
    normalized_group.setdefault("verification", failed_group.verification)
    normalized_group.setdefault("source", failed_group.source)
    normalized_group.setdefault("packages", failed_group.packages)
    normalized_group.setdefault("optional", failed_group.optional)
    normalized_group.setdefault("optional_slow", failed_group.optional_slow)
    if "evidence" not in normalized_group and payload.get("evidence") is not None:
        normalized_group["evidence"] = payload.get("evidence")
    try:
        group = _parse_command_group(normalized_group)
    except ResearchPlanValidationError as exc:
        raise RepairActionValidationError(str(exc)) from exc
    if group.stage != fallback_stage:
        raise RepairActionValidationError(
            f"retry_group stage must remain {fallback_stage.value}, got {group.stage.value}."
        )
    return {"action": action, "group": group}


def build_repair_prompt(
    *,
    stage: SetupStage,
    failed_group: CommandGroup,
    failure_type: str,
    failed_command: str,
    stdout_tail: str,
    stderr_tail: str,
    facts: SetupFacts,
    recent_results: list[str],
    native_search_active: bool = False,
) -> str:
    recent_block = "\n\n".join(result.strip() for result in recent_results[-3:] if result.strip())
    search_mode = (
        "Provider-native search is ENABLED in repair mode. Use it when current package names, "
        "driver instructions, distro docs, project pages, or compatibility information are uncertain. "
        "Harness search actions are still allowed when you need auditable target-specific repo/AUR/web "
        "results fed back into the next repair turn.\n"
        if native_search_active
        else "Provider-native search is NOT available in repair mode. Use harness search actions if needed.\n"
    )
    return (
        "You are repairing ONE failed remote-setup command group.\n"
        "Return ONE JSON object only with no markdown fences or prose.\n\n"
        "Allowed actions:\n"
        '- {"action":"repo_search","query":"...","reason":"..."}\n'
        '- {"action":"community_search","query":"...","reason":"..."}\n'
        '- {"action":"web_search","query":"...","max_results":5,"reason":"..."}\n'
        '- {"action":"question","question":"...","choices":[...],"required":true}\n'
        '- {"action":"retry_group","group":{...replacement command group...}}\n'
        '- {"action":"manual_followup","reason":"...","app_names":["..."]}\n\n'
        "Rules:\n"
        "- Repair only the current failed group; do not redesign the whole run.\n"
        f"- {search_mode}"
        "- Search-first repair policy: for package, driver, desktop/theme, display, or app failures, your first repair action should normally be repo_search, community_search, or web_search. Do not guess another command until you have fresh evidence or the failure is obviously local state/permissions.\n"
        "- Prefer repo/community/web search over guessing package names.\n"
        "- For VM guest driver repairs, trust virtualization/system_vendor/product_name facts over generic GPU adapter labels. Example: VirtualBox with VMSVGA may report a `VMware SVGA` display adapter; if VM integration is needed, use distro-provided VirtualBox guest utility packages, not VMware tools.\n"
        "- Do not repair VM guest integration by installing `virtualbox-guest-additions-iso`, running or recommending `VBoxLinuxAdditions.run`, or compiling Guest Additions from the ISO. If official distro packages are missing, incompatible with the current kernel, or leave `vboxadd.service` failed, emit manual_followup for VM integration instead of keeping it as a required setup blocker; the follow-up should say to use distro docs/packages or skip advanced VM integration.\n"
        "- If you emit retry_group, include evidence on the replacement group unless the failure was purely local state/permissions. Evidence entries use {source_type, summary, query, url, title, confidence}.\n"
        "- If a desktop wrapper or other slow optional app is the problem, prefer manual_followup unless truly necessary.\n"
        "- For Ubuntu/Debian desktop repairs after debootstrap, do NOT use `ubuntu-desktop` or `ubuntu-desktop-minimal`. Use explicit GNOME/display-manager packages instead. Do not add Firefox/Mozilla repos or Snap just to repair a basic desktop unless the user explicitly requested that stack.\n"
        "- If you emit retry_group, keep it scoped to this stage and include at least one command.\n"
        "- Each retry command item runs as its own SSH execution. Do NOT emit heredoc syntax such as `<<EOF`/`EOF_CHROOT`; use explicit `arch-chroot /mnt sh -c '...'` commands or direct `/mnt` edits.\n"
        "- Do not emit standalone shell variable assignments as retry commands; combine assignment and use in the same command because each command gets a fresh SSH shell.\n"
        "- Do not repair Arch base install by adding `--noconfirm` to `pacstrap`; `pacstrap` rejects that flag. Keep `pacstrap -K /mnt ...` and fix package/provider ambiguity directly.\n"
        "- Do not repair fstab/root checks with whitespace-sensitive `grep -q ' / ' /mnt/etc/fstab`; use `findmnt`, `mountpoint`, or awk field checks.\n"
        "- Do not use broad `pkill -f`/`killall` patterns against apt, dpkg, debconf, snapd, PackageKit, or unattended-upgrades. They can kill the SSH command itself or leave dpkg half-configured. Stop exact services/timers, inspect lock holders, wait, or kill only a confirmed stale PID.\n"
        "- Do not emit final success prose.\n\n"
        f"== CURRENT STAGE ==\n{stage.value}\n\n"
        "== FAILED GROUP ==\n"
        + json.dumps(asdict(failed_group), indent=2, default=str)
        + "\n\n== FAILURE ==\n"
        + json.dumps(
            {
                "failure_type": failure_type,
                "failed_command": failed_command,
                "stdout_tail": stdout_tail[-1200:],
                "stderr_tail": stderr_tail[-1200:],
            },
            indent=2,
        )
        + "\n\n== NORMALIZED SETUP FACTS ==\n"
        + json.dumps(asdict(facts), indent=2, default=str)
        + "\n\n== RECENT EXECUTION RESULTS ==\n"
        + (recent_block or "(none)")
    )
