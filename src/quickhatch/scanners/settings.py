"""Scan system settings (theme, defaults, locale)."""

from __future__ import annotations

import locale
import subprocess
import sys

from quickhatch.console import print_info, print_warning


def _scan_linux_settings(settings: dict) -> dict:
    """Scan Linux desktop settings."""
    # Theme (GNOME/GTK)
    try:
        result = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        scheme = (result.stdout or "").strip().strip("'")
        if "dark" in scheme:
            settings["theme"] = "dark"
        elif "light" in scheme or "default" in scheme:
            settings["theme"] = "light"
        else:
            # Fallback: check gtk-theme name
            result2 = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            theme_name = (result2.stdout or "").strip().strip("'").lower()
            settings["theme"] = "dark" if "dark" in theme_name else "light"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        settings["theme"] = "unknown"

    # Default browser
    try:
        result = subprocess.run(
            ["xdg-settings", "get", "default-web-browser"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        browser_desktop = (result.stdout or "").strip()
        browser_map = {
            "google-chrome": "Google Chrome",
            "firefox": "Mozilla Firefox",
            "chromium": "Chromium",
            "brave-browser": "Brave Browser",
            "microsoft-edge": "Microsoft Edge",
            "opera": "Opera",
            "vivaldi": "Vivaldi",
        }
        browser = browser_desktop.replace(".desktop", "")
        for key, name in browser_map.items():
            if key in browser.lower():
                settings["default_browser"] = name
                break
        else:
            settings["default_browser"] = browser or "Unknown"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        settings["default_browser"] = "Unknown"

    # Keyboard layout
    try:
        result = subprocess.run(
            ["localectl", "status"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in (result.stdout or "").split("\n"):
            if "X11 Layout" in line:
                layouts = line.split(":", 1)[1].strip()
                settings["keyboard_layouts"] = [l.strip() for l in layouts.split(",") if l.strip()]
                break
        if "keyboard_layouts" not in settings:
            settings["keyboard_layouts"] = ["us"]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        settings["keyboard_layouts"] = ["us"]

    return settings


def scan() -> dict:
    """Scan relevant system settings."""
    print_info("Scanning system settings...")
    settings: dict = {}

    # System locale
    try:
        loc, enc = locale.getlocale()
        settings["locale"] = loc or "en_US"
        settings["encoding"] = enc or "UTF-8"
    except (ValueError, AttributeError):
        settings["locale"] = "en_US"
        settings["encoding"] = "UTF-8"

    if sys.platform == "linux":
        _scan_linux_settings(settings)
        print_info(f"Theme: {settings.get('theme', 'unknown')}")
        print_info(f"Default browser: {settings.get('default_browser', 'unknown')}")
        print_info(f"Locale: {settings.get('locale', 'unknown')}")
        return settings

    if sys.platform != "win32":
        print_warning("Not running on Windows or Linux - settings scan limited.")
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
