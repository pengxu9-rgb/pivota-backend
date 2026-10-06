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
