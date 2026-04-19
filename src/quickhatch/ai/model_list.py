"""Live model-list fetchers for every provider QuickHatch supports.

Shared between the TUI wizard (quickhatch.wizard.ai_setup) and the GUI
model-fetch endpoint. The signatures return plain lists of model-id
strings or [] on any failure — callers should treat an empty list as
'show a retry prompt, don't let the user type a model name'.
"""

from __future__ import annotations


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
