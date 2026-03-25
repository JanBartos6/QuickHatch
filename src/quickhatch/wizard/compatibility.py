"""Step 4: Application compatibility report."""

from rich.table import Table

from quickhatch.analysis.app_mapper import map_apps
from quickhatch.analysis.driver_check import check_drivers
from quickhatch.config import UserProfile
from quickhatch.console import console, print_info, print_step, print_success, print_warning


def run(profile: UserProfile) -> None:
    print_step(4, "Compatibility Analysis")

    if not profile.apps:
        print_warning("No apps detected - skipping compatibility analysis.")
        profile.completed_steps.append("compatibility")
        return

    # Map apps to Linux alternatives
    profile.apps = map_apps(profile.apps)

    # Display compatibility report
    table = Table(title="Application Compatibility Report", show_lines=True)
    table.add_column("Windows App", style="cyan", min_width=20)
    table.add_column("Linux Alternative", style="green", min_width=25)
    table.add_column("Method", style="yellow", min_width=10)
    table.add_column("Notes", style="dim", min_width=20)

    native_count = 0
    alt_count = 0
    wine_count = 0
    unknown_count = 0

    for app in profile.apps:
        if app.compatibility == "unknown" and not app.linux_alternative:
            unknown_count += 1
            continue

        method_style = {
            "native": "[green]Native[/green]",
            "alternative": "[yellow]Alternative[/yellow]",
            "wine": "[red]Wine/Proton[/red]",
            "none": "[red]No support[/red]",
        }.get(app.compatibility, "[dim]Unknown[/dim]")

        if app.compatibility == "native":
            native_count += 1
        elif app.compatibility == "alternative":
            alt_count += 1
        elif app.compatibility == "wine":
            wine_count += 1

        table.add_row(
            app.name,
            app.linux_alternative or "Unknown",
            method_style,
            app.notes or "",
        )

    console.print(table)
    console.print()

    total_mapped = native_count + alt_count + wine_count
    print_success(
        f"Compatibility summary: {native_count} native, "
        f"{alt_count} alternatives, {wine_count} via Wine, "
        f"{unknown_count} unknown"
    )

    # Driver compatibility
    if profile.hardware and profile.hardware.gpus:
        console.print()
        driver_recs = check_drivers(profile.hardware)
        if driver_recs:
            driver_table = Table(title="Hardware Driver Status")
            driver_table.add_column("Component", style="cyan")
            driver_table.add_column("Status", style="yellow")
            driver_table.add_column("Recommendation", style="dim")

            for rec in driver_recs:
                status_style = {
                    "good": "[green]Good[/green]",
                    "action_needed": "[yellow]Action Needed[/yellow]",
                    "check": "[cyan]Check[/cyan]",
                }.get(rec["status"], rec["status"])

                driver_table.add_row(
                    rec["component"],
                    status_style,
                    rec["recommendation"][:80] + "..." if len(rec["recommendation"]) > 80 else rec["recommendation"],
                )

            console.print(driver_table)

    profile.completed_steps.append("compatibility")
