"""Shared Rich console instance and helper functions."""

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.theme import Theme

custom_theme = Theme({
    "info": "cyan",
    "warning": "yellow",
    "error": "bold red",
    "success": "bold green",
    "step_header": "bold magenta",
    "highlight": "bold cyan",
})

console = Console(theme=custom_theme)


BANNER = r"""
   ____        _      _    _    _       _       _
  / __ \      (_)    | |  | |  | |     | |     | |
 | |  | |_   _ _  ___| | _| |__| | __ _| |_ ___| |__
 | |  | | | | | |/ __| |/ /  __  |/ _` | __/ __| '_ \
 | |__| | |_| | | (__|   <| |  | | (_| | || (__| | | |
  \___\_\\__,_|_|\___|_|\_\_|  |_|\__,_|\__\___|_| |_|
"""


def print_banner():
    """Print the QuickHatch welcome banner."""
    console.print(Text(BANNER, style="bold cyan"))
    console.print(
        Panel(
            "[highlight]Windows -> Linux Migration Wizard[/highlight]\n"
            "Powered by AI to make your switch seamless",
            border_style="cyan",
        )
    )
    console.print()


def print_step(number: int, title: str):
    """Print a step header."""
    console.print()
    console.print(
        Panel(
            f"[step_header]Step {number}: {title}[/step_header]",
            border_style="magenta",
            width=60,
        )
    )
    console.print()


def print_success(message: str):
    """Print a success message."""
    console.print(f"[success]\u2713[/success] {message}")


def print_warning(message: str):
    """Print a warning message."""
    console.print(f"[warning]\u26a0[/warning] {message}")


def print_error(message: str):
    """Print an error message."""
    console.print(f"[error]\u2717[/error] {message}")


def print_info(message: str):
    """Print an info message."""
    console.print(f"[info]\u2139[/info] {message}")
