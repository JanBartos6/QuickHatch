"""Step 2: User preferences questionnaire."""

import questionary

from quickhatch.config import UserProfile
from quickhatch.console import console, print_info, print_step, print_success


def run(profile: UserProfile) -> None:
    print_step(2, "Your Preferences")

    console.print("Let's learn about what you want from your Linux system.\n")

    # Desktop look preference
    desktop_look = questionary.select(
        "What desktop style do you prefer?",
        choices=[
            questionary.Choice(
                "Windows-like (taskbar, start menu, familiar layout)",
                value="windows_like",
            ),
            questionary.Choice("macOS-like (dock, top bar, clean aesthetic)", value="macos_like"),
            questionary.Choice("Minimal (clean, distraction-free)", value="minimal"),
            questionary.Choice(
                "Tiling window manager (keyboard-driven, for power users)",
                value="tiling",
            ),
            questionary.Choice("I'll decide later / custom", value="custom"),
        ],
    ).ask()
    if desktop_look is None:
        raise KeyboardInterrupt
    profile.preferences.desktop_look = desktop_look

    # Icon style
    icon_style = questionary.select(
        "What icon style do you like?",
        choices=[
            questionary.Choice("Flat & modern (Papirus-style)", value="flat"),
            questionary.Choice("Rounded & friendly (Tela-style)", value="rounded"),
            questionary.Choice("Classic / traditional", value="classic"),
            questionary.Choice("Colorful & vibrant", value="colorful"),
            questionary.Choice("No preference", value="default"),
        ],
    ).ask()
    if icon_style is None:
        raise KeyboardInterrupt
    profile.preferences.icon_style = icon_style

    # Wallpaper style
    wallpaper_style = questionary.select(
        "What kind of wallpapers do you prefer?",
        choices=[
            questionary.Choice("Nature & landscapes", value="nature"),
            questionary.Choice("Abstract & geometric", value="abstract"),
            questionary.Choice("Dark & moody", value="dark"),
            questionary.Choice("Minimal & clean", value="minimal"),
            questionary.Choice("I'll set my own", value="custom"),
        ],
    ).ask()
    if wallpaper_style is None:
        raise KeyboardInterrupt
    profile.preferences.wallpaper_style = wallpaper_style

    # Use cases
    use_cases = questionary.checkbox(
        "What will you primarily use this computer for? (select all that apply)",
        choices=[
            questionary.Choice("Software development / programming", value="development"),
            questionary.Choice("Gaming", value="gaming"),
            questionary.Choice("Office work (documents, spreadsheets)", value="office"),
            questionary.Choice("Creative work (photo/video/audio editing)", value="creative"),
            questionary.Choice("Web browsing & media consumption", value="browsing"),
            questionary.Choice("System administration / DevOps", value="sysadmin"),
            questionary.Choice("Data science / machine learning", value="datascience"),
            questionary.Choice("School / university", value="education"),
            questionary.Choice("Casual / everyday use", value="casual"),
        ],
    ).ask()
    if use_cases is None:
        raise KeyboardInterrupt
    profile.preferences.use_cases = use_cases

    # Distro preference
    knows_distro = questionary.confirm("Do you already have a Linux distro in mind?").ask()
    if knows_distro is None:
        raise KeyboardInterrupt

    if knows_distro:
        distro = questionary.select(
            "Which distro?",
            choices=[
                "Ubuntu",
                "Linux Mint",
                "Fedora",
                "Pop!_OS",
                "Manjaro",
                "Arch Linux",
                "Debian",
                "openSUSE",
                "Zorin OS",
                "elementary OS",
                questionary.Choice("Other (type it in)", value="other"),
            ],
        ).ask()
        if distro is None:
            raise KeyboardInterrupt
        if distro == "other":
            distro = questionary.text("Enter the distro name:").ask()
            if distro is None:
                raise KeyboardInterrupt
        profile.preferences.distro_preference = distro
        print_success(f"Noted: you prefer {distro}")
    else:
        print_info("No worries! We'll recommend one based on your needs and hardware.")

    # Additional notes
    additional = questionary.text(
        "Anything else you want the AI to know about your setup? (press Enter to skip)",
        default="",
    ).ask()
    if additional is None:
        raise KeyboardInterrupt
    profile.preferences.additional_notes = additional

    print_success("Preferences saved!")
    profile.completed_steps.append("questionnaire")
