"""services/deal_code_retention.py: ADR-027 Q18 retention of crawled codes (peng 2026-11-07).

Pinned: each rule deletes exactly what it names and nothing else; only merchant_published rows are touched; opt-out
matches every form; batches and the per-run cap bound the work; a second run is a no-op; the kill switch stops it;
one rule's failure never stops the others; nothing raises; logs carry counts only."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services import deal_code_retention as r  # noqa: E402

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def db(monkeypatch):
    from db import deal_lane
    from db.catalog import catalog_products
    from db.database import database, engine, metadata

    tables = [deal_lane.observed_store_offers, deal_lane.observed_store_offer_verifications, deal_lane.deal_lane_optout,
              catalog_products]
    metadata.create_all(engine, tables=tables)
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()

    async def _clean():
        for table in reversed(tables):
            await database.execute(table.delete())

    await _clean()
    monkeypatch.delenv("DEAL_CODE_RETENTION_PAUSED", raising=False)
    try:
        yield database
    finally:
        await _clean()
        if not was_connected:
            await database.disconnect()


def _ago(x):
    return x if isinstance(x, timedelta) else timedelta(days=x)


async def offer(db, name, *, seller="glow.example", code=None, source="merchant_published", observed_days=0,
                created_days=0, terms="Get 15% off with code X", crawl=True):
    from db.deal_lane import observed_store_offer_key, observed_store_offers as o

    # Crawl rows carry the writer's own deal_id prefix; `crawl=False` is a merchant_published row entered another way.
    deal_id = ("dlc_" if crawl else "dl_") + name
    code = code or name.upper()
    key = observed_store_offer_key(seller_id=seller, market="US", code=code, platform_rule_ref=None,
                                   program_id="prog_1" if source == "authorized_feed" else None)
    await db.execute(o.insert().values(
        deal_id=deal_id, deal_key=key, seller_id=seller, market="US", code=code, kind="percent", value=15,
        source=source, program_id="prog_1" if source == "authorized_feed" else None, terms_text=terms,
        observed_at=NOW - _ago(observed_days), created_at=NOW - _ago(created_days),
        updated_at=NOW - _ago(observed_days),
    ))


async def verification(db, name, state, *, updated_days=0, expires_days=None, variant="44012345678901",
                       lapsed_days=None):
    """`expires_days`: a checkout_verified row valid until NOW + that offset. `lapsed_days`: an `expired` or re-queued
    `unverified` row whose verification was valid until that long ago (the lifecycle after a verification lapses)."""
    from db.deal_lane import observed_store_offer_verifications as v

    deal_id = "dlc_" + name

    values = dict(deal_id=deal_id, variant_id=variant, state=state, updated_at=NOW - _ago(updated_days))
    if lapsed_days is not None:  # a re-queue keeps verified_at, expires_at and the reference amounts (the contract)
        values.update(verified_at=NOW - _ago(lapsed_days) - timedelta(days=1), expires_at=NOW - _ago(lapsed_days),
                      ref_discount_amount=5)
    if state == "checkout_verified":
        expires_at = NOW + _ago(expires_days if expires_days is not None else 1)
        values.update(verified_at=expires_at - timedelta(days=1), verified_via="ucp_checkout", authorized_by="q2_approval",
                      ref_currency="USD", ref_unit_price=25, ref_subtotal=25, ref_discount_amount=5, ref_automatic_discounts=[],
                      expires_at=expires_at)
    if state == "failed":
        values.update(failure_reason="discount_code_invalid")
    await db.execute(v.insert().values(**values))


async def ids(db):
    from db.deal_lane import observed_store_offers as o

    return sorted(row["deal_id"].split("_", 1)[1] for row in await db.fetch_all(o.select()))


async def test_unverified_rows_go_14_days_after_they_were_last_seen(db):
    await offer(db, "fresh", observed_days=13)
    await offer(db, "stale", observed_days=15)
    out = await r.run(now=NOW)
    assert await ids(db) == ["fresh"]
    assert out["deleted_unverified"] == 1


async def test_refused_rows_go_7_days_after_the_last_refusal_unless_applied_elsewhere(db):
    await offer(db, "recentfail", observed_days=1)
    await verification(db, "recentfail", "failed", updated_days=6)
    await offer(db, "oldfail", observed_days=1)
    await verification(db, "oldfail", "failed", updated_days=8)
    await offer(db, "mixed", observed_days=1)
    await verification(db, "mixed", "failed", updated_days=8, variant="1")
    await verification(db, "mixed", "checkout_verified", variant="2")
    out = await r.run(now=NOW)
    assert await ids(db) == ["mixed", "recentfail"]
    assert out["deleted_refused"] == 1


async def test_applied_rows_stay_while_valid_and_go_30_days_after_they_stopped_being_valid(db):
    await offer(db, "valid", observed_days=40)
    await verification(db, "valid", "checkout_verified", expires_days=1)
    await offer(db, "lately", observed_days=40)
    await verification(db, "lately", "checkout_verified", expires_days=-29)
    await offer(db, "longgone", observed_days=40)
    await verification(db, "longgone", "checkout_verified", expires_days=-31)
    out = await r.run(now=NOW)
    assert await ids(db) == ["lately", "valid"]
    assert out["deleted_applied_expired"] == 1


@pytest.mark.parametrize("optout_id", ["glow.example", "www.glow.example", "merch_obs_glow"])
async def test_an_opted_out_seller_loses_every_row_at_once(db, optout_id):
    from db.catalog import catalog_products
    from db.deal_lane import deal_lane_optout

    await db.execute(catalog_products.insert().values(product_key="p1", merchant_id="merch_obs_glow", platform="shopify",
                                                      source_product_id="1", source_domain="www.glow.example", title="t"))
    await offer(db, "glow1")
    await offer(db, "glow2")
    await verification(db, "glow2", "checkout_verified")
    await offer(db, "other", seller="other.example")
    await db.execute(deal_lane_optout.insert().values(seller_id=optout_id, requested_at=NOW, recorded_by="test"))
    out = await r.run(now=NOW)
    assert await ids(db) == ["other"]
    assert out["deleted_optout"] == 2


async def test_bar_text_is_cleared_once_checked_or_after_14_days(db):
    from db.deal_lane import observed_store_offers as o

    await offer(db, "young")
    await offer(db, "refused")
    await verification(db, "refused", "failed")
    await offer(db, "applied")
    await verification(db, "applied", "checkout_verified")
    await offer(db, "queued")  # a verification queued but not yet checked keeps the text the verifier may need (P3-2)
    await verification(db, "queued", "unverified")
    await offer(db, "oldtext", observed_days=1, created_days=15)
    out = await r.run(now=NOW)
    texts = {row["deal_id"].split("_", 1)[1]: row["terms_text"] for row in await db.fetch_all(o.select())}
    assert texts == {"young": "Get 15% off with code X", "refused": None, "applied": None,
                     "queued": "Get 15% off with code X", "oldtext": None}
    assert out["cleared_terms_text"] == 3


async def test_other_sources_are_never_touched(db):
    await offer(db, "feedold", source="authorized_feed", observed_days=60, created_days=60)
    await offer(db, "chanold", source="channel_exclusive", observed_days=60, created_days=60)
    await r.run(now=NOW)
    assert await ids(db) == ["chanold", "feedold"]


async def test_batches_and_the_per_run_cap_bound_the_work_and_the_next_run_resumes(db, monkeypatch):
    monkeypatch.setattr(r, "BATCH", 2)
    monkeypatch.setattr(r, "MAX_ROWS_PER_RULE", 3)
    for i in range(5):
        await offer(db, f"stale{i}", observed_days=20)
    first = await r.run(now=NOW)
    assert first["deleted_unverified"] == 3
    assert len(await ids(db)) == 2
    second = await r.run(now=NOW)
    assert second["deleted_unverified"] == 2
    assert await ids(db) == []
    third = await r.run(now=NOW)
    assert third["deleted_unverified"] == 0


async def test_the_kill_switch_stops_it(db, monkeypatch):
    await offer(db, "stale", observed_days=20)
    monkeypatch.setenv("DEAL_CODE_RETENTION_PAUSED", "true")
    assert await r.run(now=NOW) == {"paused": 1}
    assert await ids(db) == ["stale"]
    monkeypatch.setenv("DEAL_CODE_RETENTION_PAUSED", "1")  # only "true" pauses it
    await r.run(now=NOW)
    assert await ids(db) == []


async def test_one_rules_failure_never_stops_the_others_and_logs_counts_only(db, monkeypatch, caplog):
    await offer(db, "stale", seller="secret-shop.example", code="SECRETCODE", observed_days=20)
    real = r._delete_opted_out

    async def boom(*a, **k):
        raise RuntimeError("optout table gone SECRETCODE")

    monkeypatch.setattr(r, "_delete_opted_out", boom)
    caplog.set_level(logging.WARNING)
    out = await r.run(now=NOW)
    assert out["errors"] == 1 and out["deleted_unverified"] == 1
    assert await ids(db) == []
    text = caplog.text
    assert "SECRETCODE" not in text and "secret-shop" not in text and "optout table gone" not in text
    assert "deleted_unverified" in text  # the counts are logged
    monkeypatch.setattr(r, "_delete_opted_out", real)


async def test_nothing_raises_when_the_database_is_down(monkeypatch):
    from db.database import database

    async def down(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(database, "fetch_all", down)
    monkeypatch.setattr(database, "execute", down)
    out = await r.run(now=NOW)
    assert out["errors"] == 5  # every rule failed, none raised



async def test_the_refresh_job_runs_retention_whatever_the_write_flag_says(db, monkeypatch):
    import jobs.external_referral_refresh as job

    async def fake_batch(**kwargs):
        return {"status": "success", "rows": 1}

    monkeypatch.setattr(job, "run_external_referral_refresh_batch", fake_batch)
    for name in ("DEAL_CODE_CAPTURE_WRITE", "DEAL_CODE_CAPTURE_SHADOW"):
        monkeypatch.delenv(name, raising=False)
    # Nothing to expire: the summary is unchanged.
    assert await job.run_daily_external_referral_refresh(limit=1) == {"status": "success", "rows": 1}
    # A row written while the write flag WAS on, now off (a rollback), still expires.
    await offer(db, "leftover", observed_days=30)
    summary = await job.run_daily_external_referral_refresh(limit=1)
    assert summary["deal_code_retention"]["deleted_unverified"] == 1
    assert await ids(db) == []



async def test_a_merchant_published_row_the_crawl_writer_did_not_create_is_never_touched(db):
    from db.deal_lane import observed_store_offers as o

    await offer(db, "handentered", observed_days=60, created_days=60, crawl=False)
    await offer(db, "crawled", observed_days=60, created_days=60)
    out = await r.run(now=NOW)
    rows = {row["deal_id"]: row for row in await db.fetch_all(o.select())}
    assert list(rows) == ["dl_handentered"]
    assert rows["dl_handentered"]["terms_text"] == "Get 15% off with code X"
    assert out["deleted_unverified"] == 1


async def test_the_prefix_is_matched_literally_not_as_a_like_wildcard(db):
    from db.deal_lane import observed_store_offer_key, observed_store_offers as o

    # "dlcX..." would match an unescaped LIKE 'dlc_%'.
    key = observed_store_offer_key(seller_id="glow.example", market="US", code="LOOKALIKE", platform_rule_ref=None, program_id=None)
    await db.execute(o.insert().values(deal_id="dlcXlookalike", deal_key=key, seller_id="glow.example", market="US",
                                       code="LOOKALIKE", kind="percent", value=10, source="merchant_published",
                                       terms_text="bar text", observed_at=NOW - timedelta(days=60),
                                       created_at=NOW - timedelta(days=60)))
    await r.run(now=NOW)
    rows = [(row["deal_id"], row["terms_text"]) for row in await db.fetch_all(o.select())]
    assert rows == [("dlcXlookalike", "bar text")], "neither deleted nor its bar text cleared"



async def test_the_opt_out_is_case_insensitive_and_has_no_id_limit(db):
    from db.catalog import catalog_products
    from db.deal_lane import deal_lane_optout

    for i in range(30):
        await db.execute(catalog_products.insert().values(product_key=f"p{i}", merchant_id=f"merch_obs_{i:02d}", platform="shopify",
                                                          source_product_id=str(i), source_domain="WWW.Glow.Example", title="t"))
    await offer(db, "glow1")
    await db.execute(deal_lane_optout.insert().values(seller_id="MERCH_OBS_29", requested_at=NOW, recorded_by="test"))
    out = await r.run(now=NOW)
    assert await ids(db) == []
    assert out["deleted_optout"] == 1



# ── exact boundaries (review of be#2735, P2-4): exactly N days is kept, one second past is deleted ──────────────────
SECOND = timedelta(seconds=1)


@pytest.mark.parametrize("past,kept", [(timedelta(days=14), True), (timedelta(days=14) + SECOND, False)])
async def test_the_unverified_boundary_is_exactly_14_days(db, past, kept):
    await offer(db, "edge", observed_days=past)
    await r.run(now=NOW)
    assert (await ids(db) == ["edge"]) is kept


@pytest.mark.parametrize("past,kept", [(timedelta(days=7), True), (timedelta(days=7) + SECOND, False)])
async def test_the_refused_boundary_is_exactly_7_days_after_the_last_refusal(db, past, kept):
    await offer(db, "edge", observed_days=1)
    await verification(db, "edge", "failed", updated_days=past)
    await r.run(now=NOW)
    assert (await ids(db) == ["edge"]) is kept


@pytest.mark.parametrize("past,kept", [(timedelta(days=30), True), (timedelta(days=30) + SECOND, False)])
async def test_the_applied_boundary_is_exactly_30_days_after_the_last_valid_until(db, past, kept):
    await offer(db, "edge", observed_days=60)
    await verification(db, "edge", "checkout_verified", expires_days=-past)
    await r.run(now=NOW)
    assert (await ids(db) == ["edge"]) is kept


@pytest.mark.parametrize("past,kept", [(timedelta(days=14), True), (timedelta(days=14) + SECOND, False)])
async def test_the_bar_text_boundary_is_exactly_14_days_after_creation(db, past, kept):
    from db.deal_lane import observed_store_offers as o

    await offer(db, "edge", observed_days=1, created_days=past)
    await r.run(now=NOW)
    text = (await db.fetch_one(o.select()))["terms_text"]
    assert (text is not None) is kept


async def test_a_code_refused_twice_is_kept_7_days_after_the_LAST_refusal(db):
    await offer(db, "twice", observed_days=1)
    await verification(db, "twice", "failed", updated_days=10, variant="1")
    await verification(db, "twice", "failed", updated_days=3, variant="2")
    await r.run(now=NOW)
    assert await ids(db) == ["twice"]


async def test_a_recent_refusal_keeps_a_row_not_seen_for_more_than_14_days(db):
    await offer(db, "refusedlately", observed_days=20)
    await verification(db, "refusedlately", "failed", updated_days=2)
    await r.run(now=NOW)
    assert await ids(db) == ["refusedlately"]


async def test_an_applied_row_with_a_refused_variant_follows_the_applied_clock(db):
    # Valid for one variant, refused for another: kept by validity.
    await offer(db, "validmix", observed_days=60)
    await verification(db, "validmix", "checkout_verified", expires_days=1, variant="1")
    await verification(db, "validmix", "failed", updated_days=20, variant="2")
    # Validity ended 31 days ago; a refusal 2 days ago (no verified_at) is not applied history and does not extend it.
    await offer(db, "endedmix", observed_days=60)
    await verification(db, "endedmix", "checkout_verified", expires_days=-31, variant="1")
    await verification(db, "endedmix", "failed", updated_days=2, variant="2")
    out = await r.run(now=NOW)
    assert await ids(db) == ["validmix"]
    assert out["deleted_applied_expired"] == 1


# ── a lapsed verification keeps the applied clock (review of be#2735, P2-2) ─────────────────────────────────────────
@pytest.mark.parametrize("state", ["expired", "unverified"])
async def test_a_lapsed_verification_keeps_the_30_day_grace_whatever_its_state_now(db, state):
    await offer(db, "lapsed", observed_days=20)
    await verification(db, "lapsed", state, lapsed_days=1)
    await offer(db, "lapsedlong", observed_days=20)
    await verification(db, "lapsedlong", state, lapsed_days=31)
    out = await r.run(now=NOW)
    assert await ids(db) == ["lapsed"]
    assert out["deleted_applied_expired"] == 1 and out["deleted_unverified"] == 0


async def test_an_expired_verification_counts_as_applied_even_without_verified_at(db):
    from db.deal_lane import observed_store_offer_verifications as v

    await offer(db, "expirednoat", observed_days=20)
    await db.execute(v.insert().values(deal_id="dlc_expirednoat", variant_id="1", state="expired",
                                       expires_at=NOW - timedelta(days=1), updated_at=NOW - timedelta(days=1)))
    out = await r.run(now=NOW)
    assert await ids(db) == ["expirednoat"]
    assert out["deleted_unverified"] == 0


async def test_a_refused_row_once_applied_follows_the_applied_clock_not_the_refusal_clock(db):
    await offer(db, "wasapplied", observed_days=20)
    await verification(db, "wasapplied", "expired", lapsed_days=5, variant="1")
    await verification(db, "wasapplied", "failed", updated_days=10, variant="2")
    out = await r.run(now=NOW)
    assert await ids(db) == ["wasapplied"]
    assert out["deleted_refused"] == 0


# ── the opt-out (review of be#2735, P2-1, P2-3, P3-8) ──────────────────────────────────────────────────────────────
async def _catalog(db, i, merchant, domain):
    from db.catalog import catalog_products

    await db.execute(catalog_products.insert().values(product_key=f"p{i}", merchant_id=merchant, platform="shopify",
                                                      source_product_id=str(i), source_domain=domain, title="t"))


async def _optout(db, seller_id):
    from db.deal_lane import deal_lane_optout

    await db.execute(deal_lane_optout.insert().values(seller_id=seller_id, requested_at=NOW, recorded_by="test"))


async def test_the_placeholder_merchant_id_never_reaches_the_catalog(db, caplog):
    await _catalog(db, 1, "external_seed", "a.example")
    await _catalog(db, 2, "external_seed", "b.example")
    await offer(db, "a", seller="a.example")
    await offer(db, "b", seller="b.example")
    await _optout(db, "External_Seed")
    caplog.set_level(logging.WARNING)
    out = await r.run(now=NOW)
    assert await ids(db) == ["a", "b"]
    assert out["deleted_optout"] == 0
    assert out["optout_ids_placeholder"] == 1  # in the summary: that opt-out row denies nothing
    assert "optout_ids_placeholder" in caplog.text and "External_Seed" not in caplog.text


async def test_a_merchant_id_over_the_host_limit_deletes_nothing_and_is_reported_in_the_summary(db, caplog):
    # Four different registrable domains: not one seller's. Retention deletes nothing through it (the writer blocks
    # writes on all four: tests/test_deal_code_capture_writer.py) and the summary says so on every run.
    for i, host in enumerate(["a.example", "b.example", "c.example", "d.example"]):
        await _catalog(db, i, "merch_wide", host)
        await offer(db, host[0], seller=host)
    await _optout(db, "merch_wide")
    caplog.set_level(logging.WARNING)
    out = await r.run(now=NOW)
    assert await ids(db) == ["a", "b", "c", "d"]
    assert out["deleted_optout"] == 0 and out["optout_ids_too_broad"] == 1
    assert "optout_ids_too_broad" in caplog.text and "merch_wide" not in caplog.text


@pytest.mark.parametrize("hosts", [
    # apex, a shop. subdomain, a regional domain and the platform tenant: 3 registrable domains (review P2-N1, D8).
    ["store.example", "shop.store.example", "store.example.sg", "store-1.myshopify.com"],
    # an apex and three of its own subdomains: one registrable domain (D8c).
    ["store.example", "shop.store.example", "us.store.example", "eu.store.example"],
])
async def test_one_stores_hosts_collapse_to_registrable_domains_before_the_limit(db, hosts):
    for i, host in enumerate(hosts):
        await _catalog(db, i, "merch_store", host)
        await offer(db, f"h{i}", seller=host)
    await offer(db, "unrelated", seller="other.example")
    await _optout(db, "merch_store")
    out = await r.run(now=NOW)
    assert await ids(db) == ["unrelated"]
    assert out["deleted_optout"] == len(hosts) and "optout_ids_too_broad" not in out


async def test_a_merchant_id_on_a_few_hosts_and_its_www_forms_counts_each_host_once(db):
    # www.a.example and a.example are one host; with b and c that is 3 hosts, the limit: applied.
    for i, host in enumerate(["www.a.example", "a.example", "b.example", "c.example"]):
        await _catalog(db, i, "merch_few", host)
    for host in ("a.example", "b.example", "c.example", "z.example"):
        await offer(db, host[0], seller=host)
    await _optout(db, "merch_few")
    out = await r.run(now=NOW)
    assert await ids(db) == ["z"]
    assert out["deleted_optout"] == 3


async def test_the_opt_out_trims_the_id_and_matches_a_www_stored_seller(db):
    await offer(db, "bare")
    await offer(db, "wwwseller", seller="www.glow.example")
    await offer(db, "other", seller="other.example")
    await _optout(db, "  Glow.Example \t")
    out = await r.run(now=NOW)
    assert await ids(db) == ["other"]
    assert out["deleted_optout"] == 2


async def test_the_opt_out_reads_a_fixed_number_of_times_whatever_the_row_count(db, monkeypatch):
    from db.database import database

    await _catalog(db, 1, "merch_a", "a.example")
    for k in range(3):
        await _optout(db, f"nomatch{k}.example")
    await _optout(db, "merch_a")
    for i in range(40):
        await offer(db, f"row{i}", seller=f"s{i}.example")
    calls = []
    real = database.fetch_all

    async def counting(query, *a, **k):
        calls.append(1)
        return await real(query, *a, **k)

    monkeypatch.setattr(database, "fetch_all", counting)
    assert await r._delete_opted_out() == 0
    # One read of the opt-out table, one of the catalog by merchant id (host ids skip the catalog), one membership
    # select: never one per row or per opt-out.
    assert len(calls) == 3


async def test_no_opt_out_rows_means_one_read_and_nothing_else(db, monkeypatch):
    from db.database import database

    await offer(db, "row")
    calls = []
    real = database.fetch_all

    async def counting(query, *a, **k):
        calls.append(1)
        return await real(query, *a, **k)

    monkeypatch.setattr(database, "fetch_all", counting)
    assert await r._delete_opted_out() == 0
    assert len(calls) == 1


# ── deletes re-check their rule; no endless loop (review of be#2735, P3-5, P3-10) ─────────────────────────────────
async def test_a_delete_re_checks_its_condition(db):
    from db.deal_lane import observed_store_offers as o

    await offer(db, "stale", observed_days=20)
    # The rule no longer holds when the DELETE runs (say a verification landed): the row is kept, and counted as kept.
    assert await r._delete_ids(["dlc_stale"], o.c.observed_at > NOW) == 0
    assert await ids(db) == ["stale"]


async def test_rows_the_prefix_belt_refuses_end_the_loop(db, monkeypatch):
    import asyncio

    from db.deal_lane import observed_store_offer_key, observed_store_offers as o

    monkeypatch.setattr(r, "BATCH", 2)
    # SQLite's LIKE is case-insensitive, so the SELECT returns these; the case-sensitive belt refuses them.
    for i in range(3):
        key = observed_store_offer_key(seller_id="glow.example", market="US", code=f"UP{i}", platform_rule_ref=None, program_id=None)
        await db.execute(o.insert().values(deal_id=f"DLC_up{i}", deal_key=key, seller_id="glow.example", market="US",
                                           code=f"UP{i}", kind="percent", value=10, source="merchant_published",
                                           observed_at=NOW - timedelta(days=60), created_at=NOW - timedelta(days=60)))
    await asyncio.wait_for(r.run(now=NOW), timeout=10)
    assert len(await db.fetch_all(o.select())) == 3



async def test_an_opted_out_host_covers_its_subdomains_and_padded_sellers_but_not_look_alikes_or_platforms(db):
    await offer(db, "sub", seller="shop.glow.example")
    await offer(db, "padded", seller="  Glow.Example ")
    await offer(db, "lookalike", seller="notglow.example")
    await offer(db, "platform", seller="brand.myshopify.com")
    await _optout(db, "https://www.glow.example/")
    await _optout(db, "myshopify.com")
    out = await r.run(now=NOW)
    assert await ids(db) == ["lookalike", "platform"]
    assert out["deleted_optout"] == 2



# ── public suffixes and shared platforms never anchor subdomains (review of be#2735, P3-N3) ─────────────────────────
@pytest.mark.parametrize("optout,catalog", [("co.uk", None), ("merch_sg", "com.sg"), ("wixsite.com", None),
                                             ("merch_wix", "wixsite.com"), ("myshopify.com", None)])
async def test_a_public_suffix_or_platform_opt_out_never_deletes_the_stores_under_it(db, optout, catalog):
    if catalog:
        await _catalog(db, 1, optout, catalog)
    for i, host in enumerate(["brand9.co.uk", "shop.com.sg", "s4.wixsite.com", "s1.myshopify.com"]):
        await offer(db, f"h{i}", seller=host)
    await _optout(db, optout)
    out = await r.run(now=NOW)
    assert len(await ids(db)) == 4
    assert out.get("deleted_optout", 0) == 0


async def test_an_explicit_platform_tenant_covers_its_own_subdomains(db):
    await offer(db, "tenant", seller="s2.myshopify.com")
    await offer(db, "tenantsub", seller="x.s2.myshopify.com")
    await offer(db, "othertenant", seller="s3.myshopify.com")
    await _optout(db, "s2.myshopify.com")
    await r.run(now=NOW)
    assert await ids(db) == ["othertenant"]


async def test_a_wildcard_opt_out_names_the_host_and_its_subdomains(db):
    await offer(db, "apex")
    await offer(db, "sub", seller="shop.glow.example")
    await offer(db, "other", seller="other.example")
    await _optout(db, "*.glow.example")
    await r.run(now=NOW)
    assert await ids(db) == ["other"]


async def test_an_underscore_in_a_host_is_matched_literally(db):
    await offer(db, "sub", seller="s.a_b.example")
    await offer(db, "lookalike", seller="s.aXb.example")
    await _optout(db, "a_b.example")
    await r.run(now=NOW)
    assert await ids(db) == ["lookalike"]


# ── canonical-URL hosts block writes but delete nothing; each id has its own read (review of be#2735, P3-N2) ───────
async def test_canonical_url_hosts_delete_nothing_and_are_reported(db):
    from db.catalog import catalog_products

    await db.execute(catalog_products.insert().values(product_key="c1", merchant_id="merch_nulldomain", platform="shopify",
                                                      source_product_id="c1", canonical_url="https://brand-x.example/p/1", title="t"))
    await offer(db, "brandx", seller="brand-x.example")
    await _optout(db, "merch_nulldomain")
    out = await r.run(now=NOW)
    assert await ids(db) == ["brandx"]
    assert out["optout_ids_canonical_only"] == 1


# ── applied evidence and the TTL clamp (review of be#2735, P3-N4) ────────────────────────────────────────────────────
async def test_a_failed_check_stamped_with_verified_at_but_no_discount_follows_the_refusal_clock(db):
    from db.deal_lane import observed_store_offer_verifications as v

    await offer(db, "stamped", observed_days=20)
    await db.execute(v.insert().values(deal_id="dlc_stamped", variant_id="1", state="failed", failure_reason="x",
                                       verified_at=NOW - timedelta(days=8), updated_at=NOW - timedelta(days=8)))
    out = await r.run(now=NOW)
    assert await ids(db) == []
    assert out["deleted_refused"] == 1


async def test_an_expires_at_decades_out_is_clamped_to_the_verification_ttl(db):
    from db.deal_lane import observed_store_offer_verifications as v

    await offer(db, "forever", observed_days=400)
    await db.execute(v.insert().values(
        deal_id="dlc_forever", variant_id="1", state="checkout_verified", verified_at=NOW - timedelta(days=32),
        expires_at=NOW + timedelta(days=36500), verified_via="ucp_checkout", authorized_by="q2_approval",
        ref_currency="USD", ref_unit_price=25, ref_subtotal=25, ref_discount_amount=5, ref_automatic_discounts=[],
        updated_at=NOW - timedelta(days=32)))
    out = await r.run(now=NOW)
    assert await ids(db) == []
    assert out["deleted_applied_expired"] == 1


async def test_the_latest_of_several_applied_variants_sets_the_clock(db):
    await offer(db, "variants", observed_days=60)
    await verification(db, "variants", "expired", lapsed_days=40, variant="1")
    await verification(db, "variants", "expired", lapsed_days=1, variant="2")
    out = await r.run(now=NOW)
    assert await ids(db) == ["variants"]
    assert out["deleted_applied_expired"] == 0
