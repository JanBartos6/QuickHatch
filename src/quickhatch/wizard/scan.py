"""Step 3: System scanning with permission prompts."""

import questionary
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from quickhatch.config import UserProfile
from quickhatch.console import console, print_step, print_success, print_warning, print_info
from quickhatch.scanners import apps, hardware, settings, files


def run(profile: UserProfile) -> None:
    """Run the system scan step."""
    print_step(3, "System Scan")

    console.print(
        "We'd like to scan your system to understand what you use.\n"
        "This helps us plan your Linux setup and find alternatives\n"
        "for your Windows applications.\n"
    )

    # Permission prompts
    scan_apps = questionary.confirm(
        "Scan installed applications? (registry, winget, Microsoft Store)",
        default=True,
    ).ask()

    scan_hw = questionary.confirm(
        "Scan hardware? (GPU, WiFi, Bluetooth, CPU, RAM, disks)",
        default=True,
    ).ask()

    scan_settings = questionary.confirm(
        "Scan Windows settings? (theme, default apps, locale)",
        default=True,
    ).ask()

    scan_files = questionary.confirm(
        "Scan user files? (Documents, bookmarks, configs - no file contents read)",
        default=True,
    ).ask()

    console.print()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        if scan_hw:
            task = progress.add_task("Scanning hardware...", total=None)
            profile.hardware = hardware.scan()
            progress.update(task, description="[success]Hardware scan complete[/success]")
            progress.remove_task(task)

        if scan_apps:
            task = progress.add_task("Scanning installed applications...", total=None)
            profile.apps = apps.scan()
            progress.update(task, description="[success]App scan complete[/success]")
            progress.remove_task(task)

        if scan_settings:
            task = progress.add_task("Scanning Windows settings...", total=None)
            profile.windows_settings = settings.scan()
            progress.update(task, description="[success]Settings scan complete[/success]")
            progress.remove_task(task)

        if scan_files:
            task = progress.add_task("Scanning user files...", total=None)
            profile.files_summary = files.scan()
            progress.update(task, description="[success]File scan complete[/success]")
            progress.remove_task(task)

    # Display summary
    console.print()
    _display_summary(profile, scan_apps, scan_hw, scan_settings, scan_files)

    profile.completed_steps.append("scan")


def _display_summary(profile: UserProfile, scan_apps: bool, scan_hw: bool,
                     scan_settings: bool, scan_files: bool):
    """Display a summary of scan results."""
    if scan_hw:
        hw = profile.hardware
        table = Table(title="Hardware Summary", border_style="cyan")
        table.add_column("Component", style="bold")
        table.add_column("Details")
        table.add_row("CPU", f"{hw.cpu} ({hw.cpu_cores} cores)")
        table.add_row("RAM", f"{hw.ram_gb:.1f} GB")
        for gpu in hw.gpus:
            table.add_row("GPU", f"{gpu.name} ({gpu.vendor})")
        if hw.wifi_adapters:
            table.add_row("WiFi", ", ".join(hw.wifi_adapters))
        table.add_row("Bluetooth", "Yes" if hw.bluetooth else "No")
        table.add_row("Boot Mode", "UEFI" if hw.efi else "Legacy BIOS")
        for disk in hw.disks:
            table.add_row("Disk", f"{disk.device} - {disk.total_gb:.0f} GB ({disk.free_gb:.0f} GB free)")
        console.print(table)
        console.print()

    if scan_apps:
        print_info(f"Found {len(profile.apps)} installed applications")

    if scan_settings:
        theme = profile.windows_settings.get("theme", "unknown")
        browser = profile.windows_settings.get("default_browser", "unknown")
        print_info(f"Windows theme: {theme}, Default browser: {browser}")

    if scan_files:
        total_files = profile.files_summary.get("total_files", 0)
        total_size = profile.files_summary.get("total_size_gb", 0)
        print_info(f"User files: {total_files} files, {total_size:.1f} GB total")

    print_success("System scan complete!")
