"""Live model-list fetchers for every provider QuickHatch supports.

Shared between the TUI wizard (quickhatch.wizard.ai_setup) and the GUI
model-fetch endpoint. The signatures return plain lists of model-id
strings or [] on any failure — callers should treat an empty list as
'show a retry prompt, don't let the user type a model name'.
"""

from __future__ import annotations

# Whitelist of valid provider kinds. Any other value submitted to
# /wizard/fetch-models or `fetch()` is rejected by the dispatcher.
VALID_KINDS = frozenset({"anthropic", "openai-compat", "ollama"})

# Whitelist of valid provider labels (the value of ai_config.provider).
# Used by the agent code to decide which API shape to call. Keep in
# sync with the wizard tile lists in ai_setup.py and gui/app.html.
HOSTED_API_PROVIDERS = frozenset(
    {"api_anthropic", "api_openai", "api_gemini", "api_grok", "api_custom"}
)
LOCAL_PROVIDERS = frozenset({"local_ollama", "local_lmstudio", "codex"})
VALID_PROVIDERS = HOSTED_API_PROVIDERS | LOCAL_PROVIDERS


def is_anthropic(provider: str | None) -> bool:
    """True iff the provider is the Anthropic API specifically.

    Replaces the historical `"anthropic" in endpoint` substring hack,
    which mis-routed any custom endpoint with 'anthropic' in its
    hostname through api.anthropic.com (a credential leak).
    """
    return provider == "api_anthropic"


def chat_completions_url(provider: str | None, endpoint: str | None) -> str:
    """Build the OpenAI-compatible /chat/completions URL for ``provider``.

    Handles the Ollama special case: the wizard stores
    ``http://localhost:11434`` (no /v1), but Ollama's OpenAI-compat
    endpoint is at ``/v1/chat/completions``. Append /v1 when missing
    so the historical Ollama 404 doesn't bite.

    For Anthropic this returns the messages URL — callers should
    almost always check is_anthropic() first and use the messages
    body shape, but having the getter centralized prevents drift.
    """
    if is_anthropic(provider):
        return f"{(endpoint or 'https://api.anthropic.com').rstrip('/')}/v1/messages"
    base = (endpoint or "").rstrip("/")
    if provider == "local_ollama" and not base.endswith("/v1"):
        base = base + "/v1"
    return f"{base}/chat/completions"


def fetch_openai_compat(endpoint: str, api_key: str | None) -> list[str]:
    """OpenAI-compatible /models endpoint with Bearer auth.

    Covers OpenAI, Gemini (via /v1beta/openai/models), Grok, OpenRouter,
    Together, DeepInfra, LM Studio, anything that exposes the standard
    list shape.
    """
    try:
        import requests
    except ImportError:
        return []
    try:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        resp = requests.get(f"{endpoint.rstrip('/')}/models", headers=headers, timeout=10)
        if resp.status_code != 200:
            return []
        payload = resp.json()
        data = payload.get("data", []) if isinstance(payload, dict) else []
        ids = [m.get("id") for m in data if isinstance(m, dict) and m.get("id")]
        # Gemini prefixes ids with "models/" — strip so the OpenAI-compat
        # chat-completions path receives the bare name.
        ids = [i[7:] if i.startswith("models/") else i for i in ids]
        return sorted(set(ids))
    except Exception:
        return []


def fetch_anthropic(endpoint: str, api_key: str) -> list[str]:
    """Anthropic's /v1/models uses x-api-key + anthropic-version headers."""
    try:
        import requests
    except ImportError:
        return []
    try:
        url = f"{endpoint.rstrip('/')}/v1/models"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return []
        payload = resp.json()
        data = payload.get("data", []) if isinstance(payload, dict) else []
        ids = [m.get("id") for m in data if isinstance(m, dict) and m.get("id")]
        return sorted(set(ids))
    except Exception:
        return []


def fetch_ollama(endpoint: str) -> list[str]:
    """Ollama lists pulled models via /api/tags (different shape from OpenAI)."""
    try:
        import requests
    except ImportError:
        return []
    try:
        resp = requests.get(f"{endpoint.rstrip('/')}/api/tags", timeout=5)
        if resp.status_code != 200:
            return []
        payload = resp.json() or {}
        models = payload.get("models", [])
        ids = [m.get("name") for m in models if isinstance(m, dict) and m.get("name")]
        return sorted(set(ids))
    except Exception:
        return []


def fetch(kind: str, endpoint: str, api_key: str | None) -> list[str]:
    """Dispatch to the right helper by provider kind.

    kind:
      'anthropic'     — Anthropic (x-api-key auth)
      'openai-compat' — OpenAI, Gemini, Grok, custom OpenAI-compat hosts
      'ollama'        — local Ollama server
    """
    if kind == "anthropic":
        return fetch_anthropic(endpoint, api_key or "")
    if kind == "ollama":
        return fetch_ollama(endpoint)
    return fetch_openai_compat(endpoint, api_key)


__all__ = ["fetch", "fetch_openai_compat", "fetch_anthropic", "fetch_ollama"]
