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


def resolve_primer(distro: str | None, preferred: str | None = None) -> dict[str, Any] | None:
    """Given any user-typed distro hint, find the matching primer entry.

    Matches against canonical names and aliases with light normalization
    (lowercase, hyphenate whitespace). Returns the index entry dict
    (with file path, family, release model) or ``None`` if no match.

    ``preferred`` is the user's stated distro preference — used as a
    strong tiebreaker when the free-text hint mentions multiple distros
    (e.g. the analysis output names Arch as the recommendation and then
    mentions Fedora in a negative comparison). If the preferred distro
    appears anywhere in the hint, it wins.
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

    # Helper: is a distro token mentioned in a NEGATIVE context? The model
    # often contrasts the pick with alternatives ("unlike Ubuntu", "not
    # Fedora", "the bloat of distributions like Ubuntu or Fedora") — those
    # mentions must not win the resolver.
    def _is_negative_context(text: str, match_start: int, match_end: int) -> bool:
        # Look back only within the current local clause. A negative cue
        # from an earlier clause ("not fedora. go with arch") must not
        # poison the later positive recommendation.
        raw_window = text[max(0, match_start - 60) : match_start + 1]
        clause_window = re.split(r"[.;:!?]", raw_window)[-1]
        window = clause_window or raw_window
        # Word-boundary cues so "likely" doesn't trip "like" and
        # "Linux-Mint" doesn't trip "not". Each cue is wrapped so it
        # matches only at word boundaries inside the hyphenated needle.
        negative_cue_words = (
            "unlike",
            "not",
            "rather",
            "instead",
            "avoid",
            "such",
            "compared",
            "versus",
            "vs",
            "over",  # "chose X over Y"
            "than",  # "better than Y"
            "bloat",
            "constraints",
        )
        for cue in negative_cue_words:
            pattern = rf"(?:^|[^a-z0-9]){re.escape(cue)}(?:$|[^a-z0-9])"
            if re.search(pattern, window):
                return True
        # "like" is ambiguous — "I'd like arch" (positive) vs
        # "distributions like X" (negative). Only treat as negative when
        # preceded by a list-noun within ~20 chars.
        list_noun_like = re.compile(
            r"(?:distributions?|distros?|systems?|options?|oses|choices?|"
            r"alternatives?)[^a-z0-9]{1,5}like(?:$|[^a-z0-9])",
            re.IGNORECASE,
        )
        if list_noun_like.search(window):
            return True
        return False

    # 2a. STRONGEST SIGNAL: if the caller passed a preferred distro AND it
    # appears in the text as a non-negative mention, return it. This
    # prevents the comparison-trailing-mention bug where "Arch is better
    # than Fedora" resolves to Fedora.
    if preferred:
        pref_entry: dict[str, Any] | None = None
        pref_needle = _normalize(preferred)
        for entry in _load_index().get("distros", []):
            canonical = _normalize(entry.get("canonical", ""))
            aliases = [_normalize(a) for a in entry.get("aliases", [])]
            if pref_needle == canonical or pref_needle in aliases:
                pref_entry = entry
                break
        if pref_entry is not None:
            for candidate in [
                _normalize(pref_entry.get("canonical", "")),
                *(_normalize(a) for a in pref_entry.get("aliases", [])),
            ]:
                if not candidate:
                    continue
                pattern = rf"(?:^|[^a-z0-9]){re.escape(candidate)}(?:$|[^a-z0-9])"
                for mobj in re.finditer(pattern, needle):
                    if not _is_negative_context(needle, mobj.start(), mobj.end()):
                        return pref_entry

    # 2b. Word-boundary search within a longer free-text hint
    #    ("I'd like arch btw" → matches "arch"). Still rejects
    #    accidental-substring cases like "templeos" matching "eos".
    #
    # When multiple distros appear in the text (common when the model
    # compares options before settling), we want the one the model
    # ACTUALLY picks — usually the last occurrence after "recommend",
    # "recommended", "choose", or "go with". First try a targeted
    # recommendation-phrase scan; fall back to last-occurrence.
    #
    # The recommend scan now looks for ANY distro token within 80 chars
    # after a recommendation verb — not just the immediately-next word.
    # Previously "recommended distribution: arch linux" captured the
    # word "distribution" (junk), failed to match, and fell through to
    # the last-mention heuristic (which picked up "Fedora" from a
    # negative comparison at the end of the paragraph).
    _RECOMMEND_RE = re.compile(
        r"(?:recommend(?:ation|ed|ing)?|i\s+suggest|i\s+pick|i\s+choose|"
        r"go\s+with|final\s+choice[: ]|best\s+fit[: ])"
        r"([\s\S]{0,80})",
        re.IGNORECASE,
    )
    rec_matches = list(_RECOMMEND_RE.finditer(needle))
    if rec_matches:
        for rm in reversed(rec_matches):
            window = rm.group(1)
            # Recompute absolute offsets so negative-context detection
            # works against the full needle.
            window_start = rm.start(1)
            for entry in _load_index().get("distros", []):
                for candidate in (
                    _normalize(entry.get("canonical", "")),
                    *(_normalize(alias) for alias in entry.get("aliases", [])),
                ):
                    if not candidate:
                        continue
                    pattern = rf"(?:^|[^a-z0-9]){re.escape(candidate)}(?:$|[^a-z0-9])"
                    m = re.search(pattern, window)
                    if m and not _is_negative_context(
                        needle, window_start + m.start(), window_start + m.end()
                    ):
                        return entry

    # No recommendation phrase matched a known distro — fall back to
    # scanning the WHOLE text but return the LAST distro mentioned that
    # isn't in a negative context.
    last_match: tuple[int, dict[str, Any]] | None = None
    for entry in _load_index().get("distros", []):
        haystack = [
            _normalize(entry.get("canonical", "")),
            *(_normalize(alias) for alias in entry.get("aliases", [])),
        ]
        for candidate in haystack:
            if not candidate:
                continue
            pattern = rf"(?:^|[^a-z0-9]){re.escape(candidate)}(?:$|[^a-z0-9])"
            for mobj in re.finditer(pattern, needle):
                if _is_negative_context(needle, mobj.start(), mobj.end()):
                    continue
                if last_match is None or mobj.start() > last_match[0]:
                    last_match = (mobj.start(), entry)
    if last_match is not None:
        return last_match[1]
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
