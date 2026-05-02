"""Fast local debug helpers for harness decisions."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from quickhatch.config import UserProfile

from .failure import classify_failure
from .families import get_family_installer
from .health import run_harness_health
from .preflight import preflight_setup_decisions
from .types import SetupFacts


def load_facts_payload(path: Path | str | None) -> SetupFacts:
    if not path:
        return SetupFacts()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("facts"), dict):
        data = data["facts"]
    if not isinstance(data, dict):
        raise ValueError("facts JSON must be an object or contain a `facts` object")
    allowed = set(SetupFacts.__dataclass_fields__)
    return SetupFacts(**{key: value for key, value in data.items() if key in allowed})


def _enum_payload(value: Any) -> Any:
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, dict):
        return {key: _enum_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_enum_payload(item) for item in value]
    return value


def preflight_debug_payload(
    command: str,
    *,
    facts: SetupFacts | None = None,
    shadow_mode: bool = False,
) -> dict[str, Any]:
    facts = facts or SetupFacts()
    decisions = preflight_setup_decisions(command, facts, shadow_mode=shadow_mode)
    return {
        "command": command,
        "facts_summary": {
            "distro_family": facts.distro_family,
            "package_manager": facts.package_manager,
            "community_helper": facts.community_helper,
            "is_live": facts.is_live,
            "current_user": facts.current_user,
            "is_root": facts.is_root,
            "sudo_nopasswd": facts.sudo_nopasswd,
        },
        "decisions": [_enum_payload(asdict(decision)) for decision in decisions],
        "blocked": any(
            getattr(decision.kind, "value", decision.kind) == "block" for decision in decisions
        ),
    }


def classify_debug_payload(
    *,
    command: str = "",
    rc: int | None = None,
    stdout: str = "",
    stderr: str = "",
    detail: str = "",
) -> dict[str, Any]:
    result = classify_failure(command=command, rc=rc, stdout=stdout, stderr=stderr, detail=detail)
    return _enum_payload(asdict(result))


def adapter_spine_payload(
    *,
    family: str,
    profile_path: Path | str | None = None,
) -> dict[str, Any]:
    profile = UserProfile()
    if profile_path:
        loaded = UserProfile.from_dict(json.loads(Path(profile_path).read_text(encoding="utf-8")))
        profile = loaded or profile
    facts = SetupFacts(distro_family=family, os_id=family, is_live=True)
    installer = get_family_installer(family)
    spine = installer.build_install_spine(facts, profile)
    return asdict(spine)


def harness_health_payload(
    *,
    root: Path | str | None = None,
    keep_artifacts: bool = False,
) -> dict[str, Any]:
    return run_harness_health(root=root, keep_artifacts=keep_artifacts)
