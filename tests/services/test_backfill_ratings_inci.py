"""Unit tests for scripts/backfill_ratings_inci.py.

No live network, no live DB: the Shopify feed (fetch_shopify_products), the SK PDP
fetch (fetch_text), the canonical INCI writer (ingest_canonical_inci), and the
`database` handle are all monkeypatched. The REAL extraction functions
(inci_from_body_html, extract_product / _parse_aggregate_rating +
extract_inci_from_next_rsc) run against inline HTML fixtures so the wiring — not a
mock of the parser — is what's under test.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./pivota_test.db")

import pytest  # noqa: E402

import scripts.backfill_ratings_inci as bf  # noqa: E402


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

_INCI = "Water, Glycerin, Butylene Glycol, Sodium Hyaluronate, Betaine, Panthenol"


def _shopify_product(handle: str, *, with_inci: bool) -> dict:
    body = "<p>Hydrating essence.</p>"
    if with_inci:
        body += "<p>Ingredients: " + _INCI + "</p><p>How to use: apply daily.</p>"
    return {
        "handle": handle,
        "title": "COSRX Snail Essence",
        "vendor": "COSRX",
        "body_html": body,
        "variants": [{"price": "25.00"}],
    }


def _ld_block(with_rating: bool) -> str:
    obj = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "COSRX Advanced Snail 96 Mucin Power Essence",
        "brand": {"@type": "Brand", "name": "COSRX"},
        "description": "A lightweight snail essence.",
        "offers": {"@type": "Offer", "price": "25.00", "priceCurrency": "USD",
                   "availability": "https://schema.org/InStock"},
    }
    if with_rating:
        obj["aggregateRating"] = {"@type": "AggregateRating", "ratingValue": "4.3",
                                  "reviewCount": 215, "bestRating": 5}
    return '<script type="application/ld+json">' + json.dumps(obj) + "</script>"


def _next_rsc_chunk(inci: str | None) -> str:
    if inci is None:
        flight = '3:{"title":"COSRX Essence","allIngredients":""}\n'
    else:
        flight = ('3:{"title":"COSRX Essence","allIngredients":"$51"}\n'
                  f"51:T{format(len(inci.encode('utf-8')), 'x')},{inci}\n52:x\n")
    body = json.dumps(flight)[1:-1]
    return f'<script>self.__next_f.push([1,"{body}"])</script>'


def _sk_pdp(*, with_rating: bool, inci: str | None) -> str:
    return "<html><head>" + _ld_block(with_rating) + "</head><body>" + _next_rsc_chunk(inci) + "</body></html>"


class _FakeDB:
    """Records execute() calls; the backfill only writes ratings through it."""

    def __init__(self):
        self.executed = []

    async def execute(self, sql, params=None):
        self.executed.append((" ".join(str(sql).split()), params))


def _capture_intake(monkeypatch):
    """Replace ingest_canonical_inci in the backfill namespace with a recorder that
    mimics a successful write (one SKU written)."""
    calls = []

    async def _fake(product_key, raw_inci, source, *, dry_run=False):
        calls.append({"pk": product_key, "inci": raw_inci, "source": source, "dry_run": dry_run})
        return {"status": "ok", "written_skus": ["sk::canonical"], "content_key": "ck_" + product_key}

    monkeypatch.setattr(bf, "ingest_canonical_inci", _fake)
    return calls


# --------------------------------------------------------------------------
# Lane 1 — brand-official INCI from Shopify body_html
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_l1_shopify_body_html_writes_brand_official_inci(monkeypatch):
    calls = _capture_intake(monkeypatch)

    async def _fake_feed(domain, *, max_products=800):
        assert domain == "cosrx.com"
        return [_shopify_product("snail-essence", with_inci=True)]

    monkeypatch.setattr(bf, "fetch_shopify_products", _fake_feed)

    row = {"product_key": "pk1", "content_key": "ck1",
           "canonical_url": "https://cosrx.com/products/snail-essence",
           "source_domain": "cosrx.com"}
    totals = _fresh_totals()
    touched: set = set()
    await bf._process_l1_row(row, cache={}, max_products=800, apply=True,
                             totals=totals, touched=touched)

    assert len(calls) == 1
    assert calls[0]["source"] == bf.INCI_SOURCE_BRAND_OFFICIAL  # brand_official
    assert calls[0]["inci"] == _INCI
    assert calls[0]["dry_run"] is False
    assert totals["l1_inci_written"] == 1
    assert "ck1" in touched


@pytest.mark.asyncio
async def test_l1_provenance_guard_skips_foreign_domain(monkeypatch):
    calls = _capture_intake(monkeypatch)
    monkeypatch.setattr(bf, "fetch_shopify_products",
                        _unexpected_feed("feed must not be fetched for a foreign-domain row"))

    # canonical host (a retailer) != source_domain (the brand) -> never fill.
    row = {"product_key": "pk2", "content_key": "ck2",
           "canonical_url": "https://some-retailer.com/products/snail-essence",
           "source_domain": "cosrx.com"}
    totals = _fresh_totals()
    await bf._process_l1_row(row, cache={}, max_products=800, apply=True,
                             totals=totals, touched=set())
    assert calls == []
    assert totals["l1_foreign_domain"] == 1


@pytest.mark.asyncio
async def test_l1_no_inci_on_storefront_stays_null(monkeypatch):
    calls = _capture_intake(monkeypatch)

    async def _fake_feed(domain, *, max_products=800):
        return [_shopify_product("snail-essence", with_inci=False)]  # prose, no Ingredients label

    monkeypatch.setattr(bf, "fetch_shopify_products", _fake_feed)
    row = {"product_key": "pk3", "content_key": "ck3",
           "canonical_url": "https://cosrx.com/products/snail-essence",
           "source_domain": "cosrx.com"}
    totals = _fresh_totals()
    await bf._process_l1_row(row, cache={}, max_products=800, apply=True,
                             totals=totals, touched=set())
    assert calls == []  # never fabricate — no recoverable INCI
    assert totals["l1_no_inci_on_storefront"] == 1


@pytest.mark.asyncio
async def test_l1_dry_run_passes_dry_run_and_writes_nothing(monkeypatch):
    calls = _capture_intake(monkeypatch)

    async def _fake_feed(domain, *, max_products=800):
        return [_shopify_product("snail-essence", with_inci=True)]

    monkeypatch.setattr(bf, "fetch_shopify_products", _fake_feed)
    row = {"product_key": "pk4", "content_key": "ck4",
           "canonical_url": "https://cosrx.com/products/snail-essence",
           "source_domain": "cosrx.com"}
    totals = _fresh_totals()
    await bf._process_l1_row(row, cache={}, max_products=800, apply=False,
                             totals=totals, touched=set())
    assert len(calls) == 1 and calls[0]["dry_run"] is True  # ingest told not to write


# --------------------------------------------------------------------------
# Lane 2 — StyleKorean ratings + reseller INCI
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_l2_writes_clamped_rating_and_reseller_inci(monkeypatch):
    calls = _capture_intake(monkeypatch)
    db = _FakeDB()
    monkeypatch.setattr(bf, "database", db)
    monkeypatch.setattr(bf, "fetch_text", lambda url: _sk_pdp(with_rating=True, inci=_INCI))

    row = {"product_key": "pk5", "content_key": "ck5",
           "sk_url": "https://www.stylekorean.com/product/cosrx-essence/1234",
           "rating_value": None, "has_inci": False}
    totals = _fresh_totals()
    touched: set = set()
    await bf._process_l2_row(row, cache={}, delay=0.0, apply=True, totals=totals, touched=touched)

    # rating -> catalog_products UPDATE, clamped into [0,5]
    updates = [(s, p) for (s, p) in db.executed if "UPDATE catalog_products" in s]
    assert len(updates) == 1
    _, p = updates[0]
    assert p["rv"] == 4.3 and 0 <= p["rv"] <= 5 and p["rc"] == 215 and p["pk"] == "pk5"
    assert totals["l2_rating_written"] == 1

    # reseller INCI -> ingest with reseller_listing source (precedence-protected)
    assert len(calls) == 1
    assert calls[0]["source"] == bf.INCI_SOURCE_RESELLER  # reseller_listing
    assert calls[0]["inci"] == _INCI
    assert totals["l2_inci_written"] == 1
    assert "ck5" in touched


@pytest.mark.asyncio
async def test_l2_no_signals_on_page_stays_null(monkeypatch):
    calls = _capture_intake(monkeypatch)
    db = _FakeDB()
    monkeypatch.setattr(bf, "database", db)
    monkeypatch.setattr(bf, "fetch_text", lambda url: _sk_pdp(with_rating=False, inci=None))

    row = {"product_key": "pk6", "content_key": "ck6",
           "sk_url": "https://www.stylekorean.com/product/x/9",
           "rating_value": None, "has_inci": False}
    totals = _fresh_totals()
    await bf._process_l2_row(row, cache={}, delay=0.0, apply=True, totals=totals, touched=set())

    assert db.executed == []          # no rating write
    assert calls == []                # no INCI write — never fabricate
    assert totals["l2_no_rating_on_page"] == 1
    assert totals["l2_no_inci_on_page"] == 1


@pytest.mark.asyncio
async def test_l2_skips_rating_when_already_present(monkeypatch):
    # Idempotency: a row that already carries a rating and INCI does no work.
    calls = _capture_intake(monkeypatch)
    db = _FakeDB()
    monkeypatch.setattr(bf, "database", db)
    monkeypatch.setattr(bf, "fetch_text", lambda url: _sk_pdp(with_rating=True, inci=_INCI))

    row = {"product_key": "pk7", "content_key": "ck7",
           "sk_url": "https://www.stylekorean.com/product/x/7",
           "rating_value": 4.0, "has_inci": True}
    totals = _fresh_totals()
    await bf._process_l2_row(row, cache={}, delay=0.0, apply=True, totals=totals, touched=set())
    assert db.executed == [] and calls == []  # nothing re-written


@pytest.mark.asyncio
async def test_l2_dry_run_writes_nothing(monkeypatch):
    calls = _capture_intake(monkeypatch)
    db = _FakeDB()
    monkeypatch.setattr(bf, "database", db)
    monkeypatch.setattr(bf, "fetch_text", lambda url: _sk_pdp(with_rating=True, inci=_INCI))

    row = {"product_key": "pk8", "content_key": "ck8",
           "sk_url": "https://www.stylekorean.com/product/x/8",
           "rating_value": None, "has_inci": False}
    totals = _fresh_totals()
    await bf._process_l2_row(row, cache={}, delay=0.0, apply=False, totals=totals, touched=set())

    assert db.executed == []                          # rating UPDATE gated on apply
    assert len(calls) == 1 and calls[0]["dry_run"] is True  # INCI ingest told not to write
    # still counts the projected gains for the dry-run report
    assert totals["l2_rating_written"] == 1 and totals["l2_inci_written"] == 1


# --------------------------------------------------------------------------
# rating clamp is real (defense-in-depth beyond the JSON-LD range guard)
# --------------------------------------------------------------------------

def test_rating_clamp_drops_out_of_range():
    assert bf._coerce_rating_value("4.3") == 4.3
    assert bf._coerce_rating_value("9.9") is None
    assert bf._coerce_rating_value("-1") is None
    assert bf._coerce_rating_count("-2") is None


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _fresh_totals() -> dict:
    return {k: 0 for k in (
        "l1_no_handle", "l1_foreign_domain", "l1_no_inci_on_storefront", "l1_rejected",
        "l1_inci_written", "l1_skipped_outranked",
        "l2_no_product", "l2_rating_written", "l2_rating_failed", "l2_no_rating_on_page",
        "l2_inci_written", "l2_inci_skipped_outranked", "l2_inci_rejected", "l2_no_inci_on_page",
    )}


def _unexpected_feed(msg: str):
    async def _f(domain, *, max_products=800):
        raise AssertionError(msg)
    return _f
