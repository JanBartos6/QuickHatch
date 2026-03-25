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
        lines = result.stdout.strip().split("\n")
        # Skip header lines (find the separator line with dashes)
        data_start = 0
        for i, line in enumerate(lines):
            if set(line.strip()) <= {"-", " "}  and len(line.strip()) > 10:
                data_start = i + 1
                break

        for line in lines[data_start:]:
            parts = line.strip()
            if not parts:
                continue
            # winget list output is column-aligned; extract name from first column
            name = parts[:40].strip()
            if name and len(name) > 1:
                apps.append(
                    AppInfo(name=name, source="winget")
                )
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
                apps.append(AppInfo(name=name.strip(), version=version.strip() or None, source="dpkg"))
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
                apps.append(AppInfo(name=name.strip(), version=version.strip() or None, source="rpm"))
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
    else:
        # Linux: try native package managers, then flatpak/snap
        all_apps: list[AppInfo] = []

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
