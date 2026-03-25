"""Step 5: Distro recommendation."""

import questionary
from rich.panel import Panel
from rich.table import Table

from quickhatch.analysis.distro_engine import recommend
from quickhatch.config import UserProfile
from quickhatch.console import console, print_info, print_step, print_success


def run(profile: UserProfile) -> None:
    print_step(5, "Distro Recommendation")

    if profile.preferences.distro_preference:
        console.print(
            f"You mentioned you'd like [bold cyan]{profile.preferences.distro_preference}[/bold cyan].\n"
            "Let's see how it compares with our recommendations.\n"
        )

    rankings = recommend(profile)
    top_5 = rankings[:5]

    if not top_5:
        print_warning("No distro data available. Defaulting to Ubuntu.")
        profile.recommended_distro = "Ubuntu"
        profile.recommended_desktop = "GNOME"
        profile.completed_steps.append("distro")
        return

    # Display rankings
    table = Table(title="Top Distro Recommendations")
    table.add_column("Rank", style="bold", width=6)
    table.add_column("Distro", style="cyan", min_width=15)
    table.add_column("Score", style="yellow", width=8)
    table.add_column("Why", style="dim", min_width=30)

    for i, (name, score, reason) in enumerate(top_5, 1):
        medal = {1: "[gold1]#1[/gold1]", 2: "[grey70]#2[/grey70]", 3: "[orange3]#3[/orange3]"}.get(i, f"#{i}")
        table.add_row(medal, name, f"{score:.0f}", reason)

    console.print(table)
    console.print()

    # Let user confirm or change
    top_name = top_5[0][0] if top_5 else "Ubuntu"

    if profile.preferences.distro_preference:
        use_preferred = questionary.confirm(
            f"Would you like to stick with {profile.preferences.distro_preference}?",
            default=True,
        ).ask()
        if use_preferred is None:
            raise KeyboardInterrupt
        if use_preferred:
            profile.recommended_distro = profile.preferences.distro_preference
        else:
            choice = questionary.select(
                "Which distro would you like?",
                choices=[name for name, _, _ in top_5],
            ).ask()
            if choice is None:
                raise KeyboardInterrupt
            profile.recommended_distro = choice
    else:
        use_top = questionary.confirm(
            f"Our top recommendation is {top_name}. Would you like to go with it?",
            default=True,
        ).ask()
        if use_top is None:
            raise KeyboardInterrupt
        if use_top:
            profile.recommended_distro = top_name
        else:
            choice = questionary.select(
                "Which distro would you like?",
                choices=[name for name, _, _ in top_5] + ["Other (type it in)"],
            ).ask()
            if choice is None:
                raise KeyboardInterrupt
            if choice == "Other (type it in)":
                choice = questionary.text("Enter the distro name:").ask()
                if choice is None:
                    raise KeyboardInterrupt
            profile.recommended_distro = choice

    # Desktop environment recommendation
    de_map = {
        "windows_like": ("KDE Plasma or Cinnamon", "Familiar taskbar and start menu layout"),
        "macos_like": ("GNOME or Pantheon", "Dock-based, clean and elegant"),
        "minimal": ("XFCE or LXQt", "Lightweight and clean"),
        "tiling": ("Hyprland, i3, or Sway", "Keyboard-driven window management"),
        "custom": ("Your choice", "Configure after installation"),
    }
    de_name, de_reason = de_map.get(
        profile.preferences.desktop_look, ("GNOME", "Default choice")
    )
    profile.recommended_desktop = de_name

    console.print(
        Panel(
            f"[bold cyan]Distro:[/bold cyan] {profile.recommended_distro}\n"
            f"[bold cyan]Desktop:[/bold cyan] {de_name} - {de_reason}",
            title="Your Selection",
            border_style="green",
        )
    )

    print_success(f"Selected {profile.recommended_distro} with {de_name}")
    profile.completed_steps.append("distro")
