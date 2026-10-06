"""Price write-back on the ENRICHMENT lane, through the real purchase route (tarte, real proofs).

NOT A TEST MODULE ITSELF: collected by tests/test_reap_price_writeback_enrichment.py (SQLite) and
tests/test_reap_price_writeback_enrichment_postgres.py. Owner rule: the listing's offers move only
when OUR fresh proof of the variant already says the quote's price -- the route requires the offers
to equal that proof, and the proof is never written from a quote.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import db.enrichment_cart_variant_proofs as proofs
import services.reap_price_writeback as writeback
from db.database import database
from reap_enrichment_cart_route_cases import (
    BASE, TARTE_HOST, TARTE_PK, TARTE_SKU, _ts, body, purchase_of, seed_tarte,
)


@pytest.fixture(autouse=True)
def writeback_dial(monkeypatch):
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")


async def refused_at(client, *, live):
    await seed_tarte()
    resp = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK))
    assert resp.status_code == 202, resp.text
    purchase_id = resp.json()["purchase_id"]
    when = _ts(datetime.now(timezone.utc) - timedelta(minutes=5))
    await database.execute(
        "UPDATE reap_agentic_purchases SET state = 'refused', refusal_reason = 'price_changed', "
        "live_unit_price_minor = :live, live_price_stage = 'approval', terminal_at = :t, "
        "state_entered_at = :t, updated_at = :t WHERE id = :id",
        {"live": live, "t": when, "id": purchase_id})
    return purchase_id


async def our_proof_reads(price_minor):
    await database.execute(
        f"UPDATE {proofs.TABLE} SET live_price_minor = :p, checked_at = :t WHERE sku_key = :sk",
        {"p": price_minor, "t": _ts(datetime.now(timezone.utc) - timedelta(minutes=1)), "sk": TARTE_SKU})


async def listing_prices():
    rows = await database.fetch_all(
        "SELECT CAST(merchant_effective_price AS TEXT) AS p FROM catalog_offers WHERE product_key = :pk "
        "ORDER BY offer_id", {"pk": TARTE_PK})
    return [float(r["p"]) for r in rows]


async def test_an_offer_our_proof_already_confirms_is_written_and_reconfirm_works(client):
    await refused_at(client, live=3200)
    await our_proof_reads(3200)
    # The product is unbuyable until the offers follow our own read (offers 30.00, proof 32.00).
    stuck = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK, minor=3200))
    assert stuck.status_code != 202, stuck.text
    assert await writeback.run_writeback_pass() == {"written": 1}
    # The listing's own offer on the proof's sku; the placeholder is not this variant's.
    assert await listing_prices() == [30.0, 32.0]
    again = await client.post(f"{BASE}/purchases", json=body(host=TARTE_HOST, product_key=TARTE_PK, minor=3200))
    assert again.status_code == 202, again.text
    assert (await purchase_of(again))["our_price_minor"] == 3200


@pytest.mark.parametrize("proof_minor", [3000, 3100], ids=["proof_unchanged", "proof_third_price"])
async def test_a_quote_our_proof_does_not_confirm_writes_nothing(client, proof_minor):
    await refused_at(client, live=3200)
    await our_proof_reads(proof_minor)
    before = await listing_prices()
    assert await writeback.run_writeback_pass() == {"proof_disagrees": 1}
    assert await listing_prices() == before


async def test_the_proof_is_never_written(client):
    await refused_at(client, live=3200)
    await our_proof_reads(3200)
    before = [dict(r) for r in await database.fetch_all(f"SELECT * FROM {proofs.TABLE}")]
    await writeback.run_writeback_pass()
    assert [dict(r) for r in await database.fetch_all(f"SELECT * FROM {proofs.TABLE}")] == before


from reap_enrichment_cart_route_cases import (  # noqa: E402
    TARTE_HANDLE, TARTE_OFFER_MERCHANT, TARTE_URL, TARTE_VARIANT, seed_offer, seed_proof, seed_sku,
)


async def test_an_offer_that_is_not_the_listings_own_is_never_written(client):
    await refused_at(client, live=3200)
    await seed_offer(oid="off_tarte_elsewhere", pk=TARTE_PK, sku_key=TARTE_SKU, merchant=TARTE_OFFER_MERCHANT,
                     price="30.00", source_ref=f"https://{TARTE_HOST}/products/another-handle")
    await our_proof_reads(3200)
    assert await writeback.run_writeback_pass() == {"written": 1}
    row = await database.fetch_one(
        "SELECT CAST(merchant_effective_price AS TEXT) AS p FROM catalog_offers WHERE offer_id = 'off_tarte_elsewhere'")
    assert float(row["p"]) == 30.0


async def test_a_sku_whose_own_proof_is_stale_is_not_written(client):
    """Review F5: only skus whose OWN proof row corroborates take the price."""
    await refused_at(client, live=3200)
    alt = f"{TARTE_PK}::sku_alt_spelling"
    await seed_sku(pk=TARTE_PK, sku_key=alt, merchant=TARTE_OFFER_MERCHANT, svid=TARTE_VARIANT, title="Default Title")
    await seed_offer(oid="off_tarte_alt", pk=TARTE_PK, sku_key=alt, merchant=TARTE_OFFER_MERCHANT,
                     price="30.00", source_ref=TARTE_URL)
    await seed_proof(pk=TARTE_PK, sku_key=alt, shop_host=TARTE_HOST, handle=TARTE_HANDLE,
                     variant_id=TARTE_VARIANT, price_minor=3200, age=timedelta(hours=200))
    await our_proof_reads(3200)
    outcome = await writeback.run_writeback_pass()
    assert outcome == {"written": 1}, outcome  # the corroborating sku is written...
    row = await database.fetch_one(
        "SELECT CAST(merchant_effective_price AS TEXT) AS p FROM catalog_offers WHERE offer_id = 'off_tarte_alt'")
    assert float(row["p"]) == 30.0  # ...and the stale-proof spelling is not


async def test_every_sku_our_proof_confirms_takes_the_price_whatever_it_said_before(client):
    """R2-3: the real sku and the placeholder both carry a fresh proof at 32.00 (so no single sku is
    named); the placeholder's offer says 31.00. Our own read confirms both: both move."""
    from reap_enrichment_cart_route_cases import TARTE_PLACEHOLDER

    await refused_at(client, live=3200)
    await database.execute("UPDATE catalog_offers SET merchant_effective_price = 31.00, "
                           "estimated_best_price = 31.00, list_price = 31.00 WHERE sku_key = :s",
                           {"s": TARTE_PLACEHOLDER})
    await seed_proof(pk=TARTE_PK, sku_key=TARTE_PLACEHOLDER, shop_host=TARTE_HOST, handle=TARTE_HANDLE,
                     variant_id=TARTE_VARIANT, price_minor=3200)
    await our_proof_reads(3200)
    outcome = await writeback.run_writeback_pass()
    assert outcome == {"written": 1}, outcome
    assert await listing_prices() == [32.0, 32.0]


# ── review of #2520: all-or-nothing on the enrichment lane, and what is never touched ────────


async def _listing_prices_by_id():
    rows = await database.fetch_all(
        "SELECT offer_id, CAST(merchant_effective_price AS TEXT) AS p FROM catalog_offers "
        "WHERE product_key = :pk ORDER BY offer_id", {"pk": TARTE_PK})
    return {r["offer_id"]: r["p"] for r in rows}


async def _proof_on_placeholder_too():
    from reap_enrichment_cart_route_cases import TARTE_PLACEHOLDER

    await seed_proof(pk=TARTE_PK, sku_key=TARTE_PLACEHOLDER, shop_host=TARTE_HOST, handle=TARTE_HANDLE,
                     variant_id=TARTE_VARIANT, price_minor=3200)
    return TARTE_PLACEHOLDER


@pytest.mark.parametrize("bad", ["0.00", "-1.00"])
@pytest.mark.parametrize("first", [False, True], ids=["after_a_good_offer", "sorted_first"])
async def test_an_unreadable_offer_refuses_before_any_write(client, bad, first):
    """F1: refusing after one offer was written committed that write and reported nothing."""
    await refused_at(client, live=3200)
    placeholder = await _proof_on_placeholder_too()
    await seed_offer(oid="off_tarte_a0" if first else "off_tarte_c2", pk=TARTE_PK, sku_key=placeholder,
                     merchant=TARTE_OFFER_MERCHANT, price=bad, source_ref=TARTE_URL)
    await our_proof_reads(3200)
    before = await _listing_prices_by_id()
    outcome = await writeback.run_writeback_pass()
    assert outcome == {"offer_price_unreadable": 1}, outcome
    assert await _listing_prices_by_id() == before


async def test_an_offer_in_another_currency_rolls_the_whole_write_back(client):
    await refused_at(client, live=3200)
    placeholder = await _proof_on_placeholder_too()
    await seed_offer(oid="off_tarte_c2", pk=TARTE_PK, sku_key=placeholder, merchant=TARTE_OFFER_MERCHANT,
                     price="31.00", source_ref=TARTE_URL, currency="EUR")
    await our_proof_reads(3200)
    before = await _listing_prices_by_id()
    assert await writeback.run_writeback_pass() == {"raced": 1}
    assert await _listing_prices_by_id() == before


async def test_offers_that_are_not_the_listings_own_live_ones_are_never_touched(client):
    await refused_at(client, live=3200)
    placeholder = await _proof_on_placeholder_too()
    for oid, extra in (("off_other_seller", {"merchant": "merch_obs_someoneelse"}),
                       ("off_mirror_sys", {"source_system": "external_product_seeds_mirror_v1"}),
                       ("off_oos", {"availability": "out_of_stock"}),
                       ("off_supp", {"suppressed_at": True})):
        await seed_offer(oid=oid, pk=TARTE_PK, sku_key=placeholder, price="27.00", source_ref=TARTE_URL,
                         **{"merchant": TARTE_OFFER_MERCHANT, **extra})
    await our_proof_reads(3200)
    assert await writeback.run_writeback_pass() == {"written": 1}
    after = await _listing_prices_by_id()
    for oid in ("off_other_seller", "off_mirror_sys", "off_oos", "off_supp"):
        assert float(after[oid]) == 27.0, oid
