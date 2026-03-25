"""Main CLI orchestrator for the QuickHatch migration wizard."""

from __future__ import annotations

import sys

from quickhatch.config import UserProfile
from quickhatch.console import console, print_error, print_info, print_success, print_warning


WIZARD_STEPS = [
    ("welcome", "Welcome"),
    ("ai_setup", "AI Assistant Setup"),
    ("questionnaire", "Preferences"),
    ("scan", "System Scan"),
    ("additional", "Additional Information"),
    ("bridge", "AI Agent Bridge"),
    ("usb", "Bootable USB Creation"),
    ("ssh", "SSH Key Setup"),
    ("export", "Export Migration Bundle"),
]


def main() -> None:
    """Run the QuickHatch migration wizard."""
    # Check for checkpoint / resume
    profile = UserProfile.load_checkpoint()
    if profile and profile.completed_steps:
        print_info(
            f"Found a previous session with {len(profile.completed_steps)} "
            f"completed steps: {', '.join(profile.completed_steps)}"
        )
        try:
            import questionary

            resume = questionary.confirm(
                "Would you like to resume where you left off?", default=True
            ).ask()
            if resume is None:
                raise KeyboardInterrupt
            if not resume:
                profile = UserProfile()
        except KeyboardInterrupt:
            console.print("\n[dim]Goodbye![/dim]")
            sys.exit(0)
    else:
        profile = UserProfile()

    # Import wizard steps
    from quickhatch.wizard import (
        additional,
        ai_setup,
        bridge,
        export,
        questionnaire,
        scan,
        ssh,
        usb,
        welcome,
    )

    step_modules = {
        "welcome": welcome,
        "ai_setup": ai_setup,
        "questionnaire": questionnaire,
        "scan": scan,
        "additional": additional,
        "bridge": bridge,
        "usb": usb,
        "ssh": ssh,
        "export": export,
    }

    try:
        for step_name, step_title in WIZARD_STEPS:
            if step_name in profile.completed_steps:
                print_info(f"Skipping completed step: {step_title}")
                continue

            module = step_modules[step_name]
            module.run(profile)

            # Save checkpoint after each step
            profile.save_checkpoint()

        # Final message
        console.print("\n")
        console.print(
            "[bold green]"
            "============================================================\n"
            "  QuickHatch migration preparation complete!\n"
            "============================================================"
            "[/bold green]\n"
        )
        console.print(
            "Your migration bundle is ready. Here's what to do next:\n\n"
            "  1. Boot from the USB drive you prepared\n"
            "  2. Install Linux following the distro's installer\n"
            "  3. After installation, copy the migration bundle\n"
            "  4. Run [cyan]bash quickstart.sh[/cyan] to set up the AI agent\n"
            "  5. Feed [cyan]MIGRATION_INSTRUCTIONS.md[/cyan] to your AI agent\n"
            "  6. The agent will handle the rest: drivers, apps, theming, and more!\n\n"
            "[dim]Thank you for using QuickHatch![/dim]\n"
        )

    except KeyboardInterrupt:
        console.print("\n\n[warning]Interrupted![/warning] Your progress has been saved.")
        profile.save_checkpoint()
        print_info("Run quickhatch again to resume from where you left off.")
        sys.exit(1)
    except Exception as e:
        print_error(f"An error occurred: {e}")
        profile.save_checkpoint()
        print_info("Your progress has been saved. Run quickhatch again to resume.")
        raise


if __name__ == "__main__":
    main()
