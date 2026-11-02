"""The page reader prices the variant the URL names (services/external_offers_service._pick_product_offer).

A seed's URL can name one size (`?variant=`) while the page lists every size. The reader used to take the first
JSON-LD offer, so the 15 mL URL was priced at the 50 mL's price every night; and for a URL naming no variant it
took the first offer even when that one was sold out, while the catalogue lane took the first in stock, so the
column flipped between two sizes. These run the real extractor over real-shaped Shopify pages.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from services.external_offers_service import _extract_from_html, snapshot_price_fields

HOST = "brand.example"
HANDLE = "example-serum"
PAGE = f"https://{HOST}/products/{HANDLE}"
V50, V15 = "41000000000050", "41000000000015"
IN = "https://schema.org/InStock"
OUT = "https://schema.org/OutOfStock"


def _offer(vid: str, price: str, availability: str = IN, *, link: bool = True, sku: Optional[str] = None) -> Dict[str, Any]:
    offer: Dict[str, Any] = {"@type": "Offer", "price": price, "priceCurrency": "USD", "availability": availability}
    if link:
        offer["url"] = f"{PAGE}?variant={vid}"
    if sku is not None:
        offer["sku"] = sku
    return offer


def _html(offers: List[Dict[str, Any]], *, og_price: Optional[str] = None) -> str:
    product = {"@context": "https://schema.org", "@type": "Product", "name": "Example Serum",
               "image": f"https://cdn.{HOST}/serum.jpg", "description": "A serum.", "offers": offers}
    meta = (f'<meta property="og:price:amount" content="{og_price}"><meta property="og:price:currency" content="USD">'
            if og_price else "")
    return (f'<html><head><title>Example Serum</title><link rel="canonical" href="{PAGE}">{meta}'
            f'<script type="application/ld+json">{json.dumps(product)}</script></head><body></body></html>')


def _price(url: str, html: str):
    extracted = _extract_from_html(url, html)
    amount, currency, price_read = snapshot_price_fields(extracted)
    return amount, currency, price_read, extracted


TWO_SIZES = [_offer(V50, "85.00"), _offer(V15, "34.00")]


def test_premise_the_page_lists_both_sizes_as_variants():
    _, _, _, extracted = _price(PAGE, _html(TWO_SIZES))
    assert {v["variant_id"] for v in extracted["variants"]} == {V50, V15}


def test_a_url_naming_no_variant_is_priced_at_the_first_offer_in_stock():
    amount, currency, price_read, _ = _price(PAGE, _html(TWO_SIZES))
    assert (amount, currency, price_read["status"]) == (85.0, "USD", "parsed")


def test_a_variant_url_is_priced_at_that_variant_not_the_first_offer():
    amount, _, price_read, extracted = _price(f"{PAGE}?variant={V15}", _html(TWO_SIZES))
    assert (amount, price_read["status"]) == (34.0, "parsed")
    assert extracted["availability"] == "in_stock"


def test_a_sold_out_first_offer_does_not_price_a_url_naming_no_variant():
    """The store's selected variant is the first in stock; the catalogue lane takes the same one."""
    offers = [_offer(V50, "85.00", OUT), _offer(V15, "34.00", IN)]
    amount, _, _, extracted = _price(PAGE, _html(offers))
    assert amount == 34.0 and extracted["availability"] == "in_stock"
    # Every offer sold out: the first, as before (and it says so).
    amount, _, _, extracted = _price(PAGE, _html([_offer(V50, "85.00", OUT), _offer(V15, "34.00", OUT)]))
    assert amount == 85.0 and extracted["availability"] == "out_of_stock"


def test_a_sold_out_named_variant_is_still_that_variants_price():
    offers = [_offer(V50, "85.00", IN), _offer(V15, "34.00", OUT)]
    amount, _, _, extracted = _price(f"{PAGE}?variant={V15}", _html(offers))
    assert amount == 34.0 and extracted["availability"] == "out_of_stock"


def test_a_variant_the_page_does_not_list_is_refused_never_guessed():
    """Not the first offer, and not the meta price either: that is the store's fallback variant's."""
    missing = "41000000000099"
    amount, _, price_read, _ = _price(f"{PAGE}?variant={missing}", _html(TWO_SIZES, og_price="85.00"))
    assert amount is None
    assert price_read["status"] == "url_variant_not_listed"


def test_offers_that_name_no_variant_leave_a_variant_url_to_the_meta_price():
    """The offers cannot be told apart, so none prices the URL's variant; the store renders og:price for the
    selected one."""
    offers = [_offer(V50, "85.00", link=False, sku="SERUM-50"), _offer(V15, "34.00", link=False, sku="SERUM-15")]
    amount, _, price_read, _ = _price(f"{PAGE}?variant={V15}", _html(offers, og_price="34.00"))
    assert (amount, price_read["source"]) == (34.0, "meta")
    # With no meta price either, nothing is read rather than the first offer's.
    amount, _, _, _ = _price(f"{PAGE}?variant={V15}", _html(offers))
    assert amount is None


def test_an_offer_keyed_by_its_variant_digits_as_sku_is_matched():
    offers = [_offer(V50, "85.00", link=False, sku=V50), _offer(V15, "34.00", link=False, sku=V15)]
    amount, _, _, _ = _price(f"{PAGE}?variant={V15}", _html(offers))
    assert amount == 34.0


def test_a_link_to_another_products_variant_names_nothing_here():
    """A related product's offer linking its own ?variant= is not this page's variant."""
    other = {**_offer(V15, "12.00"), "url": f"https://{HOST}/products/other-thing?variant={V15}"}
    amount, _, _, _ = _price(f"{PAGE}?variant={V15}", _html([_offer(V50, "85.00"), other]))
    assert amount is None, "the page names variants (the 50 mL) but not this one on this product"


def test_a_subscription_plan_offer_still_never_prices_the_product():
    plan = {**_offer(V15, "28.90"), "name": "Subscribe & save", "sellingPlan": "sp_1"}
    amount, _, _, _ = _price(PAGE, _html([plan, _offer(V50, "85.00"), _offer(V15, "34.00")]))
    assert amount == 85.0


@pytest.mark.parametrize("url", [f"{PAGE}?variant={V15}&variant={V50}", f"{PAGE}?variant=abc"])
def test_an_unreadable_variant_query_is_read_as_naming_none(url):
    amount, _, _, _ = _price(url, _html(TWO_SIZES))
    assert amount == 85.0


def test_a_page_read_of_a_variant_url_stores_that_variants_price_end_to_end(monkeypatch):
    """The review's case through the real refresh: the seed's URL names the 15 mL, the page lists the 50 mL first.
    The column was written at 85 every night; it is now the 15 mL's 34."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from services.external_offers_service import evidence_content_fields, evidence_variant_fields
    from tests.test_external_seed_catalogue_refresh import V15 as CV15, DEST, _Refresh, _drain_row, _page_html

    url = f"{DEST}?variant={CV15}"
    extracted = _extract_from_html(url, _page_html(85, 34))
    amount, currency, price_read = snapshot_price_fields(extracted)
    page = SimpleNamespace(
        canonical_url=extracted["canonical_url"], domain="brand.example", title=extracted.get("title"),
        image_url=extracted.get("image_url"), price_amount=amount, price_currency=currency,
        availability=extracted.get("availability") or "unknown", last_checked_at=datetime.now(timezone.utc),
        evidence={"provider": extracted.get("evidence_provider"), "price_currency_source": "page",
                  **evidence_content_fields(extracted), **evidence_variant_fields(extracted), "price_read": price_read},
    )
    row = _drain_row()
    row["destination_url"] = row["canonical_url"] = url
    run = _Refresh(monkeypatch, row)
    run.page = page
    result = asyncio_run(run.mod._refresh_external_seed_by_id(run.stored["id"], max_wait=0))
    assert result["read_source"] == "page"
    assert run.stored["price_amount"] == 34.0
    assert run.variants[CV15]["price_amount"] == 34.0


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
