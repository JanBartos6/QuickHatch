"""Scan user files and browser data."""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from pathlib import Path

from quickhatch.console import print_info


def _get_user_dirs() -> list[Path]:
    """Get common user directories."""
    home = Path.home()
    dirs = []
    for name in ["Documents", "Desktop", "Downloads", "Pictures", "Music", "Videos"]:
        d = home / name
        if d.exists():
            dirs.append(d)
    return dirs


def _scan_file_types(dirs: list[Path], max_depth: int = 3) -> dict:
    """Count file types and sizes across user directories."""
    type_counts: Counter = Counter()
    type_sizes: defaultdict[str, int] = defaultdict(int)
    total_files = 0
    total_size = 0

    for directory in dirs:
        for root, _dirs, files in os.walk(directory):
            depth = str(root).count(os.sep) - str(directory).count(os.sep)
            if depth >= max_depth:
                _dirs.clear()
                continue
            for f in files:
                try:
                    fpath = Path(root) / f
                    ext = fpath.suffix.lower() or "(no extension)"
                    size = fpath.stat().st_size
                    type_counts[ext] += 1
                    type_sizes[ext] += size
                    total_files += 1
                    total_size += size
                except (OSError, PermissionError):
                    continue

    return {
        "total_files": total_files,
        "total_size_mb": round(total_size / (1024 * 1024), 1),
        "top_types": dict(type_counts.most_common(20)),
        "scanned_dirs": [str(d) for d in dirs],
    }


def _find_browser_profiles() -> dict:
    """Find browser bookmark and profile locations."""
    import sys

    home = Path.home()
    browsers: dict[str, str] = {}

    if sys.platform == "win32":
        candidates = {
            "chrome": home / "AppData" / "Local" / "Google" / "Chrome" / "User Data",
            "firefox": home / "AppData" / "Roaming" / "Mozilla" / "Firefox" / "Profiles",
            "edge": home / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data",
            "brave": home / "AppData" / "Local" / "BraveSoftware" / "Brave-Browser" / "User Data",
        }
    elif sys.platform == "darwin":
        app_support = home / "Library" / "Application Support"
        candidates = {
            "chrome": app_support / "Google" / "Chrome",
            "firefox": app_support / "Firefox" / "Profiles",
            "safari": home / "Library" / "Safari",
            "edge": app_support / "Microsoft Edge",
            "brave": app_support / "BraveSoftware" / "Brave-Browser",
            "vivaldi": app_support / "Vivaldi",
            "opera": app_support / "com.operasoftware.Opera",
            "arc": app_support / "Arc",
        }
    else:
        # Linux
        candidates = {
            "chrome": home / ".config" / "google-chrome",
            "chromium": home / ".config" / "chromium",
            "firefox": home / ".mozilla" / "firefox",
            "edge": home / ".config" / "microsoft-edge",
            "brave": home / ".config" / "BraveSoftware" / "Brave-Browser",
            "vivaldi": home / ".config" / "vivaldi",
            "opera": home / ".config" / "opera",
        }

    for name, path in candidates.items():
        if path.exists():
            browsers[name] = str(path)

    return browsers


def scan() -> dict:
    """Scan user files and return a summary."""
    print_info("Scanning user files...")

    user_dirs = _get_user_dirs()
    file_summary = _scan_file_types(user_dirs)

    browsers = _find_browser_profiles()
    file_summary["browser_profiles"] = browsers

    print_info(
        f"Found {file_summary['total_files']} files "
        f"({file_summary['total_size_mb']} MB) across {len(user_dirs)} directories"
    )
    if browsers:
        print_info(f"Browser profiles found: {', '.join(browsers.keys())}")

    return file_summary
