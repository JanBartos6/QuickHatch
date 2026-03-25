"""Step 0: Welcome screen and migration type selection."""

import questionary
from quickhatch.config import UserProfile
from quickhatch.console import console, print_banner, print_step, print_info


def run(profile: UserProfile) -> None:
    """Run the welcome step."""
    print_banner()
    print_step(0, "Welcome")

    console.print(
        "This wizard will help you migrate from Windows to Linux.\n"
        "We'll gather information about your system, preferences,\n"
        "and set up everything you need for a smooth transition.\n"
    )

    migration_type = questionary.select(
        "What are you doing?",
        choices=[
            questionary.Choice(
                "Fresh install - I'm on Windows and want to switch to Linux",
                value="fresh_install",
            ),
            questionary.Choice(
                "Switching distros - I'm already on Linux and want to change",
                value="switching",
            ),
        ],
        style=questionary.Style([
            ("selected", "fg:cyan bold"),
            ("pointer", "fg:cyan bold"),
            ("highlighted", "fg:cyan"),
            ("question", "fg:white bold"),
        ]),
    ).ask()

    if migration_type is None:
        raise KeyboardInterrupt

    profile.preferences.migration_type = migration_type

    if migration_type == "fresh_install":
        print_info("Great! We'll scan your Windows system to help plan your Linux setup.")
    else:
        print_info("Got it! We'll help you switch to a new distro while preserving your setup.")

    profile.completed_steps.append("welcome")
