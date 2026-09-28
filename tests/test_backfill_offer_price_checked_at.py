"""The price_checked_at backfill's plan, on rows shaped like its own SELECTs.

It may date an offer only with the read that produced that offer's price. Every refusal below is a
case where some stamp exists nearby but does not describe the offer's number.
tests/test_offer_price_checked_at_postgres.py runs the whole script on Postgres.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scripts.backfill_offer_price_checked_at import plan_seed

PK = "ext:missha-pdrn-peel-shot::3595c15f"
DEST = "https://missha.us/products/pdrn-peel-shot"
READ = datetime(2026, 9, 27, 5, 31, tzinfo=timezone.utc)


def _seed(**over):
    seed = {"id": "seed_1", "destination_url": DEST, "attached_product_key": PK,
            "price_amount": 22.7, "price_currency": "USD", "last_crawled_at": READ,
            "snapshot_price_amount": "22.70"}
    seed.update(over)
    return seed


def _offer(offer_id="of_canon", **over):
    offer = {"offer_id": offer_id, "product_key": PK, "sku_key": f"{PK}::canonical",
             "merchant_id": "agent_seed::missha", "currency": "USD", "source_ref": DEST,
             "list_price": 22.7, "merchant_effective_price": 22.7, "price_checked_at": None,
             "payload_destination_url": DEST, "payload_seed_id": None, "suppressed": False}
    offer.update(over)
    return offer


def test_accept_the_listing_row_carrying_the_read_price():
    plan = plan_seed(_seed(), [_offer()])
    assert plan["writes"] == [{"offer_id": "of_canon", "checked_at": READ, "price": 22.7, "currency": "USD"}]


def test_accept_a_mirror_row_matched_on_the_seed_id():
    mirror = _offer("of_mirror", source_ref="seed_1", payload_destination_url=None)
    assert [w["offer_id"] for w in plan_seed(_seed(), [mirror])["writes"]] == ["of_mirror"]


def test_accept_a_currency_spelled_in_lower_case():
    assert plan_seed(_seed(price_currency="usd"), [_offer(currency=" USD ")])["writes"]


@pytest.mark.parametrize(
    "seed_over, offer_over, reason",
    [
        ({"last_crawled_at": None}, {}, "seed_never_read"),
        ({"price_amount": None}, {}, "seed_has_no_price"),
        # an employee edit moved the seed price after the crawl: the crawl did not read 25.00
        ({"price_amount": 25.0}, {"list_price": 25.0, "merchant_effective_price": 25.0},
         "seed_price_not_from_its_read"),
        ({"snapshot_price_amount": None}, {}, "seed_price_not_from_its_read"),
        ({}, {"merchant_effective_price": 29.0, "list_price": 29.0}, "price_differs"),
        ({}, {"merchant_effective_price": 22.71}, "price_differs"),
        ({}, {"currency": "GBP"}, "currency_differs"),
        ({}, {"sku_key": f"{PK}::v:47761881301179"}, "variant_row"),
        ({}, {"suppressed": True}, "suppressed"),
        ({}, {"price_checked_at": READ}, "already_stamped"),
        # another seller's listing on the same canonical is not this seed's offer
        ({}, {"source_ref": "https://www.ulta.com/p/pdrn", "payload_destination_url": "https://www.ulta.com/p/pdrn"},
         "no_listing_offer"),
    ],
)
def test_refuse(seed_over, offer_over, reason):
    plan = plan_seed(_seed(**seed_over), [_offer(**offer_over)])
    assert plan["writes"] == []
    assert plan["skips"] == {reason: 1}


def test_the_merchant_effective_price_is_the_price_compared():
    # the served price is coalesce(merchant_effective_price, list_price)
    assert plan_seed(_seed(), [_offer(list_price=30.0)])["writes"]
    assert plan_seed(_seed(), [_offer(merchant_effective_price=None, list_price=22.7)])["writes"]
