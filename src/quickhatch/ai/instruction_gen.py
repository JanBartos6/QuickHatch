"""Generate LLM instruction files for the migration agent.

Instead of hardcoded analysis, this module dumps all collected raw data
into a structured prompt so the LLM can reason about distro choice,
app alternatives, driver setup, desktop config, etc.
"""

from __future__ import annotations

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

    is_customize = prefs.migration_type == "customize_linux"

    # ── System prompt / role ──
    if is_customize:
        sections.append(
            "# QuickHatch — Linux Customization Instructions for AI Agent\n\n"
            "You are an AI agent running **locally on the user's Linux machine**.\n"
            "The user wants you to customize and configure their existing Linux system.\n"
            "You can execute commands directly — no SSH needed.\n\n"
            "Below is everything we know about their current system, preferences,\n"
            "and needs. Your job is to:\n\n"
            "1. **Analyze the current system** — check what's installed, what DE/desktop is running,\n"
            "   what hardware is present, and what could be improved.\n"
            "2. **Configure the desktop** — set up theming, icons, wallpaper, dock/panel,\n"
            "   and any system-specific settings to match their stated preferences.\n"
            "3. **Install requested software** — based on their use cases, install and configure\n"
            "   the right tools and packages.\n"
            "4. **Set up drivers/hardware** — if GPU, WiFi, or other hardware needs attention,\n"
            "   handle it.\n"
            "5. **System tuning** — optimize performance, configure locale/keyboard, set up\n"
            "   development tools, etc. based on their use cases.\n"
            "6. **Execute everything directly** — run the commands, verify they work, and\n"
            "   report back what was done.\n\n"
            "Use your own knowledge and reasoning. The data below is raw — no pre-analysis\n"
            "has been done. You decide what's best.\n\n"
            "---\n"
        )
    else:
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
    type_desc = {
        "fresh_install": "This is a fresh install — the user is new to Linux. Be thorough with explanations.",
        "customize_linux": "The user already has Linux installed and wants it customized. They are on the system right now.",
    }
    sections.append(
        "## Mode\n\n"
        f"{prefs.migration_type}\n\n"
        f"{type_desc.get(prefs.migration_type, 'Unknown mode.')}\n"
    )

    # ── User Preferences (raw) ──
    sections.append("## User Preferences\n")
    if prefs.desktop_look:
        sections.append("- **How they want the desktop to look** (free-text, user's own words):\n")
        sections.append(f"  > {prefs.desktop_look}\n")
        sections.append(
            "  Match this description against the curated design library. "
            "Pick the closest recipe by id and personalize within it, or blend "
            "components from a couple of recipes if no single one matches.\n"
        )
    if prefs.gallery_picks:
        sections.append(f"- **Pinned gallery recipes:** {', '.join(prefs.gallery_picks)}\n")
        sections.append(
            "  Recipe picks are authoritative references. If a recipe is pinned, personalize within it "
            "instead of inventing a different desktop from scratch.\n"
        )
    # Legacy fields — only surface if populated (older checkpoints may have them)
    if prefs.icon_style:
        sections.append(f"- **Icon style:** {prefs.icon_style}\n")
    if prefs.wallpaper_style:
        sections.append(f"- **Wallpaper preference:** {prefs.wallpaper_style}\n")
    sections.append(
        f"- **Primary use cases:** {', '.join(prefs.use_cases) if prefs.use_cases else 'Not specified'}\n"
    )
    if is_customize:
        pass  # No distro recommendation needed — they're already on one
    elif prefs.distro_preference:
        sections.append(
            f"- **Distro preference:** {prefs.distro_preference} (user's stated preference — evaluate if it's a good fit)\n"
        )
    else:
        sections.append("- **Distro preference:** None — recommend one based on the data below\n")
    if prefs.additional_notes:
        sections.append(f"- **Additional notes from questionnaire:** {prefs.additional_notes}\n")
    sections.append("")

    # ── Hardware (raw) ──
    hw_label = "scanned from this system" if is_customize else "scanned from source system"
    sections.append(f"## Hardware ({hw_label})\n")
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

    # ── Installed Applications (raw list) ──
    if is_customize:
        sections.append("## Installed Linux Packages\n\n")
        if profile.apps:
            sections.append(
                "These packages are currently installed on the system. Use this to understand\n"
                "what's already set up and avoid reinstalling things.\n\n"
            )
    else:
        sections.append("## Installed Windows Applications\n\n")
        if profile.apps:
            sections.append(
                "These apps were found on the user's Windows system. For each one,\n"
                "determine: does it run natively on Linux? Is there a good alternative?\n"
                "Can it run via Wine/Proton? Or does it have no Linux path?\n\n"
            )
    if profile.apps:
        for app in profile.apps:
            version = f" (v{app.version})" if app.version else ""
            sections.append(f"- {app.name}{version} [source: {app.source}]\n")
    else:
        sections.append("No applications were detected (scan may have been skipped).\n")
    sections.append("")

    # ── System Settings (raw) ──
    if profile.windows_settings:
        settings_label = "Current Linux Settings" if is_customize else "Windows Settings (scanned)"
        sections.append(f"## {settings_label}\n")
        for key, value in profile.windows_settings.items():
            sections.append(f"- **{key}:** {value}\n")
        if is_customize:
            sections.append(
                "\nThese are the current settings. Modify them to match the user's preferences.\n"
            )
        else:
            sections.append(
                "\nUse these to match the user's current experience "
                "(e.g., if they use dark mode, set dark mode on Linux; "
                "match their keyboard layout and locale).\n"
            )
        sections.append("")

    # ── User Files Summary (raw) ──
    if profile.files_summary:
        sections.append("## User Files Summary\n")
        sections.append(
            f"- **Total files scanned:** {profile.files_summary.get('total_files', 0)}\n"
        )
        sections.append(f"- **Total size:** {profile.files_summary.get('total_size_mb', 0)} MB\n")
        top_types = profile.files_summary.get("top_types", {})
        if top_types:
            sections.append(
                "- **Top file types:** "
                + ", ".join(f"{ext} ({count})" for ext, count in list(top_types.items())[:10])
                + "\n"
            )
        browsers = profile.files_summary.get("browser_profiles", {})
        if browsers:
            sections.append("- **Browser profiles found:** " + ", ".join(browsers.keys()) + "\n")
            sections.append("  (Plan to migrate bookmarks, extensions, and saved passwords)\n")
        scanned = profile.files_summary.get("scanned_dirs", [])
        if scanned:
            sections.append("- **Scanned directories:** " + ", ".join(scanned) + "\n")
        sections.append("")

    # ── User-Selected Apps to Install ──
    if profile.apps_to_install:
        sections.append("## Apps the User Wants on Linux\n\n")
        sections.append(
            "The user explicitly selected these apps to have on Linux.\n"
            "**You MUST install all of them** — either the native Linux version,\n"
            "a direct equivalent, or via Wine/Proton. Do not skip any.\n\n"
        )
        for app_name in profile.apps_to_install:
            sections.append(f"- {app_name}\n")
        sections.append("")

    # ── Data Migration Description ──
    transfer_method_desc = {
        "external_drive": (
            "The user will copy files to a USB / external drive before booting Linux.\n"
            "Plan: tell them which directories to copy from Windows, which Linux directories "
            "to put them in, and what post-copy fixups are needed (permissions, config edits)."
        ),
        "cloud": (
            "The user's files are already in a cloud service (Dropbox, OneDrive, iCloud, Git, etc.).\n"
            "Plan: install the appropriate Linux client or CLI, authenticate, sync. For Git "
            "repos: clone them. For OneDrive on Linux: rclone or the `onedrive` CLI."
        ),
        "keep_windows": (
            "The user will keep Windows accessible (dual-boot, network share, or a separate "
            "machine).\nPlan: give them `scp`/`rsync` commands to run from Linux to pull "
            "files. If dual-booting, mention mounting the Windows NTFS partition read-only "
            "and copying from there."
        ),
        "nothing": ("The user has nothing to migrate. Skip any data-migration steps in the plan."),
    }
    if profile.data_description or profile.data_transfer_method:
        transfer_note = transfer_method_desc.get(profile.data_transfer_method, "")
        sections.append("## Data the User Wants to Migrate\n\n")
        if profile.data_transfer_method and transfer_note:
            sections.append(
                f"**Chosen transfer method:** `{profile.data_transfer_method}`\n\n{transfer_note}\n\n"
            )
        if profile.data_description:
            sections.append(f"### What matters to them\n\n{profile.data_description}\n\n")
        sections.append(
            "**IMPORTANT:** QuickHatch does NOT automatically copy Windows files. "
            "You have SSH access to the Linux target only. Your job:\n"
            "1. Give clear step-by-step instructions tailored to the user's chosen transfer method.\n"
            "2. On the Linux side, set up destination directories, import tools, or config placeholders.\n"
            "3. Verify after the user confirms the transfer is done.\n"
            "Never claim to have copied files you can't actually reach.\n"
        )

    # ── Linux Connection Details ──
    if profile.linux_ip and profile.linux_user:
        sections.append(
            "## Linux Machine Connection\n\n"
            f"- **IP:** {profile.linux_ip}\n"
            f"- **Username:** {profile.linux_user}\n\n"
        )

    # ── Additional User Notes ──
    if profile.additional_info:
        sections.append(
            "## Additional Information from User\n\n"
            f"{profile.additional_info}\n\n"
            "Take this into account when making all decisions.\n"
        )

    if prefs.additional_notes:
        sections.append(f"## Notes from Questionnaire\n\n{prefs.additional_notes}\n")

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
            '  The user types a free-text response. You can optionally add `"choices": [...]`\n'
            "  but prefer open-ended questions — let the user speak freely.\n"
            '  Returns `{"answer": "user\'s response"}`\n\n'
            '- `POST /api/ask/async` — Ask without blocking, returns `{"question_id": "abc"}`\n'
            "  Then poll `GET /api/response/abc` for the answer.\n\n"
            "**Messages & completion:**\n"
            '- `POST /api/message` — Display a message: `{"content": "Installing drivers...", "role": "agent"}`\n'
            '- `POST /api/done` — Signal completion: `{"summary": "All done!"}`\n\n'
            "### Workflow\n\n"
            "1. `GET /api/profile` to read the user's system data\n"
            "2. Analyze it, then `POST /api/suggest` your distro recommendation\n"
            "3. `POST /api/ask` to confirm the distro choice with the user\n"
            "4. `POST /api/suggest` your app compatibility analysis\n"
            "5. `POST /api/suggest` driver/desktop plans\n"
            "6. `POST /api/ask` for any clarifications\n"
            "7. `POST /api/done` when finished\n\n"
        )

    # ── Follow-up questions ──
    sections.append(
        "## Before You Start — Ask Follow-Up Questions\n\n"
        "Before making recommendations or executing changes, ask the user 3-5\n"
        "targeted follow-up questions based on the scan results above. Examples:\n\n"
        '- If they have gaming apps/Steam: "Which specific games do you play?\n'
        '  Some need Proton/Wine configuration."\n'
        '- If they use creative tools (Photoshop, etc.): "Are you open to\n'
        '  alternatives like GIMP/Krita, or do you need exact feature parity?"\n'
        '- If they have a discrete GPU: "Will you use your GPU primarily for\n'
        '  gaming, ML/CUDA workloads, or general desktop?"\n'
        '- If their use cases include development: "What languages/frameworks\n'
        '  do you work with? Any specific IDE preference?"\n'
        '- If migrating: "Do you want to dual-boot or fully replace Windows?"\n\n'
        "Use `POST /api/ask` to get answers before proceeding with your plan.\n"
        "This helps you make better, more specific recommendations.\n\n"
    )

    # ── What to do ──
    if is_customize:
        sections.append(
            "---\n\n"
            "## Your Task\n\n"
            "You are running **locally on this Linux machine**. Execute commands directly.\n"
            "Based on ALL of the above data:\n\n"
            "1. **Analyze the current setup** — check what DE, theme, and packages are already\n"
            "   installed. Identify what needs to change to match the user's preferences.\n\n"
            "2. **Install software** — based on their use cases, install the right tools:\n"
            "   development tools, creative apps, gaming (Steam, Proton), browsers, etc.\n"
            "   Use the system's package manager (apt, dnf, pacman, etc.) and flatpak/snap\n"
            "   where appropriate.\n\n"
            "3. **Configure the desktop** — apply their preferred style:\n"
            "   - Install and set the right DE/WM if needed\n"
            "   - Apply GTK/Qt themes, icon packs, cursor themes\n"
            "   - Set wallpaper, configure dock/panel layout\n"
            "   - Set up keyboard shortcuts, font rendering, etc.\n\n"
            "4. **Set up drivers** — check GPU, WiFi, Bluetooth. Install proprietary\n"
            "   drivers or firmware if needed and not already present.\n\n"
            "5. **System tuning** — locale, keyboard layout, timezone, font rendering,\n"
            "   swap/zram, any performance tweaks relevant to their use cases.\n\n"
            "6. **Verify** — confirm changes took effect. Check that display, audio,\n"
            "   network, and GPU acceleration are all working.\n\n"
            "Use the bridge API to send progress updates and ask the user questions.\n"
            "Execute everything directly — you have local access.\n"
        )
    else:
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
            + (
                "   If `preferences.gallery_picks` is non-empty, assume the first pinned recipe will be "
                "applied. Your job is to personalize within that recipe (wallpaper, accent color, app "
                "integration) without conflicting with its DE/theme/icon choices.\n\n"
                if prefs.gallery_picks
                else ""
            )
            + (
                "### Phase 2: Remote Setup (via SSH from Windows → Linux)\n\n"
                "Once the user has Linux installed and SSH ready (they run `quickstart.sh`):\n\n"
            )
            + "5. **SSH into the Linux machine** using the IP/username provided.\n\n"
            "6. **Install EVERY app** the user selected. Do not skip any:\n"
            "   - Use the distro's package manager for native apps\n"
            "   - Use Flatpak/Snap for apps not in the repo\n"
            "   - Use Wine/Proton for Windows-only apps (install Wine, configure prefix)\n"
            "   - Verify each app launches correctly after installation\n\n"
            "7. **Migrate ALL user data:**\n"
            "   - Use `scp` to copy files from Windows to Linux\n"
            "   - Browser bookmarks: export from Windows browser, import on Linux\n"
            "   - Documents, game saves, configs — as described by the user\n"
            "   - Verify files are intact after transfer\n\n"
            "8. **Configure the desktop** — install and set up the DE/WM, theme, icons,\n"
            "   wallpaper, dock/panel, keyboard shortcuts, font rendering.\n\n"
            "9. **Install drivers** — GPU (proprietary if NVIDIA), WiFi firmware,\n"
            "   Bluetooth, printer drivers. Test each one.\n\n"
            "10. **System setup** — locale, keyboard layout, timezone, swap/zram,\n"
            "    firewall, automatic updates, any distro-specific configuration.\n\n"
            "### Phase 3: Verification (CRITICAL)\n\n"
            "**Do NOT call /api/done until ALL of the following are confirmed:**\n\n"
            "11. **Test every installed app** — launch each one, confirm it opens.\n"
            "12. **Verify data** — check migrated files exist and are accessible.\n"
            "13. **Test hardware** — WiFi connects, Bluetooth pairs, GPU acceleration\n"
            "    works (`glxinfo | grep renderer`), audio plays.\n"
            "14. **Test desktop** — theme applied, icons correct, wallpaper set.\n"
            "15. **Ask the user** via the bridge API: \"I've finished setting up.\n"
            "    Everything looks good on my end. Can you check on the Linux machine\n"
            '    and let me know if anything is missing or needs adjustment?"\n\n'
            "**If anything fails, fix it before moving on.** If you can't fix it,\n"
            "ask the user for help via `/api/ask`. Do not give up.\n\n"
            "Send status updates via `/api/message` as you work so the user can\n"
            "see progress in real time.\n"
        )

    return "\n".join(sections)


def save_instructions(profile: UserProfile, output_dir: Path) -> Path:
    """Generate and save instruction files."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # Main instruction file — write as UTF-8 explicitly. Windows' default
    # cp1252 encoding crashes on the em-dashes and arrows we use
    # throughout the instructions.
    instructions = generate_instructions(profile)
    inst_path = output_dir / "MIGRATION_INSTRUCTIONS.md"
    inst_path.write_text(instructions, encoding="utf-8")

    return inst_path
