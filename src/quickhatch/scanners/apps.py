"""Scan installed Windows applications."""

from __future__ import annotations

import subprocess
import json
import sys
from quickhatch.config import AppInfo


def scan() -> list[AppInfo]:
    """Scan for installed applications using multiple strategies."""
    apps: list[AppInfo] = []

    if sys.platform == "win32":
        apps = _scan_winget() or _scan_registry() or _scan_wmic()
    else:
        # Fallback for non-Windows (testing/demo)
        apps = _scan_linux_packages()

    # Deduplicate by name
    seen = set()
    unique = []
    for app in apps:
        key = app.name.lower().strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(app)

    return sorted(unique, key=lambda a: a.name.lower())


def _scan_winget() -> list[AppInfo] | None:
    """Scan apps using winget."""
    try:
        result = subprocess.run(
            ["winget", "list", "--disable-interactivity", "--accept-source-agreements"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return None

        apps = []
        lines = result.stdout.strip().split("\n")
        # Skip header lines (find the separator line with dashes)
        data_start = 0
        for i, line in enumerate(lines):
            if "---" in line:
                data_start = i + 1
                break

        for line in lines[data_start:]:
            parts = line.split()
            if len(parts) >= 2:
                # winget output is column-aligned, name is usually the first part
                name = " ".join(parts[:-2]) if len(parts) > 2 else parts[0]
                version = parts[-2] if len(parts) > 2 else parts[-1]
                apps.append(AppInfo(name=name, version=version, source="winget"))
        return apps if apps else None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _scan_registry() -> list[AppInfo] | None:
    """Scan apps from Windows registry."""
    try:
        import winreg
    except ImportError:
        return None

    apps = []
    registry_paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]

    for hive, path in registry_paths:
        try:
            key = winreg.OpenKey(hive, path)
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    subkey_name = winreg.EnumKey(key, i)
                    subkey = winreg.OpenKey(key, subkey_name)
                    try:
                        name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                        try:
                            version = winreg.QueryValueEx(subkey, "DisplayVersion")[0]
                        except FileNotFoundError:
                            version = None
                        apps.append(AppInfo(name=name, version=version, source="registry"))
                    except FileNotFoundError:
                        pass
                    finally:
                        winreg.CloseKey(subkey)
                except OSError:
                    pass
            winreg.CloseKey(key)
        except OSError:
            pass

    return apps if apps else None


def _scan_wmic() -> list[AppInfo] | None:
    """Scan apps using WMIC (legacy fallback)."""
    try:
        result = subprocess.run(
            ["wmic", "product", "get", "name,version", "/format:csv"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            return None

        apps = []
        for line in result.stdout.strip().split("\n")[1:]:
            parts = line.strip().split(",")
            if len(parts) >= 3:
                name = parts[1].strip()
                version = parts[2].strip()
                if name:
                    apps.append(AppInfo(name=name, version=version, source="wmi"))
        return apps if apps else None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _scan_linux_packages() -> list[AppInfo]:
    """Fallback: scan Linux packages for testing."""
    apps = []

    # Try dpkg
    try:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f", "${Package}\t${Version}\n"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                parts = line.split("\t")
                if len(parts) >= 2:
                    apps.append(AppInfo(name=parts[0], version=parts[1], source="dpkg"))
            return apps
    except FileNotFoundError:
        pass

    # Try rpm
    try:
        result = subprocess.run(
            ["rpm", "-qa", "--queryformat", "%{NAME}\t%{VERSION}\n"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                parts = line.split("\t")
                if len(parts) >= 2:
                    apps.append(AppInfo(name=parts[0], version=parts[1], source="rpm"))
            return apps
    except FileNotFoundError:
        pass

    # Try pacman
    try:
        result = subprocess.run(
            ["pacman", "-Q"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                parts = line.split()
                if len(parts) >= 2:
                    apps.append(AppInfo(name=parts[0], version=parts[1], source="pacman"))
            return apps
    except FileNotFoundError:
        pass

    return apps
