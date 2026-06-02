#!/usr/bin/env python3
"""Run a plan-only live model gauntlet against QuickHatch setup prompts.

The runner never executes model-generated shell commands. It builds the same
research/repair prompts the setup harness uses, sends them to a configured
provider when not in --dry-run mode, parses the response through the existing
planner validators, and records redacted metrics.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from quickhatch.config import AIConfig, UserProfile  # noqa: E402
from quickhatch.secrets import load_api_key  # noqa: E402
from quickhatch.setup.attempts import (  # noqa: E402
    build_attempt_signature,
    changed_strategy_allowed,
)
from quickhatch.setup.capabilities import dangerous_command_requirements  # noqa: E402
from quickhatch.setup.planner import (  # noqa: E402
    RepairActionValidationError,
    ResearchPlanValidationError,
    build_repair_prompt,
    build_research_prompt,
    extract_repair_action,
    extract_research_plan,
    model_json_loads,
)
from quickhatch.setup.runlog import redact_sensitive_text  # noqa: E402
from quickhatch.setup.types import (  # noqa: E402
    CommandGroup,
    DesktopDesignState,
    DesktopRuntimeState,
    PlanEvidence,
    PlanItemStatus,
    SetupFacts,
    SetupStage,
)

DEFAULT_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_MODEL = "gemini-3.1-flash-lite"
DEFAULT_PROVIDER = "api_gemini"
DEFAULT_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "live_model_gauntlet_requests.yaml"
DEFAULT_ENV_NAMES = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_AI_API_KEY",
    "AI_STUDIO_API_KEY",
    "QUICKHATCH_GEMINI_API_KEY",
)
SEARCH_ACTIONS = {"repo_search", "community_search", "web_search"}
SEARCH_EVIDENCE = {"repo_search", "community_search", "web_search"}


def load_cases(path: Path) -> list[dict[str, Any]]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list):
        raise ValueError(f"{path} must contain a top-level cases list.")
    for index, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            raise ValueError(f"case {index} must be an object.")
        for key in ("id", "kind", "facts"):
            if key not in case:
                raise ValueError(f"case {index} is missing {key!r}.")
        if case["kind"] not in {"plan", "repair"}:
            raise ValueError(f"case {case['id']} has unsupported kind {case['kind']!r}.")
        if case["kind"] == "plan" and "user_request" not in case:
            raise ValueError(f"plan case {case['id']} is missing 'user_request'.")
        if case["kind"] == "repair":
            for key in ("failed_group", "failure"):
                if key not in case:
                    raise ValueError(f"repair case {case['id']} is missing {key!r}.")
    return cases


def profile_from_case(case: dict[str, Any]) -> UserProfile:
    profile = UserProfile()
    profile.additional_info = str(case.get("user_request", "") or "")
    for key, value in (case.get("profile") or {}).items():
        if hasattr(profile, key):
            setattr(profile, key, value)
    return profile


def facts_from_case(case: dict[str, Any]) -> SetupFacts:
    payload = dict(case.get("facts") or {})
    if isinstance(payload.get("desktop_design"), dict):
        payload["desktop_design"] = DesktopDesignState(**payload["desktop_design"])
    if isinstance(payload.get("desktop_runtime_state"), str):
        try:
            payload["desktop_runtime_state"] = DesktopRuntimeState(payload["desktop_runtime_state"])
        except ValueError:
            pass
    allowed = {field.name for field in fields(SetupFacts)}
    return SetupFacts(**{key: value for key, value in payload.items() if key in allowed})


def command_group_from_case(payload: dict[str, Any]) -> CommandGroup:
    evidence = [
        PlanEvidence(**item) for item in payload.get("evidence", []) or [] if isinstance(item, dict)
    ]
    stage = SetupStage(str(payload.get("stage", SetupStage.APPS.value)))
    status_value = str(payload.get("status", PlanItemStatus.PENDING.value))
    try:
        status = PlanItemStatus(status_value)
    except ValueError:
        status = PlanItemStatus.PENDING
    return CommandGroup(
        id=str(payload.get("id") or "failed-group"),
        stage=stage,
        title=str(payload.get("title") or "Failed group"),
        commands=[str(command) for command in payload.get("commands", []) or []],
        verification=[str(command) for command in payload.get("verification", []) or []],
        rationale=str(payload.get("rationale", "") or ""),
        source=str(payload.get("source", "") or "repo"),
        packages=[str(package) for package in payload.get("packages", []) or []],
        optional=bool(payload.get("optional", False)),
        optional_slow=bool(payload.get("optional_slow", False)),
        status=status,
        evidence=evidence,
    )


def build_messages_for_case(case: dict[str, Any]) -> list[dict[str, str]]:
    facts = facts_from_case(case)
    if case["kind"] == "repair":
        failed_group = command_group_from_case(case["failed_group"])
        failure = case.get("failure") or {}
        prompt = build_repair_prompt(
            stage=failed_group.stage,
            failed_group=failed_group,
            failure_type=str(failure.get("failure_type", "command") or "command"),
            failed_command=str(failure.get("failed_command", "") or ""),
            stdout_tail=str(failure.get("stdout_tail", "") or ""),
            stderr_tail=str(failure.get("stderr_tail", "") or ""),
            facts=facts,
            recent_results=[str(result) for result in failure.get("recent_results", []) or []],
            native_search_active=False,
        )
        return [{"role": "user", "content": prompt}]

    prompt = build_research_prompt(
        profile_from_case(case),
        facts,
        system_info=str(case.get("system_info", "") or "Synthetic plan-only gauntlet target."),
        migration_plan_md=str(case.get("user_request", "") or ""),
        distro_primer_md=str(
            case.get("distro_primer", "")
            or "Synthetic gauntlet primer: follow current QuickHatch mode and safety rules."
        ),
        native_search_active=False,
        target_distro_family=facts.distro_family,
    )
    return [{"role": "user", "content": prompt}]


def _load_json_payload(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if len(lines) >= 2 else lines)
    try:
        return model_json_loads(raw)
    except Exception:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return model_json_loads(raw[start : end + 1])
        raise


def _walk_dicts(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        found.append(value)
        for child in value.values():
            found.extend(_walk_dicts(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_dicts(child))
    return found


def _commands_from_payload(payload: dict[str, Any]) -> list[str]:
    root = payload.get("plan") if payload.get("action") == "plan" else payload
    commands: list[str] = []
    if isinstance(root, dict):
        for group in root.get("command_groups", []) or []:
            if not isinstance(group, dict):
                continue
            commands.extend(str(command) for command in group.get("commands", []) or [])
            commands.extend(str(command) for command in group.get("verification", []) or [])
        for values in (root.get("verification_commands") or {}).values():
            commands.extend(str(command) for command in values or [])
    group = payload.get("group")
    if isinstance(group, dict):
        commands.extend(str(command) for command in group.get("commands", []) or [])
        commands.extend(str(command) for command in group.get("verification", []) or [])
    return [command for command in commands if command.strip()]


def _dangerous_command_count(payload: dict[str, Any]) -> int:
    count = 0
    for command in _commands_from_payload(payload):
        if dangerous_command_requirements(command):
            count += 1
    return count


def _uses_harness_search(payload: dict[str, Any]) -> bool:
    if str(payload.get("action", "")) in SEARCH_ACTIONS:
        return True
    for item in _walk_dicts(payload):
        source_type = str(item.get("source_type", "") or "")
        if source_type in SEARCH_EVIDENCE:
            return True
    return False


def _specific_text(value: str) -> bool:
    text = " ".join(str(value or "").split())
    generic = {"manual follow-up required", "not available", "failed", "unknown"}
    return len(text.split()) >= 5 and text.lower() not in generic


def _plan_manual_followup_quality(plan: Any) -> bool:
    for question in getattr(plan, "unresolved_questions", []) or []:
        if _specific_text(str(question.get("question", "") or "")):
            return True
    for app in getattr(plan, "apps", []) or []:
        status = getattr(app, "status", "")
        source = str(getattr(app, "source", "") or "")
        reason = str(getattr(app, "reason", "") or "")
        if status == PlanItemStatus.MANUAL_FOLLOWUP or source == "manual_followup":
            if str(getattr(app, "name", "") or "").strip() and _specific_text(reason):
                return True
    for strategy_name in ("driver_strategy", "desktop_strategy"):
        strategy = getattr(plan, strategy_name, None)
        if not strategy:
            continue
        if (
            str(getattr(strategy, "confidence", "") or "") == "manual_followup_required"
            or str(getattr(strategy, "source", "") or "") == "manual_followup"
        ) and _specific_text(str(getattr(strategy, "notes", "") or "")):
            return True
    return False


def _repair_manual_followup_quality(action: dict[str, Any], *, failed_group: CommandGroup) -> bool:
    if action.get("action") == "question":
        return _specific_text(str(action.get("question", "") or ""))
    if action.get("action") != "manual_followup":
        return False
    reason_ok = _specific_text(str(action.get("reason", "") or ""))
    if failed_group.stage != SetupStage.APPS:
        return reason_ok
    return reason_ok and bool(action.get("app_names"))


def _repair_unique(
    action: dict[str, Any],
    *,
    failed_group: CommandGroup,
    failure: dict[str, Any],
) -> bool:
    if action.get("action") in {*SEARCH_ACTIONS, "question", "manual_followup"}:
        return True
    group = action.get("group")
    if not isinstance(group, CommandGroup) or not group.commands:
        return False
    failed_command = str(failure.get("failed_command", "") or "")
    original = build_attempt_signature(
        stage=failed_group.stage.value,
        group_id=failed_group.id,
        group_title=failed_group.title,
        command=failed_command,
        stdout=str(failure.get("stdout_tail", "") or ""),
        stderr=str(failure.get("stderr_tail", "") or ""),
        detail=str(failure.get("failure_type", "") or ""),
        strategy=failed_group.source,
    )
    current = build_attempt_signature(
        stage=group.stage.value,
        group_id=group.id,
        group_title=group.title,
        command=group.commands[0],
        strategy=group.source,
    )
    return changed_strategy_allowed(original, current)


def evaluate_response(case: dict[str, Any], response_text: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": case["id"],
        "kind": case["kind"],
        "valid_structured_output": False,
        "wrong_mode_command": False,
        "dangerous_raw_command_count": 0,
        "search_used_when_needed": None,
        "manual_followup_quality": None,
        "repair_unique": None,
        "error": "",
    }
    try:
        payload = _load_json_payload(response_text)
        result["dangerous_raw_command_count"] = _dangerous_command_count(payload)
    except Exception as exc:
        result["error"] = f"json_parse: {exc}"
        return result

    uses_search = _uses_harness_search(payload)
    if bool(case.get("needs_search", False)):
        result["search_used_when_needed"] = uses_search

    if case["kind"] == "repair":
        failed_group = command_group_from_case(case["failed_group"])
        failure = case.get("failure") or {}
        try:
            action = extract_repair_action(
                json.dumps(payload),
                fallback_stage=failed_group.stage,
                failed_group=failed_group,
            )
            result["valid_structured_output"] = True
            result["repair_unique"] = _repair_unique(
                action, failed_group=failed_group, failure=failure
            )
            if bool(case.get("expects_manual_followup", False)):
                result["manual_followup_quality"] = _repair_manual_followup_quality(
                    action, failed_group=failed_group
                )
        except RepairActionValidationError as exc:
            result["error"] = str(exc)
        return result

    profile = profile_from_case(case)
    plan_text = (
        json.dumps(payload.get("plan")) if payload.get("action") == "plan" else json.dumps(payload)
    )
    if str(payload.get("action", "")) in SEARCH_ACTIONS:
        if str(payload.get("query", "") or "").strip():
            result["valid_structured_output"] = True
        return result
    if payload.get("action") == "question":
        result["valid_structured_output"] = bool(str(payload.get("question", "") or "").strip())
        if bool(case.get("expects_manual_followup", False)):
            result["manual_followup_quality"] = _specific_text(
                str(payload.get("question", "") or "")
            )
        return result
    try:
        plan = extract_research_plan(plan_text, profile, provider_native_search_active=False)
        result["valid_structured_output"] = True
        if bool(case.get("expects_manual_followup", False)):
            result["manual_followup_quality"] = _plan_manual_followup_quality(plan)
    except ResearchPlanValidationError as exc:
        error = str(exc)
        result["error"] = error
        result["wrong_mode_command"] = "Mode mismatch" in error
    return result


def summarize_results(results: list[dict[str, Any]], *, dry_run: bool) -> dict[str, Any]:
    evaluated = [result for result in results if not result.get("dry_run")]

    def rate(key: str) -> float | None:
        values = [bool(result[key]) for result in evaluated if result.get(key) is not None]
        if not values:
            return None
        return sum(values) / len(values)

    return {
        "dry_run": dry_run,
        "case_count": len(results),
        "evaluated_count": len(evaluated),
        "valid_structured_output_rate": rate("valid_structured_output"),
        "wrong_mode_command_rate": rate("wrong_mode_command"),
        "dangerous_raw_command_rate": (
            None
            if not evaluated
            else sum(1 for result in evaluated if result.get("dangerous_raw_command_count", 0) > 0)
            / len(evaluated)
        ),
        "search_usage_when_needed_rate": rate("search_used_when_needed"),
        "manual_followup_quality_rate": rate("manual_followup_quality"),
        "repair_uniqueness_rate": rate("repair_unique"),
        "errors": [
            {"id": result["id"], "error": result["error"]}
            for result in evaluated
            if result.get("error")
        ],
    }


def load_gauntlet_api_key(*, provider: str, endpoint: str, env_name: str | None) -> str | None:
    if env_name:
        return os.environ.get(env_name)
    for candidate in DEFAULT_ENV_NAMES:
        value = os.environ.get(candidate)
        if value:
            return value
    return load_api_key(provider, endpoint)


def call_model(
    *,
    provider: str,
    endpoint: str,
    model: str,
    api_key: str,
    messages: list[dict[str, str]],
    timeout: int,
) -> str:
    from quickhatch.gui.server import _call_setup_model_text

    config = AIConfig(
        provider=provider,
        model_name=model,
        api_key=api_key,
        endpoint=endpoint,
        native_search=False,
    )
    return _call_setup_model_text(
        config,
        endpoint,
        model,
        api_key,
        messages,
        timeout_seconds=timeout,
    )


def _output_dir(base: Path | None) -> Path:
    if base is not None:
        return base
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return REPO_ROOT / ".quickhatch" / "live-model-gauntlet" / stamp


def write_artifacts(
    *,
    output_dir: Path,
    summary: dict[str, Any],
    case_results: list[dict[str, Any]],
    model: str,
    provider: str,
    endpoint: str,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    with (output_dir / "cases.jsonl").open("w", encoding="utf-8") as handle:
        for result in case_results:
            handle.write(json.dumps(result, sort_keys=True) + "\n")
    lines = [
        "# Live Model Gauntlet",
        "",
        f"- provider: {provider}",
        f"- model: {model}",
        f"- endpoint: {endpoint}",
        f"- dry_run: {summary['dry_run']}",
        f"- cases: {summary['case_count']}",
        f"- evaluated: {summary['evaluated_count']}",
        f"- valid_structured_output_rate: {summary['valid_structured_output_rate']}",
        f"- wrong_mode_command_rate: {summary['wrong_mode_command_rate']}",
        f"- dangerous_raw_command_rate: {summary['dangerous_raw_command_rate']}",
        f"- search_usage_when_needed_rate: {summary['search_usage_when_needed_rate']}",
        f"- manual_followup_quality_rate: {summary['manual_followup_quality_rate']}",
        f"- repair_uniqueness_rate: {summary['repair_uniqueness_rate']}",
    ]
    (output_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--provider", default=DEFAULT_PROVIDER)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-key-env", default=None)
    parser.add_argument("--limit", type=int, default=0, help="Limit cases; 0 means all.")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv or sys.argv[1:]))
    cases = load_cases(args.fixture)
    if args.limit:
        cases = cases[: args.limit]

    output_dir = _output_dir(args.output_dir)
    if args.dry_run:
        results = [
            {
                "id": case["id"],
                "kind": case["kind"],
                "dry_run": True,
                "prompt_chars": len(build_messages_for_case(case)[0]["content"]),
            }
            for case in cases
        ]
        summary = summarize_results(results, dry_run=True)
        write_artifacts(
            output_dir=output_dir,
            summary=summary,
            case_results=results,
            model=args.model,
            provider=args.provider,
            endpoint=args.endpoint,
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
        print(f"artifacts: {output_dir}")
        return 0

    api_key = load_gauntlet_api_key(
        provider=args.provider, endpoint=args.endpoint, env_name=args.api_key_env
    )
    if not api_key:
        print(
            "No Gemini/API key found in common env vars or QuickHatch keyring. "
            "Set GEMINI_API_KEY or store a provider key before running live calls.",
            file=sys.stderr,
        )
        return 2

    results: list[dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        messages = build_messages_for_case(case)
        print(f"[{index}/{len(cases)}] {case['id']}", flush=True)
        response_text = call_model(
            provider=args.provider,
            endpoint=args.endpoint,
            model=args.model,
            api_key=api_key,
            messages=messages,
            timeout=args.timeout,
        )
        result = evaluate_response(case, response_text)
        result["response_preview"] = redact_sensitive_text(response_text[:1200])
        result["prompt_chars"] = len(messages[0]["content"])
        results.append(result)
        output_dir.mkdir(parents=True, exist_ok=True)
        with (output_dir / "cases.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result, sort_keys=True) + "\n")

    summary = summarize_results(results, dry_run=False)
    write_artifacts(
        output_dir=output_dir,
        summary=summary,
        case_results=results,
        model=args.model,
        provider=args.provider,
        endpoint=args.endpoint,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"artifacts: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
