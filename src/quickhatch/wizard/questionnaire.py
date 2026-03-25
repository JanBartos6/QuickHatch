"""Step 2: User preferences questionnaire."""

import questionary
from quickhatch.config import UserProfile
from quickhatch.console import console, print_step, print_success


def run(profile: UserProfile) -> None:
    """Run the preferences questionnaire."""
    print_step(2, "Your Preferences")

    console.print(
        "Let's learn about what you want from your Linux setup.\n"
        "This helps us recommend the right distro and desktop.\n"
    )

    # Desktop look preference
    desktop_look = questionary.select(
        "How do you want your desktop to look?",
        choices=[
            questionary.Choice("Windows-like (taskbar, start menu, familiar layout)", value="windows_like"),
            questionary.Choice("macOS-like (dock at bottom, top menu bar, clean)", value="macos_like"),
            questionary.Choice("Minimal (clean, distraction-free, keyboard-driven)", value="minimal"),
            questionary.Choice("Tiling (auto-arranged windows, power-user style)", value="tiling"),
            questionary.Choice("Custom (I'll describe what I want)", value="custom"),
        ],
    ).ask()
    if desktop_look is None:
        raise KeyboardInterrupt
    profile.preferences.desktop_look = desktop_look

    if desktop_look == "custom":
        custom_desc = questionary.text(
            "Describe your ideal desktop:",
        ).ask()
        profile.preferences.additional_notes += f"\nCustom desktop: {custom_desc}"

    # Icon style
    icon_style = questionary.select(
        "What icon style do you prefer?",
        choices=[
            questionary.Choice("Modern & flat (Papirus, Fluent-style)", value="modern_flat"),
            questionary.Choice("Classic & detailed (traditional icons)", value="classic"),
            questionary.Choice("Colorful & playful (Candy, Tela Circle)", value="colorful"),
            questionary.Choice("Monochrome & minimal (simple outlines)", value="monochrome"),
            questionary.Choice("No preference", value="any"),
        ],
    ).ask()
    if icon_style is None:
        raise KeyboardInterrupt
    profile.preferences.icon_style = icon_style

    # Wallpaper style
    wallpaper_style = questionary.select(
        "What kind of wallpaper do you like?",
        choices=[
            questionary.Choice("Dark & moody (dark themes, night scenes)", value="dark"),
            questionary.Choice("Light & clean (bright, airy)", value="light"),
            questionary.Choice("Nature (landscapes, forests, mountains)", value="nature"),
            questionary.Choice("Abstract (geometric, gradients, art)", value="abstract"),
            questionary.Choice("Minimal (solid colors, subtle patterns)", value="minimal"),
        ],
    ).ask()
    if wallpaper_style is None:
        raise KeyboardInterrupt
    profile.preferences.wallpaper_style = wallpaper_style

    # Use cases
    use_cases = questionary.checkbox(
        "What will you primarily use your computer for? (select all that apply)",
        choices=[
            questionary.Choice("Software development / Programming", value="development"),
            questionary.Choice("Gaming", value="gaming"),
            questionary.Choice("Office work (documents, spreadsheets, email)", value="office"),
            questionary.Choice("Web browsing & media consumption", value="web_media"),
            questionary.Choice("Creative work (photo/video/audio editing)", value="creative"),
            questionary.Choice("Data science / Machine learning", value="data_science"),
            questionary.Choice("System administration / DevOps", value="sysadmin"),
            questionary.Choice("Casual / everyday use", value="casual"),
            questionary.Choice("Education / Learning", value="education"),
            questionary.Choice("Music production", value="music_production"),
        ],
    ).ask()
    if use_cases is None:
        raise KeyboardInterrupt
    profile.preferences.use_cases = use_cases

    # Distro preference
    knows_distro = questionary.confirm(
        "Do you already have a Linux distribution in mind?",
        default=False,
    ).ask()

    if knows_distro:
        distro = questionary.select(
            "Which distro are you interested in?",
            choices=[
                questionary.Choice("Ubuntu", value="ubuntu"),
                questionary.Choice("Linux Mint", value="linuxmint"),
                questionary.Choice("Fedora", value="fedora"),
                questionary.Choice("Pop!_OS", value="popos"),
                questionary.Choice("Arch Linux", value="arch"),
                questionary.Choice("Manjaro", value="manjaro"),
                questionary.Choice("Debian", value="debian"),
                questionary.Choice("openSUSE", value="opensuse"),
                questionary.Choice("Zorin OS", value="zorin"),
                questionary.Choice("elementary OS", value="elementary"),
                questionary.Choice("Nobara (gaming focused)", value="nobara"),
                questionary.Choice("Other (I'll type it)", value="other"),
            ],
        ).ask()
        if distro == "other":
            distro = questionary.text("Enter the distro name:").ask()
        profile.preferences.distro_preference = distro
    else:
        console.print("[info]No worries! We'll recommend one based on your needs.[/info]")

    # Additional information
    console.print()
    additional = questionary.text(
        "Any additional information you want conveyed to the AI assistant?\n"
        "(special requirements, accessibility needs, dual-boot plans, etc.)\n"
        "Press Enter to skip:",
        default="",
    ).ask()
    if additional:
        profile.preferences.additional_notes += f"\n{additional}"

    print_success("Preferences saved!")
    profile.completed_steps.append("questionnaire")
