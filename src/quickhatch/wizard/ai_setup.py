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
            questionary.Choice("Claude API (recommended)", value="api_anthropic"),
            questionary.Choice("OpenAI Codex CLI", value="codex"),
            questionary.Choice("OpenAI API", value="api_openai"),
            questionary.Choice("Local model - Ollama", value="local_ollama"),
            questionary.Choice("Local model - LM Studio", value="local_lmstudio"),
        ],
    ).ask()

    if provider is None:
        raise KeyboardInterrupt

    # Normalize api_anthropic/api_openai to "api" for downstream code
    if provider in ("api_anthropic", "api_openai"):
        profile.ai_config.provider = "api"
    else:
        profile.ai_config.provider = provider

    if provider == "codex":
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
        endpoint = "http://localhost:1234/v1"
        models: list[str] = []
        if _check_local_endpoint(endpoint):
            try:
                import requests

                resp = requests.get(f"{endpoint}/models", timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    models = [m["id"] for m in data.get("data", []) if m.get("id")]
            except Exception:
                pass

        if models:
            print_success(f"LM Studio detected with {len(models)} model(s) loaded.")
            model = questionary.select(
                "Which model do you want to use?",
                choices=models,
            ).ask()
        else:
            print_warning(
                "Could not reach LM Studio at localhost:1234, or no models are loaded. "
                "Make sure LM Studio is running with a model loaded."
            )
            model = questionary.text(
                "Enter the model name manually:",
                default="local-model",
            ).ask()
        if model is None:
            raise KeyboardInterrupt
        profile.ai_config.model_name = model
        profile.ai_config.endpoint = endpoint

    elif provider == "api_anthropic":
        model = questionary.select(
            "Which Claude model?",
            choices=[
                questionary.Choice("Claude Opus 4.6 (most capable)", value="claude-opus-4-6"),
                questionary.Choice("Claude Sonnet 4.6 (balanced)", value="claude-sonnet-4-6"),
                questionary.Choice("Claude Haiku 4.5 (fastest)", value="claude-haiku-4-5-20251001"),
            ],
        ).ask()
        if model is None:
            raise KeyboardInterrupt
        profile.ai_config.model_name = model
        profile.ai_config.endpoint = "https://api.anthropic.com"

        api_key = questionary.password("Enter your Anthropic API key:").ask()
        if api_key is None:
            raise KeyboardInterrupt
        profile.ai_config.api_key = api_key

    elif provider == "api_openai":
        model = questionary.select(
            "Which OpenAI model?",
            choices=[
                questionary.Choice("GPT-4o (most capable)", value="gpt-4o"),
                questionary.Choice("GPT-4o mini (faster/cheaper)", value="gpt-4o-mini"),
                questionary.Choice("o3 (reasoning)", value="o3"),
            ],
        ).ask()
        if model is None:
            raise KeyboardInterrupt
        profile.ai_config.model_name = model
        profile.ai_config.endpoint = "https://api.openai.com"

        api_key = questionary.password("Enter your OpenAI API key:").ask()
        if api_key is None:
            raise KeyboardInterrupt
        profile.ai_config.api_key = api_key

    print_success(f"AI assistant configured: {provider}")
    profile.completed_steps.append("ai_setup")
