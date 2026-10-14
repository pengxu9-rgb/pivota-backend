"""The seed refresh re-reads a drain seed's per-size prices off a Shopify page's offer links.

THE DEFECT. A Shopify storefront's JSON-LD names each size offer only by `@id`
"/products/<handle>?variant=<digits>#offer" (or a `url` carrying ?variant=), so the extractor's
`variant_id` for it is that whole string. The retailer-ingest drain stores the merchant's numeric
variant id. `_reconcile_seed_variants_with_read` matched nothing, re-read no size, and the refresh
left `variant_refresh` at `not_all_re_read` -- so `catalog_offers.price_checked_at` was never
stamped on the drain's variant offers (`external_offer_dual_write` requires `all_re_read`).

THE FIX reads the digits into `offer_shopify_variant_id` (read provenance; a seed never stores it)
and the reconcile matches on it. It changes WHICH PRICES REFRESH, so every twin is here: a link
without ?variant=, a ?variant= on another product's link, a currency the stored size is not in,
two offers linking one variant, and the drain's list never being replaced because of it.

Pages go through the REAL extractor (`_extract_from_html`) and the REAL refresh harness; the
stored variants come from the drain's own ingest (see test_seed_refresh_keeps_merchant_variants).
"""

from __future__ import annotations

from typing import Any, Dict, List
from urllib.parse import urlparse

import pytest

from services.external_offers_service import _extract_from_html, seed_variants_from_evidence
from tests.test_external_seed_refresh_writes_variants import DEST, IN, TITLE, _html, _offer, _page, _refresh, _seed_row
from tests.test_seed_refresh_keeps_merchant_variants import V15, V50, _drain_seed_variants

HANDLE = DEST.rsplit("/products/", 1)[1]
STORE = f"https://{urlparse(DEST).hostname}"
WWW_STORE = f"https://www.{urlparse(DEST).hostname}"
OTHER = "another-product"


def _size_offers(price50: float = 85, price15: float = 34, *, handle: str = HANDLE, **extra: Any) -> List[Dict[str, Any]]:
    """The prod shape: one offer per size named only by its `@id`, plus a product-level offer
    carrying the merchant SKU code."""
    return [
        _offer(price50, IN, name=f"{TITLE} - 1.7 fl. oz. / 50 mL",
               **{"@id": f"/products/{handle}?variant={V50}#offer"}, **extra),
        _offer(price15, IN, name=f"{TITLE} - 0.5 fl. oz. / 15 mL",
               **{"@id": f"/products/{handle}?variant={V15}#offer"}, **extra),
        _offer(price15, IN, sku="EXBASE", **extra),
    ]


def _read(offers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return _extract_from_html(DEST, _html(offers))["variants"]


def _linked(offers: List[Dict[str, Any]]) -> List[Any]:
    return [v.get("offer_shopify_variant_id") for v in _read(offers)]


# ---- the extractor ---------------------------------------------------------------------------


def test_the_extractor_reads_the_variant_an_offer_links_to_and_keeps_its_key():
    read = _read(_size_offers())
    assert [v.get("offer_shopify_variant_id") for v in read] == [V50, V15, None]
    # `variant_id` (what the builders and the de-dupe key on) is unchanged.
    assert read[0]["variant_id"] == f"/products/{HANDLE}?variant={V50}#offer"


@pytest.mark.parametrize("link", [
    f"{STORE}/products/{HANDLE}?variant={V50}",
    f"{WWW_STORE}/products/{HANDLE}?variant={V50}",       # www vs apex is one store
    f"{STORE}/collections/sun/products/{HANDLE}?variant={V50}",
])
def test_an_offer_url_on_the_pages_own_product_names_its_variant(link):
    assert _linked([_offer(85, sku="EX50", url=link)]) == [V50]


@pytest.mark.parametrize("ids", [
    {"@id": f"/products/{HANDLE}#offer"},                                    # no ?variant=
    {"@id": f"/products/{OTHER}?variant={V50}#offer"},                       # another product
    {"url": f"https://elsewhere.example/products/{HANDLE}?variant={V50}"},   # another store
    {"@id": f"/products/{HANDLE}?variant=abc#offer"},                        # not digits
    {"@id": f"/products/{HANDLE}?variant={V50}#offer",                       # two links disagree
     "url": f"{STORE}/products/{HANDLE}?variant={V15}"},
    {"@id": f"/products/{HANDLE}?variant={V50}#offer",                       # one of them is another product's
     "url": f"{STORE}/products/{OTHER}?variant={V50}"},
    {"@id": f"/pages/{HANDLE}?variant={V50}#offer"},                         # not a product link
    {"@id": f"https://[bad/products/{HANDLE}?variant={V50}#offer"},          # malformed: no raise
])
def test_a_link_that_does_not_name_one_variant_of_this_product_names_nothing(ids):
    assert _linked([_offer(85, sku="EX50", **ids)]) == [None]


def test_a_seed_never_stores_the_link():
    page = _page(_size_offers())
    assert any(v.get("offer_shopify_variant_id") for v in page.evidence["variants"])
    assert not any("offer_shopify_variant_id" in v for v in seed_variants_from_evidence(page.evidence))


# ---- the reconcile, through the real refresh -------------------------------------------------


def _refresh_drain_seed(monkeypatch, offers):
    row = _seed_row(variants=_drain_seed_variants(), price=32.0, availability="in_stock")
    _, stored = _refresh(monkeypatch, row, _page(offers))
    seed = stored["seed_data"]
    return {v["variant_id"]: v for v in seed["variants"]}, seed["snapshot"]


def test_premise_without_the_link_no_size_is_re_read(monkeypatch):
    # The bug, through the real refresh: drop the read's link and the drain's sizes stay unread.
    import routes.employee_products as mod

    monkeypatch.setattr(mod, "_read_variant_link", lambda rv: None)
    by_id, snapshot = _refresh_drain_seed(monkeypatch, _size_offers())
    assert (by_id[V50]["price_amount"], by_id[V15]["price_amount"]) == (80.0, 32.0)
    assert snapshot["variant_refresh"]["status"] == "not_all_re_read"


def test_a_refresh_re_reads_each_size_the_page_links(monkeypatch):
    by_id, snapshot = _refresh_drain_seed(monkeypatch, _size_offers(85, 34))
    assert (by_id[V50]["price_amount"], by_id[V15]["price_amount"]) == (85.0, 34.0)
    assert snapshot["variant_refresh"]["status"] == "all_re_read"  # what price_checked_at needs
    assert "extracted_at" in snapshot


def test_the_link_never_lets_the_page_replace_the_drains_list(monkeypatch):
    # The page lists one id more than the store has sizes; #2601 keeps the drain's list because
    # the read does not carry the drain's ids as its own keys. The link must not change that:
    # adopting the page would key the seed by `@id` strings and add the product-level offer.
    by_id, _ = _refresh_drain_seed(monkeypatch, _size_offers())
    assert list(by_id) == [V50, V15]
    assert {v["variant_id_provenance"] for v in by_id.values()} == {"merchant_issued"}
    assert not any("offer_shopify_variant_id" in v for v in by_id.values())


@pytest.mark.parametrize("handle", [OTHER], ids=["another_products_link"])
def test_twin_a_variant_on_another_products_link_is_not_read(monkeypatch, handle):
    by_id, snapshot = _refresh_drain_seed(monkeypatch, _size_offers(85, 34, handle=handle))
    assert (by_id[V50]["price_amount"], by_id[V15]["price_amount"]) == (80.0, 32.0)
    assert snapshot["variant_refresh"]["status"] == "not_all_re_read"


def test_twin_an_id_without_a_variant_is_not_read(monkeypatch):
    offers = [_offer(85, IN, **{"@id": f"/products/{HANDLE}#offer-50"}),
              _offer(34, IN, **{"@id": f"/products/{HANDLE}#offer-15"})]
    by_id, snapshot = _refresh_drain_seed(monkeypatch, offers)
    assert (by_id[V50]["price_amount"], by_id[V15]["price_amount"]) == (80.0, 32.0)
    assert snapshot["variant_refresh"]["status"] == "not_all_re_read"


def test_twin_a_price_in_another_currency_is_not_read(monkeypatch):
    by_id, snapshot = _refresh_drain_seed(monkeypatch, _size_offers(85, 34, priceCurrency="EUR"))
    assert (by_id[V50]["price_amount"], by_id[V15]["price_amount"]) == (80.0, 32.0)
    assert {by_id[V50]["price_currency"], by_id[V15]["price_currency"]} == {"USD"}
    assert snapshot["variant_refresh"]["status"] == "not_all_re_read"


def test_twin_two_offers_linking_one_variant_read_neither(monkeypatch):
    # A product-level offer whose url selects the 15 mL is a second offer under that variant: the
    # 15 mL is ambiguous and stays unread; the 50 mL is still re-read.
    offers = _size_offers(85, 34)
    offers[2]["url"] = f"{STORE}/products/{HANDLE}?variant={V15}"
    offers[2]["price"] = "30"
    by_id, snapshot = _refresh_drain_seed(monkeypatch, offers)
    assert (by_id[V50]["price_amount"], by_id[V15]["price_amount"]) == (85.0, 32.0)
    assert snapshot["variant_refresh"]["status"] == "not_all_re_read"


def test_a_link_never_refuses_a_match_the_key_already_made(monkeypatch):
    # A seed adopted from an earlier read stores the offer's barcode `sku` as its id; the page now
    # also links the variant. The barcode is digit-shaped like a Shopify id -- the link joins the
    # match only, so it cannot turn into a "different native id" refusal.
    barcode = "0812345678905"
    row = _seed_row(variants=[{"variant_id": barcode, "title": "50 mL", "price_amount": 80.0,
                               "price_currency": "USD", "availability": "in_stock"},
                              {"variant_id": "0812345678912", "title": "15 mL", "price_amount": 32.0,
                               "price_currency": "USD", "availability": "in_stock"}],
                    price=32.0, availability="in_stock")
    page = _page([_offer(85, IN, sku=barcode, url=f"{STORE}/products/{HANDLE}?variant={V50}"),
                  _offer(34, IN, sku="0812345678912", url=f"{STORE}/products/{HANDLE}?variant={V15}")])
    _, stored = _refresh(monkeypatch, row, page)
    prices = {v["variant_id"]: v["price_amount"] for v in stored["seed_data"]["variants"]}
    assert prices == {barcode: 85.0, "0812345678912": 34.0}
    assert stored["seed_data"]["snapshot"]["variant_refresh"]["status"] == "all_re_read"


def test_an_offer_whose_sku_is_its_own_linked_variant_still_matches(monkeypatch):
    # Some stores use the variant id as the `sku`: the key and the link name one id on ONE offer,
    # which is not a second offer under that id.
    page = _page([_offer(85, IN, sku=V50, **{"@id": f"/products/{HANDLE}?variant={V50}#offer"}),
                  _offer(34, IN, sku=V15, **{"@id": f"/products/{HANDLE}?variant={V15}#offer"})])
    row = _seed_row(variants=_drain_seed_variants(), price=32.0, availability="in_stock")
    _, stored = _refresh(monkeypatch, row, page)
    prices = {v["variant_id"]: v["price_amount"] for v in stored["seed_data"]["variants"]}
    assert prices == {V50: 85.0, V15: 34.0}
    assert stored["seed_data"]["snapshot"]["variant_refresh"]["status"] == "all_re_read"


# ---- a link never takes a re-read away (prod census 2026-10-14) -------------------------------


def _reconcile(stored, offers):
    from routes.employee_products import _reconcile_seed_variants_with_read

    page = _page(offers)
    return _reconcile_seed_variants_with_read(
        stored, page.evidence["variants"], product_amount=None, product_currency="USD",
        product_availability=None, census=page.evidence["variant_census"])


def _stored(vid: str, **extra: Any) -> Dict[str, Any]:
    return {"variant_id": vid, "title": "50 mL", "price_amount": 80.0, "price_currency": "USD",
            "availability": "in_stock", **extra}


def test_a_page_listing_one_variant_by_link_and_by_sku_still_matches_on_the_sku():
    # The prod shape the census found: the same variant twice, once by its link (no sku), once with
    # the variant id as `sku`. Before the link existed the `sku` offer matched; it still does, and
    # its price is the one taken.
    offers = [_offer(99, IN, **{"@id": f"/products/{HANDLE}?variant={V50}#offer"}), _offer(100, IN, sku=V50)]
    (v,), report = _reconcile([_stored(V50)], offers)
    assert (v["price_amount"], report["re_read"]) == (100.0, 1)


def test_a_link_never_claims_an_offer_another_variant_matched_by_key():
    # The offer is keyed by one stored variant's sku and links another's id: the key match stands
    # and the link adds nothing (two claimants would refuse both).
    offers = [_offer(85, IN, sku="EX50", **{"@id": f"/products/{HANDLE}?variant={V50}#offer"})]
    by_key, by_link = _stored("EX50"), _stored(V50, title="50 mL (drain)")
    out, report = _reconcile([by_key, by_link], offers)
    assert [v["price_amount"] for v in out] == [85.0, 80.0]
    assert report["not_re_read"] == [V50]


def test_a_link_never_overrides_keys_that_contradict_the_stored_variant():
    # The stored variant's sku and barcode name two different offers by key -- a contradiction the
    # reconcile refuses; a third offer's link must not settle it.
    offers = [_offer(85, IN, sku="EX50"), _offer(86, IN, sku="B50"),
              _offer(87, IN, **{"@id": f"/products/{HANDLE}?variant={V50}#offer"})]
    (v,), report = _reconcile([_stored(V50, sku="EX50", barcode="B50")], offers)
    assert (v["price_amount"], report["re_read"]) == (80.0, 0)
