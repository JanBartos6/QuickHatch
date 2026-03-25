"""Scan user files and browser data."""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from pathlib import Path

from quickhatch.console import print_info, print_warning


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
    home = Path.home()
    browsers: dict[str, str] = {}

    # Chrome
    chrome_path = home / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
    if chrome_path.exists():
        browsers["chrome"] = str(chrome_path)

    # Firefox
    firefox_path = home / "AppData" / "Roaming" / "Mozilla" / "Firefox" / "Profiles"
    if firefox_path.exists():
        browsers["firefox"] = str(firefox_path)

    # Edge
    edge_path = home / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data"
    if edge_path.exists():
        browsers["edge"] = str(edge_path)

    # Brave
    brave_path = (
        home / "AppData" / "Local" / "BraveSoftware" / "Brave-Browser" / "User Data"
    )
    if brave_path.exists():
        browsers["brave"] = str(brave_path)

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
