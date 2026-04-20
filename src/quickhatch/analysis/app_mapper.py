"""Map Windows applications to Linux alternatives."""

from __future__ import annotations

import json
import re
from pathlib import Path

from quickhatch.config import AppInfo
from quickhatch.console import print_info


def _load_db() -> dict:
    db_path = Path(__file__).parent.parent / "data" / "app_alternatives.json"
    if db_path.exists():
        # Explicit utf-8 — Windows default cp1252 crashes on the non-ASCII
        # characters in some app names.
        return json.loads(db_path.read_text(encoding="utf-8"))
    return {}


def _fuzzy_match(app_name: str, db: dict) -> str | None:
    """Try to match an app name against the database keys.

    Bidirectional substring match for keys ≥ 3 chars. Skips 1-2 char
    keys entirely — those (e.g. "c", "go", "r") would poison the
    mapping by matching nearly every app name.
    """
    app_lower = app_name.lower().strip()
    if not app_lower:
        return None
    # Exact first
    for key in db:
        if key.lower() == app_lower:
            return key
    # Then bidirectional substring, min-length-gated
    for key in db:
        key_lower = key.lower()
        if len(key_lower) < 3:
            continue
        if key_lower in app_lower or app_lower in key_lower:
            return key
    return None


def _contains_native_app(app_name: str, native_linux: set[str]) -> bool:
    """Match known native apps as whole phrases, not loose substrings."""
    app_lower = app_name.lower().strip()
    if not app_lower:
        return False
    for name in native_linux:
        if app_lower == name:
            return True
        pattern = rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])"
        if re.search(pattern, app_lower):
            return True
    return False


def map_apps(apps: list[AppInfo]) -> list[AppInfo]:
    """Annotate apps with Linux alternatives and compatibility info."""
    db = _load_db()

    # Known cross-platform apps (run natively on Linux)
    native_linux = {
        "firefox",
        "chrome",
        "chromium",
        "brave",
        "vlc",
        "obs",
        "obs studio",
        "blender",
        "gimp",
        "audacity",
        "filezilla",
        "steam",
        "discord",
        "slack",
        "spotify",
        "zoom",
        "visual studio code",
        "vs code",
        "code",
        "postman",
        "docker",
        "virtualbox",
        "git",
        "python",
        "node",
        "nodejs",
        "java",
        "thunderbird",
        "keepassxc",
        "signal",
        "telegram",
        "libreoffice",
        "inkscape",
        "krita",
        "kdenlive",
        "handbrake",
        "qbittorrent",
        "deluge",
        "transmission",
    }

    mapped_count = 0

    for app in apps:
        # Check direct database match
        match = _fuzzy_match(app.name, db)
        if match:
            entry = db[match]
            app.linux_alternative = ", ".join(entry["alternatives"])
            app.compatibility = entry["method"]
            app.notes = entry.get("notes", "")
            mapped_count += 1
            continue

        # Check if it's a known native Linux app
        if _contains_native_app(app.name, native_linux):
            app.linux_alternative = f"{app.name} (runs natively on Linux)"
            app.compatibility = "native"
            mapped_count += 1
            continue

        # Mark as unknown
        app.compatibility = "unknown"

    print_info(f"Mapped {mapped_count}/{len(apps)} applications to Linux alternatives")
    return apps
