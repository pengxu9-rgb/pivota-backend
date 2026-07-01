"""build_retailer_offer_row — a retailer offer is a referral: offer_type=retailer,
not first-party, retailer market/currency, price from a trusted feed (never
invented). Pure, no DB.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.attach_retailer_offer import build_retailer_offer_row  # noqa: E402


def _row(**kw):
    base = dict(
        product_key="prod::external_seed::external_seed::anuko_32",
        merchant_id="oliveyoung_global",
        merchant_name="Olive Young Global",
        retailer_url="https://global.oliveyoung.com/product/detail?prdtNo=GA250732178",
    )
    base.update(kw)
    return build_retailer_offer_row(**base)


def test_retailer_offer_is_referral_not_first_party():
    r = _row(market="US", currency="USD", price=25.90)
    assert r["offer_type"] == "retailer"
    assert r["is_first_party"] is False
    assert r["catalog_track"] == "external_referral"
    assert r["offer_mode"] == "redirect"
    assert r["market"] == "US"
    assert r["currency"] == "USD"


def test_price_maps_to_all_three_columns():
    r = _row(price=25.90)
    assert r["list_price"] == 25.90
    assert r["merchant_effective_price"] == 25.90
    assert r["estimated_best_price"] == 25.90
    assert r["price_confidence"] == "0.9"


def test_no_price_is_destination_only():
    r = _row(price=None)
    assert r["list_price"] is None
    assert r["merchant_effective_price"] is None
    assert r["estimated_best_price"] is None
    assert r["price_confidence"] is None


def test_offer_id_is_deterministic_per_product_and_merchant():
    a = _row()["offer_id"]
    b = _row()["offer_id"]
    c = _row(merchant_id="amazon_us")["offer_id"]
    assert a == b  # idempotent
    assert a != c  # distinct per retailer
    assert a.startswith("offer:retailer:oliveyoung_global:")


def test_retailer_url_carried_as_source_ref_and_payload():
    r = _row(retailer_url="https://www.amazon.com/dp/B0DPY5YBY5", merchant_id="amazon_us")
    assert r["source_ref"] == "https://www.amazon.com/dp/B0DPY5YBY5"
    assert "amazon.com" in r["offer_payload"]
