"""Scan user files summary (no contents read, just metadata)."""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from pathlib import Path


def scan() -> dict:
    """Scan user files and return a summary."""
    home = Path.home()
    summary = {
        "home_dir": str(home),
        "total_files": 0,
        "total_size_gb": 0.0,
        "file_types": {},
        "important_dirs": {},
        "browser_profiles": [],
        "config_files": [],
    }

    # Directories to scan
    if sys.platform == "win32":
        scan_dirs = [
            home / "Documents",
            home / "Desktop",
            home / "Downloads",
            home / "Pictures",
            home / "Music",
            home / "Videos",
        ]
    else:
        scan_dirs = [
            home / "Documents",
            home / "Desktop",
            home / "Downloads",
            home / "Pictures",
            home / "Music",
            home / "Videos",
            home,
        ]

    total_size = 0
    total_files = 0
    type_counts: dict[str, int] = defaultdict(int)
    type_sizes: dict[str, int] = defaultdict(int)

    for scan_dir in scan_dirs:
        if not scan_dir.exists():
            continue

        dir_size = 0
        dir_files = 0

        try:
            for entry in scan_dir.rglob("*"):
                if entry.is_file():
                    try:
                        size = entry.stat().st_size
                        ext = entry.suffix.lower() or "(no ext)"
                        type_counts[ext] += 1
                        type_sizes[ext] += size
                        total_size += size
                        total_files += 1
                        dir_size += size
                        dir_files += 1
                    except (PermissionError, OSError):
                        pass
        except (PermissionError, OSError):
            pass

        if dir_files > 0:
            summary["important_dirs"][str(scan_dir)] = {
                "files": dir_files,
                "size_gb": round(dir_size / (1024 ** 3), 2),
            }

    summary["total_files"] = total_files
    summary["total_size_gb"] = round(total_size / (1024 ** 3), 2)

    # Top file types by count
    sorted_types = sorted(type_counts.items(), key=lambda x: x[1], reverse=True)[:20]
    summary["file_types"] = {
        ext: {"count": count, "size_mb": round(type_sizes[ext] / (1024 ** 2), 1)}
        for ext, count in sorted_types
    }

    # Detect browser profiles
    summary["browser_profiles"] = _detect_browser_profiles(home)

    # Detect config files worth migrating
    summary["config_files"] = _detect_config_files(home)

    return summary


def _detect_browser_profiles(home: Path) -> list[str]:
    """Detect browser profile directories."""
    profiles = []

    browser_paths = {
        "Chrome": [
            home / "AppData" / "Local" / "Google" / "Chrome" / "User Data",
            home / ".config" / "google-chrome",
        ],
        "Firefox": [
            home / "AppData" / "Roaming" / "Mozilla" / "Firefox" / "Profiles",
            home / ".mozilla" / "firefox",
        ],
        "Edge": [
            home / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data",
        ],
        "Brave": [
            home / "AppData" / "Local" / "BraveSoftware" / "Brave-Browser" / "User Data",
            home / ".config" / "BraveSoftware" / "Brave-Browser",
        ],
    }

    for browser, paths in browser_paths.items():
        for path in paths:
            if path.exists():
                profiles.append(browser)
                break

    return profiles


def _detect_config_files(home: Path) -> list[str]:
    """Detect configuration files worth migrating."""
    configs = []

    config_patterns = [
        (".gitconfig", "Git configuration"),
        (".ssh", "SSH keys and config"),
        (".npmrc", "NPM configuration"),
        (".vimrc", "Vim configuration"),
        (".bashrc", "Bash configuration"),
        (".zshrc", "Zsh configuration"),
        ("AppData/Roaming/Code/User/settings.json", "VS Code settings"),
        (".config/Code/User/settings.json", "VS Code settings"),
        (".config", "Application configs"),
    ]

    for pattern, description in config_patterns:
        path = home / pattern
        if path.exists():
            configs.append(f"{description} ({pattern})")

    return configs
