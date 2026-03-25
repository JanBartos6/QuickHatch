"""Distribution recommendation engine."""

from __future__ import annotations

import json
from pathlib import Path
from quickhatch.config import UserProfile


# Built-in distro database
DISTROS = {
    "ubuntu": {
        "name": "Ubuntu",
        "de": "GNOME",
        "tags": ["beginner", "stable", "office", "development", "web_media", "education"],
        "look": ["macos_like"],
        "min_ram_gb": 4,
        "description": "Most popular, huge community, great hardware support",
        "gaming_ready": True,
        "nvidia_easy": True,
    },
    "linuxmint": {
        "name": "Linux Mint",
        "de": "Cinnamon",
        "tags": ["beginner", "stable", "office", "casual", "web_media", "education"],
        "look": ["windows_like"],
        "min_ram_gb": 2,
        "description": "Most Windows-like, very beginner friendly",
        "gaming_ready": True,
        "nvidia_easy": True,
    },
    "fedora": {
        "name": "Fedora",
        "de": "GNOME",
        "tags": ["intermediate", "development", "sysadmin", "cutting_edge"],
        "look": ["macos_like"],
        "min_ram_gb": 4,
        "description": "Cutting edge, great for developers, backed by Red Hat",
        "gaming_ready": True,
        "nvidia_easy": False,
    },
    "popos": {
        "name": "Pop!_OS",
        "de": "COSMIC/GNOME",
        "tags": ["beginner", "gaming", "development", "data_science", "creative"],
        "look": ["macos_like"],
        "min_ram_gb": 4,
        "description": "Great for gaming and development, excellent NVIDIA support",
        "gaming_ready": True,
        "nvidia_easy": True,
    },
    "arch": {
        "name": "Arch Linux",
        "de": "Choose your own",
        "tags": ["advanced", "development", "sysadmin", "tiling", "custom"],
        "look": ["minimal", "tiling", "custom"],
        "min_ram_gb": 1,
        "description": "Rolling release, fully customizable, learn everything",
        "gaming_ready": True,
        "nvidia_easy": False,
    },
    "manjaro": {
        "name": "Manjaro",
        "de": "KDE Plasma",
        "tags": ["intermediate", "gaming", "development", "casual"],
        "look": ["windows_like", "custom"],
        "min_ram_gb": 2,
        "description": "Arch-based but easier, good hardware detection",
        "gaming_ready": True,
        "nvidia_easy": True,
    },
    "debian": {
        "name": "Debian",
        "de": "GNOME",
        "tags": ["intermediate", "stable", "sysadmin", "development"],
        "look": ["macos_like"],
        "min_ram_gb": 2,
        "description": "Rock-solid stability, server and desktop",
        "gaming_ready": False,
        "nvidia_easy": False,
    },
    "opensuse": {
        "name": "openSUSE Tumbleweed",
        "de": "KDE Plasma",
        "tags": ["intermediate", "development", "sysadmin"],
        "look": ["windows_like"],
        "min_ram_gb": 4,
        "description": "Great package management (YaST), rolling release",
        "gaming_ready": True,
        "nvidia_easy": True,
    },
    "zorin": {
        "name": "Zorin OS",
        "de": "GNOME (customized)",
        "tags": ["beginner", "office", "casual", "web_media"],
        "look": ["windows_like", "macos_like"],
        "min_ram_gb": 2,
        "description": "Designed for Windows/Mac switchers, beautiful UI",
        "gaming_ready": True,
        "nvidia_easy": True,
    },
    "elementary": {
        "name": "elementary OS",
        "de": "Pantheon",
        "tags": ["beginner", "casual", "creative", "web_media"],
        "look": ["macos_like"],
        "min_ram_gb": 4,
        "description": "Most macOS-like experience, curated app store",
        "gaming_ready": False,
        "nvidia_easy": True,
    },
    "nobara": {
        "name": "Nobara",
        "de": "KDE Plasma",
        "tags": ["intermediate", "gaming", "creative", "music_production"],
        "look": ["windows_like"],
        "min_ram_gb": 4,
        "description": "Gaming-focused Fedora spin, pre-configured for gaming",
        "gaming_ready": True,
        "nvidia_easy": True,
    },
    "endeavouros": {
        "name": "EndeavourOS",
        "de": "KDE Plasma",
        "tags": ["intermediate", "development", "custom"],
        "look": ["custom", "windows_like"],
        "min_ram_gb": 2,
        "description": "Arch made easier, great community",
        "gaming_ready": True,
        "nvidia_easy": True,
    },
    "ubuntustudio": {
        "name": "Ubuntu Studio",
        "de": "KDE Plasma",
        "tags": ["creative", "music_production", "intermediate"],
        "look": ["windows_like"],
        "min_ram_gb": 4,
        "description": "Pre-configured for audio/video/graphics production",
        "gaming_ready": False,
        "nvidia_easy": True,
    },
}


def recommend_distros(profile: UserProfile) -> list[tuple[str, int, str, str]]:
    """
    Score and rank distros based on user profile.

    Returns list of (distro_name, score, desktop_env, reason).
    """
    scores: list[tuple[str, int, str, str]] = []

    for key, distro in DISTROS.items():
        score = 0
        reasons = []

        # Use case matching (biggest weight)
        use_cases = profile.preferences.use_cases
        matching_cases = set(use_cases) & set(distro["tags"])
        score += len(matching_cases) * 15
        if matching_cases:
            reasons.append(f"Matches: {', '.join(matching_cases)}")

        # Gaming bonus
        if "gaming" in use_cases and distro["gaming_ready"]:
            score += 15
            reasons.append("Gaming ready")
        elif "gaming" in use_cases and not distro["gaming_ready"]:
            score -= 10

        # Desktop look preference
        if profile.preferences.desktop_look in distro["look"]:
            score += 20
            reasons.append(f"Fits {profile.preferences.desktop_look} look")

        # NVIDIA GPU handling
        has_nvidia = any(g.vendor == "nvidia" for g in profile.hardware.gpus)
        if has_nvidia and distro["nvidia_easy"]:
            score += 10
            reasons.append("Easy NVIDIA setup")
        elif has_nvidia and not distro["nvidia_easy"]:
            score -= 5

        # RAM considerations
        if profile.hardware.ram_gb > 0:
            if profile.hardware.ram_gb < 4 and distro["min_ram_gb"] <= 2:
                score += 10
                reasons.append("Lightweight, fits your RAM")
            elif profile.hardware.ram_gb < 4 and distro["min_ram_gb"] > 4:
                score -= 15
                reasons.append("May need more RAM")

        # Beginner friendliness (if switching from Windows)
        if profile.preferences.migration_type == "fresh_install":
            if "beginner" in distro["tags"]:
                score += 10
                reasons.append("Beginner friendly")

        # User's explicit preference
        if profile.preferences.distro_preference:
            if profile.preferences.distro_preference.lower() == key:
                score += 25
                reasons.append("Your preference!")

        # Music production
        if "music_production" in use_cases:
            if "music_production" in distro["tags"]:
                score += 15
                reasons.append("Audio production ready")

        # Ensure score is bounded
        score = max(0, min(100, score))

        reason_str = "; ".join(reasons[:3]) if reasons else distro["description"]
        de = _recommend_de(profile, distro)

        scores.append((distro["name"], score, de, reason_str))

    # Sort by score descending
    scores.sort(key=lambda x: x[1], reverse=True)
    return scores


def _recommend_de(profile: UserProfile, distro: dict) -> str:
    """Recommend a desktop environment based on user preferences."""
    look = profile.preferences.desktop_look

    if look == "windows_like":
        if "KDE" in distro["de"] or "Cinnamon" in distro["de"]:
            return distro["de"]
        return "KDE Plasma"
    elif look == "macos_like":
        if "GNOME" in distro["de"] or "Pantheon" in distro["de"]:
            return distro["de"]
        return "GNOME"
    elif look == "minimal":
        return "XFCE"
    elif look == "tiling":
        return "Hyprland / i3"
    else:
        return distro["de"]
