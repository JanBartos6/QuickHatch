"""Step 4: Application compatibility analysis."""

from rich.table import Table

from quickhatch.config import UserProfile
from quickhatch.console import console, print_step, print_success, print_info
from quickhatch.analysis.app_mapper import analyze_apps


def run(profile: UserProfile) -> None:
    """Run the compatibility analysis step."""
    print_step(4, "Application Compatibility")

    if not profile.apps:
        print_info("No applications were scanned. Skipping compatibility check.")
        profile.completed_steps.append("compatibility")
        return

    console.print("Analyzing your applications for Linux compatibility...\n")

    analyze_apps(profile.apps)

    # Group by compatibility
    native = [a for a in profile.apps if a.compatibility == "native"]
    alternative = [a for a in profile.apps if a.compatibility == "alternative"]
    wine = [a for a in profile.apps if a.compatibility == "wine"]
    no_support = [a for a in profile.apps if a.compatibility == "none"]
    unknown = [a for a in profile.apps if a.compatibility == "unknown"]

    # Summary table
    table = Table(title="Compatibility Summary", border_style="cyan")
    table.add_column("Status", style="bold")
    table.add_column("Count", justify="right")
    table.add_column("Description")
    table.add_row("[green]Native[/green]", str(len(native)), "Works natively on Linux")
    table.add_row("[cyan]Alternative[/cyan]", str(len(alternative)), "Good Linux alternative available")
    table.add_row("[yellow]Wine/Proton[/yellow]", str(len(wine)), "Can run via Wine or Proton")
    table.add_row("[red]No support[/red]", str(len(no_support)), "No known Linux support")
    table.add_row("[dim]Unknown[/dim]", str(len(unknown)), "Compatibility not determined")
    console.print(table)
    console.print()

    # Detailed table for alternatives
    if alternative:
        alt_table = Table(title="Linux Alternatives", border_style="green")
        alt_table.add_column("Windows App", style="bold")
        alt_table.add_column("Linux Alternative")
        alt_table.add_column("Notes")
        for app in alternative:
            alt_table.add_row(app.name, app.linux_alternative or "—", app.compatibility_notes)
        console.print(alt_table)
        console.print()

    # Show native apps
    if native:
        native_table = Table(title="Apps Available on Linux", border_style="green")
        native_table.add_column("Application", style="bold")
        native_table.add_column("Notes")
        for app in native[:20]:  # limit display
            native_table.add_row(app.name, app.compatibility_notes)
        if len(native) > 20:
            native_table.add_row(f"... and {len(native) - 20} more", "")
        console.print(native_table)
        console.print()

    # Show problematic apps
    if no_support:
        console.print("[warning]Apps with no known Linux support:[/warning]")
        for app in no_support:
            console.print(f"  [red]x[/red] {app.name}")
        console.print()

    print_success("Compatibility analysis complete!")
    profile.completed_steps.append("compatibility")
