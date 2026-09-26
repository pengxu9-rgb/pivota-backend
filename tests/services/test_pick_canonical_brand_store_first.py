"""The served PDP takes the brand's own store's copy over a retailer listing's (ADR-001 precedence).

Measured 2026-09-26 on prod: 75 content_keys mix retailer-lane listings (ext:retailer:...) with other
rows; in 49 of them the lowest product_key -- a retailer listing -- supplied the served title,
description and images while the brand's own store row sat beside it (westman-atelier.com under
edeniowa.com / shoprescuespa.com, saiehello.com under credobeauty.com, naturium.com under
sokoglam.com). The rows below are shaped like those prod rows.
"""
from __future__ import annotations

import pytest

from services.agent_pdp_view_assembler import fetch_products_for_key, pick_canonical


BRAND_COPY = "A soft, dense brush that presses cream products into the skin for a seamless finish."
RETAILER_COPY = "Westman Atelier's spot check brush, sold here with free shipping over fifty dollars."


def _row(product_key, *, host, brand="Westman Atelier", description=BRAND_COPY, bd=False, primary=True,
         sig="sig_" + "a" * 32, platform="external_seed", image="https://cdn.example/i.jpg"):
    return {"product_key": product_key, "source_domain": host, "brand": brand, "description": description,
            "has_brand_direct_offer": bd, "group_is_primary": primary, "pivota_signature_id": sig,
            "platform": platform, "canonical_url": f"https://{host}/products/x" if host else None,
            "image_url": image}


RETAILER = _row("ext:retailer:3296a977c4bfa327fb14c92fa14", host="edeniowa.com", description=RETAILER_COPY)
BRAND = _row("ext:westman-atelier-spot-check-brush::38", host="westman-atelier.com", bd=True)


def test_the_brand_store_row_beats_a_retailer_listing_that_sorts_first():
    assert RETAILER["product_key"] < BRAND["product_key"]  # the old ladder's tiebreak
    assert pick_canonical([RETAILER, BRAND]) is BRAND
    assert pick_canonical([BRAND, RETAILER]) is BRAND


def test_a_brand_store_proven_by_its_offers_wins_where_the_domain_is_not_the_brand():
    """saiehello.com is not "saie" (Tier A fails) but ingest typed its offers brand_direct (Tier B)."""
    saie = _row("ext:saie-slip-tint-radiant-all-over-conc::1", host="saiehello.com", brand="Saie", bd=True)
    credo = _row("ext:retailer:5d28ef17fe5f410319f86ade26f", host="credobeauty.com", brand="Saie")
    assert pick_canonical([credo, saie]) is saie


def test_a_brand_store_proven_by_its_domain_alone_wins():
    naturium = _row("prod::external_seed::external_seed::ext_n", host="naturium.com", brand="Naturium")
    soko = _row("ext:retailer:a019be8e5d64420b83c5f98e424", host="sokoglam.com", brand="Naturium")
    assert pick_canonical([soko, naturium]) is naturium


@pytest.mark.parametrize("other", [
    # a multi-brand store's row that is not a retailer-lane key (cocomo.sg holds 30 brands this way)
    _row("ext:vely-vely-aqua-sun-cream::1", host="cocomo.sg", brand="VELY VELY"),
    # the brand's own store, but with nothing to serve: it must not replace a retailer's copy with nothing
    _row("prod::external_seed::external_seed::char", host="charlottetilbury.com", brand="Charlotte Tilbury",
         description="", bd=True),
    _row("prod::external_seed::external_seed::chas", host="charlottetilbury.com", brand="Charlotte Tilbury",
         description="   ", bd=True),
    # the serving gate would refuse it (review #2384): a winner under 50 chars or without an image takes the
    # whole product off serving, and an unsigned winner loses the served id
    _row("ext:westman-atelier-lip-brush::56", host="westman-atelier.com", description="Brand serum.", bd=True),
    _row("ext:westman-atelier-lip-brush::57", host="westman-atelier.com", bd=True, image=None),
    _row("ext:westman-atelier-lip-brush::59", host="westman-atelier.com", bd=True, image="  "),
    _row("ext:westman-atelier-lip-brush::58", host="westman-atelier.com", bd=True, sig=None),
    # a known retailer host never counts as the brand's store, whatever its offers say
    _row("ext:westman-atelier-lip-brush::55", host="sephora.com", bd=True),
    # an audit seed is never a brand-store row
    _row("prod::m::url_audit::a", host="westman-atelier.com", bd=True, platform="url_audit"),
    # a caller that loads none of the new fields keeps today's order
    {"product_key": "ext:westman-atelier-blender::1", "group_is_primary": True, "pivota_signature_id": "sig_x"},
])
def test_rows_that_are_not_the_brand_store_keep_todays_order(other):
    retailer = _row("ext:retailer:0000", host="bluemercury.com", brand=other.get("brand"),
                    description=RETAILER_COPY)
    assert pick_canonical([other, retailer]) is retailer


def test_a_group_without_a_retailer_listing_is_ordered_exactly_as_before():
    """No retailer listing -> the brand-store rule does not apply, even to a brand-store row."""
    seed = _row("ext:aaa-other::1", host="cocomo.sg", brand="Westman Atelier")
    assert seed["product_key"] < BRAND["product_key"]
    assert pick_canonical([BRAND, seed]) is seed


def test_the_older_rules_still_order_rows_of_the_same_rank():
    two_brand = _row("ext:westman-atelier-a::1", host="westman-atelier.com", bd=True, primary=False)
    assert pick_canonical([RETAILER, two_brand, BRAND]) is BRAND  # both brand-store rows: primary decides
    other = _row("ext:retailer:1111", host="edeniowa.com", primary=False)
    assert pick_canonical([other, RETAILER]) is RETAILER  # no brand-store row: the old ladder, unchanged


class _FakeDB:
    sql = None

    async def fetch_all(self, sql, params=None):
        self.sql = sql
        return []


@pytest.mark.asyncio
async def test_the_loader_selects_the_fields_the_rule_reads():
    db = _FakeDB()
    await fetch_products_for_key("ck_x", db=db)
    assert "cp.source_domain" in db.sql and "cp.canonical_url" in db.sql and "cp.brand" in db.sql
    assert "cp.description" in db.sql and "cp.image_url" in db.sql and "cp.pivota_signature_id" in db.sql
    assert "offer_type = 'brand_direct'" in db.sql and "o.suppressed_at IS NULL" in db.sql
    assert "AS has_brand_direct_offer" in db.sql
