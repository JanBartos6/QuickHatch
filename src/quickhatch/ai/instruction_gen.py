"""Generate LLM instruction files for the migration agent.

Instead of hardcoded analysis, this module dumps all collected raw data
into a structured prompt so the LLM can reason about distro choice,
app alternatives, driver setup, desktop config, etc.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from quickhatch.config import UserProfile


def generate_instructions(profile: UserProfile) -> str:
    """Generate a comprehensive instruction document for the AI agent.

    This is a prompt, not a solution — the LLM should analyze the raw data
    and make its own decisions about distro, apps, drivers, theming, etc.
    """
    prefs = profile.preferences
    hw = profile.hardware
    ai = profile.ai_config

    sections = []

    # ── System prompt / role ──
    sections.append(
        "# QuickHatch — Migration Instructions for AI Agent\n\n"
        "You are an AI agent running on the user's **Windows** machine, helping them\n"
        "migrate to Linux. The user has (or will have) a Linux machine ready — you will\n"
        "**SSH into it** from Windows to perform the setup remotely.\n\n"
        "Below is everything we know about their current system, preferences,\n"
        "and needs. Your job is to:\n\n"
        "1. **Recommend a Linux distro** based on their hardware, use cases, and preferences\n"
        "2. **Analyze their installed Windows apps** — for each one, determine if it runs\n"
        "   natively on Linux, has a good alternative, can run via Wine/Proton, or has no\n"
        "   Linux equivalent. Suggest the best path for each.\n"
        "3. **Plan driver setup** — based on their exact hardware (GPU, WiFi, Bluetooth),\n"
        "   determine what drivers/firmware need to be installed and how.\n"
        "4. **Configure the desktop** — set up the desktop environment, theming, icons,\n"
        "   and wallpaper to match their stated preferences.\n"
        "5. **Handle file migration** — plan how to transfer their documents, browser data,\n"
        "   and settings.\n"
        "6. **SSH into the Linux machine and execute the full setup** — install packages,\n"
        "   configure the system, and verify everything works, all via SSH from Windows.\n\n"
        "Use your own knowledge and reasoning. The data below is raw — no pre-analysis\n"
        "has been done. You decide what's best.\n\n"
        "---\n"
    )

    # ── Migration type ──
    sections.append(
        "## Migration Type\n\n"
        f"{prefs.migration_type}\n\n"
        f"{'This is a fresh install — the user is new to Linux. Be thorough with explanations.' if prefs.migration_type == 'fresh_install' else 'The user is switching from an existing Linux setup. They have Linux experience.'}\n"
    )

    # ── User Preferences (raw) ──
    sections.append("## User Preferences\n")
    sections.append(f"- **Desktop style:** {prefs.desktop_look}\n")
    sections.append(f"- **Icon style:** {prefs.icon_style}\n")
    sections.append(f"- **Wallpaper preference:** {prefs.wallpaper_style}\n")
    sections.append(f"- **Primary use cases:** {', '.join(prefs.use_cases) if prefs.use_cases else 'Not specified'}\n")
    if prefs.distro_preference:
        sections.append(f"- **Distro preference:** {prefs.distro_preference} (user's stated preference — evaluate if it's a good fit)\n")
    else:
        sections.append("- **Distro preference:** None — recommend one based on the data below\n")
    if prefs.additional_notes:
        sections.append(f"- **Additional notes from questionnaire:** {prefs.additional_notes}\n")
    sections.append("")

    # ── Hardware (raw) ──
    sections.append("## Hardware (scanned from Windows)\n")
    sections.append(f"- **CPU:** {hw.cpu} ({hw.cpu_cores} cores)\n")
    sections.append(f"- **RAM:** {hw.ram_gb} GB\n")
    for gpu in hw.gpus:
        vram = f" ({gpu.vram_mb} MB VRAM)" if gpu.vram_mb else ""
        driver = f", current Windows driver: {gpu.driver_version}" if gpu.driver_version else ""
        sections.append(f"- **GPU:** {gpu.name}{vram} [vendor: {gpu.vendor}]{driver}\n")
    if hw.wifi_adapters:
        sections.append(f"- **WiFi adapters:** {', '.join(hw.wifi_adapters)}\n")
    else:
        sections.append("- **WiFi adapters:** None detected (may be wired-only or scan failed)\n")
    sections.append(f"- **Bluetooth:** {'Present' if hw.bluetooth else 'Not detected'}\n")
    sections.append(f"- **Boot mode:** {'EFI/UEFI' if hw.efi else 'Legacy BIOS'}\n")
    if hw.disks:
        sections.append("- **Disks:**\n")
        for disk in hw.disks:
            sections.append(
                f"  - {disk.device} ({disk.filesystem}) — "
                f"{disk.total_gb} GB total, {disk.free_gb} GB free, "
                f"mounted at {disk.mount_point}\n"
            )
    sections.append("")

    # ── Installed Windows Applications (raw list) ──
    sections.append("## Installed Windows Applications\n\n")
    if profile.apps:
        sections.append(
            "These apps were found on the user's Windows system. For each one,\n"
            "determine: does it run natively on Linux? Is there a good alternative?\n"
            "Can it run via Wine/Proton? Or does it have no Linux path?\n\n"
        )
        for app in profile.apps:
            version = f" (v{app.version})" if app.version else ""
            sections.append(f"- {app.name}{version} [source: {app.source}]\n")
    else:
        sections.append("No applications were detected (scan may have been skipped).\n")
    sections.append("")

    # ── Windows Settings (raw) ──
    if profile.windows_settings:
        sections.append("## Windows Settings (scanned)\n")
        for key, value in profile.windows_settings.items():
            sections.append(f"- **{key}:** {value}\n")
        sections.append(
            "\nUse these to match the user's current experience "
            "(e.g., if they use dark mode, set dark mode on Linux; "
            "match their keyboard layout and locale).\n"
        )
        sections.append("")

    # ── User Files Summary (raw) ──
    if profile.files_summary:
        sections.append("## User Files Summary\n")
        sections.append(f"- **Total files scanned:** {profile.files_summary.get('total_files', 0)}\n")
        sections.append(f"- **Total size:** {profile.files_summary.get('total_size_mb', 0)} MB\n")
        top_types = profile.files_summary.get("top_types", {})
        if top_types:
            sections.append("- **Top file types:** " + ", ".join(
                f"{ext} ({count})" for ext, count in list(top_types.items())[:10]
            ) + "\n")
        browsers = profile.files_summary.get("browser_profiles", {})
        if browsers:
            sections.append("- **Browser profiles found:** " + ", ".join(browsers.keys()) + "\n")
            sections.append("  (Plan to migrate bookmarks, extensions, and saved passwords)\n")
        scanned = profile.files_summary.get("scanned_dirs", [])
        if scanned:
            sections.append("- **Scanned directories:** " + ", ".join(scanned) + "\n")
        sections.append("")

    # ── Additional User Notes ──
    if profile.additional_info:
        sections.append(
            "## Additional Information from User\n\n"
            f"{profile.additional_info}\n\n"
            "Take this into account when making all decisions.\n"
        )

    if prefs.additional_notes:
        sections.append(
            "## Notes from Questionnaire\n\n"
            f"{prefs.additional_notes}\n"
        )

    # ── SSH Access ──
    if profile.ssh_public_key_path:
        priv_key = str(Path(profile.ssh_public_key_path).with_suffix(""))
        sections.append(
            "## SSH Access (Agent → Linux)\n\n"
            "You are running on Windows. An SSH key pair was generated for you to\n"
            "connect to the user's Linux machine remotely.\n\n"
            f"- **Private key (on this Windows machine):** `{priv_key}`\n"
            f"- **Public key:** `{profile.ssh_public_key_path}`\n\n"
            "The user has a `quickstart.sh` script that they will run on the Linux\n"
            "machine to enable SSH and add the public key. Once they've done that:\n\n"
            "1. **Ask the user** for the Linux machine's IP address and username\n"
            "   (via the bridge API or directly).\n"
            f"2. **Connect via SSH:** `ssh -i {priv_key} <user>@<ip>`\n"
            "3. **Execute all setup commands** over this SSH connection.\n"
            "4. You can also use `scp` to transfer files from Windows to Linux.\n\n"
            "**Important:** You run all Linux commands via SSH from Windows. You do NOT\n"
            "need to install yourself on the Linux machine.\n"
        )
    else:
        sections.append(
            "## SSH Access\n\n"
            "No SSH key was generated. Ask the user how they want you to connect\n"
            "to their Linux machine. Options:\n"
            "- Generate an SSH key now and have them add it\n"
            "- Use password-based SSH\n"
            "- Have the user run commands you dictate\n"
        )

    # ── AI Agent Info ──
    sections.append("## AI Agent\n\n")
    sections.append(f"- **Provider:** {ai.provider}\n")
    if ai.model_name:
        sections.append(f"- **Model:** {ai.model_name}\n")
    if ai.endpoint and "localhost" in ai.endpoint:
        sections.append(f"- **Bridge API:** {ai.endpoint}\n")
    elif ai.endpoint:
        sections.append(f"- **Endpoint:** {ai.endpoint}\n")
    sections.append("")

    # ── Bridge API docs ──
    if ai.endpoint and "localhost" in ai.endpoint:
        sections.append(
            "## QuickHatch Bridge API\n\n"
            "The QuickHatch wizard is running a local API server. Use it to send your\n"
            "analysis back to the user and ask them questions interactively.\n\n"
            f"**Base URL:** `{ai.endpoint}`\n\n"
            "### Endpoints\n\n"
            "**Read data:**\n"
            f"- `GET /api/profile` — Full user profile (hardware, apps, preferences)\n"
            f"- `GET /api/status` — Bridge status, pending question count\n"
            f"- `GET /api/suggestions` — All suggestions posted so far\n"
            f"- `GET /api/response/{{id}}` — Poll for answer to an async question\n\n"
            "**Send analysis back to user (they see it in the wizard TUI):**\n"
            "- `POST /api/suggest` — Post a recommendation/analysis\n"
            "  ```json\n"
            "  {\n"
            '    "category": "distro",\n'
            '    "title": "Distro Recommendation",\n'
            '    "content": {\n'
            '      "top_pick": "Pop!_OS",\n'
            '      "reasoning": "Best NVIDIA support + gaming + beginner-friendly",\n'
            '      "recommendations": [\n'
            '        {"name": "Pop!_OS", "reason": "Excellent NVIDIA, tiling, gaming"},\n'
            '        {"name": "Fedora", "reason": "Cutting-edge, great for dev"}\n'
            "      ]\n"
            "    }\n"
            "  }\n"
            "  ```\n"
            "  Categories: `distro`, `apps`, `drivers`, `desktop`, `plan`, `message`\n\n"
            "  For `apps` category, use this content format:\n"
            "  ```json\n"
            "  {\n"
            '    "apps": [\n'
            '      {"name": "Photoshop", "linux_alternative": "GIMP", "method": "alternative", "notes": "..."},\n'
            '      {"name": "Steam", "linux_alternative": "Steam", "method": "native", "notes": "..."}\n'
            "    ]\n"
            "  }\n"
            "  ```\n\n"
            "**Ask the user questions:**\n"
            "- `POST /api/ask` — Ask and block until answered\n"
            "  ```json\n"
            '  {"question": "I recommend Fedora 41 for your setup because... What do you think?"}\n'
            "  ```\n"
            "  The user types a free-text response. You can optionally add `\"choices\": [...]`\n"
            "  but prefer open-ended questions — let the user speak freely.\n"
            "  Returns `{\"answer\": \"user's response\"}`\n\n"
            "- `POST /api/ask/async` — Ask without blocking, returns `{\"question_id\": \"abc\"}`\n"
            "  Then poll `GET /api/response/abc` for the answer.\n\n"
            "**Messages & completion:**\n"
            "- `POST /api/message` — Display a message: `{\"content\": \"Installing drivers...\", \"role\": \"agent\"}`\n"
            "- `POST /api/done` — Signal completion: `{\"summary\": \"All done!\"}`\n\n"
            "### Workflow\n\n"
            "1. `GET /api/profile` to read the user's system data\n"
            "2. Analyze it, then `POST /api/suggest` your distro recommendation\n"
            "3. `POST /api/ask` to confirm the distro choice with the user\n"
            "4. `POST /api/suggest` your app compatibility analysis\n"
            "5. `POST /api/suggest` driver/desktop plans\n"
            "6. `POST /api/ask` for any clarifications\n"
            "7. `POST /api/done` when finished\n\n"
        )

    # ── What to do ──
    sections.append(
        "---\n\n"
        "## Your Task\n\n"
        "You are running on the user's Windows machine. Based on ALL of the above data:\n\n"
        "### Phase 1: Analysis & Planning (on Windows, via bridge API)\n\n"
        "1. **Recommend a distro** (with reasoning) — or validate the user's pick if they chose one.\n"
        "   Consider their hardware, use cases, experience level, and desktop preference.\n\n"
        "2. **Create an app migration plan** — go through every installed app and classify it:\n"
        "   - Runs natively on Linux (list the package name)\n"
        "   - Has a good Linux alternative (name it and explain why)\n"
        "   - Can work via Wine/Proton (note any caveats)\n"
        "   - No Linux option (explain and suggest workarounds if any)\n\n"
        "3. **Plan driver/firmware setup** — based on the exact GPU, WiFi, and Bluetooth\n"
        "   hardware, determine what needs to be installed and provide exact commands.\n\n"
        "4. **Plan desktop configuration** — based on their style preferences.\n\n"
        "### Phase 2: Remote Setup (via SSH from Windows → Linux)\n\n"
        "Once the user has Linux installed and SSH ready (they run `quickstart.sh`):\n\n"
        "5. **Ask for the Linux machine's IP address and username** via the bridge API.\n\n"
        "6. **SSH into the Linux machine** and execute all setup:\n"
        "   - Update packages, install all needed software\n"
        "   - Install GPU/WiFi/Bluetooth drivers and firmware\n"
        "   - Configure desktop environment, icons, theme, wallpaper\n"
        "   - Set up locale, keyboard, timezone\n"
        "   - Transfer browser bookmarks/data from Windows via scp\n\n"
        "7. **Verify everything** over SSH — check that WiFi works, display is correct,\n"
        "   audio works, Bluetooth pairs, GPU is accelerated, etc.\n\n"
        "Ask the user for clarification if anything is ambiguous.\n"
    )

    return "\n".join(sections)


def save_instructions(profile: UserProfile, output_dir: Path) -> Path:
    """Generate and save instruction files."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # Main instruction file
    instructions = generate_instructions(profile)
    inst_path = output_dir / "MIGRATION_INSTRUCTIONS.md"
    inst_path.write_text(instructions)

    return inst_path
