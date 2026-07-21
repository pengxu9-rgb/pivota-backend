"""Capture of the StyleKorean review + INCI signals through the ingest path.

Task 2 (reviews): JSON-LD `aggregateRating` -> extract_product ->
to_validated_record.pdp -> ingestion.rating_value/rating_count columns.
Task 3 (INCI): Next.js RSC `allIngredients` (retailer) + Shopify body_html
(brand-official) -> beauty_sku_ingredients write intent at ingest.

All fixtures are inline strings — no live network. Both signals are OPTIONAL:
absent on the page -> null / skipped, never fabricated, never blocks ingest.
"""

from __future__ import annotations

import json

import pytest

from services.catalog_enrichment_agent import apply as apply_mod  # noqa: E402
from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl  # noqa: E402
from services.curated_brand_feed import (  # noqa: E402
    inci_from_body_html,
    shopify_product_to_record,
)
from services.external_offers_service import _parse_aggregate_rating  # noqa: E402
from services.retailer_ingest import stylekorean as sk  # noqa: E402
from services.retailer_ingest.sitemap_crawler import (  # noqa: E402
    extract_inci_from_next_rsc,
    extract_product,
)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

def _ld_block(with_rating: bool) -> str:
    obj = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "COSRX Advanced Snail 96 Mucin Power Essence",
        "brand": {"@type": "Brand", "name": "COSRX"},
        "description": "A lightweight snail essence.",
        "image": ["https://img.example/snail.jpg"],
        "offers": {
            "@type": "Offer",
            "price": "25.00",
            "priceCurrency": "USD",
            "availability": "https://schema.org/InStock",
        },
    }
    if with_rating:
        obj["aggregateRating"] = {
            "@type": "AggregateRating",
            "ratingValue": "4.3",
            "reviewCount": 215,
            "bestRating": 5,
        }
    return '<script type="application/ld+json">' + json.dumps(obj) + "</script>"


def _next_rsc_chunk(inci: str | None) -> str:
    """Build a Next.js RSC push chunk. When inci is given, the flight carries an
    `allIngredients` reference resolved to a text row of the matching length."""
    if inci is None:
        flight = '3:{"title":"COSRX Essence","allIngredients":""}\n'
    else:
        flight = (
            '3:{"title":"COSRX Essence","allIngredients":"$51"}\n'
            f"51:T{format(len(inci), 'x')},{inci}\n52:something-else\n"
        )
    body = json.dumps(flight)[1:-1]  # JS-string body, exactly the push payload shape
    return f'<script>self.__next_f.push([1,"{body}"])</script>'


_INCI = "Water, Glycerin, Butylene Glycol, Sodium Hyaluronate, Betaine, Panthenol"


def _pdp_html(*, with_rating: bool, inci: str | None) -> str:
    return "<html><head>" + _ld_block(with_rating) + "</head><body>" + _next_rsc_chunk(inci) + "</body></html>"


# --------------------------------------------------------------------------
# Task 2 — aggregateRating parse (present vs absent)
# --------------------------------------------------------------------------

def test_parse_aggregate_rating_present_and_absent():
    assert _parse_aggregate_rating(
        {"aggregateRating": {"ratingValue": "4.3", "reviewCount": 215}}
    ) == (4.3, 215)
    # ratingCount is the schema.org fallback when reviewCount is absent
    assert _parse_aggregate_rating(
        {"aggregateRating": {"ratingValue": 5, "ratingCount": "7"}}
    ) == (5.0, 7)
    assert _parse_aggregate_rating({"name": "no rating here"}) == (None, None)
    assert _parse_aggregate_rating({"aggregateRating": {"ratingValue": "n/a"}}) == (None, None)


def test_extract_product_captures_rating_when_present():
    out = extract_product(_pdp_html(with_rating=True, inci=_INCI))
    assert out is not None
    assert out["rating_value"] == 4.3
    assert out["rating_count"] == 215


def test_extract_product_rating_null_when_absent():
    out = extract_product(_pdp_html(with_rating=False, inci=_INCI))
    assert out is not None
    assert out["rating_value"] is None
    assert out["rating_count"] is None
    # still a valid extraction (title/price intact) — a missing rating never drops the product
    assert out["title"] and out["price_raw"] == "25.00"


# --------------------------------------------------------------------------
# Task 3 — INCI extraction (RSC retailer + Shopify body_html), present vs absent
# --------------------------------------------------------------------------

def test_extract_inci_from_next_rsc_present():
    inci = extract_inci_from_next_rsc(_next_rsc_chunk(_INCI))
    assert inci == _INCI


def test_extract_inci_from_next_rsc_strips_step_label():
    labelled = "COSRX Snail Kit (1step): " + _INCI
    inci = extract_inci_from_next_rsc(_next_rsc_chunk(labelled))
    assert inci == _INCI  # the leading product-name label is dropped


def test_extract_inci_from_next_rsc_absent():
    assert extract_inci_from_next_rsc(_next_rsc_chunk(None)) is None
    assert extract_inci_from_next_rsc("<html><body>no rsc here</body></html>") is None


def test_inci_from_body_html_present_and_absent():
    body = (
        "<p>A gentle serum.</p><p><strong>Ingredients:</strong> "
        + _INCI
        + "</p><p>How to use: apply daily.</p>"
    )
    assert inci_from_body_html(body) == _INCI
    # prose blurb with no ingredient label -> None
    assert inci_from_body_html("<p>The best serum you will ever try.</p>") is None
    assert inci_from_body_html(None) is None


# --------------------------------------------------------------------------
# to_validated_record round-trip (retailer lane)
# --------------------------------------------------------------------------

def test_to_validated_record_carries_rating_and_inci():
    crawled = {
        "url": "https://www.stylekorean.com/product/cosrx-snail-essence/1234",
        "brand": "COSRX",
        "title": "COSRX Advanced Snail 96 Mucin Power Essence",
        "description": "A lightweight snail essence.",
        "image_url": "https://img.example/snail.jpg",
        "price_raw": "25.00",
        "currency": "USD",
        "availability": "in_stock",
        "rating_value": 4.3,
        "rating_count": 215,
        "raw_inci": _INCI,
    }
    rec = sk.to_validated_record(crawled)
    pdp = rec["pdp"]
    assert pdp["rating_value"] == 4.3
    assert pdp["rating_count"] == 215
    assert pdp["raw_inci"] == _INCI
    assert pdp["inci_source"] == "reseller_listing"  # StyleKorean is a SUPPLIER (ADR-001)
    # offer economics untouched
    off = rec["offers"][0]
    assert off["list_price"] == 25.0 and off["currency"] == "USD" and off["availability"] == "in_stock"


def test_to_validated_record_nulls_when_signals_absent():
    crawled = {
        "url": "https://www.stylekorean.com/product/x/9",
        "brand": "COSRX",
        "title": "Something",
        "price_raw": "10.00",
        "currency": "USD",
        "availability": "unknown",
    }
    pdp = sk.to_validated_record(crawled)["pdp"]
    assert pdp["rating_value"] is None and pdp["rating_count"] is None
    assert pdp["raw_inci"] is None


# --------------------------------------------------------------------------
# Brand-official mint lane (Shopify) — INCI recoverable, ratings no-op
# --------------------------------------------------------------------------

def test_shopify_record_captures_inci_and_no_rating():
    product = {
        "title": "Snail Mucin Essence",
        "handle": "snail-mucin-essence",
        "vendor": "COSRX",
        "body_html": "<p>Hydrating.</p><p>Ingredients: " + _INCI + "</p>",
        "variants": [{"price": "25.00", "available": True, "barcode": "8809416470016"}],
        "images": [{"src": "https://img.example/s.jpg"}],
        "tags": ["essence"],
    }
    rec = shopify_product_to_record(product, domain="cosrx.com", category_path="beauty/skincare")
    assert rec["pdp"]["raw_inci"] == _INCI
    assert rec["pdp"]["inci_source"] == "brand_official"
    # Shopify /products.json exposes no aggregate review data
    assert rec["pdp"]["rating_value"] is None and rec["pdp"]["rating_count"] is None


# --------------------------------------------------------------------------
# ingest plan — rating -> catalog_products columns; INCI -> write intent vs skip
# --------------------------------------------------------------------------

def test_ingest_plan_plumbs_rating_and_inci_intent():
    rec = {
        "pdp": {
            "brand": "COSRX",
            "product_name": "Advanced Snail 96 Mucin Power Essence",
            "category_path": "beauty/skincare",
            "attribute_summary": "snail essence",
            "rating_value": 4.3,
            "rating_count": 215,
            "raw_inci": _INCI,
            "inci_source": "brand_official",
        },
        "offers": [
            {"merchant_inferred": "COSRX", "canonical_url": "https://cosrx.com/products/x",
             "destination_url": "https://cosrx.com/products/x", "price": 25.0, "in_stock": True},
        ],
    }
    plan = ingest_validated_jsonl([rec])
    pdp = plan["pdps"][0]
    assert pdp["rating_value"] == 4.3 and pdp["rating_count"] == 215
    assert len(plan["incis"]) == 1
    inci = plan["incis"][0]
    assert inci["product_key"] == pdp["product_key"]
    assert inci["sku_key"] == pdp["product_key"] + "::canonical"  # canonical SKU key contract
    assert inci["raw_inci"] == _INCI and inci["source"] == "brand_official"


def test_ingest_plan_skips_inci_when_absent():
    rec = {
        "pdp": {"brand": "COSRX", "product_name": "No Ingredients Listed",
                "category_path": "beauty/skincare", "attribute_summary": "x"},
        "offers": [{"merchant_inferred": "COSRX", "canonical_url": "https://cosrx.com/products/y",
                    "destination_url": "https://cosrx.com/products/y", "price": 12.0, "in_stock": True}],
    }
    plan = ingest_validated_jsonl([rec])
    assert plan["incis"] == []
    assert plan["pdps"][0]["rating_value"] is None and plan["pdps"][0]["rating_count"] is None


# --------------------------------------------------------------------------
# apply — INCI writer is invoked per row, skips products the identity gate dropped
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_apply_inci_rows_writes_and_skips(monkeypatch):
    calls = []

    async def _fake_intake(pk, raw_inci, source, *, db=None):
        calls.append((pk, raw_inci, source))
        return {"status": "ok", "written_skus": ["sk::canonical"]}

    import services.canonical_inci_intake as intake_mod
    monkeypatch.setattr(intake_mod, "ingest_canonical_inci", _fake_intake)

    rows = [
        {"product_key": "pk_ok", "sku_key": "pk_ok::canonical", "raw_inci": _INCI, "source": "brand_official"},
        {"product_key": "pk_skipped", "sku_key": "pk_skipped::canonical", "raw_inci": _INCI, "source": "reseller_listing"},
    ]
    counts = await apply_mod._apply_inci_rows(
        rows, database=None, skipped_product_keys={"pk_skipped"}
    )
    assert counts["inci_written"] == 1
    assert counts["inci_skipped"] == 1
    assert calls == [("pk_ok", _INCI, "brand_official")]  # skipped product never written


def test_pdp_upsert_sql_binds_rating_columns():
    # The additive columns must reach the canonical upsert (both executors share it).
    sql = apply_mod._PDP_UPSERT_SQL
    assert "rating_value" in sql and "rating_count" in sql
    assert ":rating_value" in sql and ":rating_count" in sql


# ==========================================================================
# Adversarial-review regressions — these FAILED before the RSC-layer fixes.
# ==========================================================================

def _rsc_html(flight: str) -> str:
    """Wrap a raw RSC flight string into a `__next_f.push` chunk (page HTML)."""
    body = json.dumps(flight)[1:-1]
    return f'<html><body><script>self.__next_f.push([1,"{body}"])</script></body></html>'


def _text_row(row_id: str, text: str) -> str:
    """A flight text row `<id>:T<byte-hexlen>,<text>` (length is BYTES, per RSC)."""
    return f"{row_id}:T{format(len(text.encode('utf-8')), 'x')},{text}\n"


def test_b1_ref_resolution_is_row_anchored_not_substring():
    # ref "$51" must NOT bind to an earlier "151:T…" row. Before the anchor fix it
    # captured the 151 prose row and wrote it as INCI.
    prose = "This serum is lightweight, absorbs fast, and hydrates deeply"
    flight = (
        '3:{"allIngredients":"$51"}\n'
        + _text_row("151", prose)          # decoy row whose id ends in 51
        + _text_row("51", _INCI)           # the real ingredient row
        + "52:x\n"
    )
    assert extract_inci_from_next_rsc(_rsc_html(flight)) == _INCI


def test_s1_multibyte_length_does_not_overread_next_row():
    # Declared length is in BYTES; a multibyte glyph must not push the slice into
    # the next row. Before the byte-accurate slice, next-row JSON leaked in.
    mb = "Aqua, Rosa Damascena Flower Water™, Centella Asiatica Extract, Panthenol"
    flight = (
        '3:{"allIngredients":"$61"}\n'
        + _text_row("61", mb)
        + _text_row("62", "LEAKED_NEXT_ROW_TOKEN_SHOULD_NOT_APPEAR, Aqua, Water")
    )
    out = extract_inci_from_next_rsc(_rsc_html(flight))
    assert out == mb
    assert "LEAKED" not in (out or "")


def test_s4_multiple_products_is_ambiguous_returns_none():
    # Related-product cards each carry allIngredients; with no product binding,
    # two distinct valid lists is ambiguous → capture nothing (never the wrong one).
    other = "Aqua, Dimethicone, Cetyl Alcohol, Glycerin, Phenoxyethanol"
    flight = (
        '3:{"allIngredients":"$71"}\n'
        '9:{"allIngredients":"$72"}\n'
        + _text_row("71", _INCI)
        + _text_row("72", other)
    )
    assert extract_inci_from_next_rsc(_rsc_html(flight)) is None


def test_s2_prose_is_rejected_as_reseller_inci():
    prose = "This serum is lightweight, absorbs fast, and hydrates deeply"
    # inline prose in allIngredients
    assert extract_inci_from_next_rsc(_rsc_html('3:{"allIngredients":"' + prose + '"}\n')) is None
    # a resolved row that is prose, not a list
    flight = '3:{"allIngredients":"$81"}\n' + _text_row("81", prose)
    assert extract_inci_from_next_rsc(_rsc_html(flight)) is None


def test_s3_rating_out_of_range_is_nulled():
    # 0–10 / 0–100 / negative → NULL ("no data" beats a mis-scaled number).
    assert _parse_aggregate_rating({"aggregateRating": {"ratingValue": "8.5", "reviewCount": 3}}) == (None, 3)
    assert _parse_aggregate_rating({"aggregateRating": {"ratingValue": "87", "reviewCount": 3}}) == (None, 3)
    assert _parse_aggregate_rating({"aggregateRating": {"ratingValue": "-1", "reviewCount": 3}}) == (None, 3)
    # bestRating declares a non-5 scale → don't guess a rescale
    assert _parse_aggregate_rating(
        {"aggregateRating": {"ratingValue": "8", "reviewCount": 3, "bestRating": 10}}
    ) == (None, 3)
    # negative reviewCount dropped, valid rating kept
    assert _parse_aggregate_rating({"aggregateRating": {"ratingValue": "4.5", "reviewCount": -2}}) == (4.5, None)
    # in-range with explicit 5 scale is kept
    assert _parse_aggregate_rating(
        {"aggregateRating": {"ratingValue": "4.3", "reviewCount": 215, "bestRating": 5}}
    ) == (4.3, 215)


def test_ingestion_rating_coerce_range_guard():
    from services.catalog_enrichment_agent.ingestion import (
        _coerce_rating_count,
        _coerce_rating_value,
    )

    assert _coerce_rating_value("4.2") == 4.2
    assert _coerce_rating_value("9.9") is None   # out of 0–5
    assert _coerce_rating_value("-0.5") is None
    assert _coerce_rating_count("215") == 215
    assert _coerce_rating_count("-3") is None


# --- Real apply-path INCI write (no monkeypatch of the intake) --------------
#
# The wrapper test above monkeypatches ingest_canonical_inci, so it never
# exercises the real beauty_sku_ingredients write shape. This drives the SHARED
# stage `_apply_inci_rows` (called identically by both the per-row and batched
# executors) through the REAL canonical INCI intake against a fake DB, asserting
# the actual UPSERT is issued for the written product and NOT for the skipped one.

class _InciFakeDB:
    """Minimal async DB double for services.canonical_inci_intake.ingest_canonical_inci."""

    def __init__(self):
        self.executed = []  # (sql, params)
        self.is_connected = True

    async def fetch_one(self, sql, params=None):
        s = " ".join(str(sql).split())
        if "FROM catalog_products" in s:
            return {"content_key": "ck_" + params["pk"], "merchant_id": "m_" + params["pk"]}
        return None  # no pre-existing beauty_sku_ingredients row

    async def fetch_all(self, sql, params=None):
        s = " ".join(str(sql).split())
        if "FROM beauty_sku_ingredients" in s:
            return []  # none yet → intake attaches to the catalog SKU
        if "FROM catalog_skus" in s:
            return [{"sku_key": params["pk"] + "::canonical"}]
        return []

    async def execute(self, sql, params=None):
        self.executed.append((" ".join(str(sql).split()), params))


@pytest.mark.asyncio
async def test_apply_inci_rows_real_write_and_isolation(monkeypatch):
    # Isolate the serving-eligibility recompute (its own DB machinery).
    import services.canonical_inci_intake as intake_mod

    async def _no_recompute(content_key, reason=None):
        return True

    monkeypatch.setattr(intake_mod, "recompute_serving_eligibility", _no_recompute)

    db = _InciFakeDB()
    rows = [
        {"product_key": "pk_ok", "sku_key": "pk_ok::canonical", "raw_inci": _INCI, "source": "brand_official"},
        {"product_key": "pk_skip", "sku_key": "pk_skip::canonical", "raw_inci": _INCI, "source": "reseller_listing"},
    ]
    counts = await apply_mod._apply_inci_rows(
        rows, database=db, skipped_product_keys={"pk_skip"}
    )
    assert counts["inci_written"] == 1 and counts["inci_skipped"] == 1

    writes = [(s, p) for (s, p) in db.executed if "INSERT INTO beauty_sku_ingredients" in s]
    assert len(writes) == 1, "exactly one product should have written INCI"
    sql, params = writes[0]
    assert params["pk"] == "pk_ok"                      # skipped product never written
    assert params["sk"] == "pk_ok::canonical"           # canonical SKU key contract
    assert params["mid"] == "m_pk_ok"                    # seller resolved by lookup, not parse
    assert params["inci"] == _INCI
    assert params["src"] == "brand_official"
    assert "raw_inci = EXCLUDED.raw_inci" in sql        # real upsert shape
