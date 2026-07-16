"""Tests for the ATTACH lane's deterministic offer selection + price guards
(scripts/ingest_stylekorean_brand.py). One offer slot exists per
(product_key, merchant) — N retailer listings matching one canonical must
collapse deterministically, never last-write-wins."""

from scripts.ingest_stylekorean_brand import _offer_eligible, _pick_offer_per_product


def _item(pk, title, price, currency="USD", availability="in_stock", url="u"):
    return {
        "matched_product_key": pk, "title": title, "price": price,
        "currency": currency, "url": url,
        "record": {"offers": [{"availability": availability}]},
    }


def test_base_item_beats_bundle_price():
    # bundle crawled AFTER the base item must not win the offer slot
    items = [
        _item("pk1", "Gel Cleanser Special Set (150ml+150ml)", "24.00", url="u-bundle"),
        _item("pk1", "Gel Cleanser 150ml", "12.50", url="u-base"),
    ]
    winners, collapsed = _pick_offer_per_product(items)
    assert len(winners) == 1 and collapsed == 1
    assert winners[0]["url"] == "u-base" and float(winners[0]["price"]) == 12.50


def test_in_stock_beats_cheaper_out_of_stock():
    items = [
        _item("pk1", "Toner 150ml", "9.00", availability="out_of_stock"),
        _item("pk1", "Toner 150ml", "11.00", availability="in_stock", url="u-in"),
    ]
    winners, _ = _pick_offer_per_product(items)
    assert winners[0]["url"] == "u-in"


def test_selection_is_order_independent():
    a = [_item("pk1", "Essence 100ml", "17.50", url="u1"),
         _item("pk1", "Essence Special Set", "30.00", url="u2")]
    w1, _ = _pick_offer_per_product(a)
    w2, _ = _pick_offer_per_product(list(reversed(a)))
    assert w1[0]["url"] == w2[0]["url"] == "u1"


def test_zero_negative_and_non_usd_prices_never_attach():
    assert not _offer_eligible(_item("pk1", "X", "0"))
    assert not _offer_eligible(_item("pk1", "X", "-1"))
    assert not _offer_eligible(_item("pk1", "X", None))
    assert not _offer_eligible(_item("pk1", "X", "abc"))
    assert not _offer_eligible(_item("pk1", "X", "12000", currency="KRW"))
    assert _offer_eligible(_item("pk1", "X", "12.50"))
    assert _offer_eligible(_item("pk1", "X", "12.50", currency=""))


def test_unmatched_items_never_produce_offers():
    items = [_item(None, "X", "9.99")]
    items[0]["matched_product_key"] = None
    winners, collapsed = _pick_offer_per_product(items)
    assert winners == [] and collapsed == 0
