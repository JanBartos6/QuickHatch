"""Post-processing for the recipe gallery: dedupe, normalize, descriptions.

Runs AFTER scrape + filter + extract. Consumes the per-entry JSON files in
``.scrape-cache/extracted/`` and produces the curated, description-enriched
``src/quickhatch/data/gallery.json`` that ships with the package and feeds
the harness LLM when picking a design.

Responsibilities:
1. Dedupe by canonical ``dotfiles_url`` (keep highest-upvote attribution).
2. Normalize theme / icons / font tokens into a canonical kebab-case form so
   the harness LLM can filter/match on them without fuzzy compare.
3. Load curator-written descriptions from ``data/gallery_descriptions.json``
   and merge them in (one description per recipe id).
4. Score + cap the gallery by quality × DE diversity.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .common import EXTRACTED_CACHE_DIR, canonical_repo_url

# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

_THEME_ALIASES = {
    "catppuccin": "catppuccin",
    "catppuccin-mocha": "catppuccin-mocha",
    "catppuccin mocha": "catppuccin-mocha",
    "catppuccin-macchiato": "catppuccin-macchiato",
    "catppuccin macchiato": "catppuccin-macchiato",
    "catppuccin-frappe": "catppuccin-frappe",
    "catppuccin-latte": "catppuccin-latte",
    "tokyonight": "tokyonight",
    "tokyo-night": "tokyonight",
    "tokyonight-moon": "tokyonight-moon",
    "tokyonight-storm": "tokyonight-storm",
    "tokyonight-night": "tokyonight-night",
    "gruvbox": "gruvbox",
    "gruvbox-dark": "gruvbox-dark",
    "gruvbox-material": "gruvbox-material",
    "nord": "nord",
    "nordic": "nord",
    "dracula": "dracula",
    "everforest": "everforest",
    "kanagawa": "kanagawa",
    "rose-pine": "rose-pine",
    "rose pine": "rose-pine",
    "rosé-pine": "rose-pine",
    "rosepine": "rose-pine",
    "solarized": "solarized",
    "solarized-dark": "solarized-dark",
    "whitesur": "whitesur",
    "whitesur-dark": "whitesur-dark",
    "orchis": "orchis",
    "adwaita": "adwaita",
    "adwaita-dark": "adwaita-dark",
    "adw-gtk3": "adw-gtk3",
    "breeze": "breeze",
    "arc": "arc",
    "arc-dark": "arc-dark",
    "materia": "materia",
    "material-you": "material-you",
    "matugen": "material-you",
    "juno": "juno",
    "fluent": "fluent",
}

_ICON_ALIASES = {
    "papirus": "papirus",
    "papirus-dark": "papirus-dark",
    "papirus-nord": "papirus-nord",
    "papirus-icon-theme": "papirus",
    "tela": "tela",
    "tela-circle": "tela-circle",
    "bibata": "bibata",
    "bibata-modern": "bibata-modern",
    "bibata-modern-ice": "bibata-modern-ice",
    "whitesur": "whitesur",
    "tokyonight": "tokyonight",
    "nordzy": "nordzy",
    "adwaita": "adwaita",
    "breeze": "breeze",
    "reversal": "reversal",
    "paper": "paper",
    "moka": "moka",
}

_FONT_MONO_ALIASES = {
    "jetbrainsmono": "JetBrainsMono",
    "jetbrains-mono": "JetBrainsMono",
    "jetbrains mono": "JetBrainsMono",
    "firacode": "FiraCode",
    "fira-code": "FiraCode",
    "fira code": "FiraCode",
    "iosevka": "Iosevka",
    "iosevka term": "Iosevka Term",
    "iosevkaterm": "Iosevka Term",
    "hack": "Hack",
    "cascadia code": "CascadiaCode",
    "cascadia-code": "CascadiaCode",
    "caskaydiacove": "CaskaydiaCove",
    "caskaydiacove nerd font": "CaskaydiaCove Nerd Font",
    "monaspace": "Monaspace",
    "source code pro": "SourceCodePro",
    "ibm plex mono": "IBMPlexMono",
    "geist mono": "GeistMono",
    "geistmono": "GeistMono",
    "comicshannsmono": "ComicShannsMono",
    "mononoki": "Mononoki",
}

_FONT_UI_ALIASES = {
    "inter": "Inter",
    "cantarell": "Cantarell",
    "noto sans": "Noto Sans",
    "roboto": "Roboto",
    "sf pro": "SF Pro",
    "sf pro display": "SF Pro Display",
    "adwaita sans": "Adwaita Sans",
    "ibm plex sans": "IBMPlexSans",
    "geist": "Geist",
    "lexend": "Lexend",
    "montserrat": "Montserrat",
    "poppins": "Poppins",
    "ubuntu": "Ubuntu",
    "source sans pro": "SourceSansPro",
    "google sans": "Google Sans",
    "work sans": "WorkSans",
}


def _normalize_token(value: str | None, aliases: dict[str, str]) -> str | None:
    """Normalize a token against an alias map, or return a lowercase kebab version.

    Falls through to kebab-case normalization if no alias match, so brand-new
    themes don't get lost.
    """
    if not value:
        return None
    cleaned = value.strip().strip("`'\"").lower()
    if not cleaned:
        return None
    # Normalize accented characters that sometimes show up (e.g. "Rosé Pine").
    replacements = {
        "é": "e",
        "è": "e",
        "ê": "e",
        "ë": "e",
        "á": "a",
        "à": "a",
        "ä": "a",
        "ö": "o",
        "ü": "u",
    }
    for bad, good in replacements.items():
        cleaned = cleaned.replace(bad, good)
    # Nerd Font family: preserve "Nerd Font" suffix capitalization if present.
    if "nerd font" in cleaned:
        core = re.sub(r"\s*nerd\s*font.*$", "", cleaned).strip()
        base = aliases.get(core, None)
        if base is None:
            base = re.sub(r"\s+", "", core).capitalize() if core else core
            # Preserve aliased CamelCase for well-known families.
            for alias_key, alias_val in aliases.items():
                if alias_key == core:
                    base = alias_val
                    break
            else:
                # If no alias match, use CamelCase-ish preservation of the
                # original (before lowercasing).
                base = re.sub(r"\s*nerd\s*font.*$", "", value, flags=re.IGNORECASE).strip()
        return f"{base} Nerd Font"
    # Strip " theme", " font", " icons", " cursors" trailing noise. These are
    # applied iteratively so "papirus-icon-theme" collapses cleanly to "papirus".
    for _ in range(3):
        prev = cleaned
        cleaned = re.sub(
            r"[-\s]?(gtk|gtk3|gtk4|icon|icons|theme|themes|cursor|cursors|font|fonts)$",
            "",
            cleaned,
        )
        cleaned = re.sub(r"\s*\(.*\)$", "", cleaned)
        cleaned = cleaned.strip().strip("-").strip("_").strip()
        if cleaned == prev:
            break
    if not cleaned:
        return None
    if cleaned in aliases:
        return aliases[cleaned]
    # Fall through: kebab case.
    return re.sub(r"\s+", "-", cleaned)


def normalize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """In-place normalization of free-form tokens to canonical forms."""
    entry["theme"] = _normalize_token(entry.get("theme"), _THEME_ALIASES)
    entry["icons"] = _normalize_token(entry.get("icons"), _ICON_ALIASES)
    entry["font_ui"] = _normalize_token(entry.get("font_ui"), _FONT_UI_ALIASES)
    entry["font_mono"] = _normalize_token(entry.get("font_mono"), _FONT_MONO_ALIASES)
    entry["dotfiles_url"] = canonical_repo_url(entry.get("dotfiles_url", "")) or entry.get(
        "dotfiles_url", ""
    )
    return entry


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def dedupe_by_repo(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge entries that point at the same canonical repo URL.

    Keeps the highest-upvote entry as the primary (best attribution) but
    records every source URL and author so the harness can show "featured
    on r/unixporn, r/hyprland".
    """
    by_repo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        key = canonical_repo_url(entry.get("dotfiles_url", "")) or entry.get("id")
        by_repo[key].append(entry)

    result: list[dict[str, Any]] = []
    for entries_for_repo in by_repo.values():
        if len(entries_for_repo) == 1:
            result.append(entries_for_repo[0])
            continue
        entries_for_repo.sort(key=lambda e: int(e.get("upvotes", 0)), reverse=True)
        primary = dict(entries_for_repo[0])
        extra_sources = []
        for extra in entries_for_repo[1:]:
            extra_sources.append(
                {
                    "source": extra.get("source"),
                    "source_url": extra.get("source_url"),
                    "upvotes": extra.get("upvotes"),
                    "author": extra.get("author"),
                }
            )
        primary["additional_sources"] = extra_sources
        result.append(primary)
    return result


# ---------------------------------------------------------------------------
# Quality scoring + capping
# ---------------------------------------------------------------------------


def quality_score(entry: dict[str, Any]) -> float:
    """Rank entries so we can pick the best N while keeping DE diversity."""
    upvotes = int(entry.get("upvotes", 0) or 0)
    conf = {"high": 1.0, "medium": 0.7, "low": 0.4}.get(
        entry.get("extraction_confidence") or "low", 0.4
    )
    staleness = float(entry.get("staleness_score", 0.5) or 0.5)  # 0 = fresh, 1 = stale
    # Boost entries that have real design data
    richness = 0
    for field in ("theme", "icons", "font_ui", "font_mono"):
        if entry.get(field):
            richness += 1
    comps = entry.get("components") or {}
    for field in ("panel", "launcher", "lockscreen", "terminal"):
        if comps.get(field):
            richness += 1
    return conf * (1 - staleness) * (1 + 0.15 * richness) * (1 + upvotes / 1000.0)


def cap_with_diversity(entries: list[dict[str, Any]], *, cap: int = 250) -> list[dict[str, Any]]:
    """Return up to ``cap`` entries, skewed to preserve DE diversity.

    Algorithm: sort by quality, then round-robin pick within DE buckets so a
    massive pile of Hyprland rices doesn't crowd out i3 / bspwm / gnome.
    """
    if len(entries) <= cap:
        return sorted(entries, key=quality_score, reverse=True)
    by_de: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in entries:
        by_de[e.get("de") or "unknown"].append(e)
    for bucket in by_de.values():
        bucket.sort(key=quality_score, reverse=True)
    # Compute per-DE quota: proportional to sqrt(count) to dampen long tails
    # (so Hyprland gets more slots than i3 but doesn't swallow the gallery).
    import math

    weights = {de: math.sqrt(len(b)) for de, b in by_de.items()}
    total_weight = sum(weights.values())
    quotas = {de: max(1, int(round(cap * w / total_weight))) for de, w in weights.items()}
    result: list[dict[str, Any]] = []
    for de, bucket in by_de.items():
        result.extend(bucket[: quotas[de]])
    # Fill the rest by score if we came in under cap (rounding slack).
    if len(result) < cap:
        remainder = sorted([e for e in entries if e not in result], key=quality_score, reverse=True)
        result.extend(remainder[: cap - len(result)])
    return sorted(result[:cap], key=quality_score, reverse=True)


# ---------------------------------------------------------------------------
# Description merging
# ---------------------------------------------------------------------------


def merge_descriptions(
    entries: list[dict[str, Any]], descriptions: dict[str, Any]
) -> list[dict[str, Any]]:
    """Attach curator-written descriptions + best_for to each entry."""
    for entry in entries:
        eid = entry.get("id")
        if eid in descriptions:
            payload = descriptions[eid]
            entry["description"] = payload.get("description", "")
            entry["best_for"] = payload.get("best_for", [])
    return entries


# ---------------------------------------------------------------------------
# Digest for the harness LLM
# ---------------------------------------------------------------------------

_DIGEST_KEEP_FIELDS = (
    "id",
    "de",
    "theme",
    "icons",
    "cursor",
    "font_ui",
    "font_mono",
    "wallpaper_hint",
    "accent_color",
    "palette",
    "mood",
    "density",
    "components",
    "decorations",
    "signature_features",
    "description",
    "best_for",
    "upvotes",
    "source_url",
    "dotfiles_url",
    "license_status",
    "preview_image",
)


def build_digest(entry: dict[str, Any]) -> dict[str, Any]:
    """Slim-down form of a gallery entry for inclusion in LLM prompts."""
    return {k: entry.get(k) for k in _DIGEST_KEEP_FIELDS if entry.get(k) is not None}


# ---------------------------------------------------------------------------
# Pipeline entry
# ---------------------------------------------------------------------------


def load_extracted(extracted_dir: Path = EXTRACTED_CACHE_DIR) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(extracted_dir.glob("*.json")):
        try:
            entries.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return entries


def curate(
    *,
    extracted_dir: Path = EXTRACTED_CACHE_DIR,
    descriptions_path: Path | None = None,
    cap: int = 250,
) -> list[dict[str, Any]]:
    """Full curation pipeline."""
    entries = load_extracted(extracted_dir)
    entries = [normalize_entry(e) for e in entries]
    entries = dedupe_by_repo(entries)
    if descriptions_path and descriptions_path.exists():
        descriptions = json.loads(descriptions_path.read_text(encoding="utf-8"))
        entries = merge_descriptions(entries, descriptions)
    return cap_with_diversity(entries, cap=cap)


__all__ = [
    "build_digest",
    "cap_with_diversity",
    "curate",
    "dedupe_by_repo",
    "load_extracted",
    "merge_descriptions",
    "normalize_entry",
    "quality_score",
]
