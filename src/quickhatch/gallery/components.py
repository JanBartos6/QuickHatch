"""Deterministic extraction of desktop components and decorations from a
cloned dotfiles repo.

Produces the ``components`` and ``decorations`` substructures used by the
gallery's LLM-curated schema v2. Regex-based, fast, zero-LLM. The LLM pass
handles the qualitative (description, best_for, density, mood).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Component detection
# ---------------------------------------------------------------------------

PANEL_SIGNATURES = {
    "waybar": ("waybar/config", "waybar/config.jsonc", "waybar/style.css"),
    "polybar": ("polybar/config.ini", "polybar/launch.sh", "polybar/config"),
    "plasma-panel": ("plasma-org.kde.plasma.desktop-appletsrc",),
    "xfce4-panel": ("xfce4/panel/",),
    "i3bar": ("i3status/config", "i3blocks/config", "i3status.conf"),
    "gnome-extensions": ("gnome-shell/extensions/",),
    "ags": ("ags/config.js", "ags/config.ts", "ags/main.ts", "ags/app.ts", "ags/App.ts"),
    "eww": ("eww/eww.yuck",),
    "quickshell": ("quickshell/shell.qml", "quickshell/ii/", "quickshell/config.qml"),
}

DOCK_SIGNATURES = {
    "plank": (".config/plank/", "plank/"),
    "latte-dock": (".config/lattedockrc",),
    "dockbarx": ("dockbarx/",),
    "nwg-dock": ("nwg-dock/",),
    "swaydock": ("waydroid.conf",),
}

LAUNCHER_SIGNATURES = {
    "rofi": (".config/rofi/config.rasi", "rofi/config.rasi", "rofi/config"),
    "wofi": (".config/wofi/config", "wofi/config"),
    "anyrun": (".config/anyrun/config.ron", "anyrun/"),
    "fuzzel": (".config/fuzzel/fuzzel.ini", "fuzzel/"),
    "walker": (".config/walker/config.json", "walker/"),
    "tofi": (".config/tofi/config", "tofi/"),
    "dmenu": ("dmenu/config.h", "dmenu-config.h"),
    "krunner": ("krunnerrc",),
    "ulauncher": (".config/ulauncher/", "ulauncher/"),
}

NOTIFICATIONS_SIGNATURES = {
    "dunst": (".config/dunst/dunstrc", "dunst/dunstrc"),
    "swaync": (".config/swaync/config.json", "swaync/"),
    "mako": (".config/mako/config", "mako/"),
    "fnott": (".config/fnott/", "fnott/"),
    "xfce4-notifyd": ("xfce4-notifyd.xml",),
}

LOCKSCREEN_SIGNATURES = {
    "hyprlock": (".config/hypr/hyprlock.conf", "hypr/hyprlock.conf", "hyprlock.conf"),
    "swaylock": (".config/swaylock/config", "swaylock/config", "swaylock.conf"),
    "gtklock": (".config/gtklock/", "gtklock/"),
    "waylock": ("waylock/",),
    "i3lock": (".config/i3/lock", "i3-lock"),
    "betterlockscreen": ("betterlockscreen/",),
}

TERMINAL_SIGNATURES = {
    "kitty": (".config/kitty/kitty.conf", "kitty/kitty.conf", "kitty.conf"),
    "alacritty": (
        ".config/alacritty/alacritty.toml",
        ".config/alacritty/alacritty.yml",
        "alacritty/alacritty.toml",
        "alacritty.toml",
    ),
    "wezterm": (".config/wezterm/wezterm.lua", "wezterm/wezterm.lua", "wezterm.lua"),
    "foot": (".config/foot/foot.ini", "foot/foot.ini"),
    "st": ("st/config.h", "st-config.h"),
    "ghostty": (".config/ghostty/config", "ghostty/"),
    "terminator": (".config/terminator/", "terminator/"),
}

SHELL_SIGNATURES = {
    "fish": (".config/fish/config.fish", "fish/config.fish"),
    "zsh": (".zshrc", "zsh/.zshrc", "zshrc"),
    "bash": (".bashrc", "bash/.bashrc"),
    "nushell": (".config/nushell/config.nu", "nushell/"),
}

PROMPT_SIGNATURES = {
    "starship": (".config/starship.toml", "starship.toml"),
    "powerlevel10k": (".p10k.zsh", "powerlevel10k/"),
    "oh-my-posh": (".config/oh-my-posh/", "oh-my-posh/"),
    "spaceship": ("spaceship.zsh",),
    "pure": ("pure/",),
}

FILE_MANAGER_SIGNATURES = {
    "nautilus": ("nautilus/",),
    "dolphin": ("dolphinrc",),
    "thunar": (".config/Thunar/", "thunar/"),
    "nnn": (".config/nnn/", "nnn/"),
    "lf": (".config/lf/lfrc", "lf/lfrc"),
    "ranger": (".config/ranger/rc.conf", "ranger/rc.conf"),
    "yazi": (".config/yazi/yazi.toml", "yazi/"),
    "pcmanfm": ("pcmanfm/",),
}

EDITOR_SIGNATURES = {
    "neovim": (".config/nvim/init.lua", ".config/nvim/init.vim", "nvim/init.lua", "nvim/init.vim"),
    "vim": (".vimrc", "vim/.vimrc", "vimrc"),
    "helix": (".config/helix/config.toml", "helix/"),
    "emacs": (".emacs", ".emacs.d/init.el", "emacs/init.el"),
    "vscode": (".config/Code/User/settings.json", "vscode/settings.json"),
    "zed": (".config/zed/settings.json", "zed/"),
}


def _relative_paths(repo_dir: Path) -> set[str]:
    """Collect every file path relative to repo_dir as forward-slash strings."""
    paths: set[str] = set()
    try:
        for path in repo_dir.rglob("*"):
            if path.is_file() and ".git" not in path.parts:
                rel = path.relative_to(repo_dir).as_posix()
                paths.add(rel)
                # Also store without leading dotdirs for prefix matching.
                if rel.startswith(".config/"):
                    paths.add(rel[len(".config/") :])
    except OSError:
        pass
    return paths


def _signature_matches(paths: set[str], sig: str) -> bool:
    """Match a signature path fragment against the repo's file index.

    Signatures are treated as path suffixes so ``dots/.config/hypr/hyprland.conf``
    and ``home/user/.config/hypr/hyprland.conf`` both match ``hypr/hyprland.conf``.
    """
    sig_lower = sig.lower().rstrip("/")
    if "/" not in sig_lower:
        # Single-token signatures (rare) match a basename anywhere.
        return any(p.lower().endswith("/" + sig_lower) or p.lower() == sig_lower for p in paths)
    # Multi-segment signatures match as path suffixes or contained folders.
    for path in paths:
        plow = path.lower()
        if plow.endswith(sig_lower):
            return True
        if "/" + sig_lower + "/" in "/" + plow + "/":
            return True
    return False


def _detect_component(paths: set[str], signatures: dict[str, tuple[str, ...]]) -> str | None:
    for name, sigs in signatures.items():
        if any(_signature_matches(paths, sig) for sig in sigs):
            return name
    return None


def _detect_all(paths: set[str], signatures: dict[str, tuple[str, ...]]) -> list[str]:
    """Return every signature that matched. Order preserved."""
    return [
        name
        for name, sigs in signatures.items()
        if any(_signature_matches(paths, sig) for sig in sigs)
    ]


def _has_any_file_under(repo_dir: Path, folder_name: str, extensions: tuple[str, ...]) -> bool:
    """True if repo_dir contains any file with extension in folder named folder_name."""
    try:
        for path in repo_dir.rglob(f"*{folder_name}*"):
            if path.is_dir() and ".git" not in path.parts:
                for ext in extensions:
                    if any(
                        sub.is_file() and sub.name.endswith(ext) for sub in path.rglob(f"*{ext}")
                    ):
                        return True
    except OSError:
        return False
    return False


def extract_components(repo_dir: Path) -> dict[str, Any]:
    """Detect high-level desktop components from a cloned repo."""
    paths = _relative_paths(repo_dir)

    # Panel detection has a catch-all: ANY quickshell dir with .qml files,
    # ANY ags dir with .js/.ts, are strong signals those are the panels.
    panel = _detect_component(paths, PANEL_SIGNATURES)
    if panel is None:
        if _has_any_file_under(repo_dir, "quickshell", (".qml",)):
            panel = "quickshell"
        elif _has_any_file_under(repo_dir, "ags", (".js", ".ts")):
            panel = "ags"

    return {
        "panel": panel,
        "dock": _detect_component(paths, DOCK_SIGNATURES),
        "launcher": _detect_component(paths, LAUNCHER_SIGNATURES),
        "notifications": _detect_component(paths, NOTIFICATIONS_SIGNATURES),
        "lockscreen": _detect_component(paths, LOCKSCREEN_SIGNATURES),
        "terminal": _detect_component(paths, TERMINAL_SIGNATURES),
        "shell": _detect_component(paths, SHELL_SIGNATURES),
        "prompt": _detect_component(paths, PROMPT_SIGNATURES),
        "file_manager": _detect_component(paths, FILE_MANAGER_SIGNATURES),
        "editor": _detect_component(paths, EDITOR_SIGNATURES),
        "all_terminals": _detect_all(paths, TERMINAL_SIGNATURES),
        "all_launchers": _detect_all(paths, LAUNCHER_SIGNATURES),
    }


# ---------------------------------------------------------------------------
# Window manager decorations
# ---------------------------------------------------------------------------

HYPR_DECORATIONS_BLOCK_RE = re.compile(r"decoration\s*\{([^}]*)\}", re.IGNORECASE | re.DOTALL)
HYPR_GENERAL_BLOCK_RE = re.compile(r"general\s*\{([^}]*)\}", re.IGNORECASE | re.DOTALL)
HYPR_ANIMATIONS_BLOCK_RE = re.compile(r"animations\s*\{([^}]*)\}", re.IGNORECASE | re.DOTALL)

_INT_RE = re.compile(r"=\s*(\d+)")
_BOOL_RE = re.compile(r"=\s*(true|yes|on|1|false|no|off|0)", re.IGNORECASE)


def _parse_kv_int(block: str, key: str) -> int | None:
    pattern = re.compile(rf"\b{re.escape(key)}\s*=\s*(\d+)", re.IGNORECASE)
    m = pattern.search(block)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def _parse_kv_bool(block: str, key: str) -> bool | None:
    pattern = re.compile(rf"\b{re.escape(key)}\s*=\s*(true|yes|on|1|false|no|off|0)", re.IGNORECASE)
    m = pattern.search(block)
    if not m:
        return None
    return m.group(1).lower() in {"true", "yes", "on", "1"}


def _extract_hyprland_decorations(hypr_text: str) -> dict[str, Any]:
    """Parse hyprland.conf decoration/general/animations blocks."""
    result: dict[str, Any] = {
        "blur": None,
        "rounded_corners_px": None,
        "shadows": None,
        "gaps_in_px": None,
        "gaps_out_px": None,
        "border_size_px": None,
        "animations": None,
    }
    dec_match = HYPR_DECORATIONS_BLOCK_RE.search(hypr_text)
    if dec_match:
        block = dec_match.group(1)
        result["rounded_corners_px"] = _parse_kv_int(block, "rounding")
        result["shadows"] = _parse_kv_bool(block, "drop_shadow")
        # Blur is a nested block; detect presence + enabled.
        blur_inner = re.search(r"blur\s*\{([^}]*)\}", block, re.IGNORECASE | re.DOTALL)
        if blur_inner:
            enabled = _parse_kv_bool(blur_inner.group(1), "enabled")
            result["blur"] = bool(enabled) if enabled is not None else True
        else:
            result["blur"] = _parse_kv_bool(block, "blur")

    gen_match = HYPR_GENERAL_BLOCK_RE.search(hypr_text)
    if gen_match:
        block = gen_match.group(1)
        result["gaps_in_px"] = _parse_kv_int(block, "gaps_in")
        result["gaps_out_px"] = _parse_kv_int(block, "gaps_out")
        result["border_size_px"] = _parse_kv_int(block, "border_size")

    anim_match = HYPR_ANIMATIONS_BLOCK_RE.search(hypr_text)
    if anim_match:
        block = anim_match.group(1)
        enabled = _parse_kv_bool(block, "enabled")
        bezier_count = len(re.findall(r"^\s*bezier\s*=", block, re.MULTILINE))
        animation_count = len(re.findall(r"^\s*animation\s*=", block, re.MULTILINE))
        if enabled is False:
            result["animations"] = "none"
        elif animation_count >= 8:
            result["animations"] = "maximalist"
        elif animation_count >= 3 or bezier_count >= 2:
            result["animations"] = "subtle"
        elif animation_count > 0:
            result["animations"] = "subtle"

    return result


def _extract_sway_decorations(sway_text: str) -> dict[str, Any]:
    """Parse sway/i3 config for gap/border/blur style decorations."""
    result: dict[str, Any] = {
        "blur": None,
        "rounded_corners_px": None,
        "shadows": None,
        "gaps_in_px": None,
        "gaps_out_px": None,
        "border_size_px": None,
        "animations": None,
    }
    gap_inner = re.search(r"gaps\s+inner\s+(\d+)", sway_text, re.IGNORECASE)
    if gap_inner:
        result["gaps_in_px"] = int(gap_inner.group(1))
    gap_outer = re.search(r"gaps\s+outer\s+(\d+)", sway_text, re.IGNORECASE)
    if gap_outer:
        result["gaps_out_px"] = int(gap_outer.group(1))
    default_border = re.search(r"default_border\s+pixel\s+(\d+)", sway_text, re.IGNORECASE)
    if default_border:
        result["border_size_px"] = int(default_border.group(1))
    # Sway itself has no blur; swayfx does.
    if re.search(r"^blur\s+(on|enable|1)", sway_text, re.IGNORECASE | re.MULTILINE):
        result["blur"] = True
    corner = re.search(r"corner_radius\s+(\d+)", sway_text, re.IGNORECASE)
    if corner:
        result["rounded_corners_px"] = int(corner.group(1))
    return result


def _find_first_by_name(repo_dir: Path, name: str, max_depth: int = 6) -> Path | None:
    """Locate a file anywhere in the repo by name (recursive, depth-limited)."""
    try:
        for path in repo_dir.rglob(name):
            if path.is_file() and ".git" not in path.parts:
                # Respect depth cap to avoid pathological deep searches.
                if len(path.relative_to(repo_dir).parts) <= max_depth:
                    return path
    except OSError:
        return None
    return None


def extract_decorations(repo_dir: Path) -> dict[str, Any]:
    """Detect WM-level decoration settings. Returns empty dict if no WM config.

    Uses recursive rglob because real rices often nest configs under
    ``dots/``, ``home/``, ``nix/home/.config/``, etc.
    """
    hypr = _find_first_by_name(repo_dir, "hyprland.conf")
    if hypr is not None:
        try:
            text = hypr.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        # Concatenate common Hyprland modular config fragments if they sit
        # next to hyprland.conf.
        for sub_name in (
            "decoration.conf",
            "general.conf",
            "animations.conf",
            "variables.conf",
        ):
            sub = hypr.parent / sub_name
            if sub.exists() and sub.is_file():
                try:
                    text += "\n" + sub.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pass
            # Also check hypr/custom/ subfolder convention.
            sub2 = hypr.parent / "custom" / sub_name
            if sub2.exists() and sub2.is_file():
                try:
                    text += "\n" + sub2.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pass
        if text:
            return _extract_hyprland_decorations(text)

    # Sway / swayfx.
    sway = _find_first_by_name(repo_dir, "config")
    if sway is not None and "sway" in sway.as_posix().lower():
        try:
            return _extract_sway_decorations(sway.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass

    # i3.
    for rel in (".config/i3/config", "i3/config"):
        path = repo_dir / rel
        if path.exists() and path.is_file():
            try:
                return _extract_sway_decorations(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue

    return {}


# ---------------------------------------------------------------------------
# Accent color heuristic
# ---------------------------------------------------------------------------

_HEX_RE = re.compile(r"(?<![A-Fa-f0-9])#([0-9A-Fa-f]{6})(?![0-9A-Fa-f])")
_ACCENT_KEYWORDS = ("accent", "primary", "active", "focused", "highlight", "selection")


def _is_neutral(hex_color: str) -> bool:
    """Reject near-grayscale or near-black/white picks."""
    try:
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
    except ValueError:
        return True
    # Near-black or near-white.
    if max(r, g, b) < 30 or min(r, g, b) > 230:
        return True
    # Too close to grayscale.
    if abs(r - g) < 15 and abs(g - b) < 15 and abs(r - b) < 15:
        return True
    return False


def extract_accent_color(repo_dir: Path) -> str | None:
    """Grep for an accent/primary color keyword near a hex code."""
    candidates = (
        ".config/waybar/style.css",
        "waybar/style.css",
        ".config/hypr/hyprland.conf",
        "hypr/hyprland.conf",
        ".config/kitty/theme.conf",
        ".config/rofi/config.rasi",
        ".config/rofi/themes",
        ".config/kvantum.kvconfig",
    )
    for rel in candidates:
        path = repo_dir / rel
        try:
            if path.is_dir():
                files = [p for p in path.rglob("*") if p.is_file()][:10]
            elif path.is_file():
                files = [path]
            else:
                continue
        except OSError:
            continue
        for file_path in files:
            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                lower = line.lower()
                if not any(kw in lower for kw in _ACCENT_KEYWORDS):
                    continue
                m = _HEX_RE.search(line)
                if m and not _is_neutral(m.group(1)):
                    return "#" + m.group(1).upper()
    return None


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def extract_all(repo_dir: Path) -> dict[str, Any]:
    """Bundle components + decorations + accent color for a cloned repo."""
    return {
        "components": extract_components(repo_dir),
        "decorations": extract_decorations(repo_dir),
        "accent_color": extract_accent_color(repo_dir),
    }


__all__ = [
    "extract_all",
    "extract_accent_color",
    "extract_components",
    "extract_decorations",
]
