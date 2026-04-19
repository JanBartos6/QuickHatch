"""Build a concrete 'apply this gallery recipe' prompt block.

Originally the desktop-section prompt told the model 'assume the first
pinned recipe will be applied automatically.' That was a lie — nothing
applied it — so models would skip theme/icon/wallpaper work and the
user ended up with a raw default desktop.

This module turns the user's first pinned recipe into an explicit
block of concrete apply instructions (theme name, icon pack, fonts,
wallpaper hint, dotfiles URL, component tooling) that gets injected
into the desktop + plan sections AND the setup agent's system prompt.
The model is expected to execute the instructions during post-install
configuration rather than hand-waving them away.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

_GALLERY_PACKAGE = "quickhatch.data"


def _load_gallery() -> list[dict[str, Any]]:
    """Load the gallery entries list. Cached read."""
    try:
        with (
            resources.files(_GALLERY_PACKAGE)
            .joinpath("gallery.json")
            .open("r", encoding="utf-8") as handle
        ):
            data = json.load(handle)
    except (OSError, json.JSONDecodeError, ModuleNotFoundError):
        return []
    if isinstance(data, dict):
        # Legacy shape {id: entry}.
        return list(data.values())
    if isinstance(data, list):
        return data
    return []


def _find_entry(recipe_id: str) -> dict[str, Any] | None:
    if not recipe_id:
        return None
    for entry in _load_gallery():
        if entry.get("id") == recipe_id:
            return entry
    return None


def _stringy(value: Any) -> str:
    """Return a short display-safe string for prompt injection."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return ", ".join(_stringy(v) for v in value if v not in (None, ""))
    if isinstance(value, dict):
        # Show the non-null key:value pairs.
        flat = [f"{k}={_stringy(v)}" for k, v in value.items() if v not in (None, "")]
        return "; ".join(flat)
    return str(value)


def _applicable_fields(entry: dict[str, Any]) -> list[tuple[str, str]]:
    """Pick the subset of fields the agent can actually act on.

    Order matters — we emit them in the sequence the agent should apply.
    """
    pairs: list[tuple[str, str]] = []

    def add(label: str, raw: Any) -> None:
        v = _stringy(raw)
        if v:
            pairs.append((label, v))

    add("Title", entry.get("title"))
    add("Source", entry.get("source_url") or entry.get("dotfiles_url"))
    add("Desktop environment", entry.get("de"))
    add("Theme (GTK/Qt)", entry.get("theme"))
    add("Icon pack", entry.get("icons"))
    add("Cursor theme", entry.get("cursor"))
    add("UI font", entry.get("font_ui"))
    add("Monospace font", entry.get("font_mono"))
    add("Accent color", entry.get("accent_color"))
    add("Wallpaper hint", entry.get("wallpaper_hint"))

    components = entry.get("components") or {}
    if isinstance(components, dict):
        for ck in ("terminal", "shell", "launcher", "panel", "dock", "editor", "file_manager"):
            add(f"Component — {ck}", components.get(ck))

    add("Packages (pre-listed)", entry.get("packages"))
    add("Install method", entry.get("install_method"))
    add("Dotfiles URL", entry.get("dotfiles_url"))
    add("Dotfiles license", entry.get("dotfiles_license"))

    return pairs


def recipe_apply_block(gallery_picks: list[str] | None) -> str:
    """Return a prompt block that tells the agent HOW to apply the
    first pinned recipe. Empty string when no picks / the pick isn't
    known to the gallery.

    Designed to be concatenated into a larger system prompt. Callers
    decide where to slot it in.
    """
    if not gallery_picks:
        return ""
    entry = _find_entry(gallery_picks[0])
    if entry is None:
        return ""

    pairs = _applicable_fields(entry)
    if not pairs:
        return ""

    lines: list[str] = []
    lines.append("== APPLY THIS GALLERY RECIPE ==")
    lines.append("")
    lines.append(
        "The user pinned a visual recipe from QuickHatch's gallery. The "
        "values below are the TARGET state — you MUST apply them during "
        "post-install configuration, not merely describe them. Use "
        "DE-appropriate commands:"
    )
    lines.append("")
    lines.append(
        "  - KDE Plasma: `kwriteconfig6 --file kdeglobals --group <Group> "
        "--key <Key> <Value>` for theme/icons/cursor, `plasma-apply-"
        "wallpaperimage` for wallpaper.\n"
        "  - GNOME: `gsettings set org.gnome.desktop.interface "
        "icon-theme '<Pack>'`, `gtk-theme`, `cursor-theme`, "
        "`monospace-font-name`; `gsettings set org.gnome.desktop."
        "background picture-uri 'file://<path>'` for wallpaper.\n"
        "  - Hyprland / i3 / bspwm / sway: write the DE's config files "
        "directly (e.g. `~/.config/hypr/hyprland.conf`) and restart the "
        "compositor. Reference the dotfiles URL for the authoritative "
        "config layout."
    )
    lines.append("")
    lines.append("Concrete values to apply:")
    lines.append("")
    for label, value in pairs:
        lines.append(f"- **{label}:** {value}")
    lines.append("")
    lines.append(
        "Package install: if the theme/icons/fonts above aren't already "
        "installed by the primer-driven package set, install them "
        "before applying (distro package manager first, then AUR/"
        "Flatpak/manual). For fonts from non-repo sources, drop them "
        "into `~/.local/share/fonts/` and run `fc-cache -fv`."
    )
    lines.append("")
    lines.append(
        "If a value above is impossible on the chosen DE (e.g. a Plasma-"
        "specific theme on GNOME), pick the closest equivalent from the "
        "same palette and note the substitution in your summary — do not "
        "skip the step entirely."
    )
    lines.append("")
    return "\n".join(lines)


__all__ = ["recipe_apply_block"]
