"""Map Windows applications to Linux alternatives."""

from __future__ import annotations

import json
from pathlib import Path
from quickhatch.config import AppInfo


# Built-in mapping database
APP_ALTERNATIVES: dict[str, dict] = {
    # Browsers
    "google chrome": {"alt": "Google Chrome / Chromium", "compat": "native", "notes": "Available natively on Linux"},
    "mozilla firefox": {"alt": "Firefox", "compat": "native", "notes": "Pre-installed on many distros"},
    "firefox": {"alt": "Firefox", "compat": "native", "notes": "Pre-installed on many distros"},
    "brave": {"alt": "Brave", "compat": "native", "notes": "Available as .deb/.rpm"},
    "microsoft edge": {"alt": "Microsoft Edge / Firefox / Brave", "compat": "alternative", "notes": "Edge available on Linux, or use Firefox/Brave"},
    "opera": {"alt": "Opera", "compat": "native", "notes": "Available natively on Linux"},
    "vivaldi": {"alt": "Vivaldi", "compat": "native", "notes": "Available natively on Linux"},

    # Office
    "microsoft office": {"alt": "LibreOffice / OnlyOffice", "compat": "alternative", "notes": "Full office suite, good .docx compatibility"},
    "microsoft word": {"alt": "LibreOffice Writer", "compat": "alternative", "notes": "Good .docx compatibility"},
    "microsoft excel": {"alt": "LibreOffice Calc", "compat": "alternative", "notes": "Good .xlsx compatibility"},
    "microsoft powerpoint": {"alt": "LibreOffice Impress", "compat": "alternative", "notes": "Good .pptx compatibility"},
    "microsoft outlook": {"alt": "Thunderbird / Evolution", "compat": "alternative", "notes": "Full email client with calendar"},
    "microsoft onenote": {"alt": "Joplin / Obsidian", "compat": "alternative", "notes": "Markdown-based note taking"},
    "microsoft teams": {"alt": "Microsoft Teams (web) / Element", "compat": "native", "notes": "Teams works in browser, or use Linux client"},

    # Development
    "visual studio code": {"alt": "VS Code", "compat": "native", "notes": "Fully supported on Linux"},
    "visual studio": {"alt": "VS Code / JetBrains IDEs", "compat": "alternative", "notes": "Full Visual Studio not available; use VS Code or JetBrains"},
    "git": {"alt": "Git", "compat": "native", "notes": "Native Linux tool"},
    "nodejs": {"alt": "Node.js", "compat": "native", "notes": "Available via package manager or nvm"},
    "python": {"alt": "Python", "compat": "native", "notes": "Usually pre-installed on Linux"},
    "docker desktop": {"alt": "Docker Engine", "compat": "native", "notes": "Docker is native on Linux, no Desktop needed"},
    "wsl": {"alt": "Native Linux", "compat": "native", "notes": "You're switching to Linux - WSL not needed!"},
    "windows terminal": {"alt": "Kitty / Alacritty / GNOME Terminal", "compat": "alternative", "notes": "Many excellent terminal emulators"},
    "powershell": {"alt": "Bash / Zsh / Fish", "compat": "alternative", "notes": "PowerShell available on Linux, but bash/zsh preferred"},
    "notepad++": {"alt": "Kate / gedit / micro", "compat": "alternative", "notes": "Many good text editors available"},
    "sublime text": {"alt": "Sublime Text", "compat": "native", "notes": "Available natively on Linux"},
    "jetbrains": {"alt": "JetBrains IDEs", "compat": "native", "notes": "All JetBrains IDEs support Linux"},
    "postman": {"alt": "Postman / Insomnia", "compat": "native", "notes": "Available as snap or flatpak"},

    # Media
    "vlc": {"alt": "VLC", "compat": "native", "notes": "Available on all distros"},
    "spotify": {"alt": "Spotify", "compat": "native", "notes": "Available as snap or flatpak"},
    "itunes": {"alt": "Rhythmbox / Strawberry", "compat": "alternative", "notes": "Music management alternatives"},
    "adobe photoshop": {"alt": "GIMP / Krita", "compat": "alternative", "notes": "GIMP for photo editing, Krita for digital painting"},
    "adobe illustrator": {"alt": "Inkscape", "compat": "alternative", "notes": "Full vector graphics editor"},
    "adobe premiere": {"alt": "DaVinci Resolve / Kdenlive", "compat": "alternative", "notes": "DaVinci Resolve is professional-grade, free"},
    "adobe after effects": {"alt": "Natron / Blender", "compat": "alternative", "notes": "Natron for compositing, Blender for motion graphics"},
    "adobe acrobat": {"alt": "Okular / Evince", "compat": "alternative", "notes": "PDF viewers built into most DEs"},
    "obs studio": {"alt": "OBS Studio", "compat": "native", "notes": "Fully supported on Linux"},
    "audacity": {"alt": "Audacity", "compat": "native", "notes": "Available on all distros"},
    "blender": {"alt": "Blender", "compat": "native", "notes": "Fully supported on Linux"},
    "davinci resolve": {"alt": "DaVinci Resolve", "compat": "native", "notes": "Available on Linux"},
    "handbrake": {"alt": "HandBrake", "compat": "native", "notes": "Available on all distros"},

    # Communication
    "discord": {"alt": "Discord", "compat": "native", "notes": "Available as .deb or flatpak"},
    "slack": {"alt": "Slack", "compat": "native", "notes": "Available as snap or flatpak"},
    "zoom": {"alt": "Zoom", "compat": "native", "notes": "Available as .deb/.rpm"},
    "telegram": {"alt": "Telegram", "compat": "native", "notes": "Available as snap or flatpak"},
    "signal": {"alt": "Signal", "compat": "native", "notes": "Available on Linux"},
    "whatsapp": {"alt": "WhatsApp Web / ZapZap", "compat": "alternative", "notes": "Use web version or third-party client"},
    "skype": {"alt": "Skype", "compat": "native", "notes": "Available as snap"},

    # Gaming
    "steam": {"alt": "Steam", "compat": "native", "notes": "Native Linux client with Proton for Windows games"},
    "epic games": {"alt": "Heroic Games Launcher", "compat": "alternative", "notes": "Open-source Epic/GOG launcher"},
    "gog galaxy": {"alt": "Heroic Games Launcher / Lutris", "compat": "alternative", "notes": "Use Heroic or Lutris"},
    "battle.net": {"alt": "Lutris", "compat": "wine", "notes": "Runs via Lutris/Wine"},
    "nvidia geforce experience": {"alt": "NVIDIA X Server Settings", "compat": "alternative", "notes": "Use nvidia-settings and MangoHud"},

    # Utilities
    "7-zip": {"alt": "p7zip / File Roller", "compat": "native", "notes": "7z support built into most file managers"},
    "winrar": {"alt": "File Roller / Ark", "compat": "alternative", "notes": "Archive managers built into DEs"},
    "ccleaner": {"alt": "BleachBit", "compat": "alternative", "notes": "System cleaner for Linux"},
    "everything": {"alt": "fsearch / locate", "compat": "alternative", "notes": "Fast file search tools"},
    "rufus": {"alt": "Ventoy / dd / GNOME Disks", "compat": "alternative", "notes": "Multiple USB creation tools"},
    "putty": {"alt": "Native SSH (built-in)", "compat": "native", "notes": "SSH is built into Linux terminal"},
    "winscp": {"alt": "FileZilla / Nautilus (SFTP)", "compat": "alternative", "notes": "File managers support SFTP natively"},
    "bitwarden": {"alt": "Bitwarden", "compat": "native", "notes": "Available as snap or flatpak"},
    "keepass": {"alt": "KeePassXC", "compat": "native", "notes": "Available on all distros"},

    # System
    "task manager": {"alt": "htop / System Monitor", "compat": "alternative", "notes": "htop is a powerful terminal-based monitor"},
    "file explorer": {"alt": "Nautilus / Dolphin / Thunar", "compat": "alternative", "notes": "Depends on desktop environment"},

    # Music production
    "fl studio": {"alt": "LMMS / Ardour / Bitwig", "compat": "alternative", "notes": "LMMS is free, Bitwig is commercial and excellent"},
    "ableton live": {"alt": "Bitwig Studio / Ardour", "compat": "alternative", "notes": "Bitwig closest to Ableton on Linux"},
    "reaper": {"alt": "REAPER", "compat": "native", "notes": "REAPER has native Linux support"},
}


def analyze_apps(apps: list[AppInfo]) -> None:
    """Analyze a list of apps and annotate with Linux compatibility info."""
    # Load external database if available
    ext_db = _load_external_db()

    for app in apps:
        name_lower = app.name.lower().strip()

        # Check built-in database
        match = _find_match(name_lower, APP_ALTERNATIVES)
        if not match:
            match = _find_match(name_lower, ext_db)

        if match:
            app.linux_alternative = match["alt"]
            app.compatibility = match["compat"]
            app.compatibility_notes = match.get("notes", "")
        else:
            # Heuristics
            app.compatibility, app.linux_alternative, app.compatibility_notes = _heuristic_check(name_lower, app.name)


def _find_match(name_lower: str, db: dict) -> dict | None:
    """Find a matching entry in the database."""
    # Exact match
    if name_lower in db:
        return db[name_lower]

    # Partial match
    for key, value in db.items():
        if key in name_lower or name_lower in key:
            return value

    return None


def _heuristic_check(name_lower: str, original_name: str) -> tuple[str, str | None, str]:
    """Apply heuristics to guess compatibility."""
    # Electron apps generally work on Linux
    electron_indicators = ["electron", "nw.js"]
    if any(ind in name_lower for ind in electron_indicators):
        return "native", original_name, "Electron app - likely available on Linux"

    # Common cross-platform frameworks
    cross_platform = ["qt", "gtk", "java", "mono", "dotnet"]
    if any(cp in name_lower for cp in cross_platform):
        return "native", original_name, "Cross-platform framework detected"

    # Microsoft-specific apps
    ms_indicators = ["microsoft", "windows", "xbox", "cortana", "onedrive"]
    if any(ms in name_lower for ms in ms_indicators):
        return "none", None, "Microsoft-specific application"

    # Driver/hardware specific
    driver_indicators = ["driver", "chipset", "realtek", "intel(r)", "nvidia"]
    if any(d in name_lower for d in driver_indicators):
        return "none", None, "Hardware driver - Linux has its own drivers"

    # Runtime/framework
    runtime_indicators = ["visual c++", "redistributable", ".net framework", "directx"]
    if any(r in name_lower for r in runtime_indicators):
        return "none", None, "Windows runtime - not needed on Linux"

    return "unknown", None, ""


def _load_external_db() -> dict:
    """Load external app alternatives database if available."""
    db_path = Path(__file__).parent.parent / "data" / "app_alternatives.json"
    if db_path.exists():
        try:
            return json.loads(db_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}
