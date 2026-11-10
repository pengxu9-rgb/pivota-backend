"""A new self-serve agent key starts with NO merchant scope, and `[]` means none everywhere (S-02).

`agents.allowed_merchants` NULL means EVERY merchant. Self-serve registration used to leave it NULL,
so one unauthenticated call minted a key scoped to all merchants. It now writes `[]`, and every
reader must treat `[]` as "no merchant":

- `AgentContext.can_access_merchant` (quote, v2 quote read, per-merchant routes): False for all;
- GET /agent/v1/merchants: empty for every scope;
- GET /agent/v1/products/search: no products, no catalogue read (it used to fall back to
  cross-merchant search, i.e. `[]` read as "unrestricted"), and a named merchant is a 403;
- POST /agent/internal/products/search: no products, no fast-mode read.

NULL stays "all" for existing rows: the meaning of NULL is not changed here.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.agent_auth import AgentContext, get_agent_context, has_no_merchant_scope


def _ctx(allowed: Any, agent_id: str = "agent_scope_test") -> AgentContext:
    return AgentContext({"agent_id": agent_id, "agent_name": "t", "allowed_merchants": allowed}, request=None)


# ── registration writes [] ─────────────────────────────────────────────────────────────────────


class _RegistrationDb:
    def __init__(self) -> None:
        self.executed: List[tuple] = []

    async def fetch_one(self, query, values=None):
        q = " ".join(str(query).split())
        if "to_regclass('public.api_keys')" in q:
            return {"api_keys_table": "api_keys", "agent_api_keys_table": None}
        if q.startswith("SELECT id FROM users"):
            return None
        if q.startswith("INSERT INTO users"):
            return {"id": 91}
        return None

    async def execute(self, query, values=None):
        self.executed.append((" ".join(str(query).split()), dict(values or {})))
        return None


def test_a_new_self_serve_registration_writes_an_empty_merchant_list(monkeypatch):
    import db.agents as agents_db
    import routes.agent_account as module

    monkeypatch.setattr(module, "_REGISTRATION_IP_LIMIT_STORE", {})
    monkeypatch.delenv("AGENT_SELF_SERVE_REGISTRATION_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_REGISTRATION_NOTIFY_WEBHOOK_URL", raising=False)
    fake = _RegistrationDb()
    monkeypatch.setattr(module.database, "fetch_one", fake.fetch_one)
    monkeypatch.setattr(module.database, "execute", fake.execute)
    monkeypatch.setattr(agents_db, "IS_POSTGRES", True)
    monkeypatch.setattr(agents_db, "_AGENT_AUTH_KEY_TABLE_MODE", "auto")
    monkeypatch.setattr(agents_db, "_AGENT_AUTH_KEY_TABLE_CACHE", {"table": None, "expires_at": 0.0})

    async def _no_membership(**_kwargs):
        return None

    monkeypatch.setattr(module, "_sync_agent_auth_membership", _no_membership)
    app = FastAPI()
    app.include_router(module.router)
    resp = TestClient(app).post(
        "/agent/account/register",
        json={"email": "scope-none@example.com", "password": "longenough1", "agent_name": "Scope None"},
    )
    assert resp.status_code == 200, resp.text

    [(sql, values)] = [(q, v) for q, v in fake.executed if q.startswith("INSERT INTO agents")]
    columns = sql.split("(", 1)[1].split(")", 1)[0].replace(" ", "").split(",")
    assert "allowed_merchants" in columns
    assert ":allowed_merchants" in sql
    # JSON '[]', never NULL (NULL = every merchant).
    assert values["allowed_merchants"] is not None
    assert json.loads(values["allowed_merchants"]) == []

    # Read back the way the auth door reads it: an empty list, and no merchant is accessible.
    ctx = _ctx(values["allowed_merchants"])
    assert ctx.allowed_merchants == []
    assert not ctx.can_access_merchant("merch_any") and not ctx.can_access_merchant("external_seed")
    assert has_no_merchant_scope(ctx)


def test_null_still_means_every_merchant_and_a_list_still_means_those():
    assert _ctx(None).can_access_merchant("merch_any") and not has_no_merchant_scope(_ctx(None))
    listed = _ctx(["merch_a"])
    assert listed.can_access_merchant("merch_a") and not listed.can_access_merchant("merch_b")
    assert not has_no_merchant_scope(listed)
    assert has_no_merchant_scope(_ctx([])) and has_no_merchant_scope(_ctx("[]"))
    assert has_no_merchant_scope(type("C", (), {"allowed_merchants": ["", "  "]})())
    assert not has_no_merchant_scope(object())


# ── GET /agent/v1/products/search ──────────────────────────────────────────────────────────────


@pytest.fixture
def search_client(monkeypatch):
    try:
        from main import app
    except Exception:  # pragma: no cover - environment-specific
        pytest.skip("main app import failed in this environment")
    import routes.agent_api as agent_api

    reads: List[str] = []

    async def _no_read(*args, **kwargs):
        reads.append("db")
        raise AssertionError("an agent with no merchant scope must not reach the catalogue")

    monkeypatch.setattr(agent_api.database, "fetch_all", _no_read)
    monkeypatch.setattr(agent_api.database, "fetch_one", _no_read, raising=False)
    monkeypatch.setattr(agent_api, "_load_external_seed_products_with_cache", _no_read)
    monkeypatch.setattr(agent_api, "_search_products_fast_mode", _no_read)
    monkeypatch.setattr(agent_api, "log_agent_request", AsyncMock(return_value=None))
    state: Dict[str, Any] = {"ctx": _ctx([]), "reads": reads}
    app.dependency_overrides[get_agent_context] = lambda: state["ctx"]
    try:
        yield TestClient(app), state
    finally:
        app.dependency_overrides.pop(get_agent_context, None)


@pytest.mark.parametrize("params", [
    {"query": "sunscreen", "search_all_merchants": "true"},
    {"query": "sunscreen"},
    {"search_all_merchants": "true"},  # browse mode: the products_cache fast path
    {"query": "sunscreen", "fast_mode": "true", "search_all_merchants": "true"},
    {"query": "sunscreen", "external_seed_only": "true"},
])
def test_search_with_an_empty_allowlist_returns_nothing_and_reads_nothing(search_client, params):
    client, state = search_client
    resp = client.get("/agent/v1/products/search", params={"limit": 10, **params})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["products"] == []
    assert body["pagination"]["total_count"] == 0 and body["pagination"]["has_more"] is False
    assert body["metadata"]["reason_code"] == "no_merchant_scope"
    assert body["search_context"]["cross_merchant_search"] is False
    assert state["reads"] == []


@pytest.mark.parametrize("params", [
    {"merchant_id": "merch_a", "query": "x"},
    {"merchant_id": "external_seed", "query": "x"},
    {"merchant_ids": ["merch_a"], "query": "x"},
])
def test_search_naming_a_merchant_with_an_empty_allowlist_is_403(search_client, params):
    client, state = search_client
    assert client.get("/agent/v1/products/search", params=params).status_code == 403
    assert state["reads"] == []


def test_search_with_null_scope_still_searches(search_client):
    """NULL is unchanged: the request goes past the guard and reaches the catalogue."""
    client, state = search_client
    state["ctx"] = _ctx(None)
    client.get("/agent/v1/products/search", params={"query": "sunscreen", "search_all_merchants": "true"})
    assert state["reads"], "a NULL-scope agent must still reach the search"


# ── POST /agent/internal/products/search ───────────────────────────────────────────────────────


def test_internal_search_with_an_empty_allowlist_returns_nothing(monkeypatch):
    import routes.agent_api as agent_api
    import routes.agent_internal_products as internal

    calls: List[Any] = []

    async def _fast(**kwargs):
        calls.append(kwargs)
        return {"products": [{"product_id": "p1"}], "total": 1}

    monkeypatch.setattr(agent_api, "_search_products_fast_mode", _fast)
    app = FastAPI()
    app.include_router(internal.router)
    ctx = {"value": _ctx([])}
    app.dependency_overrides[get_agent_context] = lambda: ctx["value"]
    client = TestClient(app)

    resp = client.post("/agent/internal/products/search", json={"query": "sunscreen"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["products"] == [] and resp.json()["metadata"]["reason_code"] == "no_merchant_scope"
    assert calls == []
    assert client.post("/agent/internal/products/search", json={"query": "x", "merchant_id": "m"}).status_code == 403

    # NULL scope is unchanged: the primitive runs.
    ctx["value"] = _ctx(None)
    resp = client.post("/agent/internal/products/search", json={"query": "sunscreen"})
    assert resp.status_code == 200 and len(calls) == 1


# ── GET /agent/v1/merchants ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("scope", ["available", "mine", "protocol"])
async def test_merchants_with_an_empty_allowlist_is_empty_for_every_scope(monkeypatch, scope):
    import routes.agent_sdk_fixed as sdk
    from services import agent_merchant_directory as directory

    async def _no_read(*args, **kwargs):
        raise AssertionError("no merchant read for an agent with no merchant scope")

    monkeypatch.setattr(sdk.database, "fetch_all", _no_read)
    monkeypatch.setattr(directory, "merchant_facts", _no_read)
    app = FastAPI()
    app.include_router(sdk.router)
    app.dependency_overrides[get_agent_context] = lambda: _ctx([])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.get("/agent/v1/merchants", params={"scope": scope})
    assert resp.status_code == 200, resp.text
    assert resp.json()["merchants"] == []


# ── POST /agent/v1/quotes/preview and the v2 quote door ────────────────────────────────────────


@pytest.mark.parametrize("merchant_id", ["merch_a", "external_seed"])
def test_quote_with_an_empty_allowlist_is_403_before_any_pricing(monkeypatch, merchant_id):
    import routes.quote_routes as quote_routes

    priced: List[Any] = []

    class _NoService:
        async def preview_quote(self, **kwargs):
            priced.append(kwargs)
            raise AssertionError("no quote for an agent with no merchant scope")

    monkeypatch.setattr(quote_routes, "QuoteService", _NoService)
    app = FastAPI()
    app.include_router(quote_routes.router)
    app.dependency_overrides[get_agent_context] = lambda: _ctx([])
    resp = TestClient(app).post("/agent/v1/quotes/preview", json={
        "merchant_id": merchant_id, "items": [{"product_id": "p", "variant_id": "v", "quantity": 1}],
    })
    assert resp.status_code == 403
    assert priced == []
