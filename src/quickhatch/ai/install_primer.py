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
    needle = _normalize(distro)
    if not needle:
        return None
    for entry in _load_index().get("distros", []):
        canonical = _normalize(entry.get("canonical", ""))
        if canonical == needle:
            return entry
        if any(_normalize(alias) == needle for alias in entry.get("aliases", [])):
            return entry
    # Fallback: word-boundary match on canonical + aliases. Useful for
    # free-text inputs like "I'd like arch btw" → matches "arch" as a
    # standalone token, but "templeos" does NOT match the "eos" alias.
    import re

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


def load_primer_text(distro: str | None) -> str | None:
    """Return the primer Markdown body for ``distro``, or None if no match."""
    entry = resolve_primer(distro)
    if entry is None:
        return None
    filename = entry.get("file")
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
        fallback = (
            f"The requested distro ``{distro}`` is not in QuickHatch's "
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
