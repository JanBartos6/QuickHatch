"""Step 5: Distribution recommendation."""

import questionary
from rich.panel import Panel
from rich.table import Table

from quickhatch.config import UserProfile
from quickhatch.console import console, print_step, print_success, print_info
from quickhatch.analysis.distro_engine import recommend_distros


def run(profile: UserProfile) -> None:
    """Run the distro recommendation step."""
    print_step(5, "Distribution Recommendation")

    if profile.preferences.distro_preference:
        console.print(
            f"You mentioned you're interested in [highlight]{profile.preferences.distro_preference}[/highlight].\n"
            "Let's see how it compares with our recommendations.\n"
        )

    ranked = recommend_distros(profile)

    # Display recommendations
    table = Table(title="Recommended Distributions", border_style="cyan")
    table.add_column("#", style="bold", width=3)
    table.add_column("Distribution", style="bold cyan")
    table.add_column("Score", justify="right")
    table.add_column("Desktop Environment")
    table.add_column("Why It Fits")

    for i, (distro_name, score, de, reason) in enumerate(ranked[:5], 1):
        marker = " [green]<-- your pick[/green]" if distro_name.lower() == (profile.preferences.distro_preference or "").lower() else ""
        table.add_row(
            str(i),
            f"{distro_name}{marker}",
            f"{score}/100",
            de,
            reason,
        )
    console.print(table)
    console.print()

    # Let user choose
    distro_choices = [
        questionary.Choice(f"{name} (score: {score}/100)", value=name)
        for name, score, de, reason in ranked[:5]
    ]
    distro_choices.append(questionary.Choice("Other (I'll type it)", value="other"))

    chosen = questionary.select(
        "Which distribution would you like to install?",
        choices=distro_choices,
    ).ask()

    if chosen is None:
        raise KeyboardInterrupt

    if chosen == "other":
        chosen = questionary.text("Enter the distribution name:").ask()
        if chosen is None:
            raise KeyboardInterrupt

    profile.recommended_distro = chosen

    # Find the recommended DE for the chosen distro
    for name, score, de, reason in ranked:
        if name == chosen:
            profile.recommended_de = de
            break

    if not profile.recommended_de:
        profile.recommended_de = questionary.select(
            "Which desktop environment do you want?",
            choices=["GNOME", "KDE Plasma", "Cinnamon", "XFCE", "MATE", "i3/Sway", "Hyprland"],
        ).ask()

    console.print()
    print_success(f"Selected: {chosen} with {profile.recommended_de}")
    profile.completed_steps.append("distro")
