"""Distro recommendation engine."""

from __future__ import annotations

import json
from pathlib import Path

from quickhatch.config import HardwareInfo, Preferences, UserProfile
from quickhatch.console import print_info


def _load_distros() -> dict:
    db_path = Path(__file__).parent.parent / "data" / "distros.json"
    if db_path.exists():
        return json.loads(db_path.read_text())
    return {}


def recommend(profile: UserProfile) -> list[tuple[str, float, str]]:
    """Score and rank distros based on user profile.

    Returns list of (distro_name, score, reason) sorted by score descending.
    """
    distros = _load_distros()
    prefs = profile.preferences
    hw = profile.hardware
    scores: list[tuple[str, float, str]] = []

    for name, info in distros.items():
        score = 0.0
        reasons = []
        tags = info.get("tags", [])

        # Match use cases to tags
        for use_case in prefs.use_cases:
            if use_case in tags:
                score += 15
                reasons.append(f"good for {use_case}")

        # Desktop look preference
        if prefs.desktop_look == "windows_like" and "windows-like" in tags:
            score += 20
            reasons.append("Windows-like interface")
        elif prefs.desktop_look == "macos_like" and "macos-like" in tags:
            score += 20
            reasons.append("macOS-like interface")
        elif prefs.desktop_look == "tiling" and "tiling" in tags:
            score += 20
            reasons.append("tiling WM support")
        elif prefs.desktop_look == "minimal" and "minimal" in tags:
            score += 15
            reasons.append("minimal design")

        # Fresh install bonus for beginner-friendly
        if prefs.migration_type == "fresh_install" and "beginner-friendly" in tags:
            score += 15
            reasons.append("beginner-friendly")

        # Hardware considerations
        if hw.gpus:
            for gpu in hw.gpus:
                if gpu.vendor == "nvidia":
                    nvidia_support = info.get("nvidia_support", "moderate")
                    if nvidia_support == "excellent":
                        score += 15
                        reasons.append("excellent NVIDIA support")
                    elif nvidia_support == "good":
                        score += 8

        # Low RAM -> prefer lightweight
        if hw.ram_gb and hw.ram_gb < 4 and "lightweight" in tags:
            score += 10
            reasons.append("lightweight for low RAM")

        # Gaming
        if "gaming" in prefs.use_cases:
            gaming = info.get("gaming_support", "moderate")
            if gaming == "excellent":
                score += 10
                reasons.append("excellent gaming support")

        reason_str = "; ".join(reasons[:4]) if reasons else "general-purpose distro"
        scores.append((name, score, reason_str))

    scores.sort(key=lambda x: x[1], reverse=True)
    return scores
