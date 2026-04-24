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


def _data_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data"


def _normalize_name(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return re.sub(r"\s+", " ", cleaned)


def _entry_from_mapping(raw: dict[str, Any]) -> AppCatalogEntry | None:
    name = str(raw.get("name") or "").strip()
    app_id = str(raw.get("id") or name).strip()
    if not name or not app_id:
        return None
    match_names = tuple(
        str(v).strip() for v in raw.get("match_names", []) if str(v).strip()
    ) or (name,)
    alternatives = tuple(
        str(v).strip() for v in raw.get("alternatives", []) if str(v).strip()
    ) or (name,)
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
    )


@lru_cache(maxsize=1)
def load_catalog() -> tuple[AppCatalogEntry, ...]:
    """Load the generated popular-app catalog plus legacy hand mappings."""

    entries: list[AppCatalogEntry] = []
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
    # "Mozilla Firefox ESR". Avoid one/two-letter aliases.
    for entry in load_catalog():
        for alias in (entry.name, *entry.match_names):
            alias_norm = _normalize_name(alias)
            if len(alias_norm) < 3:
                continue
            if alias_norm in normalized or normalized in alias_norm:
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
