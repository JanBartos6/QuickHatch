"""Step 1: Choose AI assistant for the migration."""

import shutil
import subprocess

import questionary

from quickhatch.config import UserProfile
from quickhatch.console import print_error, print_info, print_step, print_success, print_warning


def _check_cli(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _check_local_endpoint(endpoint: str) -> bool:
    try:
        import requests

        resp = requests.get(endpoint, timeout=3)
        return resp.status_code < 500
    except Exception:
        return False


def run(profile: UserProfile) -> None:
    print_step(1, "AI Assistant Setup")

    provider = questionary.select(
        "Which AI assistant do you want to use on your new Linux system?",
        choices=[
            questionary.Choice("Claude Code (recommended)", value="claude_code"),
            questionary.Choice("OpenAI Codex CLI", value="codex"),
            questionary.Choice("Local model - Ollama", value="local_ollama"),
            questionary.Choice("Local model - LM Studio", value="local_lmstudio"),
            questionary.Choice("API key (Anthropic or OpenAI)", value="api"),
        ],
    ).ask()

    if provider is None:
        raise KeyboardInterrupt

    profile.ai_config.provider = provider

    if provider == "claude_code":
        if _check_cli("claude"):
            print_success("Claude Code CLI detected!")
        else:
            print_warning(
                "Claude Code CLI not found on this system. "
                "It will be set up on your new Linux machine."
            )
        profile.ai_config.model_name = "claude-sonnet-4-6"

    elif provider == "codex":
        if _check_cli("codex"):
            print_success("Codex CLI detected!")
        else:
            print_warning("Codex CLI not found. It will be set up on Linux.")
        profile.ai_config.model_name = "codex"

    elif provider == "local_ollama":
        print_info("Ollama will be installed on your new Linux system.")
        model = questionary.text(
            "Which model do you want to use? (e.g., llama3, codellama, mistral)",
            default="llama3",
        ).ask()
        if model is None:
            raise KeyboardInterrupt
        profile.ai_config.model_name = model
        profile.ai_config.endpoint = "http://localhost:11434"

    elif provider == "local_lmstudio":
        print_info("LM Studio will be set up on your new Linux system.")
        model = questionary.text(
            "Which model do you want to use?",
            default="local-model",
        ).ask()
        if model is None:
            raise KeyboardInterrupt
        profile.ai_config.model_name = model
        profile.ai_config.endpoint = "http://localhost:1234/v1"

    elif provider == "api":
        api_provider = questionary.select(
            "Which API provider?",
            choices=[
                questionary.Choice("Anthropic (Claude)", value="anthropic"),
                questionary.Choice("OpenAI", value="openai"),
            ],
        ).ask()
        if api_provider is None:
            raise KeyboardInterrupt

        api_key = questionary.password(
            f"Enter your {api_provider.title()} API key:"
        ).ask()
        if api_key is None:
            raise KeyboardInterrupt

        profile.ai_config.api_key = api_key
        if api_provider == "anthropic":
            profile.ai_config.endpoint = "https://api.anthropic.com"
            profile.ai_config.model_name = "claude-sonnet-4-6"
        else:
            profile.ai_config.endpoint = "https://api.openai.com"
            profile.ai_config.model_name = "gpt-4o"

    print_success(f"AI assistant configured: {provider}")
    profile.completed_steps.append("ai_setup")
