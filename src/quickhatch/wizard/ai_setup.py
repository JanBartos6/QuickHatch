"""Step 1: Choose AI assistant for the migration."""

import shutil
import subprocess

import questionary
from quickhatch.config import AIConfig, UserProfile
from quickhatch.console import console, print_step, print_success, print_warning, print_info


def _check_command(cmd: str) -> bool:
    """Check if a command is available on PATH."""
    return shutil.which(cmd) is not None


def _test_ollama() -> bool:
    """Check if Ollama is running."""
    try:
        import requests
        r = requests.get("http://localhost:11434/api/tags", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def _test_lmstudio() -> bool:
    """Check if LM Studio server is running."""
    try:
        import requests
        r = requests.get("http://localhost:1234/v1/models", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def run(profile: UserProfile) -> None:
    """Run the AI setup step."""
    print_step(1, "AI Assistant Setup")

    console.print(
        "Choose an AI assistant to help with your migration.\n"
        "The assistant will receive detailed instructions about your\n"
        "system and preferences to set up your new Linux install.\n"
    )

    choices = [
        questionary.Choice(
            "Claude Code (recommended) - Anthropic's coding agent",
            value="claude_code",
        ),
        questionary.Choice(
            "Codex - OpenAI's coding agent",
            value="codex",
        ),
        questionary.Choice(
            "Local model - Ollama (free, runs locally)",
            value="local_ollama",
        ),
        questionary.Choice(
            "Local model - LM Studio (free, runs locally)",
            value="local_lmstudio",
        ),
        questionary.Choice(
            "API key - Use any OpenAI-compatible API",
            value="api",
        ),
    ]

    provider = questionary.select(
        "Which AI assistant do you want to use?",
        choices=choices,
    ).ask()

    if provider is None:
        raise KeyboardInterrupt

    ai_config = AIConfig(provider=provider)

    if provider == "claude_code":
        if _check_command("claude"):
            print_success("Claude Code CLI detected on your system!")
        else:
            print_warning(
                "Claude Code CLI not found. It will need to be installed on your Linux system.\n"
                "  Install: npm install -g @anthropic-ai/claude-code"
            )
        console.print()
        has_key = questionary.confirm(
            "Do you have an Anthropic API key to use with Claude Code?",
            default=False,
        ).ask()
        if has_key:
            ai_config.api_key = questionary.password(
                "Enter your Anthropic API key:"
            ).ask()

    elif provider == "codex":
        if _check_command("codex"):
            print_success("Codex CLI detected!")
        else:
            print_warning("Codex CLI not found. It will need to be installed on Linux.")
        has_key = questionary.confirm(
            "Do you have an OpenAI API key?",
            default=False,
        ).ask()
        if has_key:
            ai_config.api_key = questionary.password(
                "Enter your OpenAI API key:"
            ).ask()

    elif provider == "local_ollama":
        if _test_ollama():
            print_success("Ollama is running!")
        else:
            print_info("Ollama will need to be installed on your Linux system.")
            print_info("  Install: curl -fsSL https://ollama.com/install.sh | sh")

        model = questionary.text(
            "Which model do you want to use? (e.g., llama3, codellama, mistral)",
            default="llama3",
        ).ask()
        ai_config.model_name = model
        ai_config.endpoint = "http://localhost:11434"

    elif provider == "local_lmstudio":
        if _test_lmstudio():
            print_success("LM Studio server is running!")
        else:
            print_info("LM Studio will need to be set up on your Linux system.")

        model = questionary.text(
            "Which model do you want to use?",
            default="auto",
        ).ask()
        ai_config.model_name = model
        ai_config.endpoint = "http://localhost:1234/v1"

    elif provider == "api":
        ai_config.endpoint = questionary.text(
            "Enter the API endpoint URL:",
            default="https://api.openai.com/v1",
        ).ask()
        ai_config.api_key = questionary.password(
            "Enter your API key:"
        ).ask()
        ai_config.model_name = questionary.text(
            "Enter the model name:",
            default="gpt-4",
        ).ask()

    profile.ai_config = ai_config
    print_success(f"AI assistant configured: {provider}")
    profile.completed_steps.append("ai_setup")
