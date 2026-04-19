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
    """Return local Ollama models via GET /api/tags. Empty on failure."""
    try:
        import requests

        resp = requests.get(f"{endpoint.rstrip('/')}/api/tags", timeout=5)
        if resp.status_code != 200:
            return []
        payload = resp.json() or {}
        models = payload.get("models", [])
        ids = [m.get("name") for m in models if isinstance(m, dict) and m.get("name")]
        return sorted(set(ids))
    except Exception:
        return []


def _fetch_api_models(provider_kind: str, endpoint: str, api_key: str) -> list[str]:
    """Hit the provider's /models endpoint and return the available model ids.

    Returns [] on any failure (network, auth, unexpected shape) so the
    caller can transparently fall back to free-text entry. Provider-kind
    dispatches on the auth-header shape:
      - "anthropic": x-api-key + anthropic-version headers
      - "openai-compat": Bearer auth, used by OpenAI / Gemini / Grok
    """
    try:
        import requests
    except ImportError:
        return []

    try:
        base = endpoint.rstrip("/")
        if provider_kind == "anthropic":
            url = f"{base}/v1/models"
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            }
        else:  # openai-compat
            url = f"{base}/models"
            headers = {"Authorization": f"Bearer {api_key}"}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return []
        payload = resp.json()
        # Both shapes put models under "data" with an "id" per entry.
        data = payload.get("data", []) if isinstance(payload, dict) else []
        ids = [m.get("id") for m in data if isinstance(m, dict) and m.get("id")]
        # Gemini prefixes with "models/" — strip for the OpenAI-compat
        # chat-completions path that expects the bare name.
        ids = [i[7:] if i.startswith("models/") else i for i in ids]
        return sorted(set(ids))
    except Exception:
        return []


def _prompt_model_choice(
    provider_label: str,
    provider_kind: str,
    endpoint: str,
    api_key: str,
    fallback_example: str,
    default: str | None = None,
) -> str | None:
    """Fetch models; on success offer a picker with an 'Other…' fallback.
    On fetch failure, fall back to free-text entry. Returns the chosen
    model id, or None if the user aborts (Ctrl+C).
    """
    models = _fetch_api_models(provider_kind, endpoint, api_key)
    OTHER = "Other… (type model id manually)"
    if models:
        # Try to preselect a sensible default if present in the list.
        preselect = None
        if default and default in models:
            preselect = default
        choice = questionary.select(
            f"Pick a {provider_label} model ({len(models)} available):",
            choices=models + [OTHER],
            default=preselect,
        ).ask()
        if choice is None:
            return None
        if choice != OTHER:
            return choice
        # fall through to free-text
    else:
        print_warning(
            f"Could not list {provider_label} models automatically "
            f"(network down, bad API key, or endpoint unreachable). "
            f"Enter the model id manually."
        )
    while True:
        model = questionary.text(
            f"{provider_label} model id (e.g. {fallback_example}):",
            default=default or "",
        ).ask()
        if model is None:
            return None
        model = (model or "").strip()
        if model:
            return model
        print_info("Please enter a model id.")


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
        print_info("Checking local Ollama at " + endpoint + " …")
        ollama_models = _fetch_ollama_models(endpoint)
        OTHER = "Other… (type model name manually)"
        if ollama_models:
            print_success(f"Ollama detected with {len(ollama_models)} model(s) pulled.")
            choice = questionary.select(
                "Which model do you want to use?",
                choices=ollama_models + [OTHER],
            ).ask()
            if choice is None:
                raise KeyboardInterrupt
            if choice != OTHER:
                model = choice
            else:
                model = None
        else:
            print_warning(
                "Could not reach Ollama at localhost:11434, or no models are "
                "pulled. Make sure Ollama is running (`ollama serve`) and you "
                "have pulled at least one model (`ollama pull llama3`)."
            )
            model = None
        if model is None:
            while True:
                model = questionary.text(
                    "Ollama model name (e.g. llama3, qwen3:32b, deepseek-coder):",
                    default="llama3",
                ).ask()
                if model is None:
                    raise KeyboardInterrupt
                model = (model or "").strip()
                if model:
                    break
                print_info("Please enter a model name.")
        profile.ai_config.model_name = model
        profile.ai_config.endpoint = endpoint

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

        model = None
        OTHER = "Other… (type model name manually)"
        if models:
            print_success(f"LM Studio detected with {len(models)} model(s) loaded.")
            choice = questionary.select(
                "Which model do you want to use?",
                choices=models + [OTHER],
            ).ask()
            if choice is None:
                raise KeyboardInterrupt
            if choice != OTHER:
                model = choice
        else:
            print_warning(
                "Could not reach LM Studio at localhost:1234, or no models are loaded. "
                "Make sure LM Studio is running with a model loaded."
            )
        if model is None:
            while True:
                model = questionary.text(
                    "LM Studio model name:",
                    default="local-model",
                ).ask()
                if model is None:
                    raise KeyboardInterrupt
                model = (model or "").strip()
                if model:
                    break
                print_info("Please enter a model name.")
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
