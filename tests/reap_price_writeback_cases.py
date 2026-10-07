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
    if IS_POSTGRES:  # production's type (migration 044); the write-back compares the document
        await database.execute(
            "ALTER TABLE external_product_seeds ALTER COLUMN seed_data TYPE JSONB USING seed_data::jsonb")
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

    async def lost(offers, **kwargs):
        return 0, "planned"

    monkeypatch.setattr(writeback, "_write_offers", lost)
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written_not_effective": 1}


# ── review of #2519: the production row shape, races, quote order, filters ───────────────────

from reap_selection_prepare_cases import SELLER, SKU, VARIANT  # noqa: E402

PLACEHOLDER = PRODUCT + "::canonical"
OTHER_VARIANT = "49819267309999"


async def _to_placeholder_only():
    """Production's dominant mirror shape (6,395 of 7,858): the offer on the `::canonical`
    placeholder, none on the real sku."""
    await database.execute("DELETE FROM catalog_offers WHERE product_key = :pk", {"pk": PRODUCT})
    await database.execute(
        "INSERT INTO catalog_skus (sku_key,product_key,merchant_id,platform,source_product_id,source_variant_id,title,currency) "
        "VALUES (:sku,:pk,:seller,'external_seed','x',:pk,'Default','USD')",
        {"sku": PLACEHOLDER, "pk": PRODUCT, "seller": SELLER})
    await database.execute(
        "INSERT INTO catalog_offers (offer_id,sku_key,product_key,merchant_id,currency,merchant_effective_price,availability) "
        "VALUES ('ph-offer',:sku,:pk,:seller,'USD','13.99','in_stock')",
        {"sku": PLACEHOLDER, "pk": PRODUCT, "seller": SELLER})


async def _edit_seed(change):
    row = await database.fetch_one("SELECT seed_data FROM external_product_seeds WHERE id = :id", {"id": SEED})
    data = row["seed_data"] if isinstance(row["seed_data"], dict) else json.loads(row["seed_data"])
    change(data)
    await database.execute("UPDATE external_product_seeds SET seed_data = :d WHERE id = :id",
                           {"d": json.dumps(data), "id": SEED})


async def test_a_placeholder_priced_row_takes_the_increase(client, monkeypatch):
    await refused_at(client, monkeypatch, live=1499)
    await _to_placeholder_only()
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}
    assert await offer_prices() == ["14.99"]


async def test_a_placeholder_that_now_stands_for_another_variant_is_not_written(client, monkeypatch):
    """Review F1: the proof now names another shade as the sole live variant, so the placeholder
    prices THAT shade. This purchase's quote must not be written onto it."""
    await refused_at(client, monkeypatch, live=1499)
    await _to_placeholder_only()
    await database.execute("DELETE FROM catalog_skus WHERE sku_key = :s", {"s": SKU})

    def other_shade(d):
        d["snapshot"]["variants"].append({"title": "New shade", "price": "13.99", "shopify_variant_id": OTHER_VARIANT})
        d["snapshot"]["shopify_cart_proof"]["variant_id"] = OTHER_VARIANT
    await _edit_seed(other_shade)
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    outcome = await writeback.run_writeback_pass()
    assert set(outcome) <= {"route_refuses", "route_prices_another_variant"}, outcome
    assert await offer_prices() == ["13.99"]


async def test_an_offer_moved_between_read_and_write_is_not_overwritten(client, monkeypatch):
    """Review F3: the compare-and-set is in SQL; the whole write rolls back."""
    await refused_at(client, monkeypatch, live=1499)
    real = writeback._mirror_offer_targets

    async def racing(*args, **kwargs):
        found = await real(*args, **kwargs)
        await database.execute("UPDATE catalog_offers SET merchant_effective_price = 15.99")
        return found

    monkeypatch.setattr(writeback, "_mirror_offer_targets", racing)
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"raced": 1}
    assert await offer_prices() == ["15.99"]
    assert (await seed_state())[1] == ["13.99"]  # the seed write rolled back with it


async def test_a_crawl_between_read_and_write_wins(client, monkeypatch):
    """Review F3: the seed write is a compare-and-set on the document as read."""
    await refused_at(client, monkeypatch, live=1499)
    real = writeback._mirror_offer_targets

    async def racing(*args, **kwargs):
        found = await real(*args, **kwargs)
        await _edit_seed(lambda d: d["snapshot"].update(title="CRAWLED"))
        return found

    monkeypatch.setattr(writeback, "_mirror_offer_targets", racing)
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"raced": 1}
    _amount, _variants, data = await seed_state()
    assert data["snapshot"]["title"] == "CRAWLED"
    assert await offer_prices() == ["13.99"]  # the offers rolled back with it


async def _corroborated(monkeypatch, price):
    async def our_read(row, *, now, max_age):
        import services.reap_price_corroboration as corroboration

        return corroboration.Corroboration(price, corroboration.MIRROR_SOURCE, now)

    monkeypatch.setattr(writeback.corroboration, "independent_unit_price", our_read)


async def test_the_newest_quote_wins_not_the_newest_row(client, monkeypatch):
    """Review F2: R's quote is 60 min old but its row moved 1 min ago (it completed); F's quote is
    10 min old. F (14.99) is applied; R (an older 12.99) must never be written back over it."""
    body, f_id = await refused_at(client, monkeypatch, live=1499, key="order-F")
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}
    later = await client.post(BASE, json={**body, "idempotency_key": "order-R", "expected_unit_price_minor": 1499})
    assert later.status_code == 202, later.text
    now = datetime.now(timezone.utc)
    await database.execute(
        "UPDATE reap_agentic_purchases SET state = 'completed', live_unit_price_minor = 1299, "
        "live_price_stage = 'preflight', preflight_checked_at = :q, price_rebound_from_minor = 1499, "
        "price_rebound_to_minor = 1299, terminal_at = :u, state_entered_at = :u, updated_at = :u WHERE id = :id",
        {"q": _ts(now - timedelta(minutes=60)), "u": _ts(now - timedelta(minutes=1)),
         "id": later.json()["purchase_id"]})
    await database.execute(
        "UPDATE reap_agentic_purchases SET our_price_minor = 1299, terminal_at = :t, updated_at = :t WHERE id = :id",
        {"t": _ts(now - timedelta(minutes=10)), "id": f_id})
    await _corroborated(monkeypatch, 1299)
    await writeback.run_writeback_pass()
    assert await offer_prices() == ["14.99"]


async def test_a_corroborated_rebind_that_continued_is_written(client, monkeypatch):
    """A purchase that continued on a corroborated lower price (not refused) is an observation."""
    body, purchase_id = await refused_at(client, monkeypatch, live=1299)
    await database.execute(
        "UPDATE reap_agentic_purchases SET state = 'completed', refusal_reason = NULL, "
        "price_rebound_from_minor = 1399, price_rebound_to_minor = 1299, price_corroborated_at = terminal_at "
        "WHERE id = :id", {"id": purchase_id})
    await _corroborated(monkeypatch, 1299)
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}
    assert await offer_prices() == ["12.99"]


@pytest.mark.parametrize("hours,read", [(71, True), (73, False)])
async def test_the_window_is_seventy_two_hours_of_quote_time(client, monkeypatch, hours, read):
    await refused_at(client, monkeypatch, live=1499, age=timedelta(hours=hours))
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "shadow")
    assert await writeback.run_writeback_pass() == ({"would_write": 1} if read else {})


@pytest.mark.parametrize("other", ["currency", "seller"])
async def test_another_currency_or_sellers_offer_on_the_sku_is_never_written(client, monkeypatch, other):
    await refused_at(client, monkeypatch, live=1499)
    await database.execute(
        "INSERT INTO catalog_offers (offer_id,sku_key,product_key,merchant_id,currency,merchant_effective_price,availability) "
        "VALUES ('other-offer',:sku,:pk,:seller,:cur,'13.99','in_stock')",
        {"sku": SKU, "pk": PRODUCT, "seller": "merch_obs_another" if other == "seller" else SELLER,
         "cur": "EUR" if other == "currency" else "USD"})
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}
    other_price = await database.fetch_one(
        "SELECT CAST(merchant_effective_price AS TEXT) AS p FROM catalog_offers WHERE offer_id = 'other-offer'")
    assert float(other_price["p"]) == 13.99


async def test_the_seed_price_moves_only_when_it_lists_only_this_variant(client, monkeypatch):
    await refused_at(client, monkeypatch, live=1499)
    # The top-level list (the gateway reads it after `snapshot.variants`); the route's sole-variant
    # proof reads the snapshot, so the row stays buyable.
    await _edit_seed(lambda d: d.update(variants=[
        {"title": "Selected shade", "price": "13.99", "shopify_variant_id": VARIANT},
        {"title": "Another shade", "price": "20.00", "shopify_variant_id": OTHER_VARIANT}]))
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    outcome = await writeback.run_writeback_pass()
    assert outcome == {"written": 1}, outcome
    price_amount, variant_prices, data = await seed_state()
    assert (price_amount, variant_prices) == (13.99, ["14.99"])
    assert [v["price"] for v in data["variants"]] == ["14.99", "20.00"]


async def test_an_older_quote_never_overwrites_a_newer_quotes_write(client, monkeypatch):
    """Across passes: F (10 min old, 14.99) was written; only R (60 min old, corroborated 12.99)
    is still an observation. The seed's own record of F's quote stops R."""
    body, f_id = await refused_at(client, monkeypatch, live=1499, key="across-F")
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}
    await database.execute("UPDATE reap_agentic_purchases SET live_unit_price_minor = NULL WHERE id = :id",
                           {"id": f_id})
    later = await client.post(BASE, json={**body, "idempotency_key": "across-R", "expected_unit_price_minor": 1499})
    assert later.status_code == 202, later.text
    now = datetime.now(timezone.utc)
    await database.execute(
        "UPDATE reap_agentic_purchases SET state = 'refused', refusal_reason = 'price_changed', "
        "live_unit_price_minor = 1299, live_price_stage = 'preflight', preflight_checked_at = :q, "
        "terminal_at = :u, updated_at = :u WHERE id = :id",
        {"q": _ts(now - timedelta(minutes=60)), "u": _ts(now), "id": later.json()["purchase_id"]})
    await _corroborated(monkeypatch, 1299)
    outcome = await writeback.run_writeback_pass()
    assert outcome == {"catalog_write_newer": 1}, outcome
    assert await offer_prices() == ["14.99"]


async def test_a_recently_touched_row_with_an_old_quote_is_outside_the_window(client, monkeypatch):
    """The window is quote time: a row whose quote is 73 h old but which moved a minute ago."""
    _body, purchase_id = await refused_at(client, monkeypatch, live=1499, age=timedelta(hours=73))
    await database.execute("UPDATE reap_agentic_purchases SET updated_at = :t WHERE id = :id",
                           {"t": _ts(datetime.now(timezone.utc)), "id": purchase_id})
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "shadow")
    assert await writeback.run_writeback_pass() == {}


async def test_of_two_pending_quotes_the_newer_is_applied(client, monkeypatch):
    """Neither applied yet: F (quote 10 min ago, increase to 14.99, row last moved 10 min ago) and R
    (quote 60 min ago, corroborated 12.99, row moved 1 min ago). The newest QUOTE is F."""
    body, f_id = await refused_at(client, monkeypatch, live=1499, key="pending-F", age=timedelta(minutes=10))
    later = await client.post(BASE, json={**body, "idempotency_key": "pending-R"})
    assert later.status_code == 202, later.text
    now = datetime.now(timezone.utc)
    await database.execute(
        "UPDATE reap_agentic_purchases SET state = 'refused', refusal_reason = 'price_changed', "
        "live_unit_price_minor = 1299, live_price_stage = 'preflight', preflight_checked_at = :q, "
        "terminal_at = :u, updated_at = :u WHERE id = :id",
        {"q": _ts(now - timedelta(minutes=60)), "u": _ts(now - timedelta(minutes=1)),
         "id": later.json()["purchase_id"]})
    await _corroborated(monkeypatch, 1299)
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}
    assert await offer_prices() == ["14.99"]


async def test_a_real_sku_that_now_names_another_variant_is_not_written(client, monkeypatch):
    """Review F1: the route now prices ANOTHER variant (the catalog's sku and the proof both name
    it). The first refusal is the route's own answer, not a downstream guard."""
    await refused_at(client, monkeypatch, live=1499)
    await database.execute("UPDATE catalog_skus SET source_variant_id = :v WHERE sku_key = :s",
                           {"v": "ext_0f95730ee5ba05a6b7957ada:" + OTHER_VARIANT, "s": SKU})

    def other_shade(d):
        d["snapshot"]["variants"] = [{"title": "New shade", "price": "13.99", "shopify_variant_id": OTHER_VARIANT}]
        d["snapshot"]["shopify_cart_proof"]["variant_id"] = OTHER_VARIANT
    await _edit_seed(other_shade)
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    outcome = await writeback.run_writeback_pass()
    assert outcome == {"route_prices_another_variant": 1}, outcome
    assert await offer_prices() == ["13.99"]


async def test_a_placeholder_write_survives_the_seed_to_offer_reprojection(client, monkeypatch):
    """Review of #2519 round 2 (F-A): the seed lists two variants, the proof shows one live; the
    route prices from the placeholder. The seed->offer projection re-copies `price_amount` onto the
    placeholder, so the write must move it too, or the next projection undoes the fix for good."""
    from services.external_offer_dual_write import derive_mirror_offer_id

    await refused_at(client, monkeypatch, live=1499)
    await _to_placeholder_only()
    mirror_id = derive_mirror_offer_id(PRODUCT)
    await database.execute("UPDATE catalog_offers SET offer_id = :m WHERE offer_id = 'ph-offer'", {"m": mirror_id})
    await _edit_seed(lambda d: d.update(variants=[
        dict(d["snapshot"]["variants"][0]),
        {"title": "Sold out shade", "price": "20.00", "shopify_variant_id": OTHER_VARIANT, "available": False}]))
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}
    assert (await seed_state())[0] == 14.99
    # What MIRROR_OFFER_UPSERT_SQL's conflict branch does: the offer takes the seed's price_amount.
    await database.execute(
        "UPDATE catalog_offers SET merchant_effective_price = (SELECT price_amount FROM external_product_seeds "
        "WHERE id = :s) WHERE offer_id = :m", {"s": SEED, "m": mirror_id})
    assert await offer_prices() == ["14.99"]
    assert await writeback.run_writeback_pass() == {"already_current": 1}


async def test_a_stale_placeholder_the_route_does_not_read_does_not_block_the_write(client, monkeypatch):
    """Round 2 (P5): the route prices the real sku's offer; the placeholder holds a stale 12.50. The
    placeholder is not what the route reads, so it is neither written nor a reason to refuse."""
    await refused_at(client, monkeypatch, live=1499)
    await database.execute(
        "INSERT INTO catalog_skus (sku_key,product_key,merchant_id,platform,source_product_id,source_variant_id,title,currency) "
        "VALUES (:sku,:pk,:seller,'external_seed','x',:pk,'Default','USD')",
        {"sku": PLACEHOLDER, "pk": PRODUCT, "seller": SELLER})
    await database.execute(
        "INSERT INTO catalog_offers (offer_id,sku_key,product_key,merchant_id,currency,merchant_effective_price,availability) "
        "VALUES ('ph-offer',:sku,:pk,:seller,'USD','12.50','in_stock')",
        {"sku": PLACEHOLDER, "pk": PRODUCT, "seller": SELLER})
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}
    assert await offer_prices() == ["12.5", "14.99"]


def test_a_seed_copy_already_at_the_new_price_is_an_earlier_write_not_a_conflict():
    seed = {"variants": [{"id": "111", "price": "14.99"}],
            "snapshot": {"variants": [{"variant_id": "111", "price": "13.99"}]}}
    plan, _product_level, reason = writeback.plan_seed_write(
        seed, variant_id="111", old_minor=1399, new_minor=1499, currency="USD",
        purchase_id="rp_1", observed=None)
    assert reason == "planned"
    assert (plan["variants"][0]["price"], plan["snapshot"]["variants"][0]["price"]) == ("14.99", "14.99")


# ── review of #2519 round 2 (follow-up): the LOW findings and the unpinned guards ────────────


async def _add_placeholder(price):
    await database.execute(
        "INSERT INTO catalog_skus (sku_key,product_key,merchant_id,platform,source_product_id,source_variant_id,title,currency) "
        "VALUES (:sku,:pk,:seller,'external_seed','x',:pk,'Default','USD')",
        {"sku": PLACEHOLDER, "pk": PRODUCT, "seller": SELLER})
    await database.execute(
        "INSERT INTO catalog_offers (offer_id,sku_key,product_key,merchant_id,currency,merchant_effective_price,availability) "
        "VALUES ('ph-offer',:sku,:pk,:seller,'USD',:p,'in_stock')",
        {"sku": PLACEHOLDER, "pk": PRODUCT, "seller": SELLER, "p": price})


async def test_a_placeholder_still_at_the_old_price_moves_with_the_real_sku(client, monkeypatch):
    """R2-1: real sku and its projected placeholder both at 13.99 (sole-variant proof). Both move,
    and so does the seed's price, so the projection keeps the placeholder at the new price."""
    await refused_at(client, monkeypatch, live=1499)
    await _add_placeholder("13.99")
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}
    assert await offer_prices() == ["14.99", "14.99"]
    assert (await seed_state())[0] == 14.99


async def test_a_sole_variant_write_refuses_a_seed_price_column_at_a_third_price(client, monkeypatch):
    """R2-2: the column the projection copies onto the placeholder is at 12.00; moving the offer
    alone would be undone, so nothing is written."""
    await refused_at(client, monkeypatch, live=1499)
    await _to_placeholder_only()
    await database.execute("UPDATE external_product_seeds SET price_amount = 12.00 WHERE id = :id", {"id": SEED})
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    before = await unchanged_state()
    assert await writeback.run_writeback_pass() == {"seed_price_unexpected": 1}
    assert await unchanged_state() == before


async def test_a_seed_price_column_moved_between_read_and_write_wins(client, monkeypatch):
    """INFO: the column has its own compare-and-set (an employee edit keeping seed_data equal)."""
    await refused_at(client, monkeypatch, live=1499)
    real = writeback._mirror_offer_targets

    async def racing(*args, **kwargs):
        found = await real(*args, **kwargs)
        await database.execute("UPDATE external_product_seeds SET price_amount = 15.49 WHERE id = :id", {"id": SEED})
        return found

    monkeypatch.setattr(writeback, "_mirror_offer_targets", racing)
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"raced": 1}
    assert (await seed_state())[0] == 15.49
    assert await offer_prices() == ["13.99"]


async def test_the_seed_keeps_its_updated_at_and_moves_its_top_level_price(client, monkeypatch):
    """Round 1 F6: no updated_at bump (the gateway picks a market's seed by it); the top-level
    seed_data.price_amount moves with the product-level price."""
    await refused_at(client, monkeypatch, live=1499)
    stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    await database.execute("UPDATE external_product_seeds SET updated_at = :t WHERE id = :id",
                           {"t": _ts(stamp), "id": SEED})
    await _edit_seed(lambda d: d.update(price_amount="13.99"))
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}
    row = await database.fetch_one("SELECT updated_at FROM external_product_seeds WHERE id = :id", {"id": SEED})
    import services.reap_price_corroboration as corroboration
    assert corroboration._aware(row["updated_at"]) == stamp
    assert (await seed_state())[2]["price_amount"] == "14.99"


async def test_the_after_check_requires_the_purchases_own_variant(client, monkeypatch):
    """After the write the route must price THIS variant at the live price, not just any variant."""
    await refused_at(client, monkeypatch, live=1499)
    real = writeback._priced_now
    calls = []

    async def other_after(lane, row, *, sku_key):
        calls.append(1)
        found = await real(lane, row, sku_key=sku_key)
        return found if len(calls) == 1 else (found[0], found[1], OTHER_VARIANT)

    monkeypatch.setattr(writeback, "_priced_now", other_after)
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written_not_effective": 1}


def test_a_rebinds_quote_time_is_its_corroboration_stamp_not_its_later_transitions():
    quote = datetime(2026, 10, 6, 1, 0, tzinfo=timezone.utc)
    row = {"live_price_stage": "approval", "price_rebound_to_minor": 1299,
           "price_corroborated_at": quote, "terminal_at": quote + timedelta(hours=2)}
    assert writeback.observed_at(row) == quote
    refused = {"live_price_stage": "approval", "terminal_at": quote}
    assert writeback.observed_at(refused) == quote
    preflight = {"live_price_stage": "preflight", "preflight_checked_at": quote, "terminal_at": quote + timedelta(hours=1)}
    assert writeback.observed_at(preflight) == quote


async def test_a_quote_dated_in_the_future_is_not_an_observation(client, monkeypatch):
    await refused_at(client, monkeypatch, live=1499, age=-timedelta(hours=1))
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "shadow")
    assert await writeback.run_writeback_pass() == {}


async def test_a_write_from_the_same_quote_instant_is_not_newer(client, monkeypatch):
    """`catalog_write_newer` is strict: a provenance stamped at exactly this quote's time (a replay
    of the same observation) does not block it."""
    _body, purchase_id = await refused_at(client, monkeypatch, live=1499)
    row = await database.fetch_one("SELECT terminal_at FROM reap_agentic_purchases WHERE id = :id", {"id": purchase_id})
    import services.reap_price_corroboration as corroboration
    same = corroboration._aware(row["terminal_at"])
    await _edit_seed(lambda d: d["snapshot"].update(price_writeback={"observed_at": same.isoformat()}))
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}


def test_a_product_level_price_copy_at_a_third_price_refuses_the_plan():
    """R2-2 inside the document: the seed lists only this variant, but its snapshot price says 12.00."""
    seed = {"snapshot": {"price_amount": 12.0, "variants": [{"variant_id": "111", "price": "13.99"}]}}
    plan, _pl, reason = writeback.plan_seed_write(
        seed, variant_id="111", old_minor=1399, new_minor=1499, currency="USD",
        purchase_id="rp_1", observed=None)
    assert (plan, reason) == (None, "seed_price_unexpected")


# ── review of #2520: the seed price column's compare-and-set and the sole-variant gate ───────


async def _seed_column():
    row = await database.fetch_one("SELECT price_amount FROM external_product_seeds WHERE id = :id", {"id": SEED})
    return row["price_amount"]


def _two_listed(d):
    d["variants"] = [dict(d["snapshot"]["variants"][0]),
                     {"title": "Other", "price": "20.00", "shopify_variant_id": OTHER_VARIANT}]


async def test_a_null_seed_price_column_is_compared_as_null_and_stays_null(client, monkeypatch):
    await refused_at(client, monkeypatch, live=1499)
    await database.execute("UPDATE external_product_seeds SET price_amount = NULL WHERE id = :id", {"id": SEED})
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}
    assert await _seed_column() is None


@pytest.mark.parametrize("before,during", [(None, 15.49), (13.99, None)], ids=["null_to_value", "value_to_null"])
async def test_a_seed_price_column_that_moves_to_or_from_null_wins(client, monkeypatch, before, during):
    await refused_at(client, monkeypatch, live=1499)
    await database.execute("UPDATE external_product_seeds SET price_amount = :p WHERE id = :id", {"p": before, "id": SEED})
    real = writeback._mirror_offer_targets

    async def racing(*args, **kwargs):
        found = await real(*args, **kwargs)
        await database.execute("UPDATE external_product_seeds SET price_amount = :p WHERE id = :id",
                               {"p": during, "id": SEED})
        return found

    monkeypatch.setattr(writeback, "_mirror_offer_targets", racing)
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"raced": 1}
    assert await offer_prices() == ["13.99"]


async def test_a_seed_price_column_already_at_the_new_price_is_not_a_conflict(client, monkeypatch):
    await refused_at(client, monkeypatch, live=1499)
    await database.execute("UPDATE external_product_seeds SET price_amount = 14.99 WHERE id = :id", {"id": SEED})
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}
    assert await _seed_column() == 14.99


async def test_a_placeholder_already_at_the_new_price_beside_a_real_sku_at_the_old(client, monkeypatch):
    await refused_at(client, monkeypatch, live=1499)
    await _add_placeholder("14.99")
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}
    assert await offer_prices() == ["14.99", "14.99"]


async def test_a_placeholder_is_not_written_beside_a_real_sku_under_a_multi_variant_proof(client, monkeypatch):
    """The placeholder is the product-level price only when ONE variant is live."""
    await refused_at(client, monkeypatch, live=1499)
    await _add_placeholder("13.99")

    def named(d):
        _two_listed(d)
        d["snapshot"]["shopify_cart_proof"].update(scope="named_variant", available=True, live_variant_count=3)
    await _edit_seed(named)
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}
    placeholder = await database.fetch_one(
        "SELECT CAST(merchant_effective_price AS TEXT) AS p FROM catalog_offers WHERE offer_id = 'ph-offer'")
    assert float(placeholder["p"]) == 13.99 and await _seed_column() == 13.99


async def test_a_sole_variant_placeholder_beside_a_real_sku_moves_the_seed_column(client, monkeypatch):
    await refused_at(client, monkeypatch, live=1499)
    await _add_placeholder("13.99")
    await _edit_seed(_two_listed)
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}
    assert await _seed_column() == 14.99


async def test_a_seed_listing_several_variants_never_judges_its_product_price(client, monkeypatch):
    """Not product-level (several listed, no placeholder written): the column is not this
    variant's, so a third price there neither moves nor refuses."""
    await refused_at(client, monkeypatch, live=1499)
    await _edit_seed(_two_listed)
    await database.execute("UPDATE external_product_seeds SET price_amount = 12.00 WHERE id = :id", {"id": SEED})
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}
    assert await _seed_column() == 12.0


async def test_a_snapshot_price_already_at_the_new_price_is_not_a_conflict(client, monkeypatch):
    await refused_at(client, monkeypatch, live=1499)
    await _edit_seed(lambda d: d["snapshot"].update(price_amount=14.99))
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}


# ── display copies of the price (staging, 2026-10-07: they stayed at the old price) ──────────


def _facts(amount, display, captured="2026-01-01T00:00:00+00:00"):
    return {"amount": amount, "currency": "USD", "display_raw": display, "captured_at": captured,
            "confidence": "high", "market_switch_status": "ok"}


async def _set_offer_payload(offer_id, payload):
    cast = "CAST(:p AS jsonb)" if IS_POSTGRES else ":p"
    await database.execute(f"UPDATE catalog_offers SET offer_payload = {cast} WHERE offer_id = :o",
                           {"p": json.dumps(payload), "o": offer_id})


async def _offer_payload(offer_id):
    row = await database.fetch_one("SELECT offer_payload FROM catalog_offers WHERE offer_id = :o", {"o": offer_id})
    value = row["offer_payload"]
    return value if isinstance(value, dict) else json.loads(value)


def test_the_facts_move_only_from_the_old_price_and_keep_their_format():
    observed = datetime(2026, 10, 7, 0, 18, tzinfo=timezone.utc)
    holder = {"price": "13.99", "price_amount": 13.99,
              "commerce_facts_v1": {"regional_price": _facts(13.99, "$13.99")},
              "agent_safe_commerce_facts": {"price": _facts("13.99", "13.99 USD")},
              "commerce_facts": {"regional_price": _facts(15.0, "$15.00")}}
    moved = writeback.move_price_facts(holder, old_minor=1399, new_minor=1499, currency="USD",
                                       observed=observed, top_keys=("price", "price_amount"))
    assert moved == 4
    assert (holder["price"], holder["price_amount"]) == ("14.99", 14.99)
    regional = holder["commerce_facts_v1"]["regional_price"]
    assert (regional["amount"], regional["display_raw"], regional["captured_at"]) == (
        14.99, "$14.99", observed.isoformat())
    safe = holder["agent_safe_commerce_facts"]["price"]
    assert (safe["amount"], safe["display_raw"]) == ("14.99", "14.99 USD")
    assert holder["commerce_facts"]["regional_price"]["amount"] == 15.0  # a third price: another writer's fact
    again = writeback.move_price_facts(holder, old_minor=1399, new_minor=1499, currency="USD",
                                       observed=observed, top_keys=("price", "price_amount"))
    assert again == 0  # already new: nothing to move


def test_a_display_text_without_the_old_number_is_dropped_not_guessed():
    holder = {"commerce_facts_v1": {"regional_price": {"amount": 13.99, "display_raw": "thirteen ninety-nine"}}}
    writeback.move_price_facts(holder, old_minor=1399, new_minor=1499, currency="USD", observed=None)
    assert holder["commerce_facts_v1"]["regional_price"] == {"amount": 14.99, "display_raw": None}


@pytest.mark.parametrize("display,old,new,want", [
    ("$13.50", 1350, 1400, "$14.00"),          # review of #2523, F1: was "$140"
    ("$13.00", 1300, 1450, "$14.50"),          # was "$14.5.00"
    ("$13.50", 1350, 1375, "$13.75"),          # was "$13.750"
    ("13.50 USD", 1350, 1600, "16.00 USD"),    # was "160 USD"
    ("US$13.99", 1399, 1499, "US$14.99"),
    ("$12.99 - $13.99", 1299, 1399, None),     # a range: was "$13.99 - $13.99"
    ("Now $13.99, bundle $113.99", 1399, 1499, None),  # another number beside it
    ("$1,299.00", 129900, 139900, None),       # thousands-grouped: was a bare "1399"
    ("12,99 €", 1299, 1349, None),             # decimal comma
])
def test_a_display_text_is_rewritten_exactly_or_not_at_all(display, old, new, want):
    holder = {"commerce_facts_v1": {"regional_price": {"amount": old / 100, "display_raw": display}}}
    writeback.move_price_facts(holder, old_minor=old, new_minor=new, currency="USD", observed=None)
    assert holder["commerce_facts_v1"]["regional_price"]["display_raw"] == want


def test_a_zero_decimal_currency_keeps_its_format():
    holder = {"price": "1500", "commerce_facts_v1": {"regional_price": {"amount": "1500", "display_raw": "¥1500"}}}
    writeback.move_price_facts(holder, old_minor=1500, new_minor=1600, currency="JPY", observed=None,
                               top_keys=("price",))
    assert (holder["price"], holder["commerce_facts_v1"]["regional_price"]["amount"],
            holder["commerce_facts_v1"]["regional_price"]["display_raw"]) == ("1600", "1600", "¥1600")


def test_sale_and_range_context_of_the_old_price_is_cleared():
    """Review of #2523, F2: a sale that ended is the usual reason a price rises."""
    holder = {"commerce_facts_v1": {"regional_price": {
        "amount": 12.99, "price_type": "sale", "compare_at_amount": 13.49, "compare_at_currency": "USD",
        "compare_at_display_raw": "$13.49", "range_min": 12.99, "range_max": 13.99}}}
    writeback.move_price_facts(holder, old_minor=1299, new_minor=1349, currency="USD", observed=None)
    price = holder["commerce_facts_v1"]["regional_price"]
    assert price["amount"] == 13.49 and price["price_type"] == "unknown"
    assert all(price[k] is None for k in ("compare_at_amount", "compare_at_currency",
                                          "compare_at_display_raw", "range_min", "range_max"))


@pytest.mark.parametrize("block", [
    {"amount": 13.99, "currency": "SGD"},
    {"amount": 13.99, "observed_currency": "EUR"},
    {"amount": 13.99, "currency": "USD", "market_switch_status": "mismatch"},
])
def test_a_block_in_another_currency_or_marked_a_mismatch_is_not_this_price(block):
    """Review of #2523, F3."""
    holder = {"commerce_facts_v1": {"regional_price": dict(block)}}
    assert writeback.move_price_facts(holder, old_minor=1399, new_minor=1499, currency="USD", observed=None) == 0
    assert holder["commerce_facts_v1"]["regional_price"] == block


def test_the_legacy_commerce_facts_block_moves_too():
    holder = {"commerce_facts": {"regional_price": {"amount": "13.99"}}}
    assert writeback.move_price_facts(holder, old_minor=1399, new_minor=1499, currency="USD", observed=None) == 1
    assert holder["commerce_facts"]["regional_price"]["amount"] == "14.99"


def test_an_unverified_agent_safe_price_is_left_as_it_is():
    holder = {"agent_safe_commerce_facts": {"price": {"status": "unverified", "reason": "x"}}}
    assert writeback.move_price_facts(holder, old_minor=1399, new_minor=1499, currency="USD", observed=None) == 0


async def test_the_written_offers_payload_and_the_seeds_facts_move_with_the_price(client, monkeypatch):
    _body, purchase_id = await refused_at(client, monkeypatch, live=1499)
    await _set_offer_payload("prepare-offer", {
        "price": "13.99", "price_amount": 13.99, "source": "external_seed",
        "commerce_facts_v1": {"regional_price": _facts(13.99, "13.99")},
        "agent_safe_commerce_facts": {"price": _facts(13.99, "13.99")}})
    await _edit_seed(lambda d: [h.update(commerce_facts_v1={"regional_price": _facts(13.99, "13.99")})
                                for h in (d, d["snapshot"])])
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}
    payload = await _offer_payload("prepare-offer")
    assert (payload["price"], payload["price_amount"], payload["source"]) == ("14.99", 14.99, "external_seed")
    assert payload["commerce_facts_v1"]["regional_price"]["amount"] == 14.99
    assert payload["agent_safe_commerce_facts"]["price"]["display_raw"] == "14.99"
    row = await database.fetch_one("SELECT terminal_at FROM reap_agentic_purchases WHERE id = :id", {"id": purchase_id})
    import services.reap_price_corroboration as corroboration
    assert corroboration._aware(payload["commerce_facts_v1"]["regional_price"]["captured_at"]) == \
        corroboration._aware(row["terminal_at"])
    _amount, _variants, data = await seed_state()
    for holder in (data, data["snapshot"]):
        assert holder["commerce_facts_v1"]["regional_price"]["amount"] == 14.99


async def test_a_seed_listing_several_variants_keeps_its_product_facts(client, monkeypatch):
    """The seed's facts are product-level: not this variant's when it lists others."""
    await refused_at(client, monkeypatch, live=1499)
    await _edit_seed(lambda d: (_two_listed(d), d.update(commerce_facts_v1={"regional_price": _facts(13.99, "13.99")})))
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}
    assert (await seed_state())[2]["commerce_facts_v1"]["regional_price"]["amount"] == 13.99


async def test_a_payload_that_moved_between_read_and_write_rolls_everything_back(client, monkeypatch):
    """The payload's compare-and-set is on the document as read. The concurrent edit is injected
    right after the read; in this single-connection test it runs inside the pass's transaction, so
    the rollback undoes it too -- what matters is that the price write is undone with it."""
    await refused_at(client, monkeypatch, live=1499)
    await _set_offer_payload("prepare-offer", {"price": "13.99"})
    original = writeback.database.fetch_one
    cast = "CAST(:p AS jsonb)" if IS_POSTGRES else ":p"

    async def fetch_then_move(query, values=None):
        found = await original(query, values)
        if query == writeback._READ_OFFER_PAYLOAD_SQL:
            await database.execute(f"UPDATE catalog_offers SET offer_payload = {cast} WHERE offer_id = :o",
                                   {"p": json.dumps({"price": "15.49"}), "o": values["offer_id"]})
        return found

    monkeypatch.setattr(writeback.database, "fetch_one", fetch_then_move)
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"raced": 1}
    monkeypatch.setattr(writeback.database, "fetch_one", original)
    assert await offer_prices() == ["13.99"]
    assert (await _offer_payload("prepare-offer"))["price"] == "13.99"
    assert (await seed_state())[1] == ["13.99"]


async def test_a_seed_about_this_variant_moves_its_placeholder_beside_a_real_sku(client, monkeypatch):
    """Staging, 2026-10-07 (Judydoll): the seed lists ONE of the storefront's live shades (a named
    proof, 8 live) and the placeholder -- the projection of the seed's price, which moves -- stayed
    at the old price, so the product view kept showing it."""
    await refused_at(client, monkeypatch, live=1499)
    await _add_placeholder("13.99")
    await _edit_seed(lambda d: d["snapshot"]["shopify_cart_proof"].update(
        scope="named_variant", available=True, live_variant_count=8))
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}
    assert await offer_prices() == ["14.99", "14.99"]
    assert await _seed_column() == 14.99



# ── review of #2523: the placeholder's payload, and payload shapes left alone ────────────────


def _named8(d):
    d["snapshot"]["shopify_cart_proof"].update(scope="named_variant", available=True, live_variant_count=8)


async def test_the_placeholders_payload_moves_with_it(client, monkeypatch):
    await refused_at(client, monkeypatch, live=1499)
    await _add_placeholder("13.99")
    await _set_offer_payload("ph-offer", {"price_amount": 13.99,
                                          "commerce_facts_v1": {"regional_price": _facts(13.99, "$13.99")}})
    await _edit_seed(_named8)
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}
    payload = await _offer_payload("ph-offer")
    assert payload["price_amount"] == 14.99
    assert payload["commerce_facts_v1"]["regional_price"]["display_raw"] == "$14.99"


async def test_a_placeholder_and_payload_of_a_seed_listing_several_variants_stay(client, monkeypatch):
    await refused_at(client, monkeypatch, live=1499)
    await _add_placeholder("13.99")
    await _set_offer_payload("ph-offer", {"price_amount": 13.99})
    await _edit_seed(lambda d: (_two_listed(d), _named8(d)))
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}
    assert await offer_prices() == ["13.99", "14.99"]
    assert (await _offer_payload("ph-offer"))["price_amount"] == 13.99


async def test_a_payload_that_is_not_an_object_is_left_and_the_price_still_moves(client, monkeypatch):
    await refused_at(client, monkeypatch, live=1499)
    cast = "CAST(:p AS jsonb)" if IS_POSTGRES else ":p"
    await database.execute(f"UPDATE catalog_offers SET offer_payload = {cast} WHERE offer_id = 'prepare-offer'",
                           {"p": json.dumps([13.99])})
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}
    assert await offer_prices() == ["14.99"]


async def test_the_rest_of_a_payload_is_preserved(client, monkeypatch):
    await refused_at(client, monkeypatch, live=1499)
    await _set_offer_payload("prepare-offer", {"zz": {"b": 1, "a": "円"}, "price": "13.99", "aa": [3, 2, 1],
                                               "agent_safe_commerce_facts": {"price": {"status": "unverified"}}})
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}
    payload = await _offer_payload("prepare-offer")
    assert payload["price"] == "14.99" and payload["zz"] == {"b": 1, "a": "円"} and payload["aa"] == [3, 2, 1]
    assert payload["agent_safe_commerce_facts"]["price"] == {"status": "unverified"}


# ── round-2 review of #2523: the LOW / INFO follow-ups ───────────────────────────────────────


@pytest.mark.parametrize("key", ["compare_at_price", "compare_at", "compareAt", "original_price",
                                 "originalPrice", "list_price"])
def test_a_seed_variants_was_price_the_new_price_reaches_is_cleared(key):
    """Finding A: the backend serves compare_at_price as original_price; once the price reaches it
    the sale is over -- an "original price" at or below the price is wrong."""
    seed = {"snapshot": {"variants": [{"variant_id": "111", "price": "13.99", key: "14.49"}]}}
    plan, _pl, reason = writeback.plan_seed_write(
        seed, variant_id="111", old_minor=1399, new_minor=1499, currency="USD",
        purchase_id="rp_1", observed=None)
    assert reason == "planned" and plan["snapshot"]["variants"][0][key] is None


def test_a_seed_variants_was_price_above_the_new_price_stays():
    seed = {"snapshot": {"variants": [{"variant_id": "111", "price": "13.99", "compare_at_price": "19.99"},
                                      {"variant_id": "222", "price": "9.99", "compare_at_price": "12.00"}]}}
    plan, _pl, _reason = writeback.plan_seed_write(
        seed, variant_id="111", old_minor=1399, new_minor=1499, currency="USD",
        purchase_id="rp_1", observed=None)
    variants = plan["snapshot"]["variants"]
    assert variants[0]["compare_at_price"] == "19.99"  # still a discount at the new price
    assert variants[1]["compare_at_price"] == "12.00"  # another variant is never touched


@pytest.mark.parametrize("where,status", [("block", "MISMATCH"), ("block", " mismatch "), ("block", "Failed"),
                                          ("facts", "mismatch"), ("facts", " FAILED ")])
def test_a_mismatch_status_in_any_case_or_at_the_facts_level_is_not_this_price(where, status):
    """Findings B/C: the status as the gateway reads it -- case/space-insensitive, the block's own
    first, else the facts object's."""
    price = {"amount": 13.99}
    facts = {"regional_price": price}
    (price if where == "block" else facts)["market_switch_status"] = status
    holder = {"commerce_facts_v1": facts}
    assert writeback.move_price_facts(holder, old_minor=1399, new_minor=1499, currency="USD", observed=None) == 0
    assert holder["commerce_facts_v1"]["regional_price"]["amount"] == 13.99


def test_an_ok_status_at_the_facts_level_still_moves():
    holder = {"commerce_facts_v1": {"market_switch_status": "ok", "regional_price": {"amount": 13.99}}}
    assert writeback.move_price_facts(holder, old_minor=1399, new_minor=1499, currency="USD", observed=None) == 1


def test_a_padded_sale_price_type_is_recognised():
    """Finding D: ' Sale ' is a sale (the gateway trims)."""
    holder = {"commerce_facts_v1": {"regional_price": {"amount": 12.99, "price_type": " Sale ",
                                                       "compare_at_amount": 15.0}}}
    writeback.move_price_facts(holder, old_minor=1299, new_minor=1349, currency="USD", observed=None)
    price = holder["commerce_facts_v1"]["regional_price"]
    assert (price["price_type"], price["compare_at_amount"]) == ("unknown", None)


@pytest.mark.parametrize("display,old,new,want", [
    ("US$ 13.50", 1350, 100000, "US$ 1,000.00"),
    ("$999.99", 99999, 123456, "$1,234.56"),
    ("$13.50", 1350, 99999, "$999.99"),
])
def test_a_four_digit_amount_reads_with_thousands_grouping(display, old, new, want):
    """Finding F: the old text had no grouping only because it was under 1,000."""
    holder = {"commerce_facts_v1": {"regional_price": {"amount": old / 100, "display_raw": display}}}
    writeback.move_price_facts(holder, old_minor=old, new_minor=new, currency="USD", observed=None)
    assert holder["commerce_facts_v1"]["regional_price"]["display_raw"] == want


async def test_the_seed_variants_was_price_clears_end_to_end(client, monkeypatch):
    await refused_at(client, monkeypatch, live=1499)
    await _edit_seed(lambda d: d["snapshot"]["variants"][0].update(compare_at_price="14.49"))
    monkeypatch.setenv(writeback.REAP_AGENTIC_PRICE_WRITEBACK_ENV, "on")
    assert await writeback.run_writeback_pass() == {"written": 1}
    assert (await seed_state())[2]["snapshot"]["variants"][0]["compare_at_price"] is None
