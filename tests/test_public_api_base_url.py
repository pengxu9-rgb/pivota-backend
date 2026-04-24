from config.settings import (
    DEFAULT_PUBLIC_API_BASE_URL,
    resolve_public_api_base_url,
)


PUBLIC_API_ENV_KEYS = (
    "PUBLIC_API_BASE_URL",
    "PUBLIC_BASE_URL",
    "APP_URL",
    "BASE_URL",
)


def _clear_public_api_env(monkeypatch):
    for key in PUBLIC_API_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_resolve_public_api_base_url_uses_canonical_default(monkeypatch):
    _clear_public_api_env(monkeypatch)

    assert resolve_public_api_base_url() == DEFAULT_PUBLIC_API_BASE_URL


def test_resolve_public_api_base_url_ignores_legacy_backend_urls(monkeypatch):
    _clear_public_api_env(monkeypatch)
    monkeypatch.setenv("PUBLIC_API_BASE_URL", "https://web-production-fedb.up.railway.app")
    monkeypatch.setenv("APP_URL", "https://pivota-backend-production.up.railway.app")
    monkeypatch.setenv("BASE_URL", "https://web-production-fedb.up.railway.app")

    assert resolve_public_api_base_url() == DEFAULT_PUBLIC_API_BASE_URL


def test_resolve_public_api_base_url_uses_valid_public_override(monkeypatch):
    _clear_public_api_env(monkeypatch)
    monkeypatch.setenv("PUBLIC_API_BASE_URL", "https://api.example.test/")

    assert resolve_public_api_base_url() == "https://api.example.test"
