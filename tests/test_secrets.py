from __future__ import annotations

import sys

from quickhatch import secrets


class FakeKeyring:
    def __init__(self):
        self.values = {}

    def set_password(self, service, account, password):
        self.values[(service, account)] = password

    def get_password(self, service, account):
        return self.values.get((service, account))

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


def test_api_key_account_is_scoped_by_provider_and_endpoint():
    a = secrets.api_key_account("api_openai", "https://api.openai.com/v1/")
    b = secrets.api_key_account("api_gemini", "https://api.openai.com/v1/")
    c = secrets.api_key_account("api_openai", "https://example.com/v1")
    assert a != b
    assert a != c
    assert a.endswith("https://api.openai.com/v1")


def test_keyring_roundtrip(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setitem(sys.modules, "keyring", fake)

    result = secrets.store_api_key("api_openai", "https://api.openai.com/v1", "sk-secret")
    assert result.ok is True
    assert secrets.load_api_key("api_openai", "https://api.openai.com/v1") == "sk-secret"
    assert secrets.has_api_key("api_openai", "https://api.openai.com/v1") is True

    deleted = secrets.delete_api_key("api_openai", "https://api.openai.com/v1")
    assert deleted.ok is True
    assert secrets.load_api_key("api_openai", "https://api.openai.com/v1") is None


def test_missing_keyring_degrades_without_crashing(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", None)

    result = secrets.store_api_key("api_openai", "https://api.openai.com/v1", "sk-secret")

    assert result.ok is False
    assert secrets.load_api_key("api_openai", "https://api.openai.com/v1") is None
