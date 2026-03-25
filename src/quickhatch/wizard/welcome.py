"""Step 0: Welcome screen and migration type selection."""

import questionary

from quickhatch.config import UserProfile
from quickhatch.console import console, print_banner, print_info, print_step


def run(profile: UserProfile) -> None:
    print_banner()
    print_step(0, "Welcome")

    console.print(
        "Welcome to [bold cyan]QuickHatch[/bold cyan]! "
        "This wizard will help you migrate from Windows to Linux.\n"
        "We'll scan your system, find Linux alternatives for your apps,\n"
        "recommend a distro, and set up an AI agent to finish the job.\n"
    )

    migration_type = questionary.select(
        "What are you doing?",
        choices=[
            questionary.Choice("Fresh install - I'm new to Linux", value="fresh_install"),
            questionary.Choice(
                "Switching from an existing Linux setup", value="switching"
            ),
        ],
    ).ask()

    if migration_type is None:
        raise KeyboardInterrupt

    profile.preferences.migration_type = migration_type

    if migration_type == "fresh_install":
        print_info("Great! We'll guide you through everything step by step.")
    else:
        print_info(
            "We'll help you migrate your setup. "
            "We'll still scan Windows to know what you need."
        )

    profile.completed_steps.append("welcome")
