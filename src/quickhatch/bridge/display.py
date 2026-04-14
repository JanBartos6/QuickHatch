"""TUI display for the bridge — shows LLM suggestions and handles user input."""

from __future__ import annotations

import questionary
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from quickhatch.bridge.server import PendingQuestion, Suggestion
from quickhatch.console import console


def display_suggestion(suggestion: Suggestion) -> None:
    """Display a suggestion from the LLM in the terminal."""
    style_map = {
        "distro": ("cyan", "Distro Recommendation"),
        "apps": ("green", "App Compatibility Analysis"),
        "drivers": ("yellow", "Driver & Hardware Setup"),
        "desktop": ("magenta", "Desktop Configuration"),
        "plan": ("blue", "Migration Plan"),
        "message": ("white", "Agent Message"),
    }

    color, default_title = style_map.get(
        suggestion.category, ("white", suggestion.category.title())
    )
    title = suggestion.title or default_title

    content = suggestion.content
    if isinstance(content, dict):
        # Structured data — render as a table or key-value pairs
        if suggestion.category == "distro" and "recommendations" in content:
            _display_distro_suggestion(content, title)
            return
        if suggestion.category == "apps" and "apps" in content:
            _display_apps_suggestion(content, title)
            return
        # Generic dict — render as markdown-ish
        lines = []
        for k, v in content.items():
            lines.append(f"**{k}:** {v}")
        content = "\n".join(lines)

    if isinstance(content, list):
        content = "\n".join(f"- {item}" for item in content)

    console.print(
        Panel(
            Markdown(str(content)) if "\n" in str(content) else str(content),
            title=f"[bold {color}]{title}[/bold {color}]",
            border_style=color,
            padding=(1, 2),
        )
    )


def _display_distro_suggestion(content: dict, title: str) -> None:
    """Display distro recommendations as a table."""
    table = Table(title=title, show_lines=True)
    table.add_column("Distro", style="cyan", min_width=15)
    table.add_column("Why", style="dim", min_width=40)

    for rec in content.get("recommendations", []):
        name = rec.get("name", "Unknown")
        reason = rec.get("reason", "")
        table.add_row(name, reason)

    console.print(table)

    if content.get("top_pick"):
        console.print(f"\n[bold green]Top pick:[/bold green] {content['top_pick']}")
    if content.get("reasoning"):
        console.print(f"[dim]{content['reasoning']}[/dim]")
    console.print()


def _display_apps_suggestion(content: dict, title: str) -> None:
    """Display app compatibility as a table."""
    table = Table(title=title, show_lines=True)
    table.add_column("Windows App", style="cyan", min_width=18)
    table.add_column("Linux Path", style="green", min_width=20)
    table.add_column("Method", style="yellow", min_width=10)
    table.add_column("Notes", style="dim", min_width=20)

    for app in content.get("apps", []):
        method_colors = {
            "native": "[green]Native[/green]",
            "alternative": "[yellow]Alternative[/yellow]",
            "wine": "[red]Wine/Proton[/red]",
            "none": "[red]No support[/red]",
        }
        table.add_row(
            app.get("name", ""),
            app.get("linux_alternative", "—"),
            method_colors.get(app.get("method", ""), app.get("method", "")),
            app.get("notes", ""),
        )

    console.print(table)
    console.print()


def prompt_question(question: PendingQuestion) -> str:
    """Display a question from the LLM and get user's answer."""
    console.print()
    console.print(
        Panel(
            question.question,
            title="[bold yellow]Agent is asking[/bold yellow]",
            border_style="yellow",
        )
    )

    if question.choices:
        answer = questionary.select(
            "Your choice:",
            choices=question.choices,
        ).ask()
    else:
        answer = questionary.text("Your answer:").ask()

    if answer is None:
        answer = ""

    return answer


def display_message(role: str, content: str) -> None:
    """Display a chat message."""
    if role == "agent":
        console.print(f"[bold cyan]Agent:[/bold cyan] {content}")
    elif role == "user":
        console.print(f"[bold green]You:[/bold green] {content}")
    else:
        console.print(f"[dim][{role}][/dim] {content}")
