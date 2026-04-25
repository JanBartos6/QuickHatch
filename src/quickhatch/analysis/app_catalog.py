"""Curated application catalog helpers.

The catalog is intentionally data-first: Screen 5 needs fast, offline
classification without spending model calls on every scanned app. Exact
distro package names are still resolved later by the setup planner.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AppCatalogEntry:
    """A normalized app compatibility catalog row."""

    id: str
    name: str
    match_names: tuple[str, ...] = field(default_factory=tuple)
    compatibility: str = "unknown"
    alternatives: tuple[str, ...] = field(default_factory=tuple)
    source_hint: str = "unknown"
    confidence: str = "low"
    category: str = "other"
    notes: str = ""
    install_hints: dict[str, dict[str, Any]] = field(default_factory=dict)
    evidence: tuple[dict[str, str], ...] = field(default_factory=tuple)


_GAMING_LAUNCHER_RE = re.compile(
    r"\b("
    r"steam|epic games|heroic games launcher|gog galaxy|lutris|bottles|"
    r"battle\.?net|ea app|origin|ubisoft connect|rockstar games launcher|"
    r"itch|itch\.io|playnite|protonup|protontricks|proton|wine|rare|"
    r"minigalaxy|prism launcher|minecraft launcher"
    r")\b",
    re.IGNORECASE,
)

_INDIVIDUAL_GAME_RE = re.compile(
    r"\b("
    r"counter[- ]strike|cs2|dota|league of legends|valorant|fortnite|"
    r"minecraft(?! launcher)|roblox|elden ring|cyberpunk|witcher|"
    r"baldur'?s gate|world of warcraft|diablo|overwatch|hearthstone|"
    r"starcraft|call of duty|warzone|apex legends|pubg|rocket league|"
    r"rainbow six|genshin|honkai|palworld|terraria|stardew valley|"
    r"rimworld|factorio|satisfactory|skyrim|fallout|doom|quake|"
    r"civilization|xcom|total war|path of exile|lost ark|runescape|"
    r"osu!|beat saber|vrchat|among us|hades|hollow knight|celeste|"
    r"subnautica|no man's sky|warframe|destiny 2|the sims|fifa|"
    r"football manager|nba 2k|gta|grand theft auto|red dead redemption|"
    r"space cadet pinball|openmw"
    r")\b",
    re.IGNORECASE,
)

_GENERIC_SINGLE_TOKEN_ALIASES = {
    "app",
    "code",
    "codec",
    "git",
    "launcher",
    "manager",
    "office",
    "pack",
    "python",
    "studio",
    "tool",
}


def _data_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data"


def _normalize_name(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return re.sub(r"\s+", " ", cleaned)


def _distro_family_candidates(value: str) -> tuple[str, ...]:
    """Return likely catalog hint keys for a distro/family label."""

    normalized = _normalize_name(value)
    if not normalized or normalized == "unknown":
        return ()
    if any(token in normalized for token in ("arch", "manjaro", "endeavour")):
        return ("arch",)
    if any(token in normalized for token in ("ubuntu", "pop os", "popos", "mint", "zorin")):
        return ("ubuntu", "debian")
    if "debian" in normalized:
        return ("debian", "ubuntu")
    if any(
        token in normalized for token in ("fedora", "rhel", "red hat", "centos", "rocky", "alma")
    ):
        return ("fedora",)
    if any(token in normalized for token in ("opensuse", "open suse", "suse")):
        return ("opensuse",)
    if "alpine" in normalized:
        return ("alpine",)
    if "void" in normalized:
        return ("void",)
    if "gentoo" in normalized:
        return ("gentoo",)
    return (normalized.replace(" ", "_"),)


def _active_install_hint(
    install_hints: dict[str, dict[str, Any]],
    distro_family: str,
) -> dict[str, Any]:
    for candidate in _distro_family_candidates(distro_family):
        hint = install_hints.get(candidate)
        if hint:
            return {"family": candidate, **hint}
    return {}


def _phrase_fallback_allowed(alias_norm: str) -> bool:
    if len(alias_norm) < 3:
        return False
    tokens = alias_norm.split()
    return len(tokens) > 1 or alias_norm not in _GENERIC_SINGLE_TOKEN_ALIASES


def _entry_from_mapping(raw: dict[str, Any]) -> AppCatalogEntry | None:
    name = str(raw.get("name") or "").strip()
    app_id = str(raw.get("id") or name).strip()
    if not name or not app_id:
        return None
    if "…" in name or "…" in app_id:
        return None
    match_names = tuple(str(v).strip() for v in raw.get("match_names", []) if str(v).strip()) or (
        name,
    )
    alternatives = tuple(str(v).strip() for v in raw.get("alternatives", []) if str(v).strip()) or (
        name,
    )
    install_hints = raw.get("install_hints") if isinstance(raw.get("install_hints"), dict) else {}
    evidence = tuple(
        {
            "label": str(item.get("label", "")).strip(),
            "url": str(item.get("url", "")).strip(),
        }
        for item in raw.get("evidence", [])
        if isinstance(item, dict)
        and str(item.get("label", "")).strip()
        and str(item.get("url", "")).strip()
    )
    return AppCatalogEntry(
        id=app_id,
        name=name,
        match_names=match_names,
        compatibility=str(raw.get("compatibility") or "unknown"),
        alternatives=alternatives,
        source_hint=str(raw.get("source_hint") or "unknown"),
        confidence=str(raw.get("confidence") or "low"),
        category=str(raw.get("category") or "other"),
        notes=str(raw.get("notes") or ""),
        install_hints={
            str(family): {
                "source": str(hint.get("source", "manual_followup")),
                "packages": [str(pkg) for pkg in hint.get("packages", []) if str(pkg).strip()],
                "notes": str(hint.get("notes", "") or ""),
            }
            for family, hint in install_hints.items()
            if isinstance(hint, dict)
        },
        evidence=evidence,
    )


@lru_cache(maxsize=1)
def load_catalog() -> tuple[AppCatalogEntry, ...]:
    """Load the generated popular-app catalog plus legacy hand mappings."""

    entries: list[AppCatalogEntry] = []
    overrides = _data_dir() / "app_catalog.overrides.json"
    if overrides.exists():
        try:
            payload = json.loads(overrides.read_text(encoding="utf-8"))
            for raw in payload.get("entries", []):
                if isinstance(raw, dict):
                    entry = _entry_from_mapping(raw)
                    if entry is not None:
                        entries.append(entry)
        except (OSError, json.JSONDecodeError):
            pass

    generated = _data_dir() / "app_catalog.generated.json"
    if generated.exists():
        try:
            payload = json.loads(generated.read_text(encoding="utf-8"))
            for raw in payload.get("entries", []):
                if isinstance(raw, dict):
                    entry = _entry_from_mapping(raw)
                    if entry is not None:
                        entries.append(entry)
        except (OSError, json.JSONDecodeError):
            pass

    # Backward-compatible hand-curated Windows-alternative DB. These
    # rows are higher confidence because they were explicitly mapped for
    # migration, not just discovered as popular Linux/Flatpak apps.
    legacy = _data_dir() / "app_alternatives.json"
    if legacy.exists():
        try:
            payload = json.loads(legacy.read_text(encoding="utf-8"))
            for key, raw in payload.items():
                if not isinstance(raw, dict):
                    continue
                alternatives = raw.get("alternatives") or []
                entries.append(
                    AppCatalogEntry(
                        id=f"legacy:{key}",
                        name=key,
                        match_names=(key,),
                        compatibility=str(raw.get("method") or "unknown"),
                        alternatives=tuple(str(v) for v in alternatives),
                        source_hint="curated",
                        confidence="high",
                        category="curated",
                        notes=str(raw.get("notes") or ""),
                        install_hints={},
                        evidence=(),
                    )
                )
        except (OSError, json.JSONDecodeError):
            pass

    return tuple(entries)


def match_catalog_entry(app_name: str) -> AppCatalogEntry | None:
    """Return the best catalog entry for a scanned app name."""

    normalized = _normalize_name(app_name)
    if not normalized:
        return None

    exact: dict[str, AppCatalogEntry] = {}
    for entry in load_catalog():
        for alias in (entry.name, *entry.match_names):
            alias_norm = _normalize_name(alias)
            if alias_norm:
                exact.setdefault(alias_norm, entry)
    if normalized in exact:
        return exact[normalized]

    # Phrase-level fallback for scanned names like "Brave Browser" or
    # "Mozilla Firefox ESR". Avoid one/two-letter aliases and do not match
    # arbitrary substrings inside larger words ("git" in "LegitApp").
    for entry in load_catalog():
        for alias in (entry.name, *entry.match_names):
            alias_norm = _normalize_name(alias)
            if not _phrase_fallback_allowed(alias_norm):
                continue
            pattern = rf"(?<![a-z0-9]){re.escape(alias_norm)}(?![a-z0-9])"
            if re.search(pattern, normalized):
                return entry
    return None


def is_gaming_launcher_or_tool(app_name: str) -> bool:
    """True for launchers/tools that should remain visible in Screen 5."""

    return bool(_GAMING_LAUNCHER_RE.search(app_name or ""))


def is_individual_game(app_name: str) -> bool:
    """Best-effort filter for game titles.

    QuickHatch should migrate launchers/tools, not individual Steam/Epic
    libraries. This deliberately catches common titles only; unknowns stay
    visible so users can decide.
    """

    if not app_name or is_gaming_launcher_or_tool(app_name):
        return False
    return bool(_INDIVIDUAL_GAME_RE.search(app_name))


def filter_migration_apps(apps: list[Any]) -> list[Any]:
    """Remove individual game titles from the migration app picker."""

    return [app for app in apps if not is_individual_game(getattr(app, "name", ""))]


def selected_app_catalog_context(
    app_names: list[str],
    scanned_apps: list[Any] | None = None,
    distro_family: str = "",
) -> list[dict[str, Any]]:
    """Build compact catalog rows for selected apps.

    This is the payload given to planning prompts. It is intentionally
    selected-app-only so a 500-entry catalog never bloats every model call.
    """

    scanned_by_name: dict[str, Any] = {}
    for app in scanned_apps or []:
        name = getattr(app, "name", None)
        if isinstance(app, dict):
            name = app.get("name")
        norm = _normalize_name(str(name or ""))
        if norm:
            scanned_by_name[norm] = app

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_name in app_names or []:
        name = str(raw_name or "").strip()
        norm = _normalize_name(name)
        if not name or norm in seen:
            continue
        seen.add(norm)
        entry = match_catalog_entry(name)
        scanned = scanned_by_name.get(norm)
        if entry is None:
            row: dict[str, Any] = {
                "name": name,
                "catalog_id": "",
                "canonical_name": name,
                "compatibility": "unknown",
                "alternatives": [],
                "source_hint": "needs_research",
                "confidence": "low",
                "category": "unknown",
                "notes": "No curated catalog match; research before installing.",
                "install_hints": {},
                "evidence": [],
            }
        else:
            active_hint = _active_install_hint(entry.install_hints, distro_family)
            row = {
                "name": name,
                "catalog_id": entry.id,
                "canonical_name": entry.name,
                "compatibility": entry.compatibility,
                "alternatives": list(entry.alternatives),
                "source_hint": entry.source_hint,
                "confidence": entry.confidence,
                "category": entry.category,
                "notes": entry.notes,
                "evidence": list(entry.evidence[:5]),
            }
            if active_hint:
                row["active_install_hint"] = active_hint
            elif not _distro_family_candidates(distro_family):
                row["install_hints"] = entry.install_hints
        if scanned is not None:
            if isinstance(scanned, dict):
                scanned_source = scanned.get("source")
                scanned_version = scanned.get("version")
                scanned_compatibility = scanned.get("compatibility")
                scanned_linux_alternative = scanned.get("linux_alternative")
                scanned_notes = scanned.get("notes")
            else:
                scanned_source = getattr(scanned, "source", None)
                scanned_version = getattr(scanned, "version", None)
                scanned_compatibility = getattr(scanned, "compatibility", None)
                scanned_linux_alternative = getattr(scanned, "linux_alternative", None)
                scanned_notes = getattr(scanned, "notes", None)
            row["scanned"] = {
                "source": scanned_source,
                "version": scanned_version,
                "compatibility": scanned_compatibility,
                "linux_alternative": scanned_linux_alternative,
                "notes": scanned_notes,
            }
        rows.append(row)
    return rows
