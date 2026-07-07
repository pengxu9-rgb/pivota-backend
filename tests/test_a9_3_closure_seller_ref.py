"""A9-3 (ADR-009 D3) — seller-keyed conversion subjects in T2-2 closure.

Proves that when the matched click carries a seller_ref (threaded by T2-1 from
external_product_seeds.seller_ref):
  - the edge SUBJECT (merchant_id) becomes seller_ref, not the converting merchant;
  - the seller check UPGRADES from raw-host compare to IDENTITY compare (converting
    tenant merchant == seller_ref → counted; else seller_mismatch=true → excluded);
  - a custom-storefront-domain seed no longer false-mismatches (the A9-1 limitation,
    inverted): identity match holds even when converting host != click dest host;
  - cross-seed closure via the seller's own webhook legitimately closes and stays
    idempotent under the seller_ref subject;
  - a click WITHOUT seller_ref keeps the A9-1 host-compare byte-identically and
    stamps seller_ref_missing=true (A9-4 kill metric).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import commerce_attribution_service as svc  # noqa: E402


class FakeDB:
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
                return None
            self.edges[key] = params
            return {"edge_id": params["edge_id"]}
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


def _click_row(*, seller_ref=None, seed_kind=None, merchant_id="merch_anchor", dest_domain=None, **over):
    ctx: Dict[str, Any] = {}
    if seller_ref:
        ctx["seller_ref"] = seller_ref
    if seed_kind:
        ctx["seed_kind"] = seed_kind
    base = {
        "click_id": "clk_known",
        "merchant_id": merchant_id,
        "interaction_id": "int_click",
        "surface": "offers.resolve",
        "commerce_surface": "offers.resolve",
        "canonical_product_id": "merch_anchor|shopify|999",
        "canonical_variant_id": "46123456789",
        "dest_domain": dest_domain,
        "context": ctx,
    }
    base.update(over)
    return base


def _md(fake: FakeDB) -> Dict[str, Any]:
    return json.loads(next(iter(fake.edges.values()))["metadata"])


# --- self seed: subject unchanged, identity match, counted --------------------


@pytest.mark.asyncio
async def test_self_seed_identity_match_subject_is_seller_and_counted(monkeypatch):
    # seller_ref == converting merchant (self): match, subject == seller_ref (== the
    # converting merchant), no mismatch, seller_ref/seed_kind stamped.
    fake = FakeDB(click_row=_click_row(seller_ref="merch_self", seed_kind="self"))
    monkeypatch.setattr(svc, "database", fake)
    result = await svc.close_external_order_conversion(
        merchant_id="merch_self", click_id="clk_known",
        external_order_id="7001", gross_amount_cents=4999, currency="USD",
        converting_shop_domain="self-store.myshopify.com",
    )
    assert result["seller_mismatch"] is False
    assert result["merchant_id"] == "merch_self"      # subject == seller_ref
    assert result["seller_ref"] == "merch_self"
    assert result["seed_kind"] == "self"
    assert result["seller_ref_missing"] is False
    stored = next(iter(fake.edges.values()))
    assert stored["merchant_id"] == "merch_self"      # edge subject keyed by seller
    md = _md(fake)
    assert md["seller_ref"] == "merch_self"
    assert md["converting_merchant_id"] == "merch_self"
    assert "seller_mismatch" not in md
    assert "seller_ref_missing" not in md


# --- cross seed: subject = seller_ref, identity match via the seller's webhook --


@pytest.mark.asyncio
async def test_cross_seed_subject_is_seller_ref_not_converting_anchor(monkeypatch):
    # Click's merchant_id is the ANCHOR (merch_anchor). The sale happened on seller
    # B's own store; B's webhook authenticates as merch_B == seller_ref → adopted
    # (despite anchor != converting) and counted with subject = seller_ref.
    fake = FakeDB(click_row=_click_row(seller_ref="merch_B", seed_kind="cross", merchant_id="merch_anchor"))
    monkeypatch.setattr(svc, "database", fake)
    result = await svc.close_external_order_conversion(
        merchant_id="merch_B", click_id="clk_known",
        external_order_id="7002", gross_amount_cents=8000, currency="USD",
        converting_shop_domain="seller-b.myshopify.com",
    )
    assert result["click_matched"] is True
    assert result["seller_mismatch"] is False
    assert result["merchant_id"] == "merch_B"          # SUBJECT = seller, not anchor
    stored = next(iter(fake.edges.values()))
    assert stored["merchant_id"] == "merch_B"
    assert _md(fake)["converting_merchant_id"] == "merch_B"


# --- A9-1 limitation, inverted: custom domain no longer false-mismatches -------


@pytest.mark.asyncio
async def test_custom_domain_storefront_no_longer_false_mismatches(monkeypatch):
    # The A9-1 failure case: the seed's destination is a custom domain
    # (brand.com) but the converting webhook authenticates the myshopify domain
    # (brand.myshopify.com). Under raw-host compare that FALSE-mismatched. Under
    # A9-3 identity compare (converting merchant == seller_ref) it MATCHES.
    fake = FakeDB(click_row=_click_row(
        seller_ref="merch_brand", seed_kind="self", dest_domain="brand.com",
    ))
    monkeypatch.setattr(svc, "database", fake)
    result = await svc.close_external_order_conversion(
        merchant_id="merch_brand", click_id="clk_known",
        external_order_id="7003", gross_amount_cents=4999, currency="USD",
        converting_shop_domain="brand.myshopify.com",   # host != dest host, but same seller
    )
    assert result["seller_mismatch"] is False           # NOT flagged
    md = _md(fake)
    assert "seller_mismatch" not in md
    assert md["converting_shop_domain"] == "brand.myshopify.com"  # forensics only


# --- cross seed via the WRONG merchant → identity mismatch, excluded -----------


@pytest.mark.asyncio
async def test_identity_mismatch_is_flagged_excluded_and_warned(monkeypatch, caplog):
    fake = FakeDB(click_row=_click_row(seller_ref="merch_B", seed_kind="cross"))
    monkeypatch.setattr(svc, "database", fake)
    with caplog.at_level(logging.WARNING, logger="commerce_attribution_service"):
        result = await svc.close_external_order_conversion(
            merchant_id="merch_A",  # a DIFFERENT converting merchant than the seller
            click_id="clk_known",
            external_order_id="7004", gross_amount_cents=4999, currency="USD",
            converting_shop_domain="merch-a.myshopify.com",
        )
    assert result["seller_mismatch"] is True
    assert result["merchant_id"] == "merch_B"           # subject still the seller
    md = _md(fake)
    assert md["seller_mismatch"] is True
    assert md["converting_merchant_id"] == "merch_A"
    assert any(
        "external_conversion_seller_mismatch" in r.message and r.levelno == logging.WARNING
        for r in caplog.records
    )


# --- cross-seed replay stays idempotent under the seller_ref subject ----------


@pytest.mark.asyncio
async def test_cross_seed_replay_idempotent_under_seller_subject(monkeypatch):
    fake = FakeDB(click_row=_click_row(seller_ref="merch_B", seed_kind="cross"))
    monkeypatch.setattr(svc, "database", fake)
    first = await svc.close_external_order_conversion(
        merchant_id="merch_B", click_id="clk_known",
        external_order_id="7005", gross_amount_cents=5000, currency="USD",
        converting_shop_domain="seller-b.myshopify.com",
    )
    second = await svc.close_external_order_conversion(
        merchant_id="merch_B", click_id="clk_known",
        external_order_id="7005", gross_amount_cents=5000, currency="USD",
        converting_shop_domain="seller-b.myshopify.com",
    )
    assert first["replayed"] is False and second["replayed"] is True
    assert fake.insert_attempts == 2
    assert len(fake.edges) == 1                          # guard held under seller subject
    assert first["edge_id"] == second["edge_id"]
    assert next(iter(fake.edges.values()))["gross_attributed_gmv_cents"] == 5000


# --- legacy (no seller_ref): host compare byte-identical + seller_ref_missing --


@pytest.mark.asyncio
async def test_legacy_no_seller_ref_keeps_host_compare_and_stamps_missing(monkeypatch):
    # Click carries NO seller_ref (pre-A9-4). Subject stays converting merchant;
    # host compare (converting == dest) still counts; seller_ref_missing stamped.
    # Legacy binding is merchant-scoped, so the click belongs to merch_test.
    fake = FakeDB(click_row=_click_row(merchant_id="merch_test", dest_domain="teststore.myshopify.com"))
    monkeypatch.setattr(svc, "database", fake)
    result = await svc.close_external_order_conversion(
        merchant_id="merch_test", click_id="clk_known",
        external_order_id="7006", gross_amount_cents=4999, currency="USD",
        converting_shop_domain="teststore.myshopify.com",
    )
    assert result["seller_mismatch"] is False
    assert result["merchant_id"] == "merch_test"        # subject == converting (legacy)
    assert result["seller_ref_missing"] is True
    md = _md(fake)
    assert md["seller_ref_missing"] is True
    assert md["click_dest_domain"] == "teststore.myshopify.com"   # host compare ran
    assert "seller_ref" not in md


@pytest.mark.asyncio
async def test_legacy_host_mismatch_still_flagged(monkeypatch):
    # Legacy path host mismatch behaves exactly as A9-1.
    fake = FakeDB(click_row=_click_row(merchant_id="merch_test", dest_domain="brand-a.myshopify.com"))
    monkeypatch.setattr(svc, "database", fake)
    result = await svc.close_external_order_conversion(
        merchant_id="merch_test", click_id="clk_known",
        external_order_id="7007", gross_amount_cents=4999, currency="USD",
        converting_shop_domain="seller-b.myshopify.com",
    )
    assert result["seller_mismatch"] is True
    assert result["seller_ref_missing"] is True
