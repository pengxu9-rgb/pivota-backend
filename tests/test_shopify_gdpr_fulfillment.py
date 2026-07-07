"""Fix #4 — Shopify GDPR compliance handlers fulfill obligations (not log-and-200).

Drives the shared _fulfill_shopify_gdpr_request against a fake DB and asserts:
  - customers/redact issues an anonymizing UPDATE on orders (RETURNING) and scrubs
    webhook-event payloads, records status=completed with counts;
  - customers/data_request exports order metadata into the audit row, no PII logged;
  - the shopify_gdpr_requests audit row is always written.
Also unit-tests the pure helpers (_redacted_email, _scrub_webhook_event_payload).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db.database as db_database  # noqa: E402
import routes.webhook_routes as wr  # noqa: E402


class _FakeGdprDB:
    def __init__(self, *, order_rows: List[Dict[str, Any]], event_rows: List[Dict[str, Any]]):
        self.order_rows = order_rows
        self.event_rows = event_rows
        self.executed: List[Dict[str, Any]] = []
        self.fetched: List[Dict[str, Any]] = []

    async def fetch_all(self, query: Any, values: Any = None):
        q = " ".join(str(query).split())
        self.fetched.append({"q": q, "v": values})
        if "UPDATE orders" in q and "RETURNING order_id" in q:
            # Simulate all order_rows matching the redaction predicate.
            return [{"order_id": r["order_id"]} for r in self.order_rows]
        if "SELECT" in q and "FROM orders" in q:
            return list(self.order_rows)
        if "FROM pcs_shopify_webhook_events" in q:
            return list(self.event_rows)
        return []

    async def execute(self, query: Any, values: Any = None):
        self.executed.append({"q": " ".join(str(query).split()), "v": values})
        return 0


@pytest.fixture
def fake_db(monkeypatch):
    db = _FakeGdprDB(
        order_rows=[
            {
                "order_id": "ord_1",
                "shopify_order_id": "555",
                "created_at": "2026-07-01T00:00:00Z",
                "total": "49.99",
                "currency": "USD",
                "status": "paid",
                "payment_status": "paid",
            }
        ],
        event_rows=[
            {"id": 1, "payload_json": {"id": 555, "email": "jane@example.com", "total_price": "49.99"}},
        ],
    )
    monkeypatch.setattr(db_database, "database", db, raising=False)
    # The handler does `from db.database import database` at call time, so patching
    # the module attribute is sufficient.
    return db


async def test_customers_redact_anonymizes_and_records(fake_db):
    data = {
        "shop_domain": "teststore.myshopify.com",
        "customer": {"id": 42, "email": "jane@example.com"},
        "orders_to_redact": [555],
    }
    out = await wr._fulfill_shopify_gdpr_request(
        merchant_id="merch_1",
        shop_domain="teststore.myshopify.com",
        topic="customers/redact",
        data=data,
    )
    assert out["status"] == "completed"
    assert out["resolution"]["orders_redacted"] >= 1
    assert out["resolution"]["webhook_events_scrubbed"] == 1

    # An anonymizing UPDATE ran with the tombstone email + REDACTED name + NULL address.
    updates = [e for e in fake_db.fetched if "UPDATE orders" in e["q"]]
    assert updates, "expected an UPDATE orders redaction query"
    v = updates[0]["v"]
    assert v["redacted_name"] == "[REDACTED]"
    assert v["redacted_email"].startswith("redacted+") and v["redacted_email"].endswith("@redacted.invalid")

    # A shopify_gdpr_requests audit row was written.
    audit = [e for e in fake_db.executed if "shopify_gdpr_requests" in e["q"]]
    assert audit, "expected a shopify_gdpr_requests insert"
    assert audit[0]["v"]["status"] == "completed"

    # Webhook event was scrubbed (UPDATE ... payload_json).
    scrub = [e for e in fake_db.executed if "pcs_shopify_webhook_events SET payload_json" in e["q"]]
    assert scrub, "expected a webhook-event scrub UPDATE"


async def test_customers_data_request_exports_no_pii_logged(fake_db):
    data = {"shop_domain": "teststore.myshopify.com", "customer": {"id": 42, "email": "jane@example.com"}}
    out = await wr._fulfill_shopify_gdpr_request(
        merchant_id="merch_1",
        shop_domain="teststore.myshopify.com",
        topic="customers/data_request",
        data=data,
    )
    assert out["status"] == "completed"
    assert out["resolution"]["orders_found"] == 1
    export = out["resolution"]["export"]
    assert export[0]["shopify_order_id"] == "555"
    assert export[0]["total"] == "49.99"
    # Export must NOT contain raw customer email/name (only order metadata).
    assert "email" not in export[0]
    assert "customer_email" not in export[0]


def test_redacted_email_is_stable_nonnull_tombstone():
    a = wr._redacted_email("Jane@Example.com")
    b = wr._redacted_email("jane@example.com")
    assert a == b  # case-insensitive, stable
    assert a.startswith("redacted+") and a.endswith("@redacted.invalid")
    assert wr._redacted_email(None) == "redacted+00000000@redacted.invalid"


def test_scrub_webhook_event_payload_removes_pii():
    p = {
        "id": 1,
        "email": "x@y.com",
        "customer": {"email": "x@y.com"},
        "shipping_address": {"zip": "00000"},
        "total_price": "10.00",
        "line_items": [{"sku": "S1", "destination_location": {"zip": "1"}}],
    }
    out = wr._scrub_webhook_event_payload(p)
    assert "email" not in out
    assert "customer" not in out
    assert "shipping_address" not in out
    assert out["total_price"] == "10.00"
    assert "destination_location" not in out["line_items"][0]
    assert out["pii_stripped"] is True
