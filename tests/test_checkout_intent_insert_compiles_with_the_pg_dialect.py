"""POST /agent/v1/checkout/intents writes its intent row: the insert compiles against the real
`checkout_intents` Table with the Postgres dialect the `databases` driver uses in production.

The insert used to name `order_id=`, a column the Table does not define. SQLAlchemy refuses that at
compile time ("Unconsumed column names: order_id"), before any SQL reaches the database, and the
route swallows the error and returns a checkout_url anyway. So no intent row was ever written:
prefill, the read budget and the token-hash binding all had nothing to read. The bound order id
lives in the signed checkout token, which is the only place anything reads it from.

The fake database below compiles every statement exactly as `databases`' Postgres backend does and
records the intent row only when that compile succeeds. The Table is the module's own, never a copy.
All data is placeholder.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("CHECKOUT_UI_KEY", "test-checkout-ui-key")
os.environ.setdefault("CHECKOUT_TOKEN_SECRET", "test-checkout-token-secret")

AGENT = "agent_placeholder"
MERCHANT = "merch_placeholder"
QUOTE = "q_placeholder"
ORDER = "ORD_placeholder"


def _postgres_dialect():
    """The dialect `databases` compiles every query with on a postgresql:// URL (no connection)."""
    from databases.backends.postgres import PostgresBackend

    return PostgresBackend("postgresql://placeholder@localhost/placeholder")._dialect


class _PostgresCompilingDb:
    def __init__(self) -> None:
        self.dialect = _postgres_dialect()
        self.intent_rows: List[Dict[str, Any]] = []
        self.compile_errors: List[str] = []

    async def execute(self, query, values=None):  # noqa: ANN001
        if isinstance(query, str):
            return 1
        try:
            compiled = query.compile(dialect=self.dialect, compile_kwargs={"render_postcompile": True})
        except Exception as exc:  # the route swallows this, so keep it for the assertions
            self.compile_errors.append(f"{type(exc).__name__}: {exc}")
            raise
        if str(compiled).lstrip().upper().startswith("INSERT INTO CHECKOUT_INTENTS"):
            self.intent_rows.append(dict(compiled.params))
        return 1

    async def fetch_one(self, query, values=None):  # noqa: ANN001
        return None


@pytest.fixture
def db(monkeypatch):
    from routes import agent_checkout_intents as intents
    from db.checkout_intents import checkout_intents
    import services.merchant_store_service as merchant_store_service

    # The route must be inserting through the module's own Table.
    assert intents.checkout_intents is checkout_intents

    d = _PostgresCompilingDb()
    monkeypatch.setattr(intents.database, "execute", d.execute)
    monkeypatch.setattr(intents.database, "fetch_one", d.fetch_one)

    async def quote_ok(_quote_id, _context):  # noqa: ANN001
        return {}

    async def load_quote(self, *, quote_id: str):  # noqa: ANN001
        return SimpleNamespace(quote_id=quote_id, merchant_id=MERCHANT)

    async def primary_store(_merchant_id: str):  # noqa: ANN001
        return {"platform": "shopify", "store_id": "store_placeholder"}

    monkeypatch.setattr(intents, "require_quote_for_caller", quote_ok)
    monkeypatch.setattr(intents.QuoteService, "load_active_quote_or_raise", load_quote)
    monkeypatch.setattr(merchant_store_service, "get_primary_store", primary_store)
    return d


@pytest.fixture
def client():
    from routes.agent_auth import get_agent_context
    from routes.agent_checkout_intents import router
    from routes.agent_user_auth import get_agent_user_context

    app = FastAPI()
    app.include_router(router)

    async def key_context(request: Request):  # noqa: ANN001
        return SimpleNamespace(agent_id=AGENT, allowed_merchants=None, can_access_merchant=lambda _m: True)

    async def no_agent_user():  # noqa: ANN001
        return None

    app.dependency_overrides[get_agent_context] = key_context
    app.dependency_overrides[get_agent_user_context] = no_agent_user
    return TestClient(app)


def _items() -> List[Dict[str, Any]]:
    return [{"product_id": "prod_placeholder", "merchant_id": MERCHANT, "quantity": 1}]


def _create(client: TestClient, body: Dict[str, Any]) -> Dict[str, Any]:
    res = client.post("/agent/v1/checkout/intents", headers={"X-API-Key": "k"}, json=body)
    assert res.status_code == 200, res.text
    return res.json()


def _claims(token: str) -> Dict[str, Any]:
    from routes.agent_auth import verify_checkout_token_claims

    return verify_checkout_token_claims(token)


def test_a_cart_intent_writes_its_row(client, db):
    out = _create(client, {"items": _items(), "quote_id": QUOTE, "customer_email": "buyer@example.com"})

    assert db.compile_errors == []
    assert len(db.intent_rows) == 1
    row = db.intent_rows[0]
    assert row["intent_id"] == out["intent_id"]
    assert row["agent_id"] == AGENT
    assert row["checkout_token_hash"]
    assert (row["prefill"] or {}).get("customer_email") == "buyer@example.com"


def test_an_order_resume_intent_writes_its_row_and_keeps_the_order_on_the_token(client, db):
    """The path that sends order_id (v2 checkout-sessions resumes an existing order)."""
    out = _create(client, {"items": _items(), "order_id": ORDER})

    assert db.compile_errors == []
    assert len(db.intent_rows) == 1
    assert db.intent_rows[0]["intent_id"] == out["intent_id"]
    assert "order_id" not in db.intent_rows[0]
    # The bound order is carried, and enforced, by the signed token.
    assert _claims(out["checkout_token"]).get("order_id") == ORDER
    assert out.get("order_id") == ORDER
