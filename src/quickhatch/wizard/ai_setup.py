"""Step 1: Choose AI assistant for the migration."""

import shutil

import questionary

from quickhatch.config import UserProfile
from quickhatch.console import print_info, print_step, print_success, print_warning


def _check_cli(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _check_local_endpoint(endpoint: str) -> bool:
    try:
        import requests

        resp = requests.get(endpoint, timeout=3)
        return resp.status_code < 500
    except Exception:
        return False


def _fetch_ollama_models(endpoint: str) -> list[str]:
    """Thin wrapper preserved for backward compat; real impl is shared."""
    from quickhatch.ai.model_list import fetch_ollama

    return fetch_ollama(endpoint)


def _fetch_api_models(provider_kind: str, endpoint: str, api_key: str) -> list[str]:
    """Thin wrapper preserved for backward compat; real impl is shared.

    The shared helpers live in quickhatch.ai.model_list so the GUI
    server can reuse them from its /wizard/fetch-models endpoint.
    """
    from quickhatch.ai.model_list import fetch

    return fetch(provider_kind, endpoint, api_key)


def _prompt_model_choice(
    provider_label: str,
    provider_kind: str,
    endpoint: str,
    api_key: str,
    fallback_example: str,  # retained for signature compat; unused now
    default: str | None = None,
) -> str | None:
    """Fetch the live model list and force the user to pick from it.

    No free-text fallback: if the fetch fails, we retry (network blip,
    wrong key) rather than let the user type a model id that may not
    actually exist. This makes bad configs surface at setup-time, not
    at the first /chat/completions call 10 minutes into a run.

    Returns the chosen model id, or None if the user aborts.
    """
    del fallback_example  # legacy parameter, ignored
    while True:
        models = _fetch_api_models(provider_kind, endpoint, api_key)
        if models:
            preselect = default if (default and default in models) else None
            return questionary.select(
                f"Pick a {provider_label} model ({len(models)} available):",
                choices=models,
                default=preselect,
            ).ask()
        print_warning(
            f"Could not list {provider_label} models. Most common causes: "
            f"bad API key, no network, wrong base URL, or the account has "
            f"no models enabled. Endpoint tried: {endpoint}"
        )
        retry = questionary.select(
            "What now?",
            choices=[
                questionary.Choice("Retry fetch", value="retry"),
                questionary.Choice("Abort (I'll fix and restart the wizard)", value="abort"),
            ],
        ).ask()
        if retry != "retry":
            return None


def run(profile: UserProfile) -> None:
    print_step(1, "AI Assistant Setup")

    provider = questionary.select(
        "Which AI assistant do you want to use on your new Linux system?",
        choices=[
            questionary.Choice("Claude API (recommended)", value="api_anthropic"),
            questionary.Choice("OpenAI API", value="api_openai"),
            questionary.Choice("Google Gemini API", value="api_gemini"),
            questionary.Choice("xAI Grok API", value="api_grok"),
            questionary.Choice(
                "Custom OpenAI-compatible API (OpenRouter, Together, DeepInfra, …)",
                value="api_custom",
            ),
            questionary.Choice("OpenAI Codex CLI", value="codex"),
            questionary.Choice("Local model - Ollama", value="local_ollama"),
            questionary.Choice("Local model - LM Studio", value="local_lmstudio"),
        ],
    ).ask()

    if provider is None:
        raise KeyboardInterrupt

    # All HTTP-API providers normalize to "api" for the downstream agent
    # code — it reads ai_config.endpoint + model_name + api_key.
    if provider in ("api_anthropic", "api_openai", "api_gemini", "api_grok", "api_custom"):
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
        endpoint = "http://localhost:11434"
        # Loop until a real fetched model is picked or the user aborts.
        # No free-text fallback — typing a model name that isn't pulled
        # would only surface as a 404 on the first agent turn.
        while True:
            print_info(f"Checking local Ollama at {endpoint} …")
            ollama_models = _fetch_ollama_models(endpoint)
            if ollama_models:
                print_success(f"Ollama detected with {len(ollama_models)} model(s) pulled.")
                model = questionary.select(
                    "Which model do you want to use?",
                    choices=ollama_models,
                ).ask()
                if model is None:
                    raise KeyboardInterrupt
                break
            print_warning(
                "Could not reach Ollama, or no models are pulled. Make sure "
                "`ollama serve` is running and you have run "
                "`ollama pull <model>` at least once."
            )
            choice = questionary.select(
                "What now?",
                choices=[
                    questionary.Choice("Retry", value="retry"),
                    questionary.Choice("Abort", value="abort"),
                ],
            ).ask()
            if choice != "retry":
                raise KeyboardInterrupt
        profile.ai_config.model_name = model
        profile.ai_config.endpoint = endpoint

    elif provider == "local_lmstudio":
        endpoint = "http://localhost:1234/v1"
        # Loop until a real loaded model is picked or the user aborts.
        while True:
            print_info(f"Checking LM Studio at {endpoint} …")
            models: list[str] = []
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
                if model is None:
                    raise KeyboardInterrupt
                break
            print_warning(
                "Could not reach LM Studio at localhost:1234, or no models "
                "are loaded. Open LM Studio and load a model first."
            )
            choice = questionary.select(
                "What now?",
                choices=[
                    questionary.Choice("Retry", value="retry"),
                    questionary.Choice("Abort", value="abort"),
                ],
            ).ask()
            if choice != "retry":
                raise KeyboardInterrupt
        profile.ai_config.model_name = model
        profile.ai_config.endpoint = endpoint

    elif provider in ("api_anthropic", "api_openai", "api_gemini", "api_grok", "api_custom"):
        # Provider-specific config: label, docs URL, endpoint, auth-kind,
        # example model, default to preselect. Ask for the key FIRST so
        # we can fetch the live model list from /models.
        # For api_custom the endpoint is unknown up front — we ask.
        if provider == "api_custom":
            print_info(
                "Enter any OpenAI-compatible endpoint — e.g. "
                "https://openrouter.ai/api/v1 , https://api.together.xyz/v1 , "
                "https://api.deepinfra.com/v1/openai , etc. Must end at the "
                "level where /chat/completions is the next path segment."
            )
            while True:
                custom_endpoint = questionary.text(
                    "Base URL:",
                    default="https://openrouter.ai/api/v1",
                ).ask()
                if custom_endpoint is None:
                    raise KeyboardInterrupt
                custom_endpoint = (custom_endpoint or "").strip()
                if custom_endpoint.startswith(("http://", "https://")):
                    break
                print_info("Must start with http:// or https://")
        else:
            custom_endpoint = ""  # unused for the named providers
        _provider_spec = {
            "api_anthropic": {
                "label": "Claude",
                "docs": "https://docs.anthropic.com/en/docs/about-claude/models",
                "endpoint": "https://api.anthropic.com",
                "kind": "anthropic",
                "example": "claude-sonnet-4-5-20250929",
                "default": None,
                "key_prompt": "Enter your Anthropic API key:",
            },
            "api_openai": {
                "label": "OpenAI",
                "docs": "https://platform.openai.com/docs/models",
                # Must include /v1 — the setup agent appends /chat/
                # completions. Bare host misses the versioned path.
                "endpoint": "https://api.openai.com/v1",
                "kind": "openai-compat",
                "example": "gpt-4o-2024-11-20",
                "default": None,
                "key_prompt": "Enter your OpenAI API key:",
            },
            "api_gemini": {
                "label": "Gemini",
                "docs": "https://ai.google.dev/gemini-api/docs/models",
                "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai",
                "kind": "openai-compat",
                "example": "gemini-2.5-pro",
                "default": "gemini-2.5-pro",
                "key_prompt": "Enter your Gemini API key (from aistudio.google.com):",
            },
            "api_grok": {
                "label": "Grok",
                "docs": "https://docs.x.ai/docs/models",
                "endpoint": "https://api.x.ai/v1",
                "kind": "openai-compat",
                "example": "grok-4",
                "default": "grok-4",
                "key_prompt": "Enter your xAI API key (from console.x.ai):",
            },
            "api_custom": {
                "label": "custom",
                "docs": "(endpoint-specific)",
                "endpoint": custom_endpoint,
                "kind": "openai-compat",
                "example": "provider/model-name",
                "default": None,
                "key_prompt": "Enter API key for this endpoint:",
            },
        }[provider]

        print_info(f"Model reference: {_provider_spec['docs']}")

        # API key first — we need it to fetch the model list.
        while True:
            api_key = questionary.password(_provider_spec["key_prompt"]).ask()
            if api_key is None:
                raise KeyboardInterrupt
            api_key = (api_key or "").strip()
            if api_key:
                break
            print_info("API key can't be empty.")
        profile.ai_config.api_key = api_key
        profile.ai_config.endpoint = _provider_spec["endpoint"]

        model = _prompt_model_choice(
            provider_label=_provider_spec["label"],
            provider_kind=_provider_spec["kind"],
            endpoint=_provider_spec["endpoint"],
            api_key=api_key,
            fallback_example=_provider_spec["example"],
            default=_provider_spec["default"],
        )
        if model is None:
            raise KeyboardInterrupt
        profile.ai_config.model_name = model

    print_success(f"AI assistant configured: {provider}")
    profile.completed_steps.append("ai_setup")
