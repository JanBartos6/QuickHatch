"""Generate the exported AI reference brief for QuickHatch bundles.

This file is intentionally NOT the live analysis prompt used by the web
wizard. The real interactive analysis/setup flow lives in
``quickhatch.gui.server`` and now uses sectioned, structured contracts.

The export bundle still benefits from a human-readable AI brief, but it
should describe the current product behavior rather than preserve an older
"one giant narrative prompt" model.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from quickhatch.config import UserProfile


def _redacted_profile_dict(profile: UserProfile) -> dict[str, Any]:
    data = asdict(profile)
    if data.get("ai_config", {}).get("api_key"):
        data["ai_config"]["api_key"] = "***REDACTED***"
    return data


def _compact_profile_json(profile: UserProfile) -> str:
    """Smaller, export-friendly profile snapshot.

    Keep the fields that matter for an external agent or manual review,
    but don't dump every bit of scan data into a second drift-prone mega-prompt.
    """
    data = _redacted_profile_dict(profile)
    prefs = data.get("preferences", {})
    hardware = data.get("hardware", {})
    apps = data.get("apps", [])
    files_summary = data.get("files_summary", {})

    trimmed = {
        "preferences": prefs,
        "ai_config": data.get("ai_config", {}),
        "hardware": {
            "cpu": hardware.get("cpu", ""),
            "cpu_cores": hardware.get("cpu_cores", 0),
            "ram_gb": hardware.get("ram_gb", 0),
            "gpus": hardware.get("gpus", []),
            "wifi_adapters": hardware.get("wifi_adapters", []),
            "bluetooth": hardware.get("bluetooth", False),
            "disks": hardware.get("disks", []),
            "efi": hardware.get("efi", False),
        },
        "apps": apps[:80],
        "apps_to_install": data.get("apps_to_install", []),
        "windows_settings": data.get("windows_settings", {}),
        "files_summary": {
            "total_files": files_summary.get("total_files", 0),
            "total_size_mb": files_summary.get("total_size_mb", 0),
            "top_types": files_summary.get("top_types", {}),
            "browser_profiles": files_summary.get("browser_profiles", {}),
        },
        "data_transfer_method": data.get("data_transfer_method", ""),
        "data_transfer_paths": data.get("data_transfer_paths", []),
        "data_description": data.get("data_description", ""),
        "linux_ip": data.get("linux_ip", ""),
        "linux_user": data.get("linux_user", ""),
        "target_hardware_override": data.get("target_hardware_override", ""),
        "additional_info": data.get("additional_info", ""),
    }
    return json.dumps(trimmed, indent=2, default=str)


def _mode_section(profile: UserProfile) -> str:
    prefs = profile.preferences
    if prefs.migration_type == "customize_linux":
        return (
            "## Mode\n\n"
            "Customization mode. You are acting on an already-installed Linux system.\n"
            "Operate locally and verify changes directly on that machine.\n"
        )
    return (
        "## Mode\n\n"
        "Migration mode. QuickHatch runs from Windows, then connects to the Linux target over SSH.\n"
        "Do not assume local execution on Linux until SSH access is ready.\n"
    )


def _operating_rules(profile: UserProfile) -> str:
    prefs = profile.preferences
    rules = [
        "## Operating Rules",
        "",
        "1. Explicit user choices override stylistic preference. If the user named a distro, use that distro unless it is not a real Linux distribution.",
        "2. Latest user feedback wins over earlier assumptions.",
        "3. Describe and verify the full end-state, not just package installation.",
        "4. If `gallery_picks` is non-empty, those recipes are authoritative design references. Personalize within them instead of inventing a different desktop direction.",
        "5. When `data_transfer_method` is `keep_windows`, QuickHatch can copy selected `data_transfer_paths` from the host to the installed target after setup. For external-drive/cloud methods, only claim Linux-side preparation and instructions.",
        "6. If `target_hardware_override` is non-empty, treat it as authoritative over scanned hardware for driver and install decisions.",
        "7. Remote setup commands run on the Linux target. They cannot read Windows-local paths unless Windows exposes them over SSH/SMB, the disk is mounted, or the user moved the files first.",
    ]
    if prefs.migration_type == "customize_linux":
        rules.append("8. In customization mode, no distro recommendation is needed.")
    else:
        rules.append(
            "8. In migration mode, plan around SSH access from Windows to the Linux target."
        )
    return "\n".join(rules) + "\n"


def _workflow_section(profile: UserProfile) -> str:
    prefs = profile.preferences
    if prefs.migration_type == "customize_linux":
        return (
            "## Expected Workflow\n\n"
            "1. Inspect the current Linux setup directly.\n"
            "2. Align the desktop with the user's look/preferences and any pinned gallery recipes.\n"
            "3. Install requested software and configure it.\n"
            "4. Handle drivers, locale, keyboard, fonts, and other system tuning.\n"
            "5. Verify the resulting desktop, apps, and hardware behavior.\n"
        )

    lines = [
        "## Expected Workflow",
        "",
        "1. Review the profile and clarify any missing requirements.",
        "2. Recommend or confirm the distro choice.",
        "3. Map requested/scanned Windows apps to Linux-native packages, alternatives, or Wine/Proton paths.",
        "4. Plan drivers, desktop setup, data migration, and post-install verification.",
        "5. Once SSH is ready, connect to the Linux target and apply the plan.",
        "6. Verify apps, desktop state, and hardware before declaring completion.",
    ]
    if profile.preferences.gallery_picks:
        lines.append(
            "7. Apply the desktop direction around the pinned gallery recipe(s), not around a fresh invention."
        )
    return "\n".join(lines) + "\n"


def _bridge_section(profile: UserProfile) -> str:
    ai = profile.ai_config
    endpoint = ai.endpoint or ""
    if "localhost" not in endpoint:
        return ""
    return (
        "## QuickHatch Bridge API\n\n"
        f"Base URL: `{endpoint}`\n\n"
        "This local API is how the interactive agent reports progress and asks follow-up questions.\n"
        "The current live analysis flow is sectioned (`distro`, `apps`, `drivers`, `desktop`, `plan`, `summary`, and sometimes `install_script`) rather than one giant response.\n\n"
        "Core endpoints:\n"
        "- `GET /api/profile` — current profile snapshot\n"
        "- `GET /api/suggestions` — accumulated section outputs\n"
        "- `POST /api/suggest` — add a section output or progress card\n"
        "- `POST /api/ask` — ask the user a blocking question\n"
        "- `POST /api/message` — post a status message\n"
        "- `POST /api/done` — signal completion\n"
    )


def _gallery_section(profile: UserProfile) -> str:
    picks = profile.preferences.gallery_picks
    if not picks:
        return (
            "## Desktop Design Inputs\n\n"
            "No gallery recipe is pinned. Use the user's desktop preferences as guidance, but keep the design grounded and reproducible.\n"
        )
    joined = ", ".join(f"`{item}`" for item in picks)
    return (
        "## Desktop Design Inputs\n\n"
        f"Pinned gallery recipe IDs: {joined}\n\n"
        "Treat these as the authoritative desktop references for theme / icon / font / wallpaper direction.\n"
    )


def _ssh_section(profile: UserProfile) -> str:
    if not profile.ssh_public_key_path:
        return (
            "## SSH Access\n\n"
            "No SSH key path is stored yet. If you need remote execution, establish SSH access first.\n"
        )
    priv_key = str(Path(profile.ssh_public_key_path).with_suffix(""))
    return (
        "## SSH Access\n\n"
        f"- Private key on the Windows host: `{priv_key}`\n"
        f"- Public key path: `{profile.ssh_public_key_path}`\n"
        "- The target Linux machine must have SSH enabled and the public key installed before remote setup can proceed.\n"
    )


def generate_instructions(profile: UserProfile) -> str:
    """Generate the exported AI reference brief.

    This document is bundled for human review or external-agent use. It is
    intentionally aligned with the current QuickHatch flow instead of acting as
    a second, divergent runtime prompt.
    """
    prefs = profile.preferences
    title = (
        "# QuickHatch Reference Brief — Linux Customization\n\n"
        if prefs.migration_type == "customize_linux"
        else "# QuickHatch Reference Brief — Windows to Linux Migration\n\n"
    )

    intro = (
        "This file is a reference brief exported by QuickHatch. It summarizes the user's profile,\n"
        "the current product workflow, and the rules an external agent or human operator should follow.\n"
        "It is not the authoritative live analysis prompt used by the web wizard; the live wizard now uses\n"
        "sectioned prompts with structured outputs.\n\n"
    )

    sections = [
        title + intro,
        _mode_section(profile),
        _operating_rules(profile),
        _workflow_section(profile),
        _gallery_section(profile),
        _ssh_section(profile),
        _bridge_section(profile),
        "## Profile Snapshot\n\n```json\n" + _compact_profile_json(profile) + "\n```\n",
    ]

    return "\n".join(part for part in sections if part).strip() + "\n"


def save_instructions(profile: UserProfile, output_dir: Path) -> Path:
    """Generate and save the exported reference brief."""
    output_dir.mkdir(parents=True, exist_ok=True)
    instructions = generate_instructions(profile)
    inst_path = output_dir / "MIGRATION_INSTRUCTIONS.md"
    inst_path.write_text(instructions, encoding="utf-8")
    return inst_path
