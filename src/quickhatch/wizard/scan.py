"""Step 3: System scan with permission prompt and progress display."""

import questionary
from rich.progress import Progress, SpinnerColumn, TextColumn

from quickhatch.config import UserProfile
from quickhatch.console import console, print_info, print_step, print_success, print_warning


def run(profile: UserProfile) -> None:
    print_step(3, "System Scan")

    console.print(
        "We'd like to scan your Windows system to understand:\n"
        "  - Installed applications (to find Linux alternatives)\n"
        "  - Hardware (GPU, WiFi, Bluetooth for driver setup)\n"
        "  - System settings (theme, locale, browser)\n"
        "  - User files (to plan file transfer)\n"
    )

    permissions = questionary.checkbox(
        "Which areas do you give us permission to scan?",
        choices=[
            questionary.Choice("Installed applications", value="apps", checked=True),
            questionary.Choice("Hardware information", value="hardware", checked=True),
            questionary.Choice("Windows settings (theme, locale)", value="settings", checked=True),
            questionary.Choice("User files (Documents, Desktop, etc.)", value="files", checked=True),
        ],
    ).ask()

    if permissions is None:
        raise KeyboardInterrupt

    if not permissions:
        print_warning("No scan permissions granted. Skipping system scan.")
        profile.completed_steps.append("scan")
        return

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        if "apps" in permissions:
            task = progress.add_task("Scanning installed applications...", total=None)
            from quickhatch.scanners.apps import scan as scan_apps

            profile.apps = scan_apps()
            progress.update(task, completed=True, description="[green]Apps scanned")

        if "hardware" in permissions:
            task = progress.add_task("Scanning hardware...", total=None)
            from quickhatch.scanners.hardware import scan as scan_hw

            profile.hardware = scan_hw()
            progress.update(task, completed=True, description="[green]Hardware scanned")

        if "settings" in permissions:
            task = progress.add_task("Scanning Windows settings...", total=None)
            from quickhatch.scanners.settings import scan as scan_settings

            profile.windows_settings = scan_settings()
            progress.update(task, completed=True, description="[green]Settings scanned")

        if "files" in permissions:
            task = progress.add_task("Scanning user files...", total=None)
            from quickhatch.scanners.files import scan as scan_files

            profile.files_summary = scan_files()
            progress.update(task, completed=True, description="[green]Files scanned")

    print_success("System scan complete!")
    profile.completed_steps.append("scan")
