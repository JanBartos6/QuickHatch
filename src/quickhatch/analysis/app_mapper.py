"""Map Windows applications to Linux alternatives."""

from __future__ import annotations

import json
from pathlib import Path

from quickhatch.config import AppInfo
from quickhatch.console import print_info


def _load_db() -> dict:
    db_path = Path(__file__).parent.parent / "data" / "app_alternatives.json"
    if db_path.exists():
        return json.loads(db_path.read_text())
    return {}


def _fuzzy_match(app_name: str, db: dict) -> str | None:
    """Try to match an app name against the database keys."""
    app_lower = app_name.lower().strip()
    for key in db:
        if key.lower() in app_lower or app_lower in key.lower():
            return key
    return None


def map_apps(apps: list[AppInfo]) -> list[AppInfo]:
    """Annotate apps with Linux alternatives and compatibility info."""
    db = _load_db()

    # Known cross-platform apps (run natively on Linux)
    native_linux = {
        "firefox", "chrome", "chromium", "brave", "vlc", "obs", "obs studio",
        "blender", "gimp", "audacity", "filezilla", "steam", "discord",
        "slack", "spotify", "zoom", "visual studio code", "vs code",
        "code", "postman", "docker", "virtualbox", "git", "python",
        "node", "nodejs", "java", "thunderbird", "keepassxc", "signal",
        "telegram", "libreoffice", "inkscape", "krita", "kdenlive",
        "handbrake", "qbittorrent", "deluge", "transmission",
    }

    # Electron apps generally work on Linux
    electron_keywords = {"electron", "nw.js", "cef"}

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
        app_lower = app.name.lower()
        if any(name in app_lower for name in native_linux):
            app.linux_alternative = f"{app.name} (runs natively on Linux)"
            app.compatibility = "native"
            mapped_count += 1
            continue

        # Mark as unknown
        app.compatibility = "unknown"

    print_info(
        f"Mapped {mapped_count}/{len(apps)} applications to Linux alternatives"
    )
    return apps
