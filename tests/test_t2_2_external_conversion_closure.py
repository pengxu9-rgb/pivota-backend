"""T2-2 — inbound loop closure for external (un-integrated-merchant) conversions.

Proves the closure primitive that turns a Shopify `orders/paid` webhook (with the
T2-1 `pivota_click_id` in note_attributes) into a self-contained `converted`
attribution edge — with NO Pivota `orders` row — and that it is strictly
idempotent on (merchant_id, external_order_id) so a webhook replay never
double-counts GMV. See tier2_v1_build_plan §T2-2 (gaps #3, #4).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import commerce_attribution_service as svc  # noqa: E402


# --- fake DB that emulates the ON CONFLICT (merchant, external_order) guard ----


class FakeDB:
    """Emulates just enough Postgres for the closure path.

    - the click lookup is the only non-str fetch_one → returns ``click_row``.
    - the INSERT ... ON CONFLICT DO NOTHING RETURNING is the only str fetch_one →
      keyed on (merchant_id, external_order_id); a second insert for the same key
      returns None (DO NOTHING) and does NOT overwrite the stored row (no
      double-count).
    """

    def __init__(self, click_row: Optional[Dict[str, Any]] = None) -> None:
        self.click_row = click_row
        self.edges: Dict[tuple, Dict[str, Any]] = {}
        self.insert_attempts = 0

    async def fetch_one(self, query: Any, values: Any = None) -> Optional[Dict[str, Any]]:
        if isinstance(query, str) and "INSERT INTO commerce_attribution_edges" in query:
            self.insert_attempts += 1
            params = dict(values or {})
            key = (params["merchant_id"], params["external_order_id"])
            if key in self.edges:
                return None  # ON CONFLICT DO NOTHING → no RETURNING row
            self.edges[key] = params
            return {"edge_id": params["edge_id"]}
        # SQLAlchemy select() on surface_click_events (the click lookup)
        return self.click_row

    async def fetch_all(self, query: Any, values: Any = None):
        return []

    async def execute(self, query: Any, values: Any = None) -> int:
        return 0


@pytest.fixture(autouse=True)
def _silence_commerce_events(monkeypatch: pytest.MonkeyPatch):
    async def _noop(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"interaction_id": "int_stub"}

    monkeypatch.setattr(svc, "record_commerce_event_best_effort", _noop)


def _click_row(**over: Any) -> Dict[str, Any]:
    base = {
        "click_id": "clk_known",
        "merchant_id": "merch_test",
        "interaction_id": "int_click",
        "surface": "offers.resolve",
        "commerce_surface": "offers.resolve",
        "canonical_product_id": "merch_test|shopify|999",
        "canonical_variant_id": "46123456789",
        "prompt_cluster": "best-serum",
        "source_channel": "chatgpt",
        "source_family": "llm",
        "query_source": "mcp",
        "agent_id": "agent_x",
        "protocol_name": "mcp",
        "llm_provider": "openai",
        "llm_model": "gpt-x",
        "caller_id": "caller_x",
    }
    base.update(over)
    return base


# --- (a) known click_id → converted edge bound to the click's product/merchant --


@pytest.mark.asyncio
async def test_known_click_creates_converted_edge_bound_to_product(monkeypatch):
    fake = FakeDB(click_row=_click_row())
    monkeypatch.setattr(svc, "database", fake)

    result = await svc.close_external_order_conversion(
        merchant_id="merch_test",
        click_id="clk_known",
        external_order_id="6001",
        gross_amount_cents=4999,
        currency="USD",
    )

    assert result["replayed"] is False
    assert result["click_matched"] is True
    assert result["state"] == "converted"
    assert result["canonical_product_id"] == "merch_test|shopify|999"
    assert result["gross_attributed_gmv_cents"] == 4999
    assert result["currency"] == "USD"

    # exactly one stored edge, fully self-contained (no orders row needed)
    assert len(fake.edges) == 1
    stored = next(iter(fake.edges.values()))
    assert stored["merchant_id"] == "merch_test"
    assert stored["external_order_id"] == "6001"
    assert stored["click_id"] == "clk_known"
    assert stored["state"] == "converted"
    assert stored["source"] == "external_redirect"
    assert stored["canonical_product_id"] == "merch_test|shopify|999"
    assert stored["converted_at"] is not None
    # synthetic order_id (order_id is NOT NULL + UNIQUE) is minted, not a real one
    assert stored["order_id"].startswith("ext_")


# --- (b) replay same external_order_id → no second edge, GMV not doubled --------


@pytest.mark.asyncio
async def test_replay_same_external_order_is_idempotent(monkeypatch):
    fake = FakeDB(click_row=_click_row())
    monkeypatch.setattr(svc, "database", fake)

    first = await svc.close_external_order_conversion(
        merchant_id="merch_test", click_id="clk_known",
        external_order_id="6002", gross_amount_cents=8000, currency="USD",
    )
    second = await svc.close_external_order_conversion(
        merchant_id="merch_test", click_id="clk_known",
        external_order_id="6002", gross_amount_cents=8000, currency="USD",
    )

    assert first["replayed"] is False
    assert second["replayed"] is True
    # DB was asked to insert twice, but the guard held → exactly one edge
    assert fake.insert_attempts == 2
    assert len(fake.edges) == 1
    stored = next(iter(fake.edges.values()))
    assert stored["gross_attributed_gmv_cents"] == 8000  # not doubled
    # deterministic keys: both calls target the same edge_id
    assert first["edge_id"] == second["edge_id"]


# --- (c) unknown click_id → recorded UNATTRIBUTED (documented decision), no crash


@pytest.mark.asyncio
async def test_unknown_click_records_unattributed_conversion(monkeypatch):
    fake = FakeDB(click_row=None)  # no matching surface_click_events row
    monkeypatch.setattr(svc, "database", fake)

    result = await svc.close_external_order_conversion(
        merchant_id="merch_test", click_id="clk_ghost",
        external_order_id="6003", gross_amount_cents=1234, currency="USD",
    )

    # DECISION: still record the conversion (GMV is genuinely Pivota-referred —
    # the order carries our minted click id) but unbound to a product.
    assert result["replayed"] is False
    assert result["click_matched"] is False
    assert result["state"] == "converted"
    assert result["gross_attributed_gmv_cents"] == 1234
    stored = next(iter(fake.edges.values()))
    assert stored["merchant_id"] == "merch_test"
    assert stored["click_id"] == "clk_ghost"
    assert stored["canonical_product_id"] is None  # never fabricated


@pytest.mark.asyncio
async def test_click_from_other_merchant_is_not_cross_bound(monkeypatch):
    # A click row exists but belongs to a DIFFERENT merchant → do not cross-bind.
    fake = FakeDB(click_row=_click_row(merchant_id="merch_other"))
    monkeypatch.setattr(svc, "database", fake)

    result = await svc.close_external_order_conversion(
        merchant_id="merch_test", click_id="clk_known",
        external_order_id="6004", gross_amount_cents=500, currency="USD",
    )
    assert result["click_matched"] is False
    stored = next(iter(fake.edges.values()))
    assert stored["merchant_id"] == "merch_test"
    assert stored["canonical_product_id"] is None


@pytest.mark.asyncio
async def test_unscoped_legacy_click_is_adopted(monkeypatch):
    # A thin/legacy click row with NULL merchant is safe to adopt.
    fake = FakeDB(click_row=_click_row(merchant_id=None))
    monkeypatch.setattr(svc, "database", fake)
    result = await svc.close_external_order_conversion(
        merchant_id="merch_test", click_id="clk_known",
        external_order_id="6005", gross_amount_cents=700, currency="USD",
    )
    assert result["click_matched"] is True
    assert result["canonical_product_id"] == "merch_test|shopify|999"


@pytest.mark.asyncio
async def test_missing_keys_skip(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(svc, "database", fake)
    assert await svc.close_external_order_conversion(
        merchant_id="", click_id="clk", external_order_id="x",
        gross_amount_cents=1, currency="USD",
    ) is None
    assert await svc.close_external_order_conversion(
        merchant_id="m", click_id="clk", external_order_id="",
        gross_amount_cents=1, currency="USD",
    ) is None
    assert fake.insert_attempts == 0


# --- (d) currency / amount parsed to cents correctly ---------------------------


def test_shopify_order_total_to_cents_from_price_set():
    data = {
        "total_price": "50.00",
        "currency": "USD",
        "total_price_set": {"shop_money": {"amount": "49.90", "currency_code": "eur"}},
    }
    # price_set (with its own currency) wins over the flat fields
    assert svc.shopify_order_total_to_cents(data) == (4990, "EUR")


def test_shopify_order_total_to_cents_flat_fallback():
    assert svc.shopify_order_total_to_cents({"total_price": "12.34", "currency": "gbp"}) == (1234, "GBP")


def test_shopify_order_total_to_cents_prefers_current_total():
    data = {
        "total_price": "80.00",
        "current_total_price": "72.50",
        "currency": "USD",
    }
    assert svc.shopify_order_total_to_cents(data) == (7250, "USD")


def test_shopify_order_total_to_cents_missing_amount_is_none():
    cents, currency = svc.shopify_order_total_to_cents({"currency": "USD"})
    assert cents is None
    assert currency == "USD"


def test_shopify_order_total_rounds_half_cent():
    assert svc.shopify_order_total_to_cents({"total_price": "9.999"})[0] == 1000


# --- note_attributes extraction -----------------------------------------------


def test_extract_click_id_from_list_shape():
    note = [
        {"name": "gift_note", "value": "hi"},
        {"name": "pivota_click_id", "value": "clk_abc"},
    ]
    assert svc.extract_click_id_from_note_attributes(note) == "clk_abc"


def test_extract_click_id_from_dict_shape():
    assert svc.extract_click_id_from_note_attributes({"pivota_click_id": "clk_xyz"}) == "clk_xyz"


def test_extract_click_id_absent_returns_none():
    assert svc.extract_click_id_from_note_attributes([{"name": "foo", "value": "bar"}]) is None
    assert svc.extract_click_id_from_note_attributes([]) is None
    assert svc.extract_click_id_from_note_attributes(None) is None
    assert svc.extract_click_id_from_note_attributes([{"name": "pivota_click_id", "value": " "}]) is None
