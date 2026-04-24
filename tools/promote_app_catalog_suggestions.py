"""Promote vetted agent app-catalog suggestions into curated overrides.

The agent research files are useful, but they are not runtime policy by
themselves. This tool applies a conservative gate so only high-confidence,
non-unknown rows with valid distro-family hints become app-catalog overrides.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "src" / "quickhatch" / "data"
DEFAULT_OVERRIDES = DATA_DIR / "app_catalog.overrides.json"
DEFAULT_SUGGESTIONS = "app_catalog.agent_suggestions*.json"

ALLOWED_COMPATIBILITY = {"native", "alternative", "wine", "none", "manual_followup"}
ALLOWED_SOURCE_HINTS = {
    "repo",
    "flatpak",
    "snap",
    "aur",
    "vendor_deb_rpm",
    "vendor_appimage",
    "vendor_repo",
    "web",
    "alternative",
    "manual_followup",
    "none",
}
CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}
DISTRO_FAMILIES = ("arch", "debian", "ubuntu", "fedora", "opensuse", "alpine", "void", "gentoo")
INSTALL_SOURCES = {
    "repo",
    "flatpak",
    "snap",
    "aur",
    "vendor_deb_rpm",
    "vendor_appimage",
    "vendor_repo",
    "web",
    "alternative",
    "manual_followup",
    "none",
}


@dataclass
class PromotionStats:
    scanned: int = 0
    promoted: int = 0
    skipped_existing: int = 0
    skipped_confidence: int = 0
    skipped_unknown: int = 0
    skipped_invalid: int = 0


def _normalize(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return re.sub(r"\s+", " ", cleaned)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _existing_aliases(entries: list[dict[str, Any]]) -> set[str]:
    aliases: set[str] = set()
    for entry in entries:
        for value in [entry.get("name", ""), *(entry.get("match_names", []) or [])]:
            norm = _normalize(str(value or ""))
            if norm:
                aliases.add(norm)
    return aliases


def _clean_string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item or "").strip()
        norm = _normalize(text)
        if text and norm not in seen:
            result.append(text)
            seen.add(norm)
    return result


def _safe_package_name(value: str) -> bool:
    if not value or len(value) > 128:
        return False
    if any(char in value for char in "\r\n;&|`$<>"):
        return False
    return not any(char.isspace() for char in value)


def _clean_package_list(raw: Any) -> list[str]:
    return [pkg for pkg in _clean_string_list(raw) if _safe_package_name(pkg)]


def _clean_evidence(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    evidence: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip()
        url = str(item.get("url", "")).strip()
        if label and url.startswith(("https://", "http://")):
            evidence.append({"label": label, "url": url})
    return evidence[:5]


def _clean_install_hints(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    hints: dict[str, dict[str, Any]] = {}
    for family in DISTRO_FAMILIES:
        hint = raw.get(family)
        if not isinstance(hint, dict):
            continue
        source = str(hint.get("source", "manual_followup") or "manual_followup")
        if source not in INSTALL_SOURCES:
            source = "manual_followup"
        packages = _clean_package_list(hint.get("packages", []))
        if source in {"manual_followup", "none", "alternative", "web"}:
            packages = []
        hints[family] = {
            "source": source,
            "packages": packages,
            "notes": str(hint.get("notes", "") or "").strip(),
        }
    return hints


def _candidate_from_suggestion(raw: dict[str, Any]) -> dict[str, Any] | None:
    name = str(raw.get("name", "")).strip()
    app_id = str(raw.get("id") or name).strip()
    if not name or not app_id or "…" in name or "…" in app_id:
        return None
    compatibility = str(raw.get("compatibility", "unknown") or "unknown")
    source_hint = str(raw.get("source_hint", "needs_research") or "needs_research")
    confidence = str(raw.get("confidence", "low") or "low")
    if compatibility not in ALLOWED_COMPATIBILITY:
        return None
    if source_hint not in ALLOWED_SOURCE_HINTS:
        return None
    alternatives = _clean_string_list(raw.get("alternatives", [])) or [name]
    entry: dict[str, Any] = {
        "id": app_id,
        "name": name,
        "match_names": _clean_string_list(raw.get("match_names", [])) or [name],
        "compatibility": compatibility,
        "alternatives": alternatives,
        "source_hint": source_hint,
        "confidence": confidence,
        "category": str(raw.get("category", "other") or "other"),
        "notes": str(raw.get("notes", "") or "").strip(),
    }
    install_hints = _clean_install_hints(raw.get("install_hints"))
    if install_hints:
        entry["install_hints"] = install_hints
    evidence = _clean_evidence(raw.get("evidence"))
    if evidence:
        entry["evidence"] = evidence
    return entry


def promote_suggestions(
    *,
    overrides_path: Path,
    suggestion_paths: list[Path],
    min_confidence: str = "high",
    require_evidence: bool = True,
    dry_run: bool = False,
) -> tuple[dict[str, Any], PromotionStats]:
    payload = _load_json(overrides_path)
    entries = list(payload.get("entries", []) or [])
    aliases = _existing_aliases(entries)
    stats = PromotionStats()
    min_score = CONFIDENCE_ORDER[min_confidence]
    promoted: list[dict[str, Any]] = []

    for path in suggestion_paths:
        raw_payload = _load_json(path)
        for raw in raw_payload.get("entries", []) or []:
            if not isinstance(raw, dict):
                continue
            stats.scanned += 1
            candidate = _candidate_from_suggestion(raw)
            if candidate is None:
                stats.skipped_invalid += 1
                continue
            confidence = candidate["confidence"]
            if CONFIDENCE_ORDER.get(confidence, -1) < min_score:
                stats.skipped_confidence += 1
                continue
            if candidate["compatibility"] == "unknown" or candidate["source_hint"] == "needs_research":
                stats.skipped_unknown += 1
                continue
            if require_evidence and not candidate.get("evidence"):
                stats.skipped_invalid += 1
                continue
            names = [candidate["name"], *(candidate.get("match_names", []) or [])]
            if any(_normalize(name) in aliases for name in names):
                stats.skipped_existing += 1
                continue
            candidate["promoted_from_agent_suggestions"] = True
            promoted.append(candidate)
            stats.promoted += 1
            for name in names:
                norm = _normalize(str(name))
                if norm:
                    aliases.add(norm)

    if promoted:
        entries.extend(sorted(promoted, key=lambda item: item["name"].lower()))
        payload["entries"] = entries
        payload["last_agent_promotion"] = {
            "date": datetime.now(UTC).date().isoformat(),
            "min_confidence": min_confidence,
            "require_evidence": require_evidence,
            "promoted": stats.promoted,
        }
    if not dry_run:
        overrides_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return payload, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES)
    parser.add_argument("--suggestions-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--pattern", default=DEFAULT_SUGGESTIONS)
    parser.add_argument("--min-confidence", choices=["low", "medium", "high"], default="high")
    parser.add_argument("--allow-missing-evidence", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    suggestion_paths = sorted(args.suggestions_dir.glob(args.pattern))
    _, stats = promote_suggestions(
        overrides_path=args.overrides,
        suggestion_paths=suggestion_paths,
        min_confidence=args.min_confidence,
        require_evidence=not args.allow_missing_evidence,
        dry_run=args.dry_run,
    )
    mode = "Would promote" if args.dry_run else "Promoted"
    print(
        f"{mode} {stats.promoted}/{stats.scanned} suggestions "
        f"(existing={stats.skipped_existing}, confidence={stats.skipped_confidence}, "
        f"unknown={stats.skipped_unknown}, invalid={stats.skipped_invalid})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
