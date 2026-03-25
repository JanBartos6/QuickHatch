"""Scan Windows settings (theme, defaults, locale)."""

from __future__ import annotations

import locale
import subprocess
import sys


def scan() -> dict:
    """Scan relevant Windows settings."""
    settings = {}

    settings["locale"] = locale.getdefaultlocale()[0] or "unknown"
    settings["encoding"] = locale.getdefaultlocale()[1] or "utf-8"

    if sys.platform == "win32":
        settings["theme"] = _get_windows_theme()
        settings["default_browser"] = _get_default_browser()
        settings["keyboard_layout"] = _get_keyboard_layout()
        settings["timezone"] = _get_timezone()
    else:
        settings["theme"] = _get_linux_theme()
        settings["default_browser"] = _get_linux_browser()
        settings["timezone"] = _get_timezone()

    return settings


def _get_windows_theme() -> str:
    """Detect Windows light/dark theme."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        value = winreg.QueryValueEx(key, "AppsUseLightTheme")[0]
        winreg.CloseKey(key)
        return "light" if value == 1 else "dark"
    except Exception:
        return "unknown"


def _get_default_browser() -> str:
    """Detect default browser on Windows."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice",
        )
        prog_id = winreg.QueryValueEx(key, "ProgId")[0]
        winreg.CloseKey(key)

        browser_map = {
            "ChromeHTML": "Chrome",
            "FirefoxURL": "Firefox",
            "MSEdgeHTM": "Edge",
            "BraveHTML": "Brave",
            "OperaStable": "Opera",
            "VivaldiHTM": "Vivaldi",
        }
        for key_str, name in browser_map.items():
            if key_str in prog_id:
                return name
        return prog_id
    except Exception:
        return "unknown"


def _get_keyboard_layout() -> str:
    """Get keyboard layout on Windows."""
    try:
        result = subprocess.run(
            ["powershell", "-Command", "Get-WinUserLanguageList | Select-Object -ExpandProperty InputMethodTips"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() or "unknown"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"


def _get_timezone() -> str:
    """Get system timezone."""
    try:
        import time
        return time.tzname[0]
    except Exception:
        return "unknown"


def _get_linux_theme() -> str:
    """Detect Linux desktop theme."""
    try:
        result = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True, text=True, timeout=5,
        )
        if "dark" in result.stdout.lower():
            return "dark"
        elif "light" in result.stdout.lower() or "default" in result.stdout.lower():
            return "light"
    except FileNotFoundError:
        pass
    return "unknown"


def _get_linux_browser() -> str:
    """Detect default browser on Linux."""
    try:
        result = subprocess.run(
            ["xdg-settings", "get", "default-web-browser"],
            capture_output=True, text=True, timeout=5,
        )
        browser = result.stdout.strip()
        if browser:
            return browser.replace(".desktop", "")
    except FileNotFoundError:
        pass
    return "unknown"
