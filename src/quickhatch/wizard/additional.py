"""Step 5: Collect any additional information the user wants to convey."""

import questionary

from quickhatch.config import UserProfile
from quickhatch.console import console, print_step, print_success


def run(profile: UserProfile) -> None:
    print_step(5, "Additional Information")

    console.print(
        "The AI agent will analyze everything we've collected —\n"
        "your apps, hardware, preferences — and figure out the best\n"
        "distro, app alternatives, driver setup, and desktop config.\n\n"
        "Before we hand it off, is there anything else you want the\n"
        "AI to know?\n"
    )

    additional = questionary.text(
        "Any additional context, requirements, or preferences?\n"
        "(e.g., 'I need dual-boot with Windows', 'I want Wayland',\n"
        " 'I have a 4K monitor', 'encrypt my drive', etc.)\n"
        "Press Enter to skip:",
        default="",
        multiline=True,
    ).ask()

    if additional is None:
        raise KeyboardInterrupt

    if additional.strip():
        profile.additional_info = additional.strip()

    print_success("Got it! All information collected.")
    profile.completed_steps.append("additional")
