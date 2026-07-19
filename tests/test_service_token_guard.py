"""
ACP service-token fail-loud guard (#1296).

During the ACP canary both sides' tokens were the literal `$TOK` (an unexpanded
shell var); because they matched, auth silently "worked". These tests pin the guard
that now rejects that class — an unexpanded shell var, or a weak token in prod —
at the point the token is used, in both the sender and the receiver.
"""
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from utils.service_token import _MIN_PROD_TOKEN_LEN, validate_service_token  # noqa: E402

_STRONG = "a" * 40  # a plausible strong secret (>= the prod minimum)


# --- validator ----------------------------------------------------------------

def test_accepts_strong_token(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    validate_service_token(_STRONG, label="X")  # no raise


def test_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        validate_service_token("   ", label="X")


def test_rejects_unexpanded_shell_var():
    with pytest.raises(ValueError, match="unexpanded shell"):
        validate_service_token("$TOK", label="PLATFORM_ORDERS_ACP_TOKEN")


def test_rejects_braced_shell_var():
    with pytest.raises(ValueError, match="unexpanded shell"):
        validate_service_token("${TOK}", label="X")


def test_prod_rejects_short_token(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(ValueError, match="too short"):
        validate_service_token("test", label="X")
    assert len("test") < _MIN_PROD_TOKEN_LEN


def test_non_prod_allows_short_dev_fallback(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    validate_service_token("test", label="X")  # dev/staging fallback stays allowed


# --- sender (routes/platform_orders_acp._resolve_acp_bearer_token) ------------

def test_sender_rejects_placeholder_token(monkeypatch):
    from routes import platform_orders_acp as mod
    monkeypatch.setattr(mod, "settings", SimpleNamespace(platform_orders_acp_token="$TOK"))
    with pytest.raises(HTTPException) as ei:
        mod._resolve_acp_bearer_token()
    assert ei.value.status_code == 503
    assert "misconfigured" in str(ei.value.detail)


def test_sender_returns_valid_token(monkeypatch):
    from routes import platform_orders_acp as mod
    monkeypatch.setattr(mod, "settings", SimpleNamespace(platform_orders_acp_token=_STRONG))
    assert mod._resolve_acp_bearer_token() == _STRONG


# --- receiver wiring (routes/agent_api ACP handler) ---------------------------

def test_receiver_wires_the_guard():
    import routes.agent_api as m
    src = inspect.getsource(m)
    assert 'validate_service_token(service_token, label="ACP_SERVICE_TOKEN")' in src, \
        "the ACP receiver must validate its service token"
