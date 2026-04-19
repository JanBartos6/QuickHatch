"""Lookup + rendering for distro install primers shipped with QuickHatch.

The harness agent installs the target distro from a live ISO via SSH. To
avoid the agent hallucinating flags (e.g. ``pacstrap`` options that changed
between Arch versions), each supported distro ships a condensed install
primer under ``data/install_primers/``. This module resolves the user's
chosen distro (fuzzy alias match) and returns its primer text so it can be
injected into the remote-setup prompt.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any

_PRIMER_PACKAGE = "quickhatch.data.install_primers"


@lru_cache(maxsize=1)
def _load_index() -> dict[str, Any]:
    """Load the primer index. Cached on first access."""
    try:
        with (
            resources.files(_PRIMER_PACKAGE)
            .joinpath("_index.json")
            .open("r", encoding="utf-8") as handle
        ):
            return json.load(handle)
    except (OSError, json.JSONDecodeError, ModuleNotFoundError):
        return {"version": 1, "distros": []}


def list_supported_distros() -> list[dict[str, Any]]:
    """Return every distro entry in the primer index."""
    return list(_load_index().get("distros", []))


def _normalize(value: str) -> str:
    return value.strip().lower().replace(" ", "-").replace("_", "-").replace("!", "")


def resolve_primer(distro: str | None) -> dict[str, Any] | None:
    """Given any user-typed distro hint, find the matching primer entry.

    Matches against canonical names and aliases with light normalization
    (lowercase, hyphenate whitespace). Returns the index entry dict
    (with file path, family, release model) or ``None`` if no match.
    """
    if not distro:
        return None
    # Defensive: callers occasionally pass non-string values (dicts from
    # partially-parsed LLM output, Path objects, etc.). Coerce or bail.
    if not isinstance(distro, str):
        try:
            distro = str(distro)
        except Exception:
            return None
    needle = _normalize(distro)
    if not needle:
        return None
    for entry in _load_index().get("distros", []):
        canonical = _normalize(entry.get("canonical", ""))
        if canonical == needle:
            return entry
        if any(_normalize(alias) == needle for alias in entry.get("aliases", [])):
            return entry

    # No exact match — try forgiving variants.
    # 1. Strip "linux"/"os" suffixes the user may have appended.
    #    ("archlinux" → "arch", "fedoralinux" → "fedora", "endeavouros" stays).
    import re

    # Only strip "linux" / "-linux" suffixes, NOT bare "os" — the latter
    # over-strips ("chromeos" → "chrome", "archos" → "arch" which would
    # spuriously match Arch's primer). Distro-"os" variants (endeavouros,
    # centos) stay reachable through their explicit aliases.
    stripped = re.sub(r"-?linux$", "", needle)
    if stripped and stripped != needle:
        for entry in _load_index().get("distros", []):
            if _normalize(entry.get("canonical", "")) == stripped:
                return entry
            if any(_normalize(alias) == stripped for alias in entry.get("aliases", [])):
                return entry

    # 2. Word-boundary search within a longer free-text hint
    #    ("I'd like arch btw" → matches "arch"). Still rejects
    #    accidental-substring cases like "templeos" matching "eos".
    for entry in _load_index().get("distros", []):
        haystack = [
            _normalize(entry.get("canonical", "")),
            *(_normalize(alias) for alias in entry.get("aliases", [])),
        ]
        for candidate in haystack:
            if not candidate:
                continue
            pattern = rf"(?:^|[^a-z0-9]){re.escape(candidate)}(?:$|[^a-z0-9])"
            if re.search(pattern, needle):
                return entry
    return None


def _read_primer_file(filename: str) -> str | None:
    """Read a primer markdown file from the package data directory."""
    if not filename:
        return None
    try:
        with (
            resources.files(_PRIMER_PACKAGE)
            .joinpath(filename)
            .open("r", encoding="utf-8") as handle
        ):
            return handle.read()
    except (OSError, ModuleNotFoundError):
        return None


def load_family_text(family: str | None) -> str | None:
    """Read a family primer by key (e.g. ``debian`` -> families/debian-family.md)."""
    if not family:
        return None
    families = _load_index().get("families") or {}
    rel = families.get(family)
    if not rel:
        return None
    return _read_primer_file(rel)


def load_primer_text(distro: str | None) -> str | None:
    """Return the full primer Markdown for ``distro``, or None if no match.

    Stitches the family primer (shared install procedure) with the distro
    card (distro-specific values + overrides) when the distro references a
    family. Standalone distros (NixOS, Alpine, etc.) return their card
    directly.
    """
    entry = resolve_primer(distro)
    if entry is None:
        return None
    card = _read_primer_file(entry.get("file", ""))
    if card is None:
        return None
    family_body = load_family_text(entry.get("family"))
    if family_body is None:
        return card
    # Family primer first, then distro card. Separator makes the boundary
    # visually obvious for the LLM reading the prompt.
    return f"{family_body}\n\n---\n\n{card}"


def supported_distros_summary() -> str:
    """Short Markdown list of every supported distro with one-line notes.

    Used in prompts where we want to tell the LLM "these are the distros
    we have install primers for" without injecting the full primer text.
    """
    lines: list[str] = ["## QuickHatch-supported distros", ""]
    for entry in list_supported_distros():
        canonical = entry.get("canonical", "?")
        family = entry.get("family", "?")
        release = entry.get("release_model", "?")
        lines.append(f"- **{canonical}** — family: {family}, release: {release}")
    return "\n".join(lines)


def primer_prompt_block(distro: str | None) -> str:
    """Wrap the selected primer in a clear header for injection into prompts.

    If no distro match, returns a generic "consult distro docs" note so the
    agent still has guidance (fall back to the LLM's general knowledge).
    """
    primer = load_primer_text(distro)
    if primer is None:
        # Don't print the literal string "None" when no distro was
        # selected — say so explicitly instead.
        distro_label = distro if distro else "(not selected)"
        fallback = (
            f"The requested distro ``{distro_label}`` is not in QuickHatch's "
            "curated primer library. "
            "Proceed using your training-data knowledge of that distro's "
            "standard install procedure (live ISO → partition → install → "
            "configure → reboot → reconnect). "
            "If you are uncertain about a flag or command, ask the user "
            "via the bridge chat before executing.\n\n"
        )
        return (
            "== DISTRO INSTALL PRIMER (not found) ==\n\n"
            + fallback
            + supported_distros_summary()
            + "\n"
        )
    entry = resolve_primer(distro)
    assert entry is not None  # load_primer_text returned non-None
    header = (
        f"== DISTRO INSTALL PRIMER: {entry['canonical']} ==\n\n"
        "The following Markdown is QuickHatch's curated install guide for this "
        "distro. It is authoritative — follow the command ordering and flag "
        "choices here rather than improvising. Placeholders in <ANGLE_BRACKETS> "
        "must be substituted with values from the user profile (username, "
        "hostname, target disk, timezone, public SSH key, passwords).\n\n"
    )
    return header + primer + "\n"


__all__ = [
    "list_supported_distros",
    "load_primer_text",
    "primer_prompt_block",
    "resolve_primer",
    "supported_distros_summary",
]
