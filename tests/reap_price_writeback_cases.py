"""Price write-back (PR 2 of the price witness), through the REAL purchase route and catalog SQL.

NOT A TEST MODULE ITSELF: collected by tests/test_reap_price_writeback.py (SQLite) and
tests/test_reap_price_writeback_postgres.py (Postgres). The purchase is opened through the real
HTTP route at the catalog price (13.99); the step's own record of a disagreeing quote
(`live_unit_price_minor`, refused `price_changed`) is then written onto it, exactly the columns
tests/reap_price_witness_cases.py proves the step writes. The pass, the route's loader, the
catalog and the seed are real. The proof that matters is the last one: after the pass, a NEW
create at the live price is accepted by the real route -- the buyer's re-confirm works.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import services.reap_price_writeback as writeback
from db.database import IS_POSTGRES, database
from reap_expected_money_cases import BASE, _purchase_row, lane_body
from reap_selection_prepare_cases import PRODUCT, SEED, prepare_seed_cleanup, unchanged_state  # noqa: F401

if IS_POSTGRES:
    from test_agent_commerce_reap_routes_postgres import _error
else:
    from test_agent_commerce_reap_routes import _error


@pytest.fixture(autouse=True)
def writeback_dial(monkeypatch):
    monkeypatch.delenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, raising=False)


async def _production_seed_columns():
    """The columns production's seed table has and the narrow prepare fixture does not."""
    stamp = "TIMESTAMPTZ" if IS_POSTGRES else "TIMESTAMP"  # migrations 044 / 202
    for column, kind in (("price_amount", "DOUBLE PRECISION"), ("last_crawled_at", stamp),
                         ("updated_at", stamp)):
        await database.execute(f"ALTER TABLE external_product_seeds ADD COLUMN {column} {kind}")
    await database.execute("UPDATE external_product_seeds SET price_amount = 13.99 WHERE id = :id",
                           {"id": SEED})


async def refused_at(client, monkeypatch, *, live, key="writeback-original", stage="approval",
                     age=timedelta(minutes=5)):
    """A real cart-link purchase at 13.99, refused `price_changed` by a quote at `live`."""
    body, minor = await lane_body(monkeypatch, "cart_link", idempotency_key=key)
    await _production_seed_columns()
    body.update(expected_unit_price_minor=minor, expected_currency="USD")
    accepted = await client.post(BASE, json=body)
    assert accepted.status_code == 202, accepted.text
    purchase_id = accepted.json()["purchase_id"]
    when = datetime.now(timezone.utc) - age
    await database.execute(
        "UPDATE reap_agentic_purchases SET state = 'refused', refusal_reason = 'price_changed', "
        "last_error_code = 'quote_items_subtotal_mismatch', live_unit_price_minor = :live, "
        "live_items_subtotal_minor = :live, live_price_stage = :stage, preflight_checked_at = :t, "
        "terminal_at = :t, state_entered_at = :t, updated_at = :t WHERE id = :id",
        {"live": live, "stage": stage, "t": _ts(when), "id": purchase_id})
    return body, purchase_id


def _ts(value):
    import db.reap_agentic_ledger as ledger

    return ledger._bind_dt(value)


async def offer_prices():
    rows = await database.fetch_all(
        "SELECT CAST(coalesce(merchant_effective_price, list_price) AS TEXT) AS p FROM catalog_offers "
        "WHERE product_key = :pk", {"pk": PRODUCT})
    return sorted(str(float(r["p"])) for r in rows)


async def seed_state():
    row = await database.fetch_one(
        "SELECT price_amount, seed_data FROM external_product_seeds WHERE id = :id", {"id": SEED})
    data = json.loads(row["seed_data"])
    return float(row["price_amount"]), [v["price"] for v in data["snapshot"]["variants"]], data


async def test_off_reads_nothing(client, monkeypatch):
    await refused_at(client, monkeypatch, live=1499)
    before = await unchanged_state()
    assert await writeback.run_writeback_pass() is None
    assert await unchanged_state() == before


async def test_an_increase_is_written_and_the_buyer_can_reconfirm_at_it(client, monkeypatch):
    body, purchase_id = await refused_at(client, monkeypatch, live=1499)
    # Before: re-confirming at the live price is refused -- the dead end this closes.
    retry = {**body, "idempotency_key": "writeback-reconfirm-early", "expected_unit_price_minor": 1499}
    early = await client.post(BASE, json=retry)
    assert early.status_code == 409 and _error(early) == "price_changed", early.text

    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}
    assert await offer_prices() == ["14.99"]
    price_amount, variant_prices, data = await seed_state()
    assert (price_amount, variant_prices) == (14.99, ["14.99"])
    assert data["snapshot"]["price_writeback"]["purchase_id"] == purchase_id
    assert data["snapshot"]["price_writeback"]["from_minor"] == 1399

    again = await client.post(BASE, json={**retry, "idempotency_key": "writeback-reconfirm"})
    assert again.status_code == 202, again.text
    assert (await _purchase_row(again.json()["purchase_id"]))["our_price_minor"] == 1499
    # Idempotent: the catalog now says what the quote said.
    assert await writeback.run_writeback_pass() == {"already_current": 1}


async def test_shadow_decides_and_writes_nothing(client, monkeypatch):
    await refused_at(client, monkeypatch, live=1499)
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "shadow")
    before = await unchanged_state()
    assert await writeback.run_writeback_pass() == {"would_write": 1}
    assert await unchanged_state() == before


async def test_a_decrease_on_reaps_word_alone_is_not_written(client, monkeypatch):
    """Owner rule: a lower price needs our own store read of the variant. The seed's proof here
    records no currency, so it corroborates nothing (reap_price_corroboration)."""
    await refused_at(client, monkeypatch, live=1299)
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    before = await unchanged_state()
    assert await writeback.run_writeback_pass() == {"decrease_unconfirmed": 1}
    assert await unchanged_state() == before


async def test_a_decrease_our_own_read_confirms_is_written(client, monkeypatch):
    await refused_at(client, monkeypatch, live=1299)
    row = await database.fetch_one("SELECT seed_data FROM external_product_seeds WHERE id = :id", {"id": SEED})
    data = json.loads(row["seed_data"])
    data["snapshot"]["shopify_cart_proof"].update(price_minor=1299, currency="USD", available=True)
    await database.execute("UPDATE external_product_seeds SET seed_data = :d WHERE id = :id",
                           {"d": json.dumps(data), "id": SEED})

    async def our_read(row, *, now, max_age):
        import services.reap_price_corroboration as corroboration

        return corroboration.Corroboration(1299, corroboration.MIRROR_SOURCE, now)

    # The reader itself is proven in tests/reap_price_witness_cases.py; here only its verdict.
    monkeypatch.setattr(writeback.corroboration, "independent_unit_price", our_read)
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}
    assert await offer_prices() == ["12.99"]


async def test_a_newer_crawl_of_the_seed_wins(client, monkeypatch):
    await refused_at(client, monkeypatch, live=1499)
    await database.execute("UPDATE external_product_seeds SET last_crawled_at = :t WHERE id = :id",
                           {"t": _ts(datetime.now(timezone.utc)), "id": SEED})
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    before = await unchanged_state()
    assert await writeback.run_writeback_pass() == {"catalog_read_newer": 1}
    assert await unchanged_state() == before


async def test_an_offer_that_moved_to_a_third_price_is_left_alone(client, monkeypatch):
    await refused_at(client, monkeypatch, live=1499)
    await database.execute("UPDATE catalog_offers SET merchant_effective_price = 15.99")
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    before = await unchanged_state()
    assert await writeback.run_writeback_pass() == {"offer_price_unexpected": 1}
    assert await unchanged_state() == before


async def test_an_observation_outside_the_window_is_not_read(client, monkeypatch):
    await refused_at(client, monkeypatch, live=1499, age=writeback.WINDOW + timedelta(hours=1))
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {}


async def test_only_price_changed_refusals_and_rebinds_are_observations(client, monkeypatch):
    _body, purchase_id = await refused_at(client, monkeypatch, live=1499)
    await database.execute("UPDATE reap_agentic_purchases SET refusal_reason = 'price_unverifiable' "
                           "WHERE id = :id", {"id": purchase_id})
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {}


def test_the_seed_plan_moves_only_this_variant_and_only_from_the_old_price():
    seed = {"variants": [{"id": "111", "price": "13.99"}, {"id": "222", "price": "13.99"}],
            "snapshot": {"price_amount": 13.99, "variants": [{"variant_id": "111", "price": "13.99"}]}}
    plan, product_level, reason = writeback.plan_seed_write(
        seed, variant_id="111", old_minor=1399, new_minor=1499, currency="USD",
        purchase_id="rp_1", observed=None)
    assert reason == "planned" and product_level is False  # the seed lists another variant
    assert [v["price"] for v in plan["variants"]] == ["14.99", "13.99"]
    assert plan["snapshot"]["variants"][0]["price"] == "14.99"
    assert plan["snapshot"]["price_amount"] == 13.99  # product-level price untouched
    _plan, _pl, unexpected = writeback.plan_seed_write(
        {"variants": [{"id": "111", "price": "12.00"}]}, variant_id="111", old_minor=1399,
        new_minor=1499, currency="USD", purchase_id="rp_1", observed=None)
    assert unexpected == "seed_price_unexpected"
    _plan, _pl, missing = writeback.plan_seed_write(
        {"variants": [{"id": "222", "price": "13.99"}]}, variant_id="111", old_minor=1399,
        new_minor=1499, currency="USD", purchase_id="rp_1", observed=None)
    assert missing == "variant_not_on_seed"


async def test_the_poll_job_runs_the_pass_and_keeps_it_out_of_its_report(client, monkeypatch):
    import jobs.reap_agentic_purchase_poll as poll

    await refused_at(client, monkeypatch, live=1499)
    baseline = await poll.run_reap_agentic_purchase_poll(worker_id="w-writeback-off")
    assert await offer_prices() == ["13.99"]  # dial off: the run reads nothing
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    report = await poll.run_reap_agentic_purchase_poll(worker_id="w-writeback-on")
    assert await offer_prices() == ["14.99"]
    assert report.errors == baseline.errors


async def test_a_failing_pass_never_fails_the_poll_or_its_alerted_errors(client, monkeypatch):
    import jobs.reap_agentic_purchase_poll as poll

    await refused_at(client, monkeypatch, live=1499)
    baseline = await poll.run_reap_agentic_purchase_poll(worker_id="w-writeback-base")

    async def broken(**kwargs):
        raise RuntimeError("catalog unreachable")

    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    monkeypatch.setattr(writeback, "run_writeback_pass", broken)
    report = await poll.run_reap_agentic_purchase_poll(worker_id="w-writeback-broken")
    assert report.errors == baseline.errors
    assert await offer_prices() == ["13.99"]


async def test_a_write_the_route_does_not_see_is_reported_not_effective(client, monkeypatch):
    """Checked AT THE SINK: the route's own loader must price the purchase at the live price. A
    write that landed nowhere the route reads (here: the offers write is lost) says so."""
    await refused_at(client, monkeypatch, live=1499)

    async def lost(offers, *, old, new, currency):
        return 0, "planned"

    monkeypatch.setattr(writeback, "_write_offers", lost)
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written_not_effective": 1}
