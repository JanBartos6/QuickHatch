"""Main CLI orchestrator for QuickHatch migration wizard."""

from __future__ import annotations

import sys

import questionary

from quickhatch.config import UserProfile
from quickhatch.console import console, print_banner, print_error, print_info, print_success, print_warning


WIZARD_STEPS = [
    ("welcome", "Welcome"),
    ("ai_setup", "AI Assistant Setup"),
    ("questionnaire", "Your Preferences"),
    ("scan", "System Scan"),
    ("compatibility", "App Compatibility"),
    ("distro", "Distro Recommendation"),
    ("usb", "Bootable USB"),
    ("ssh", "SSH Setup"),
    ("export", "Export Bundle"),
]


def main():
    """Main entry point for QuickHatch."""
    try:
        _run_wizard()
    except KeyboardInterrupt:
        console.print("\n\n[warning]Migration wizard cancelled.[/warning]")
        console.print("Your progress has been saved. Run quickhatch again to resume.")
        sys.exit(1)
    except Exception as e:
        print_error(f"An unexpected error occurred: {e}")
        console.print("[dim]Please report this issue at: https://github.com/janbartos6/quickhatch/issues[/dim]")
        sys.exit(1)


def _run_wizard():
    """Run the migration wizard."""
    # Check for existing checkpoint
    profile = UserProfile.load_checkpoint()

    if profile and profile.completed_steps:
        console.print()
        print_info(f"Found a previous session with {len(profile.completed_steps)} steps completed.")
        resume = questionary.confirm(
            "Resume from where you left off?",
            default=True,
        ).ask()

        if not resume:
            profile = UserProfile()
            print_info("Starting fresh.")
    else:
        profile = UserProfile()

    # Import wizard modules
    from quickhatch.wizard import welcome, ai_setup, questionnaire, scan, compatibility, distro, usb, ssh, export

    step_modules = {
        "welcome": welcome,
        "ai_setup": ai_setup,
        "questionnaire": questionnaire,
        "scan": scan,
        "compatibility": compatibility,
        "distro": distro,
        "usb": usb,
        "ssh": ssh,
        "export": export,
    }

    # Run each step
    for step_key, step_name in WIZARD_STEPS:
        if step_key in profile.completed_steps:
            print_info(f"Skipping completed step: {step_name}")
            continue

        module = step_modules[step_key]
        module.run(profile)

        # Save checkpoint after each step
        profile.save_checkpoint()

    # Final summary
    _print_final_summary(profile)


def _print_final_summary(profile: UserProfile):
    """Print the final migration summary."""
    from rich.panel import Panel
    from rich.table import Table

    console.print()
    console.print(Panel(
        "[bold green]Migration preparation complete![/bold green]",
        border_style="green",
        width=60,
    ))

    table = Table(border_style="green")
    table.add_column("Setting", style="bold")
    table.add_column("Value")

    table.add_row("Migration type", profile.preferences.migration_type.replace("_", " ").title())
    table.add_row("AI assistant", profile.ai_config.provider)
    table.add_row("Target distro", profile.recommended_distro or "Not selected")
    table.add_row("Desktop environment", profile.recommended_de or "Default")
    table.add_row("Apps scanned", str(len(profile.apps)))
    table.add_row("SSH key", profile.ssh_public_key_path or "Not configured")
    table.add_row("USB device", profile.usb_device or "Not created")

    console.print(table)

    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print("  1. Boot from the USB drive to install Linux")
    console.print("  2. Copy the migration bundle to the new system")
    console.print("  3. Run [cyan]./quickstart.sh[/cyan] to set up the AI agent")
    console.print("  4. Let the AI agent configure your system using INSTRUCTIONS.md")
    console.print()
    console.print("[dim]Happy migrating! Welcome to Linux![/dim]")
    console.print()


if __name__ == "__main__":
    main()
