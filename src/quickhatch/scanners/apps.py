"""Scan installed Windows applications."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING

from quickhatch.config import AppInfo
from quickhatch.console import print_info, print_warning

if TYPE_CHECKING:
    pass


def _scan_winget() -> list[AppInfo]:
    """Scan apps using winget."""
    if sys.platform != "win32":
        return []
    try:
        result = subprocess.run(
            ["winget", "list", "--accept-source-agreements", "--disable-interactivity"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if result.returncode != 0 or not result.stdout:
            return []

        apps = []
        # Strip CR that winget emits, then split.
        lines = result.stdout.replace("\r", "").strip().split("\n")
        # winget's header line starts with 'Name' and lists the remaining
        # columns with runs of whitespace between them. We locate the start
        # position of the SECOND column (Id) — everything before that in a
        # data row is the Name. A blind parts[:40] was grabbing the first
        # char of the Id column on rows with short names, which is why
        # 'Brave' showed up as 'Brave B', 'Vivaldi' as 'Vivaldi A', etc.
        data_start = 0
        name_end_col: int | None = None
        for i, line in enumerate(lines):
            # Separator line: one long run of dashes (possibly with spaces).
            if set(line.strip()) <= {"-", " "} and len(line.strip()) > 10:
                # Header is the line above — find the 2nd column's start.
                if i > 0:
                    header = lines[i - 1]
                    import re as _re

                    # Match each column header (first non-space run after
                    # whitespace). Second match's start = where Name ends.
                    matches = list(_re.finditer(r"\S+", header))
                    if len(matches) >= 2:
                        name_end_col = matches[1].start()
                data_start = i + 1
                break

        import re as _re

        for line in lines[data_start:]:
            stripped = line.strip()
            if not stripped:
                continue
            # Skip winget progress/status lines (happen on non-EN locales
            # more often: "Upgrade verfügbar...", "Aktualisierungen...",
            # CJK characters, etc.) — they typically don't contain a
            # version-looking token.
            if stripped.startswith(("-", "=", "\\", "/", "|")):
                continue
            if name_end_col is not None and len(line) >= name_end_col:
                name = line[:name_end_col].strip()
            else:
                # Fallback: split on 2+ spaces. Works even if the header
                # row got localized or the separator line was absent.
                chunks = _re.split(r"\s{2,}", stripped, maxsplit=1)
                name = chunks[0].strip() if chunks else ""
            # Discard name-only rows that look like separator artifacts
            # (e.g. all dashes from a localized header QuickHatch didn't
            # recognise).
            if set(name) <= {"-", " ", "=", "\t"}:
                continue
            if name and len(name) > 1:
                apps.append(AppInfo(name=name, source="winget"))
        return apps
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def _scan_registry() -> list[AppInfo]:
    """Scan apps from Windows registry."""
    if sys.platform != "win32":
        return []

    try:
        import winreg
    except ImportError:
        return []

    apps = []
    registry_paths = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ]

    for reg_path in registry_paths:
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path)
        except OSError:
            continue

        for i in range(1024):
            try:
                subkey_name = winreg.EnumKey(key, i)
                subkey = winreg.OpenKey(key, subkey_name)
                try:
                    name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                    version = None
                    try:
                        version = winreg.QueryValueEx(subkey, "DisplayVersion")[0]
                    except OSError:
                        pass
                    apps.append(AppInfo(name=name, version=version, source="registry"))
                except OSError:
                    pass
                finally:
                    winreg.CloseKey(subkey)
            except OSError:
                break
        winreg.CloseKey(key)

    return apps


def _scan_powershell() -> list[AppInfo]:
    """Scan apps using PowerShell Get-ItemProperty."""
    try:
        ps_cmd = (
            "Get-ItemProperty "
            "HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*, "
            "HKLM:\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\* "
            "| Select-Object DisplayName, DisplayVersion "
            "| ConvertTo-Json"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if result.returncode != 0 or not result.stdout:
            return []

        data = json.loads(result.stdout)
        if isinstance(data, dict):
            data = [data]

        apps = []
        for item in data:
            name = item.get("DisplayName")
            if name:
                apps.append(
                    AppInfo(
                        name=name,
                        version=item.get("DisplayVersion"),
                        source="registry",
                    )
                )
        return apps
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []


def _scan_dpkg() -> list[AppInfo]:
    """Scan apps via dpkg (Debian/Ubuntu)."""
    try:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f", "${Package}\t${Version}\n"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if result.returncode != 0:
            return []
        apps = []
        for line in (result.stdout or "").strip().split("\n"):
            if "\t" in line:
                name, version = line.split("\t", 1)
                apps.append(
                    AppInfo(name=name.strip(), version=version.strip() or None, source="dpkg")
                )
        return apps
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def _scan_rpm() -> list[AppInfo]:
    """Scan apps via rpm (Fedora/RHEL/SUSE)."""
    try:
        result = subprocess.run(
            ["rpm", "-qa", "--queryformat", "%{NAME}\t%{VERSION}\n"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if result.returncode != 0:
            return []
        apps = []
        for line in (result.stdout or "").strip().split("\n"):
            if "\t" in line:
                name, version = line.split("\t", 1)
                apps.append(
                    AppInfo(name=name.strip(), version=version.strip() or None, source="rpm")
                )
        return apps
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def _scan_pacman() -> list[AppInfo]:
    """Scan apps via pacman (Arch)."""
    try:
        result = subprocess.run(
            ["pacman", "-Q"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if result.returncode != 0:
            return []
        apps = []
        for line in (result.stdout or "").strip().split("\n"):
            parts = line.strip().split(None, 1)
            if parts:
                name = parts[0]
                version = parts[1] if len(parts) > 1 else None
                apps.append(AppInfo(name=name, version=version, source="pacman"))
        return apps
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def _scan_flatpak() -> list[AppInfo]:
    """Scan apps installed via Flatpak."""
    try:
        result = subprocess.run(
            ["flatpak", "list", "--columns=application,version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        if result.returncode != 0:
            return []
        apps = []
        for line in (result.stdout or "").strip().split("\n"):
            parts = line.strip().split("\t")
            if parts and parts[0]:
                name = parts[0].strip()
                version = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
                apps.append(AppInfo(name=name, version=version, source="flatpak"))
        return apps
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def _scan_snap() -> list[AppInfo]:
    """Scan apps installed via Snap."""
    try:
        result = subprocess.run(
            ["snap", "list"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        if result.returncode != 0:
            return []
        apps = []
        lines = (result.stdout or "").strip().split("\n")
        for line in lines[1:]:  # Skip header
            parts = line.split()
            if len(parts) >= 2:
                apps.append(AppInfo(name=parts[0], version=parts[1], source="snap"))
        return apps
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def _scan_brew() -> list[AppInfo]:
    """Scan apps via Homebrew (macOS/Linux)."""
    apps = []
    for list_type, source in [("--formula", "brew"), ("--cask", "brew-cask")]:
        try:
            result = subprocess.run(
                ["brew", "list", list_type, "--versions"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                for line in (result.stdout or "").strip().split("\n"):
                    parts = line.strip().split()
                    if parts:
                        apps.append(
                            AppInfo(
                                name=parts[0],
                                version=parts[1] if len(parts) > 1 else None,
                                source=source,
                            )
                        )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    return apps


def _scan_macos_apps() -> list[AppInfo]:
    """Scan /Applications folder on macOS."""
    from pathlib import Path

    apps = []
    try:
        for app_dir in sorted(Path("/Applications").glob("*.app")):
            name = app_dir.stem
            version = None
            plist = app_dir / "Contents" / "Info.plist"
            if plist.exists():
                try:
                    result = subprocess.run(
                        ["defaults", "read", str(plist), "CFBundleShortVersionString"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    val = (result.stdout or "").strip()
                    if val:
                        version = val
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    pass
            apps.append(AppInfo(name=name, version=version, source="applications"))
    except OSError:
        pass
    return apps


def scan() -> list[AppInfo]:
    """Scan for installed applications using available methods."""
    print_info("Scanning installed applications...")

    if sys.platform == "win32":
        # Try winget first (best structured output)
        apps = _scan_winget()
        if apps:
            print_info(f"Found {len(apps)} apps via winget")
            return apps

        # Try registry directly
        apps = _scan_registry()
        if apps:
            print_info(f"Found {len(apps)} apps via registry")
            return apps

        # Fall back to PowerShell
        apps = _scan_powershell()
        if apps:
            print_info(f"Found {len(apps)} apps via PowerShell")
            return apps

    elif sys.platform == "darwin":
        all_apps: list[AppInfo] = []

        # Homebrew
        apps = _scan_brew()
        if apps:
            print_info(f"Found {len(apps)} packages via Homebrew")
            all_apps.extend(apps)

        # /Applications folder
        apps = _scan_macos_apps()
        if apps:
            print_info(f"Found {len(apps)} apps in /Applications")
            all_apps.extend(apps)

        if all_apps:
            return all_apps

    else:
        # Linux: try native package managers, then flatpak/snap
        all_apps = []

        for name, scanner in [
            ("dpkg", _scan_dpkg),
            ("rpm", _scan_rpm),
            ("pacman", _scan_pacman),
        ]:
            apps = scanner()
            if apps:
                print_info(f"Found {len(apps)} packages via {name}")
                all_apps.extend(apps)
                break  # Only one native package manager will be active

        # Homebrew can also be on Linux
        apps = _scan_brew()
        if apps:
            print_info(f"Found {len(apps)} packages via Homebrew")
            all_apps.extend(apps)

        # Also check flatpak and snap (can coexist with native)
        for name, scanner in [("flatpak", _scan_flatpak), ("snap", _scan_snap)]:
            apps = scanner()
            if apps:
                print_info(f"Found {len(apps)} apps via {name}")
                all_apps.extend(apps)

        if all_apps:
            return all_apps

    print_warning(
        "Could not scan installed applications. "
        "This may happen if running in an unsupported environment."
    )
    return []
