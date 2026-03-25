"""Scan Windows settings (theme, defaults, locale)."""

from __future__ import annotations

import locale
import subprocess
import sys

from quickhatch.console import print_info, print_warning


def scan() -> dict:
    """Scan relevant Windows settings."""
    print_info("Scanning Windows settings...")
    settings: dict = {}

    # System locale
    try:
        loc, enc = locale.getlocale()
        settings["locale"] = loc or "en_US"
        settings["encoding"] = enc or "UTF-8"
    except (ValueError, AttributeError):
        settings["locale"] = "en_US"
        settings["encoding"] = "UTF-8"

    if sys.platform != "win32":
        print_warning("Not running on Windows - settings scan limited.")
        return settings

    # Dark/light theme
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        try:
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            settings["theme"] = "light" if val == 1 else "dark"
        except OSError:
            settings["theme"] = "unknown"
        finally:
            winreg.CloseKey(key)
    except (ImportError, OSError):
        settings["theme"] = "unknown"

    # Default browser
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "(Get-ItemProperty "
                    "'HKCU:\\Software\\Microsoft\\Windows\\Shell\\Associations"
                    "\\UrlAssociations\\http\\UserChoice').ProgId"
                ),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        browser_id = (result.stdout or "").strip()
        browser_map = {
            "ChromeHTML": "Google Chrome",
            "MSEdgeHTM": "Microsoft Edge",
            "FirefoxURL": "Mozilla Firefox",
            "BraveHTML": "Brave Browser",
            "OperaStable": "Opera",
        }
        settings["default_browser"] = browser_map.get(
            browser_id.split("-")[0] if "-" in browser_id else browser_id,
            browser_id or "Unknown",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        settings["default_browser"] = "Unknown"

    # Keyboard layout
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-WinUserLanguageList | Select-Object -ExpandProperty LanguageTag",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        langs = [l.strip() for l in (result.stdout or "").strip().split("\n") if l.strip()]
        settings["keyboard_layouts"] = langs if langs else ["en-US"]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        settings["keyboard_layouts"] = ["en-US"]

    print_info(f"Theme: {settings.get('theme', 'unknown')}")
    print_info(f"Default browser: {settings.get('default_browser', 'unknown')}")
    print_info(f"Locale: {settings.get('locale', 'unknown')}")

    return settings
