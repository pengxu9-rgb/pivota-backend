"""A withheld external-seed price is unknown, not zero.

agent_api's external-seed builder withholds price and stock when the referral
gate requires live verification and marks the row commerce_verification. The v2
serializer used to drop that mark and print the gaps as price "0" and
in_stock: true -- prod 2026-09-24, "Round Lab" on the agent door served five
rows at "0" whose seeds carry real prices (2.50-29.99), one out of stock.

Consumer side (must be green first): PIVOTA-Agent
tests/integration/invoke.find_products_multi_unverified_price.test.js.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from routes.agent_v2 import _canonicalize_search_product  # noqa: E402

WITHHELD_MARK = {
    "required": True,
    "status": "live_quote_required",
    "reasons": ["stale_commerce_facts"],
    "price_trusted": False,
    "availability_trusted": False,
}


def _withheld_seed_row(**overrides):
    # The shape agent_api builds when requires_live_verification is true: no
    # price/currency/in_stock keys at all, availability "unknown", and the mark.
    row = {
        "id": "round-lab:85dd4c56da58a259",
        "product_id": "round-lab:85dd4c56da58a259",
        "merchant_id": "external_seed",
        "merchant_name": "External",
        "platform": "external",
        "title": "Round Lab Sheet Mask Sampler - 9pc",
        "brand": "ROUND LAB",
        "source": "external_seed",
        "availability": "unknown",
        "buyable": False,
        "checkout_ready": False,
        "commerce_verification": dict(WITHHELD_MARK),
        "variants": [],
    }
    row.update(overrides)
    return row


def test_withheld_row_has_no_price_and_no_stock_claim():
    out = _canonicalize_search_product(_withheld_seed_row())
    offer = out["offers"][0]
    assert offer["price"] is None
    assert offer["availability"]["in_stock"] is None


def test_withheld_row_carries_the_verification_mark():
    out = _canonicalize_search_product(_withheld_seed_row())
    assert out["commerce_verification"] == WITHHELD_MARK


def test_the_mark_wins_even_if_a_stale_price_and_stock_are_present():
    # A row marked for live verification must not advertise catalog facts the
    # mark says are untrusted, whatever else it carries.
    out = _canonicalize_search_product(_withheld_seed_row(price=29.99, currency="USD", in_stock=True))
    offer = out["offers"][0]
    assert offer["price"] is None
    assert offer["availability"]["in_stock"] is None


def test_an_absent_price_is_null_not_zero_even_without_the_mark():
    row = _withheld_seed_row()
    del row["commerce_verification"]
    out = _canonicalize_search_product(row)
    assert out["offers"][0]["price"] is None
    assert "commerce_verification" not in out


def test_a_trusted_row_keeps_its_price_and_stock():
    out = _canonicalize_search_product(
        _withheld_seed_row(
            price=13.0,
            currency="USD",
            in_stock=True,
            commerce_verification={"required": False, "status": "catalog_facts_accepted"},
        )
    )
    offer = out["offers"][0]
    assert offer["price"] == "13.0"
    assert offer["availability"]["in_stock"] is True
    assert out["commerce_verification"]["required"] is False


def test_a_real_zero_price_and_a_real_out_of_stock_are_not_nulled():
    # Keyed on absence and on the mark, never on falsiness.
    out = _canonicalize_search_product(
        {
            "product_id": "sample_1",
            "merchant_id": "m_contract",
            "title": "Free Sample",
            "price": 0,
            "currency": "USD",
            "in_stock": False,
            "source": "products_cache",
        }
    )
    offer = out["offers"][0]
    assert offer["price"] == "0"
    assert offer["availability"]["in_stock"] is False


def test_a_row_without_the_mark_keeps_the_legacy_stock_default():
    # Scope guard: only a marked row loses its stock claim. An internal row that
    # simply omits in_stock keeps today's default (true) -- changing that is a
    # separate decision.
    out = _canonicalize_search_product(
        {"product_id": "p1", "merchant_id": "m_contract", "title": "Serum", "price": "42.00", "currency": "USD"}
    )
    assert out["offers"][0]["availability"]["in_stock"] is True
