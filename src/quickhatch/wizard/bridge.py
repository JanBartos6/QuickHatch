"""Step 5: Launch the AI bridge — local API + TUI for LLM interaction."""

from __future__ import annotations

import time

import questionary

from quickhatch.bridge.display import display_message, display_suggestion, prompt_question
from quickhatch.bridge.server import BridgeServer, BridgeState
from quickhatch.config import UserProfile
from quickhatch.console import console, print_error, print_info, print_step, print_success, print_warning


def _get_launch_command(profile: UserProfile, port: int) -> str:
    """Get the command the user should run in their AI tool."""
    base = f"http://localhost:{port}"
    provider = profile.ai_config.provider

    # Build a prompt that tells the AI tool to use curl/bash for HTTP calls
    prompt = (
        f"I'm migrating from Windows to Linux. QuickHatch is running a bridge API at {base}. "
        f"First, run: curl -s {base}/api/profile | python3 -m json.tool "
        f"to see my full system profile (hardware, apps, preferences). "
        f"Then analyze everything and send your recommendations back through the API using curl. "
        f"Example commands: "
        f"curl -s -X POST {base}/api/suggest -H 'Content-Type: application/json' "
        f"""-d '{{"category":"distro","title":"Distro Recommendation","content":"your analysis here"}}' """
        f"to post suggestions. "
        f"curl -s -X POST {base}/api/ask -H 'Content-Type: application/json' "
        f"""-d '{{"question":"Which distro?","choices":["Pop!_OS","Ubuntu","Fedora"]}}' """
        f"to ask me questions (this blocks until I answer). "
        f"curl -s -X POST {base}/api/done -H 'Content-Type: application/json' "
        f"""-d '{{"summary":"Done!"}}' """
        f"when finished. "
        f"Start by reading my profile, then recommend a distro, analyze app compatibility, "
        f"plan driver setup, and configure my desktop."
    )

    if provider == "claude_code":
        return f"claude -p '{prompt}'"
    elif provider == "codex":
        return f"codex '{prompt}'"
    else:
        return prompt


def _print_api_docs(port: int) -> None:
    """Print the API documentation for the user/LLM."""
    console.print(
        f"\n[bold]QuickHatch Bridge API[/bold] running at [cyan]http://localhost:{port}[/cyan]\n\n"
        "[bold]Endpoints:[/bold]\n"
        f"  [cyan]GET  /api/profile[/cyan]        — Get full system profile (hardware, apps, preferences)\n"
        f"  [cyan]GET  /api/status[/cyan]         — Check bridge status\n"
        f"  [cyan]GET  /api/suggestions[/cyan]    — List all suggestions posted so far\n"
        f"  [cyan]POST /api/suggest[/cyan]        — Post a suggestion/analysis\n"
        f"       body: {{\"category\": \"distro|apps|drivers|desktop|plan\", \"title\": \"...\", \"content\": ...}}\n"
        f"  [cyan]POST /api/ask[/cyan]            — Ask the user a question (blocks until answered)\n"
        f"       body: {{\"question\": \"...\", \"choices\": [\"a\", \"b\"] or null}}\n"
        f"  [cyan]POST /api/ask/async[/cyan]      — Ask without blocking, poll GET /api/response/{{id}}\n"
        f"  [cyan]POST /api/message[/cyan]        — Send a message to display\n"
        f"       body: {{\"role\": \"agent\", \"content\": \"...\"}}\n"
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

    # Show the command to run
    launch_cmd = _get_launch_command(profile, port)
    provider = profile.ai_config.provider

    console.print(Panel(
        f"[bold]Open a new terminal and run your AI tool:[/bold]\n\n"
        f"  [cyan]{launch_cmd}[/cyan]\n\n"
        f"Or, if using a different tool, point it at:\n"
        f"  [cyan]http://localhost:{port}/api/profile[/cyan]\n\n"
        "The agent will connect, analyze your system, and send\n"
        "recommendations back here. Answer any questions that appear.",
        title="[bold green]Launch Your AI Agent[/bold green]",
        border_style="green",
        padding=(1, 2),
    ))

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
from rich.panel import Panel
