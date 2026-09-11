"""The cohort this retire step acts on must be exactly the set #2173 re-keyed.

Pure: the storefront fetch is monkeypatched, no DB and no network. The SQL paths are
exercised in the staged run against prod, not here — what is testable in isolation, and
what actually decides which live rows get tombstoned, is the cohort.
"""

import pytest

import services.curated_brand_feed as cbf
from scripts.retire_superseded_brand_keys import build_cohort
from services.catalog_enrichment_agent.ingestion import derive_product_key


def _product(vendor, title, handle):
    return {
        "title": title, "handle": handle, "vendor": vendor, "product_type": "Cleanser",
        "images": [{"src": "https://cdn.x/img.jpg"}],
        "variants": [{"id": 111, "price": "12.00", "available": True}],
    }


@pytest.fixture
def misshaus(monkeypatch):
    """The real vendor mix measured on misshaus.com 2026-09-11, in miniature."""
    feed = [
        _product("MISSHA", "Time Revolution Essence", "tr-essence"),
        _product("MISSHA US", "Artemisia Ampoule", "artemisia"),
        _product("APIEU", "A'pieu Honey & Milk Lip Oil", "lip-oil"),
        _product("Apieu", "A pieu Juicy Pang", "juicy-pang"),
        _product("CHOGONGJIN", "Chogongjin Cream", "cho-cream"),
    ]

    async def fake_fetch(domain, *, max_products=500, timeout_s=15.0):
        return feed

    async def fake_locale(domain, **kw):
        return {"currency": "USD"}

    monkeypatch.setattr(cbf, "fetch_shopify_products", fake_fetch)
    monkeypatch.setattr(cbf, "fetch_shopify_shop_locale", fake_locale)
    return feed


@pytest.mark.asyncio
async def test_cohort_is_exactly_the_rows_whose_brand_moved(misshaus):
    cohort = await build_cohort("misshaus.com", "Missha", "beauty/skincare")
    titles = sorted(c["title"] for c in cohort)
    # The two MISSHA-vendor rows keep brand "Missha" and therefore keep their key.
    assert titles == ["A pieu Juicy Pang", "A'pieu Honey & Milk Lip Oil", "Chogongjin Cream"]
    assert all(c["stale_key"] != c["new_key"] for c in cohort)


@pytest.mark.asyncio
async def test_the_stale_key_is_the_one_the_old_code_would_have_written(misshaus):
    cohort = await build_cohort("misshaus.com", "Missha", "beauty/skincare")
    lip = next(c for c in cohort if c["title"] == "A'pieu Honey & Milk Lip Oil")
    # Exactly what `brand_override or vendor` produced — this is the row in prod today.
    assert lip["stale_key"] == derive_product_key("Missha", "A'pieu Honey & Milk Lip Oil")
    assert lip["new_key"] == derive_product_key("APIEU", "A'pieu Honey & Milk Lip Oil")
    assert lip["brand"] == "APIEU"


@pytest.mark.asyncio
async def test_a_row_that_kept_its_brand_is_never_in_the_cohort(misshaus):
    """The retire step must not touch the 98 rows the fix left alone."""
    cohort = await build_cohort("misshaus.com", "Missha", "beauty/skincare")
    stale = {c["stale_key"] for c in cohort}
    for title in ("Time Revolution Essence", "Artemisia Ampoule"):
        assert derive_product_key("Missha", title) not in stale


@pytest.mark.asyncio
async def test_the_spelling_fold_does_not_split_one_brand_across_two_keys(misshaus):
    """`APIEU` and `Apieu` fold to one spelling, and the key normalises case anyway —
    so both A'pieu rows retire onto keys under the same brand prefix."""
    cohort = await build_cohort("misshaus.com", "Missha", "beauty/skincare")
    brands = {c["brand"] for c in cohort if "pieu" in c["title"].lower()}
    assert brands == {"APIEU"}


@pytest.mark.asyncio
async def test_an_empty_cohort_when_nothing_was_re_keyed(monkeypatch):
    """A single-brand storefront: the fix changes nothing, so there is nothing to retire."""
    feed = [_product("COSRX", "Snail Mucin Gel Cleanser", "snail")]

    async def fake_fetch(domain, *, max_products=500, timeout_s=15.0):
        return feed

    async def fake_locale(domain, **kw):
        return {"currency": "USD"}

    monkeypatch.setattr(cbf, "fetch_shopify_products", fake_fetch)
    monkeypatch.setattr(cbf, "fetch_shopify_shop_locale", fake_locale)
    assert await build_cohort("cosrx.com", "COSRX", "beauty/skincare") == []
