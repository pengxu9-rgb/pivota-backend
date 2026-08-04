"""
ACP service-token fail-loud guard (#1296).

During the ACP canary both sides' tokens were the literal `$TOK` (an unexpanded
shell var); because they matched, auth silently "worked". These tests pin the guard
that now rejects that class — an unexpanded shell var, or a weak token in prod —
at the point the token is used.
"""
import sys
from pathlib import Path

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
        validate_service_token("$TOK", label="ACP_SERVICE_TOKEN")


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


# --- receiver (routes/agent_api._require_valid_acp_service_token) -------------
# ADR-021: the outbound senders (routes/platform_orders_acp._resolve_acp_bearer_token
# and services/pivota_acp_client._resolve_bearer_token) went away with the retired
# pivota-acp service. The receiver door is the surviving consumer of the guard.

def test_receiver_missing_token_500(monkeypatch):
    monkeypatch.delenv("ACP_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("ACP_API_KEY", raising=False)
    from routes.agent_api import _require_valid_acp_service_token
    with pytest.raises(HTTPException) as ei:
        _require_valid_acp_service_token()
    assert ei.value.status_code == 500 and "Missing" in str(ei.value.detail)


def test_receiver_rejects_placeholder_token_500(monkeypatch):
    monkeypatch.setenv("ACP_SERVICE_TOKEN", "$TOK")
    from routes.agent_api import _require_valid_acp_service_token
    with pytest.raises(HTTPException) as ei:
        _require_valid_acp_service_token()
    assert ei.value.status_code == 500 and "misconfigured" in str(ei.value.detail)


def test_receiver_returns_valid_token(monkeypatch):
    monkeypatch.setenv("ACP_SERVICE_TOKEN", _STRONG)
    from routes.agent_api import _require_valid_acp_service_token
    assert _require_valid_acp_service_token() == _STRONG
