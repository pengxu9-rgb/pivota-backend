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

from services.external_offers_service import URL_VARIANT_PICK_ENV, _extract_from_html, snapshot_price_fields

HOST = "brand.example"
HANDLE = "example-serum"
PAGE = f"https://{HOST}/products/{HANDLE}"
V50, V15 = "41000000000050", "41000000000015"
IN = "https://schema.org/InStock"
OUT = "https://schema.org/OutOfStock"


@pytest.fixture(autouse=True)
def _pick_on(monkeypatch):
    """The URL-variant pick serves (EXTERNAL_OFFER_URL_VARIANT_PICK=on); the off and shadow modes have their own tests."""
    monkeypatch.setenv(URL_VARIANT_PICK_ENV, "on")


def _offer(vid: str, price: str, availability: Optional[str] = IN, *, link: bool = True, sku: Optional[str] = None,
           mpn: Optional[str] = None) -> Dict[str, Any]:
    offer: Dict[str, Any] = {"@type": "Offer", "price": price, "priceCurrency": "USD"}
    if availability is not None:
        offer["availability"] = availability
    if mpn is not None:
        offer["mpn"] = mpn
    if link:
        offer["url"] = f"{PAGE}?variant={vid}"
    if sku is not None:
        offer["sku"] = sku
    return offer


def _html(offers: List[Dict[str, Any]], *, og_price: Optional[str] = None, canonical: str = PAGE, jsonld: Any = None) -> str:
    product = jsonld if jsonld is not None else {
        "@context": "https://schema.org", "@type": "Product", "name": "Example Serum",
        "image": f"https://cdn.{HOST}/serum.jpg", "description": "A serum.", "offers": offers}
    meta = (f'<meta property="og:price:amount" content="{og_price}"><meta property="og:price:currency" content="USD">'
            if og_price else "")
    return (f'<html><head><title>Example Serum</title><link rel="canonical" href="{canonical}">{meta}'
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



# ---- review of #2690: an id field confirms, only a link refuses; groups; modes --------------------------------------


def test_a_lone_offer_carrying_a_barcode_as_sku_prices_a_variant_url():
    """The P1: one offer, a 13-digit barcode sku, no link. Not evidence of another variant: it is the product."""
    offers = [_offer(V15, "28.00", link=False, sku="8809123456789")]
    for og in (None, "28.00"):
        amount, _, price_read, extracted = _price(f"{PAGE}?variant={V15}", _html(offers, og_price=og))
        assert (amount, price_read["status"], extracted["availability"]) == (28.0, "parsed", "in_stock")


def test_numeric_ids_that_are_not_the_urls_variant_never_refuse_the_page():
    """Several offers, numeric MPNs, no links: they cannot be told apart, so the meta price (rendered for the
    selected variant) prices the URL; nothing is refused."""
    offers = [_offer(V50, "85.00", link=False, mpn="1234567"), _offer(V15, "34.00", link=False, mpn="7654321")]
    amount, _, price_read, _ = _price(f"{PAGE}?variant={V15}", _html(offers, og_price="34.00"))
    assert (amount, price_read["source"], price_read["status"]) == (34.0, "meta", "parsed")


def test_a_link_on_the_offered_item_names_the_variant():
    offers = [{"@type": "Offer", "price": "85.00", "priceCurrency": "USD", "availability": IN,
               "itemOffered": {"@type": "Product", "url": f"{PAGE}?variant={V50}"}},
              {"@type": "Offer", "price": "34.00", "priceCurrency": "USD", "availability": IN,
               "itemOffered": {"@type": "Product", "url": f"{PAGE}?variant={V15}"}}]
    amount, _, _, _ = _price(f"{PAGE}?variant={V15}", _html(offers))
    assert amount == 34.0


def test_the_fetched_url_names_the_product_when_the_canonical_does_not():
    """A canonical pointing at a collection: the product is the URL that was fetched."""
    amount, _, _, _ = _price(f"{PAGE}?variant={V15}", _html(TWO_SIZES, canonical=f"https://{HOST}/collections/serums"))
    assert amount == 34.0


def test_an_offer_stating_no_availability_is_not_skipped_for_it():
    """First not stated sold out: as before when the page states nothing."""
    amount, _, _, _ = _price(PAGE, _html([_offer(V50, "85.00", None), _offer(V15, "34.00", IN)]))
    assert amount == 85.0


def _group(sold_out_first: bool) -> Dict[str, Any]:
    def variant(vid, price, availability):
        return {"@type": "Product", "name": f"{price} size", "sku": f"SERUM-{vid[-2:]}",
                "offers": {"@type": "Offer", "price": price, "priceCurrency": "USD", "availability": availability,
                           "url": f"{PAGE}?variant={vid}"}}
    return {"@context": "https://schema.org", "@type": "ProductGroup", "name": "Example Serum",
            "hasVariant": [variant(V50, "85.00", OUT if sold_out_first else IN), variant(V15, "34.00", IN)]}


def test_a_product_group_is_priced_at_its_first_variant_not_sold_out():
    amount, _, _, extracted = _price(PAGE, _html([], jsonld=_group(sold_out_first=True)))
    assert amount == 34.0 and extracted["availability"] == "in_stock"
    amount, _, _, _ = _price(PAGE, _html([], jsonld=_group(sold_out_first=False)))
    assert amount == 85.0
    # And a variant URL is that variant's, sold out or not.
    amount, _, _, _ = _price(f"{PAGE}?variant={V50}", _html([], jsonld=_group(sold_out_first=True)))
    assert amount == 85.0


def test_off_reads_the_first_offer_as_before_and_shadow_logs_the_difference(monkeypatch):
    from utils import logger as shared

    lines: List[str] = []
    monkeypatch.setattr(shared.logger, "warning", lambda msg, *args, **kw: lines.append(msg % args if args else msg))
    url = f"{PAGE}?variant={V15}"
    monkeypatch.setenv(URL_VARIANT_PICK_ENV, "off")
    assert _price(url, _html(TWO_SIZES))[0] == 85.0
    monkeypatch.delenv(URL_VARIANT_PICK_ENV)
    assert _price(url, _html(TWO_SIZES))[0] == 85.0, "off is the default"
    assert not [m for m in lines if "page_offer_pick_shadow" in m], "off logs nothing"
    monkeypatch.setenv(URL_VARIANT_PICK_ENV, "shadow")
    assert _price(url, _html(TWO_SIZES))[0] == 85.0, "shadow serves the first offer"
    shadow = [m for m in lines if "page_offer_pick_shadow" in m]
    assert len(shadow) == 1
    assert json.loads(shadow[0].split(" ", 1)[1]) == {
        "host": HOST, "served_price": "85.00", "picked_price": "34.00", "picked_by": "url_variant",
        "served_availability": "in_stock", "picked_availability": "in_stock"}
    _price(PAGE, _html(TWO_SIZES))  # both pick the 50 mL: no line
    assert len([m for m in lines if "page_offer_pick_shadow" in m]) == 1
    monkeypatch.setenv(URL_VARIANT_PICK_ENV, "garbage")
    assert _price(url, _html(TWO_SIZES))[0] == 85.0, "an unknown mode is off"


def test_a_product_group_without_variant_links_refuses_a_variant_url_meta_included():
    """Every variant node holds a lone offer, none linked: the URL's variant cannot be told from the first node, and
    on a group page the meta price is the store's first available size for a variant it no longer lists."""
    group = _group(sold_out_first=True)
    for node in group["hasVariant"]:
        node["offers"].pop("url")
    amount, _, price_read, _ = _price(f"{PAGE}?variant={V15}", _html([], jsonld=group, og_price="34.00"))
    assert (amount, price_read["status"]) == (None, "url_variant_unidentifiable")
    # A URL naming no variant is still the first not sold out.
    assert _price(PAGE, _html([], jsonld=group))[0] == 34.0


def test_a_named_sold_out_variant_in_a_group_keeps_its_own_price():
    """The URL names the 50 mL, which is linked and sold out; the 15 mL's node carries no link. Still the 50 mL."""
    group = _group(sold_out_first=True)
    group["hasVariant"][1]["offers"].pop("url")
    amount, _, _, extracted = _price(f"{PAGE}?variant={V50}", _html([], jsonld=group))
    assert amount == 85.0 and extracted["availability"] == "out_of_stock"


def test_a_group_of_one_variant_is_priced_by_its_lone_offer():
    group = _group(sold_out_first=False)
    group["hasVariant"] = group["hasVariant"][:1]
    group["hasVariant"][0]["offers"].pop("url")
    amount, _, _, _ = _price(f"{PAGE}?variant={V15}", _html([], jsonld=group))
    assert amount == 85.0



# ---- review r2 of #2690: the page answers for the URL's variant; off pinned; stock; modes ---------------------------


def _review_node(price: str) -> Dict[str, Any]:
    """A review app's Product node: one offer, no link, scored like the product's."""
    return {"@type": "Product", "name": "Example Serum", "image": f"https://cdn.{HOST}/r.jpg", "description": "Reviews.",
            "offers": {"@type": "Offer", "price": price, "priceCurrency": "USD", "availability": IN}}


def test_off_reads_the_first_offer_on_a_sold_out_first_page_and_on_a_variant_url(monkeypatch):
    """What goes live at merge: exactly base's first offer (review r2 Q17)."""
    monkeypatch.setenv(URL_VARIANT_PICK_ENV, "off")
    amount, _, _, extracted = _price(PAGE, _html([_offer(V50, "85.00", OUT), _offer(V15, "34.00", IN)]))
    assert (amount, extracted["availability"]) == (85.0, "out_of_stock")
    amount, _, price_read, _ = _price(f"{PAGE}?variant={V15}", _html(TWO_SIZES))
    assert (amount, price_read["status"]) == (85.0, "parsed")


def test_the_node_naming_the_urls_variant_prices_it_over_a_higher_scored_one():
    """A linked group plus a review app's one-offer node at 85; the URL names the 34 variant (review r2 X8)."""
    page = [_group(sold_out_first=False), _review_node("85.00")]
    amount, _, _, _ = _price(f"{PAGE}?variant={V15}", _html([], jsonld=page))
    assert amount == 34.0


def test_a_link_refusal_holds_against_another_nodes_lone_offer():
    """The URL names a variant the linked group does not list; the review node's lone offer is not the answer (X9)."""
    page = [_group(sold_out_first=False), _review_node("85.00")]
    amount, _, price_read, _ = _price(f"{PAGE}?variant=41000000000099", _html([], jsonld=page, og_price="85.00"))
    assert (amount, price_read["status"]) == (None, "url_variant_not_listed")


def test_a_lone_offer_is_the_products_only_when_it_is_the_only_node_with_offers():
    page = [{"@type": "Product", "name": "Example Serum", "offers": [_offer(V50, "85.00", link=False, sku="A"),
                                                                    _offer(V15, "34.00", link=False, sku="B")]},
            _review_node("85.00")]
    amount, _, price_read, _ = _price(f"{PAGE}?variant={V15}", _html([], jsonld=page, og_price="34.00"))
    assert (amount, price_read["source"]) == (34.0, "meta"), "unidentified: the meta price, not the review node's"
    amount, _, price_read, _ = _price(f"{PAGE}?variant={V15}", _html([], jsonld=page))
    assert (amount, price_read["status"]) == (None, "url_variant_unidentifiable"), "counted apart from a page with no price"


def test_past_a_sold_out_first_offer_a_stated_in_stock_one_beats_one_stating_nothing():
    offers = [_offer(V50, "85.00", OUT), _offer("41000000000030", "30.00", None), _offer(V15, "34.00", IN)]
    assert _price(PAGE, _html(offers))[0] == 34.0
    # A first offer stating nothing is still the first, as before.
    assert _price(PAGE, _html([_offer(V50, "85.00", None), _offer(V15, "34.00", IN)]))[0] == 85.0
    # Nothing stated in stock: the first not stated sold out.
    assert _price(PAGE, _html([_offer(V50, "85.00", OUT), _offer(V15, "34.00", None)]))[0] == 34.0


def test_a_group_past_a_sold_out_first_variant_prefers_one_stated_in_stock():
    group = _group(sold_out_first=True)
    unknown = json.loads(json.dumps(group["hasVariant"][1]))
    unknown["offers"].pop("availability")
    unknown["offers"]["price"] = "30.00"
    unknown["offers"]["url"] = f"{PAGE}?variant=41000000000030"
    group["hasVariant"].insert(1, unknown)
    assert _price(PAGE, _html([], jsonld=group))[0] == 34.0


@pytest.mark.parametrize("raw, mode", [(" On ", "on"), ("SHADOW", "shadow"), ("off", "off"), ("1", "off"), ("", "off")])
def test_the_mode_is_read_trimmed_and_case_blind(monkeypatch, raw, mode):
    from services.external_offers_service import url_variant_pick_mode

    monkeypatch.setenv(URL_VARIANT_PICK_ENV, raw)
    assert url_variant_pick_mode() == mode


def test_shadow_logs_a_difference_in_stock_alone_and_never_breaks_a_read(monkeypatch):
    from utils import logger as shared

    lines: List[str] = []
    monkeypatch.setattr(shared.logger, "warning", lambda msg, *args, **kw: lines.append(msg % args if args else msg))
    monkeypatch.setenv(URL_VARIANT_PICK_ENV, "shadow")
    same_price = [_offer(V50, "34.00", OUT), _offer(V15, "34.00", IN)]  # base: sold out; the pick: in stock
    assert _price(PAGE, _html(same_price))[0] == 34.0
    (line,) = [m for m in lines if "page_offer_pick_shadow" in m]
    fields = json.loads(line.split(" ", 1)[1])
    assert (fields["served_availability"], fields["picked_availability"]) == ("out_of_stock", "in_stock")

    def boom(*_a, **_kw):
        raise RuntimeError("log pipeline down")

    monkeypatch.setattr(shared.logger, "warning", boom)
    amount, _, price_read, _ = _price(f"{PAGE}?variant={V15}", _html(TWO_SIZES))
    assert (amount, price_read["status"]) == (85.0, "parsed"), "shadow serves base, whatever the log does"


def test_two_nodes_each_with_a_lone_offer_leave_the_urls_variant_unidentified():
    page = [{"@type": "Product", "name": "Example Serum", "image": f"https://cdn.{HOST}/a.jpg", "description": "A.",
             "offers": _offer(V50, "85.00", link=False, sku="A")},
            _review_node("50.00")]
    amount, _, price_read, _ = _price(f"{PAGE}?variant={V15}", _html([], jsonld=page))
    assert (amount, price_read["status"]) == (None, "url_variant_unidentifiable")


def test_a_variant_group_with_one_priced_node_still_cannot_name_the_urls_variant():
    group = _group(sold_out_first=False)
    for node in group["hasVariant"]:
        node["offers"].pop("url")
    group["hasVariant"][1].pop("offers")
    amount, _, price_read, _ = _price(f"{PAGE}?variant={V15}", _html([], jsonld=group, og_price="34.00"))
    assert (amount, price_read["status"]) == (None, "url_variant_unidentifiable")
