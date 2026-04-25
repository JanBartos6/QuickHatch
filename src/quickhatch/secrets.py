"""Best-effort OS credential storage for local QuickHatch secrets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quickhatch.config import AIConfig

SERVICE_NAME = "QuickHatch"


@dataclass(frozen=True)
class SecretStoreResult:
    ok: bool
    message: str = ""


def _normalise_endpoint(endpoint: str | None) -> str:
    return (endpoint or "").strip().rstrip("/")


def api_key_account(provider: str | None, endpoint: str | None) -> str:
    """Stable keyring account name for one provider/endpoint pair."""
    return f"api-key:{(provider or 'unknown').strip()}:{_normalise_endpoint(endpoint)}"


def _load_keyring():
    try:
        import keyring  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - platform/import dependent
        return None, f"OS credential storage is unavailable: {exc}"
    return keyring, ""


def keyring_available() -> bool:
    keyring, _ = _load_keyring()
    return keyring is not None


def store_api_key(
    provider: str | None, endpoint: str | None, api_key: str | None
) -> SecretStoreResult:
    key = (api_key or "").strip()
    if not key:
        return SecretStoreResult(False, "No API key provided.")
    keyring, error = _load_keyring()
    if keyring is None:
        return SecretStoreResult(False, error)
    try:
        keyring.set_password(SERVICE_NAME, api_key_account(provider, endpoint), key)
    except Exception as exc:  # pragma: no cover - backend dependent
        return SecretStoreResult(False, f"Could not save API key in OS credential storage: {exc}")
    return SecretStoreResult(True, "API key saved in OS credential storage.")


def load_api_key(provider: str | None, endpoint: str | None) -> str | None:
    keyring, _ = _load_keyring()
    if keyring is None:
        return None
    try:
        return keyring.get_password(SERVICE_NAME, api_key_account(provider, endpoint)) or None
    except Exception:  # pragma: no cover - backend dependent
        return None


def delete_api_key(provider: str | None, endpoint: str | None) -> SecretStoreResult:
    keyring, error = _load_keyring()
    if keyring is None:
        return SecretStoreResult(False, error)
    try:
        keyring.delete_password(SERVICE_NAME, api_key_account(provider, endpoint))
    except Exception as exc:  # pragma: no cover - backend dependent
        return SecretStoreResult(False, f"Could not delete stored API key: {exc}")
    return SecretStoreResult(True, "Stored API key deleted.")


def has_api_key(provider: str | None, endpoint: str | None) -> bool:
    return bool(load_api_key(provider, endpoint))


def load_api_key_for_config(config: AIConfig) -> str | None:
    if not config.provider:
        return None
    return load_api_key(config.provider, config.endpoint)
