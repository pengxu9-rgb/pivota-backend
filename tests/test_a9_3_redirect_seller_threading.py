"""A9-3 (ADR-009 D3) — seller_ref/seed_kind threading through the T2-1 redirect.

Proves the chain the closure depends on, link by link:
  1. `_external_seed_redirect_identity` reads the seed row's stored
     seller_ref/seed_kind (write-time derived — the hot path never mints);
  2. `_make_external_redirect_url` stamps them into the signed token ctx
     alongside the ANCHOR merchant_id (the anchor stays a separate dimension);
  3. `record_surface_event` persists the ctx — seller_ref included — into
     `surface_click_events.context` (JSONB), which is exactly where
     `close_external_order_conversion._seed_seller_from_click` reads it back;
  4. legacy seeds (NULL seller_ref) thread through as ABSENT keys, never a
     defaulted 'self' (founder no-fallback directive).

See docs/adr/ADR-009-seller-of-record-identity.md §D3 and
docs/IDENTITY_REFERENCE.md §4.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

import pytest

import routes.agent_shop_gateway as gateway
from services import commerce_attribution_service as svc
from services.commerce_attribution_service import PVT_CLICK_ID
from services.outbound_links_service import parse_and_verify_redirect_token


def _decode_redirect(redirect_url: str) -> dict:
    token = parse_qs(urlparse(redirect_url).query)["token"][0]
    return parse_and_verify_redirect_token(token)


# --- 1. seed row → redirect identity -------------------------------------------


def test_redirect_identity_reads_stored_seller_ref_and_kind():
    identity = gateway._external_seed_redirect_identity(
        row={
            "attached_product_key": "merch_anchor|shopify|999",
            "domain": "brand.com",
            "seller_ref": "merch_obs_cafecafecafecafe",
            "seed_kind": "cross",
        },
        seed_data={},
    )
    assert identity["merchant_id"] == "merch_anchor"          # anchor unchanged
    assert identity["seller_ref"] == "merch_obs_cafecafecafecafe"
    assert identity["seed_kind"] == "cross"


def test_redirect_identity_legacy_null_seller_is_none_not_self():
    # Pre-A9-4 row: seller_ref/seed_kind NULL → None, NEVER a defaulted 'self'.
    identity = gateway._external_seed_redirect_identity(
        row={"attached_product_key": "merch_anchor|shopify|999", "domain": "brand.com"},
        seed_data={},
    )
    assert identity["seller_ref"] is None
    assert identity["seed_kind"] is None


# --- 2. redirect identity → signed token ctx ------------------------------------


@pytest.mark.asyncio
async def test_token_ctx_carries_seller_ref_alongside_anchor():
    redirect_url = await gateway._make_external_redirect_url(
        market="US",
        tool="offers.resolve",
        destination_url="https://brand.com/products/widget",
        utm_template=None,
        ctx={"seedId": "seed_1"},
        allowed_domains=["brand.com"],
        merchant_id="merch_anchor",             # the ANCHOR (surface dimension)
        product_id="merch_anchor|shopify|999",
        variant_id=None,
        shop_domain="brand.com",
        platform=None,
        seller_ref="merch_obs_cafecafecafecafe",  # the SELLER (conversion subject)
        seed_kind="cross",
        cart_variant_id=None,  # this test is about the token ctx, not the permalink
    )
    assert redirect_url is not None
    ctx = _decode_redirect(redirect_url)["ctx"]
    assert ctx["merchant_id"] == "merch_anchor"
    assert ctx["seller_ref"] == "merch_obs_cafecafecafecafe"
    assert ctx["seed_kind"] == "cross"
    assert ctx[PVT_CLICK_ID].startswith("clk_")


@pytest.mark.asyncio
async def test_token_ctx_omits_seller_keys_for_legacy_seed():
    redirect_url = await gateway._make_external_redirect_url(
        market="US",
        tool="offers.resolve",
        destination_url="https://brand.com/products/widget",
        utm_template=None,
        ctx={"seedId": "seed_legacy"},
        allowed_domains=["brand.com"],
        merchant_id="merch_anchor",
        product_id=None,
        variant_id=None,
        shop_domain="brand.com",
        platform=None,
        seller_ref=None,
        seed_kind=None,
        cart_variant_id=None,
    )
    assert redirect_url is not None
    ctx = _decode_redirect(redirect_url)["ctx"]
    # ABSENT, not defaulted — closure sees no seller_ref and stamps
    # seller_ref_missing (the A9-4 kill metric), never an assumed 'self'.
    assert "seller_ref" not in ctx
    assert "seed_kind" not in ctx


# --- 3. token ctx → surface_click_events.context --------------------------------


class _CaptureDB:
    """Captures the surface_click_events INSERT that record_surface_event emits."""

    def __init__(self) -> None:
        self.inserted: Optional[Dict[str, Any]] = None

    async def fetch_one(self, query: Any, values: Any = None):
        return None  # no existing click row → the INSERT path

    async def execute(self, query: Any, values: Any = None):
        params = dict(query.compile().params)
        self.inserted = params
        return 1


@pytest.mark.asyncio
async def test_click_row_context_persists_seller_ref(monkeypatch):
    fake = _CaptureDB()
    monkeypatch.setattr(svc, "database", fake)

    async def _noop_event(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"interaction_id": "int_stub"}

    monkeypatch.setattr(svc, "record_commerce_event_best_effort", _noop_event)
    monkeypatch.setattr(svc, "record_traffic_taxonomy", lambda **_: None)

    token_payload = {
        "tool": "offers.resolve",
        "dest": "https://brand.com/products/widget?pvt_click=clk_a93test",
        "dest_domain": "brand.com",
        "ctx": {
            "seedId": "seed_1",
            PVT_CLICK_ID: "clk_a93test",
            "merchant_id": "merch_anchor",
            "seller_ref": "merch_obs_cafecafecafecafe",
            "seed_kind": "cross",
        },
    }
    await svc.record_surface_event(
        token_payload=token_payload,
        request_meta={"user_agent": "ua", "ip": "127.0.0.1"},
        event_type="click",
    )
    assert fake.inserted is not None
    assert fake.inserted["click_id"] == "clk_a93test"
    assert fake.inserted["merchant_id"] == "merch_anchor"     # anchor on the column
    context = fake.inserted["context"]
    # The whole ctx lands in the JSONB context — including the seller keys the
    # closure reads back via _seed_seller_from_click.
    assert context["seller_ref"] == "merch_obs_cafecafecafecafe"
    assert context["seed_kind"] == "cross"
    seller_ref, seed_kind = svc._seed_seller_from_click({"context": context})
    assert (seller_ref, seed_kind) == ("merch_obs_cafecafecafecafe", "cross")


@pytest.mark.asyncio
async def test_click_row_context_legacy_has_no_seller_keys(monkeypatch):
    fake = _CaptureDB()
    monkeypatch.setattr(svc, "database", fake)

    async def _noop_event(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"interaction_id": "int_stub"}

    monkeypatch.setattr(svc, "record_commerce_event_best_effort", _noop_event)
    monkeypatch.setattr(svc, "record_traffic_taxonomy", lambda **_: None)

    await svc.record_surface_event(
        token_payload={
            "tool": "offers.resolve",
            "dest": "https://brand.com/p",
            "ctx": {"seedId": "seed_legacy", PVT_CLICK_ID: "clk_legacy", "merchant_id": "merch_anchor"},
        },
        request_meta={},
        event_type="click",
    )
    context = fake.inserted["context"]
    assert "seller_ref" not in context and "seed_kind" not in context
    assert svc._seed_seller_from_click({"context": context}) == (None, None)


# --- 4. _seed_seller_from_click driver-agnostic JSON coercion --------------------


def test_seed_seller_from_click_handles_json_string_and_garbage():
    # asyncpg returns dicts; the SQLite/JSON path can hand back a string.
    as_string = '{"seller_ref": "merch_B", "seed_kind": "self"}'
    assert svc._seed_seller_from_click({"context": as_string}) == ("merch_B", "self")
    assert svc._seed_seller_from_click({"context": "not-json"}) == (None, None)
    assert svc._seed_seller_from_click({"context": None}) == (None, None)
    assert svc._seed_seller_from_click(None) == (None, None)
