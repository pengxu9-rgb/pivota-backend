"""A brand-official storefront's seed is 'self', not 'cross'.

2026-09-28: all 3,540 catalog_row_trust rows shadowed IDENTITY_LIVE_READ_DISABLED were merch_obs_
sellers with seed_kind='cross'; 1,937 of them were the brand's own storefront written by the
retailer-ingest drain as source_role=brand_official (tartecosmetics.com 354, stilacosmetics.com 106,
tower28beauty.com 29, ...). Path C attaches every seed to a synthetic pk_<hash>, so no tenant anchor
ever owned the domain and derive_seed_seller filed the brand's own store as 'cross' -- which strips
the observed-seller identity-coverage exemption in catalog_trust_policy and leaves the row shadow
until someone promotes live-read by hand. Every positive case has a refusing twin.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from services import curated_brand_feed as feed
from services import seller_identity as si
from services.catalog_enrichment_agent import apply as writer
from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl
from tests.services.test_partial_apply_skip_reasons import CatalogDB, real_resolver  # noqa: F401 -- fixture

_REAL_DERIVE_FOR_PLAN_ROW = writer._derive_seed_seller_for_plan_row


async def _mint(**_: Any) -> str:
    return "merch_obs_0123456789abcdef"


async def _derive(**kwargs: Any):
    return await si.derive_seed_seller(
        anchor_merchant_id=None, brand="Tarte", source_system="test", **kwargs,
    )


# --- derive_seed_seller -------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("destination,own_store", [
    ("tartecosmetics.com", "tartecosmetics.com"),
    ("https://www.tartecosmetics.com/products/shape-tape", "tartecosmetics.com"),
    # a market subdomain store and its own seeds reduce to the same registrable domain
    ("us.frankbody.com", "us.frankbody.com"),
])
async def test_seed_on_the_brand_official_storefront_is_self(monkeypatch, destination, own_store):
    monkeypatch.setattr(si, "ensure_observed_seller", _mint)
    assert await _derive(destination_domain=destination, brand_official_domain=own_store) == (
        "merch_obs_0123456789abcdef", "self")


@pytest.mark.asyncio
@pytest.mark.parametrize("destination,own_store", [
    ("tartecosmetics.com", None),                 # no brand-official row: today's CROSS
    ("ulta.com", "tartecosmetics.com"),           # the row's offer on a retailer host
    ("sephora.com", "sephora.com"),               # a known retailer is never a brand's own store
    ("tartecosmetics.co.uk", "tartecosmetics.com"),  # another registrable domain, even the same brand
])
async def test_seed_off_the_brand_official_storefront_stays_cross(monkeypatch, destination, own_store):
    monkeypatch.setattr(si, "ensure_observed_seller", _mint)
    assert await _derive(destination_domain=destination, brand_official_domain=own_store) == (
        "merch_obs_0123456789abcdef", "cross")


@pytest.mark.asyncio
async def test_no_destination_is_still_null_even_on_a_brand_official_row(monkeypatch):
    monkeypatch.setattr(si, "ensure_observed_seller", _mint)
    assert await _derive(destination_domain=None, brand_official_domain="tartecosmetics.com") == (None, None)


# --- the real producer: feed record -> ingest plan -> apply -> seed upsert ----------------------


def _plan(domain: str, *, role: str) -> Dict[str, Any]:
    rec = feed.shopify_product_to_record(
        {"id": 8101, "vendor": "Tarte", "title": "Shape Tape Concealer", "handle": "shape-tape",
         "product_type": "Concealer", "body_html": "<p>Ingredients: Water, Glycerin</p>",
         "images": [{"src": f"https://cdn.{domain}/shape-tape.jpg"}],
         "variants": [{"id": 8102, "price": "32.00", "available": True, "sku": "ST1"}]},
        domain=domain, category_path="beauty", brand_override="Tarte", currency="USD", source_role=role,
        retailer_name=domain if role == "retailer" else None, emit_native_variants=True)
    return ingest_validated_jsonl([rec])


class SeedCapturingDB(CatalogDB):
    def __init__(self) -> None:
        super().__init__()
        self.seed_kinds: List[Any] = []

    async def execute(self, query, values=None):
        if "INSERT INTO external_product_seeds" in query:
            self.seed_kinds += [v for k, v in (values or {}).items() if k.startswith("seed_kind")]
        return await super().execute(query, values)


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_mode", [False, True])
@pytest.mark.parametrize("domain,role,expected", [
    ("tartecosmetics.com", "brand_official", "self"),
    ("ulta.com", "retailer", "cross"),
])
async def test_apply_stamps_seed_kind_from_the_rows_source_role(
    real_resolver, monkeypatch, batch_mode, domain, role, expected,  # noqa: F811
):
    # real_resolver stubs the seller derivation out; put the real one back, minting only the seller id.
    monkeypatch.setattr(writer, "_derive_seed_seller_for_plan_row", _REAL_DERIVE_FOR_PLAN_ROW)
    monkeypatch.setattr(si, "ensure_observed_seller", _mint)
    plan = _plan(domain, role=role)
    assert plan["seeds"], "the producer must plan at least one seed"
    db = SeedCapturingDB()

    counts = await writer.apply_ingest_plan(plan, batch_label="t", db=db, batch=batch_mode)

    assert counts["seeds"] >= 1
    assert db.seed_kinds and set(db.seed_kinds) == {expected}


def test_brand_official_storefronts_maps_only_brand_official_rows():
    official = _plan("tartecosmetics.com", role="brand_official")["pdps"][0]
    retailer = _plan("ulta.com", role="retailer")["pdps"][0]
    assert writer._brand_official_storefronts([official, retailer]) == {
        official["product_key"]: "tartecosmetics.com"}
