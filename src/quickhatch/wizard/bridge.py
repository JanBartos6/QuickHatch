"""Step 5: Launch the AI bridge — local API + TUI for LLM interaction."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import questionary

from quickhatch.bridge.display import display_message, display_suggestion, prompt_question
from quickhatch.bridge.server import BridgeServer, BridgeState
from quickhatch.config import UserProfile
from quickhatch.console import (
    console,
    print_error,
    print_info,
    print_step,
    print_success,
    print_warning,
)


def _write_prompt_file(profile: UserProfile, port: int) -> Path:
    """Write the agent prompt to a file and return the path."""
    base = f"http://localhost:{port}"

    prompt = (
        f"I am migrating from Windows to Linux. QuickHatch is running a bridge API at {base}.\n\n"
        f"First, run: curl -s {base}/api/profile | python3 -m json.tool\n"
        f"to see my full system profile (hardware, apps, preferences).\n\n"
        f"Then analyze everything and send your recommendations back through the API using curl.\n\n"
        f"Use POST {base}/api/suggest to send analysis:\n"
        f'  curl -s -X POST {base}/api/suggest -H "Content-Type: application/json" '
        f'-d \'{{"category":"distro","title":"...","content":"your analysis"}}\'\n\n'
        f"Categories: distro, apps, drivers, desktop, plan.\n\n"
        f"Use POST {base}/api/ask to ask me questions:\n"
        f'  curl -s -X POST {base}/api/ask -H "Content-Type: application/json" '
        f'-d \'{{"question":"your question"}}\'\n\n'
        f"Omit choices -- let me type freely, or provide choices only when it truly makes sense.\n\n"
        f"Use POST {base}/api/done when finished:\n"
        f'  curl -s -X POST {base}/api/done -H "Content-Type: application/json" '
        f'-d \'{{"summary":"Done!"}}\'\n\n'
        f"Think freely. Use your full knowledge. Provide links, reasoning, pros and cons.\n"
        f"Do not limit yourself to a few options -- consider the full Linux ecosystem.\n\n"
        f"Start by reading my profile, then give me a thorough distro analysis,\n"
        f"app compatibility breakdown, driver plan, and desktop config.\n"
    )

    # Write under $HOME, not CWD — if quickhatch was launched from a
    # read-only dir (Program Files etc.) the write would otherwise raise,
    # and leaving prompt files scattered in random directories pollutes
    # the filesystem + leaks the profile contents.
    prompt_file = Path.home() / ".quickhatch-prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    return prompt_file


def _get_launch_commands(profile: UserProfile, port: int) -> dict[str, str]:
    """Get launch commands for both PowerShell and bash."""
    prompt_file = _write_prompt_file(profile, port)
    provider = profile.ai_config.provider

    # Quote the path for each shell — $HOME on Windows (`C:\Users\my name`)
    # often contains spaces. PowerShell wants single quotes with `''`
    # escaping, POSIX shell wants single quotes with `'\''` escaping.
    pf_str = str(prompt_file)
    ps_quoted = "'" + pf_str.replace("'", "''") + "'"
    sh_quoted = "'" + pf_str.replace("'", "'\\''") + "'"
    if provider == "codex":
        return {
            "powershell": f"codex (Get-Content {ps_quoted} -Raw)",
            "bash": f'codex "$(cat {sh_quoted})"',
        }
    else:
        return {
            "powershell": f"Get-Content {ps_quoted}",
            "bash": f"cat {sh_quoted}",
        }


def _print_api_docs(port: int) -> None:
    """Print the API documentation for the user/LLM."""
    console.print(
        f"\n[bold]QuickHatch Bridge API[/bold] running at [cyan]http://localhost:{port}[/cyan]\n\n"
        "[bold]Endpoints:[/bold]\n"
        f"  [cyan]GET  /api/profile[/cyan]        — Get full system profile (hardware, apps, preferences)\n"
        f"  [cyan]GET  /api/status[/cyan]         — Check bridge status\n"
        f"  [cyan]GET  /api/suggestions[/cyan]    — List all suggestions posted so far\n"
        f"  [cyan]POST /api/suggest[/cyan]        — Post a suggestion/analysis\n"
        f'       body: {{"category": "distro|apps|drivers|desktop|plan", "title": "...", "content": ...}}\n'
        f"  [cyan]POST /api/ask[/cyan]            — Ask the user a question (blocks until answered)\n"
        f'       body: {{"question": "...", "choices": ["a", "b"] or null}}\n'
        f"  [cyan]POST /api/ask/async[/cyan]      — Ask without blocking, poll GET /api/response/{{id}}\n"
        f"  [cyan]POST /api/message[/cyan]        — Send a message to display\n"
        f'       body: {{"role": "agent", "content": "..."}}\n'
        f"  [cyan]POST /api/done[/cyan]           — Signal migration setup is complete\n"
    )


def run(profile: UserProfile) -> None:
    print_step(5, "AI Agent Bridge")

    console.print(
        "We'll start a local API server so the AI agent can communicate\n"
        "with this wizard. The agent will analyze your data, make\n"
        "recommendations, and ask you questions — all through this interface.\n"
    )

    # Start bridge server
    state = BridgeState(profile)
    server = BridgeServer(state, port=0)  # 0 = auto-pick available port

    # Register display callbacks
    state.on_suggestion(lambda s: display_suggestion(s))
    state.on_message(lambda role, content: display_message(role, content))
    # Questions are handled in the main loop below

    try:
        port = server.start()
    except OSError as e:
        print_error(f"Could not start bridge server: {e}")
        print_warning("Skipping bridge mode. The instruction file will still be generated.")
        profile.completed_steps.append("bridge")
        return

    print_success(f"Bridge server running on port {port}")
    _print_api_docs(port)

    # Save commands for both shells
    cmds = _get_launch_commands(profile, port)

    # Write wrapper scripts under $HOME alongside the prompt file.
    # Writing to CWD fails when quickhatch is launched from a read-only
    # directory (Program Files, a bundle install).
    cmd_file_sh = Path.home() / ".quickhatch-agent-cmd.sh"
    cmd_file_sh.write_text(cmds["bash"] + "\n")

    cmd_file_ps1 = Path.home() / ".quickhatch-agent-cmd.ps1"
    cmd_file_ps1.write_text(cmds["powershell"] + "\n")

    console.print()

    if sys.platform == "win32":
        print_success("Command saved to .quickhatch-agent-cmd.ps1")
        console.print(
            "\n[bold]Open a new PowerShell terminal and run:[/bold]\n\n"
            "  [cyan].\\\\.quickhatch-agent-cmd.ps1[/cyan]\n\n"
            "Or copy this:\n"
        )
        console.print(cmds["powershell"], highlight=False, soft_wrap=True)
        console.print("\n[dim]On bash/WSL instead: bash .quickhatch-agent-cmd.sh[/dim]")
    else:
        print_success("Command saved to .quickhatch-agent-cmd.sh")
        console.print(
            "\n[bold]Open a new terminal and run:[/bold]\n\n"
            "  [cyan]bash .quickhatch-agent-cmd.sh[/cyan]\n\n"
            "Or copy this:\n"
        )
        console.print(cmds["bash"], highlight=False, soft_wrap=True)

    console.print(f"\n[dim]Or point any AI tool at: http://localhost:{port}/api/profile[/dim]\n")

    # Store port in profile for export
    profile.ai_config.endpoint = f"http://localhost:{port}"

    # Main loop — wait for agent and handle questions
    console.print("\n[dim]Waiting for agent to connect... (Ctrl+C to skip)[/dim]")

    try:
        # Wait for connection
        while not state.agent_connected:
            # Check for pending questions even before "connected" flag
            _handle_pending_questions(state)
            time.sleep(1)

        print_success("Agent connected!")
        console.print("[dim]Agent is analyzing your system...[/dim]\n")

        # Run until agent says it's done
        while not state.agent_done:
            _handle_pending_questions(state)
            time.sleep(0.5)

        # Let user review everything
        console.print()
        print_success("Agent has finished its analysis!")

        if state.suggestions:
            review = questionary.confirm(
                "Would you like to review the suggestions before proceeding?",
                default=True,
            ).ask()

            if review:
                for suggestion in state.suggestions:
                    display_suggestion(suggestion)
                    if suggestion.accepted is None:
                        accept = questionary.confirm(
                            f"Accept this {suggestion.category} suggestion?",
                            default=True,
                        ).ask()
                        if accept is not None:
                            state.accept_suggestion(suggestion.id, accept)

    except KeyboardInterrupt:
        console.print("\n")
        print_warning("Bridge mode interrupted.")
        skip = questionary.confirm(
            "Continue without the bridge? (Instructions will still be exported)",
            default=True,
        ).ask()
        if not skip:
            raise

    finally:
        server.stop()
        print_info("Bridge server stopped.")

    profile.completed_steps.append("bridge")


def _handle_pending_questions(state: BridgeState) -> None:
    """Check for and handle any pending questions from the agent."""
    for q in state.get_pending_questions():
        answer = prompt_question(q)
        state.answer_question(q.id, answer)


# Need Panel import
