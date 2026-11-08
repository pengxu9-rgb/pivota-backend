"""The Pivota-issued card rail is REFUSED in production, whatever its env says (S-16).

`POST /agent/v1/cards`, `GET /agent/v1/cards/{id}`, `POST /webhooks/reap` and
`POST /webhooks/reap/authorize` exist only for Pivota-minted cards, which are out of policy (Pivota
never holds money). They stay mounted, but in production (config.platform.is_production) every one
answers 404 before auth, body parsing, signature checks or any issuer/DB call, with every flag and
secret they read set. Staging and local keep their behaviour.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.agent_cards as cards_mod
import routes.reap_webhooks as webhooks_mod
from routes.agent_auth import get_agent_context

SECRET = "whsec_test_prod_refusal_123"
AUTH_SECRET = "whsec_test_prod_refusal_auth_456"
SIG_HEADER = "x-reap-webhook-signature"

_PLATFORM_ENV_KEYS = (
    "PIVOTA_ENV", "PIVOTA_PLATFORM", "RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME",
    "K_SERVICE", "K_REVISION", "K_CONFIGURATION", "CLOUD_RUN_JOB", "CLOUD_RUN_EXECUTION",
)
_PRODUCTION_SHAPES = {
    "pivota_env": {"PIVOTA_ENV": "production"},
    "cloud_run_unresolved": {"K_SERVICE": "pivota-backend", "K_REVISION": "pivota-backend-00001-abc"},
    "railway": {"RAILWAY_ENVIRONMENT": "production"},
}
_CARD_PATHS = {"/agent/v1/cards", "/agent/v1/cards/{card_id}"}
_WEBHOOK_PATHS = {"/webhooks/reap", "/webhooks/reap/authorize"}


def _sign(raw: bytes, secret: str) -> str:
    t = str(int(time.time()))
    mac = hmac.new(secret.encode(), t.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


class _Ctx:
    agent_id = "agent_prod_refusal"
    allowed_merchants = None

    def can_access_merchant(self, merchant_id: str) -> bool:
        return True


@pytest.fixture
def calls(monkeypatch) -> List[str]:
    """Every flag and secret ON; every downstream call recorded. A refusal must reach none."""
    for key in _PLATFORM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENT_CARD_ISSUANCE_ENABLED", "1")
    monkeypatch.setenv("CARD_ISSUER", "mock")
    monkeypatch.setenv("REAP_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("REAP_EXTERNAL_AUTH_ENABLED", "true")
    monkeypatch.setenv("REAP_AUTH_WEBHOOK_SECRET", AUTH_SECRET)
    seen: List[str] = []

    def _record(name, result=None):
        async def _fake(*args, **kwargs):
            seen.append(name)
            return result
        return _fake

    monkeypatch.setattr(cards_mod, "resolve_issuer", lambda: seen.append("resolve_issuer") or None)
    monkeypatch.setattr(cards_mod, "resolve_merchant_quote", _record("resolve_merchant_quote"))
    monkeypatch.setattr(cards_mod, "create_card_guarded", _record("create_card_guarded"))
    monkeypatch.setattr(cards_mod, "get_card", _record("get_card"))
    monkeypatch.setattr(webhooks_mod, "record_event_once", _record("record_event_once", True))
    monkeypatch.setattr(webhooks_mod, "find_by_issuer_ref", _record("find_by_issuer_ref"))
    monkeypatch.setattr(webhooks_mod, "decide", _record("decide"))
    return seen


def _app(*, authed: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(cards_mod.router)
    app.include_router(webhooks_mod.router)
    if authed:
        app.dependency_overrides[get_agent_context] = lambda: _Ctx()
    return TestClient(app)


def _webhook(client: TestClient, path: str, secret: str):
    raw = json.dumps({"id": "evt_1", "type": "CARD_AUTHORIZATION_APPROVED", "data": {"cardId": "rc_1"}}).encode()
    return client.post(path, content=raw, headers={"content-type": "application/json", SIG_HEADER: _sign(raw, secret)})


@pytest.mark.parametrize("shape", sorted(_PRODUCTION_SHAPES))
@pytest.mark.parametrize("authed", [True, False])
def test_in_production_every_card_route_is_404_with_its_flags_on(monkeypatch, calls, shape, authed):
    for key, value in _PRODUCTION_SHAPES[shape].items():
        monkeypatch.setenv(key, value)
    client = _app(authed=authed)
    assert client.post("/agent/v1/cards", json={"merchant_domain": "shop.example.com", "checkout_id": "chk_1"}).status_code == 404
    # A body the route would reject (extra field) is still a 404: the refusal runs before validation.
    assert client.post("/agent/v1/cards", json={"amount": 1}).status_code == 404
    assert client.get("/agent/v1/cards/crd_1").status_code == 404
    assert calls == []


@pytest.mark.parametrize("shape", sorted(_PRODUCTION_SHAPES))
def test_in_production_both_issuer_webhooks_are_404_even_correctly_signed(monkeypatch, calls, shape):
    for key, value in _PRODUCTION_SHAPES[shape].items():
        monkeypatch.setenv(key, value)
    client = _app()
    assert _webhook(client, "/webhooks/reap", SECRET).status_code == 404
    assert _webhook(client, "/webhooks/reap/authorize", AUTH_SECRET).status_code == 404
    assert calls == []


@pytest.mark.parametrize("env", [None, "staging", "development"])
def test_outside_production_the_card_routes_answer_as_before(monkeypatch, calls, env):
    if env:
        monkeypatch.setenv("PIVOTA_ENV", env)
    client = _app()
    # Flag on, no issuer (patched to None): the route body runs and answers its own 503.
    r = client.post("/agent/v1/cards", json={"merchant_domain": "shop.example.com", "checkout_id": "chk_1"})
    assert r.status_code == 503 and r.json()["detail"] == "no card issuer is configured"
    assert calls == ["resolve_issuer"]
    monkeypatch.setenv("AGENT_CARD_ISSUANCE_ENABLED", "0")
    assert client.post("/agent/v1/cards", json={"merchant_domain": "shop.example.com", "checkout_id": "chk_1"}).status_code == 503
    assert client.get("/agent/v1/cards/crd_1").status_code == 404 and calls[-1] == "get_card"


@pytest.mark.parametrize("env", [None, "staging"])
def test_outside_production_the_webhooks_answer_as_before(monkeypatch, calls, env):
    if env:
        monkeypatch.setenv("PIVOTA_ENV", env)
    client = _app()
    # A wrong signature is the receivers' own 401, not a 404: the route body ran.
    assert _webhook(client, "/webhooks/reap", "whsec_wrong").status_code == 401
    assert _webhook(client, "/webhooks/reap/authorize", "whsec_wrong").status_code == 401
    monkeypatch.setenv("REAP_EXTERNAL_AUTH_ENABLED", "false")
    assert _webhook(client, "/webhooks/reap/authorize", AUTH_SECRET).status_code == 503
    monkeypatch.delenv("REAP_WEBHOOK_SECRET")
    assert _webhook(client, "/webhooks/reap", SECRET).status_code == 503


def test_the_real_app_mounts_every_card_and_issuer_route_behind_the_refusal():
    """main.app, not a test app: each mounted route carries the production refusal as a dependency."""
    from main import app

    found: Dict[str, Any] = {}
    for route in app.routes:
        path = getattr(route, "path", None)
        if path in _CARD_PATHS | _WEBHOOK_PATHS:
            names = [getattr(d.call, "__name__", "") for d in route.dependant.dependencies]
            found[path] = names
    assert set(found) == _CARD_PATHS | _WEBHOOK_PATHS
    for path, names in found.items():
        assert names and names[0].startswith("refuse_in_production_"), (path, names)
