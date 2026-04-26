"""Research-plan prompt and validation helpers."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from quickhatch.analysis.app_catalog import selected_app_catalog_context
from quickhatch.config import UserProfile

from .types import (
    AppPlanItem,
    CommandGroup,
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
    try:
        return PlanItemStatus(value)
    except ValueError as exc:
        raise ResearchPlanValidationError(f"Unknown plan item status: {value}") from exc


def _extract_json_payload(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if len(lines) >= 2 else lines)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
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
        source=str(raw.get("source", "manual_followup")),
        packages=list(raw.get("packages", []) or []),
        services=list(raw.get("services", []) or []),
        notes=str(raw.get("notes", "") or ""),
    )


def _parse_command_group(item: Any) -> CommandGroup:
    if not isinstance(item, dict):
        raise ResearchPlanValidationError("Each command group must be an object.")
    gid = str(item.get("id", "")).strip()
    title = str(item.get("title", "")).strip()
    stage = _coerce_stage(str(item.get("stage", "")).strip())
    commands = list(item.get("commands", []) or [])
    cleaned_commands = [str(cmd) for cmd in commands if str(cmd).strip()]
    if not gid or not title or not cleaned_commands:
        raise ResearchPlanValidationError(
            "Each command group needs id, title, stage, and at least one command."
        )
    return CommandGroup(
        id=gid,
        stage=stage,
        title=title,
        commands=cleaned_commands,
        verification=[str(cmd) for cmd in (item.get("verification", []) or []) if str(cmd).strip()],
        rationale=str(item.get("rationale", "") or ""),
        source=str(item.get("source", "repo") or "repo"),
        packages=[str(pkg) for pkg in (item.get("packages", []) or []) if str(pkg).strip()],
        optional=bool(item.get("optional", False)),
        optional_slow=bool(item.get("optional_slow", False)),
        status=_coerce_status(item.get("status")),
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
        source = str(item.get("source", "manual_followup") or "manual_followup")
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
            )
        )
    requested_lower = {name.lower(): name for name in requested}
    known = {item.name.lower() for item in items}
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
    return ResearchPlan(
        target_summary=target_summary,
        distro_family=distro_family,
        package_manager=package_manager,
        driver_strategy=driver,
        desktop_strategy=desktop,
        command_groups=command_groups,
        apps=apps,
        unresolved_questions=questions,
        verification_commands={
            str(k): [str(cmd) for cmd in (v or []) if str(cmd).strip()]
            for k, v in (payload.get("verification_commands", {}) or {}).items()
        },
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
        "Do not mention harness lookup actions in the output.\n"
        if native_search_active
        else "Provider-native search is NOT available here. Use the harness planning actions when exact package names or distro-specific steps are uncertain.\n"
    )
    response_contract = (
        (
            "You are generating a STRICT remote-setup research plan. "
            "Return ONE JSON object only, with no markdown fences or prose.\n\n"
        )
        if native_search_active
        else (
            "You are generating a staged remote-setup research plan. "
            "Because native search is unavailable, you may use a small planning action loop.\n"
            "Return JSON only. You may either:\n"
            '- emit one action object: {"action":"repo_search"|"community_search"|"web_search","query":"...","reason":"..."}\n'
            '- emit one action object: {"action":"question","question":"...","choices":[...],"required":true}\n'
            '- or emit one final action object: {"action":"plan","plan":{...full plan schema below...}}\n\n'
        )
    )
    return (
        response_contract + "The plan schema MUST contain:\n"
        "- target_summary (string)\n"
        "- distro_family (string)\n"
        "- package_manager (string)\n"
        "- driver_strategy (object: strategy, confidence, source, packages, services, notes)\n"
        "- desktop_strategy (object: strategy, confidence, source, packages, services, notes)\n"
        "- command_groups (non-empty list; each item: id, stage, title, commands, verification, rationale, source, packages, optional, optional_slow)\n"
        "- apps (list; each item: name, package_name, source, status, required, optional_slow, reason)\n"
        "- unresolved_questions (list of objects with question, required, choices)\n"
        "- verification_commands (object keyed by stage)\n"
        "- required_items (list)\n"
        "- optional_items (list)\n"
        "- planner_revision (integer)\n\n"
        "Rules:\n"
        "- Every command group MUST belong to exactly one canonical stage: connect_and_probe, research_and_plan, install_or_skip, postboot_driver, desktop, apps, or final_audit.\n"
        "- Put disk partitioning, formatting, mounting, base install, chroot configuration, initramfs, bootloader, and install reboot work under install_or_skip. Do not invent stages such as disk_prepare, partitioning, chroot, bootloader, or base_install.\n"
        "- Put driver/GPU work under postboot_driver, display manager/theme/runtime desktop work under desktop, and requested application installs under apps.\n"
        "- No mutating work can happen before research_and_plan finishes.\n"
        "- Use official repos by default; only use community sources when needed and explicit.\n"
        "- Classify slow desktop wrappers (ChatGPT/Claude/Codex desktop wrappers, Electron bundle wrappers, etc.) as optional_slow.\n"
        "- Use selected_app_catalog as curated prior knowledge for selected apps. Prefer high-confidence source_hint values and active_install_hint when its family matches the target distro.\n"
        "- Treat active_install_hint as advisory package-resolution data, not proof of installation success. Verify package names with repo/community search when confidence is not high, source is manual_followup/web/alternative, or packages are empty.\n"
        "- If an app is unresolved, mark it manual_followup or ask a blocking question; do not hallucinate a package.\n"
        "- Use the scanned display info only as preference input; remote runtime facts are truth.\n"
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
    try:
        group = _parse_command_group(normalized_group)
    except ResearchPlanValidationError as exc:
        raise RepairActionValidationError(str(exc)) from exc
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
) -> str:
    recent_block = "\n\n".join(result.strip() for result in recent_results[-3:] if result.strip())
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
        "- Provider-native search is NOT available in repair mode. Use harness search actions if needed.\n"
        "- Prefer repo/community search over guessing package names.\n"
        "- If a desktop wrapper or other slow optional app is the problem, prefer manual_followup unless truly necessary.\n"
        "- If you emit retry_group, keep it scoped to this stage and include at least one command.\n"
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
