"""Shared Rich console instance and helper functions."""

from rich.console import Console
from rich.panel import Panel
from rich.theme import Theme

QUICKHATCH_THEME = Theme(
    {
        "info": "cyan",
        "warning": "yellow",
        "error": "bold red",
        "success": "bold green",
        "step_header": "bold magenta",
        "highlight": "bold white on blue",
    }
)

console = Console(theme=QUICKHATCH_THEME)


def print_banner() -> None:
    banner = r"""
   ____        _      _    _    _       _       _
  / __ \      (_)    | |  | |  | |     | |     | |
 | |  | |_   _ _  ___| | _| |__| | __ _| |_ ___| |__
 | |  | | | | | |/ __| |/ /  __  |/ _` | __/ __| '_ \
 | |__| | |_| | | (__|   <| |  | | (_| | || (__| | | |
  \___\_\\__,_|_|\___|_|\_\_|  |_|\__,_|\__\___|_| |_|
"""
    console.print(
        Panel(
            banner,
            title="[bold]v0.1.0[/bold]",
            subtitle="Windows to Linux Migration Wizard",
            border_style="cyan",
        )
    )


def print_step(n: int, title: str) -> None:
    console.print(f"\n[step_header]{'=' * 60}[/step_header]")
    console.print(f"[step_header]  Step {n}: {title}[/step_header]")
    console.print(f"[step_header]{'=' * 60}[/step_header]\n")


def print_success(msg: str) -> None:
    console.print(f"[success][+][/success] {msg}")


def print_warning(msg: str) -> None:
    console.print(f"[warning][!][/warning] {msg}")


def print_error(msg: str) -> None:
    console.print(f"[error][X][/error] {msg}")


def print_info(msg: str) -> None:
    console.print(f"[info][*][/info] {msg}")
