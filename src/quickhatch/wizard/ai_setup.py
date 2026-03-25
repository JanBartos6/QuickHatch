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
        print_info("Claude Code manages its own model — no extra config needed.")

    elif provider == "codex":
        if _check_cli("codex"):
            print_success("Codex CLI detected!")
        else:
            print_warning("Codex CLI not found. It will be set up on Linux.")
        print_info("Codex manages its own model — no extra config needed.")

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

        if api_provider == "anthropic":
            model = questionary.select(
                "Which Claude model?",
                choices=[
                    questionary.Choice("Claude Opus 4.6 (most capable)", value="claude-opus-4-6"),
                    questionary.Choice("Claude Sonnet 4.6 (balanced)", value="claude-sonnet-4-6"),
                    questionary.Choice("Claude Haiku 4.5 (fastest)", value="claude-haiku-4-5-20251001"),
                ],
            ).ask()
            profile.ai_config.endpoint = "https://api.anthropic.com"
        else:
            model = questionary.select(
                "Which OpenAI model?",
                choices=[
                    questionary.Choice("GPT-4o (most capable)", value="gpt-4o"),
                    questionary.Choice("GPT-4o mini (faster/cheaper)", value="gpt-4o-mini"),
                    questionary.Choice("o3 (reasoning)", value="o3"),
                ],
            ).ask()
            profile.ai_config.endpoint = "https://api.openai.com"

        if model is None:
            raise KeyboardInterrupt
        profile.ai_config.model_name = model

        api_key = questionary.password(
            f"Enter your {api_provider.title()} API key:"
        ).ask()
        if api_key is None:
            raise KeyboardInterrupt
        profile.ai_config.api_key = api_key

    print_success(f"AI assistant configured: {provider}")
    profile.completed_steps.append("ai_setup")
