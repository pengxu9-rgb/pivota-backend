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


async def offer(db, deal_id, *, seller="glow.example", code=None, source="merchant_published", observed_days=0,
                created_days=0, terms="Get 15% off with code X"):
    from db.deal_lane import observed_store_offer_key, observed_store_offers as o

    code = code or deal_id.upper()
    key = observed_store_offer_key(seller_id=seller, market="US", code=code, platform_rule_ref=None,
                                   program_id="prog_1" if source == "authorized_feed" else None)
    await db.execute(o.insert().values(
        deal_id=deal_id, deal_key=key, seller_id=seller, market="US", code=code, kind="percent", value=15,
        source=source, program_id="prog_1" if source == "authorized_feed" else None, terms_text=terms,
        observed_at=NOW - timedelta(days=observed_days), created_at=NOW - timedelta(days=created_days),
        updated_at=NOW - timedelta(days=observed_days),
    ))


async def verification(db, deal_id, state, *, updated_days=0, expires_days=None, variant="44012345678901"):
    from db.deal_lane import observed_store_offer_verifications as v

    values = dict(deal_id=deal_id, variant_id=variant, state=state, updated_at=NOW - timedelta(days=updated_days))
    if state == "checkout_verified":
        expires_at = NOW + timedelta(days=expires_days if expires_days is not None else 1)
        values.update(verified_at=expires_at - timedelta(days=1), verified_via="ucp_checkout", authorized_by="q2_approval",
                      ref_currency="USD", ref_unit_price=25, ref_subtotal=25, ref_discount_amount=5, ref_automatic_discounts=[],
                      expires_at=expires_at)
    if state == "failed":
        values.update(failure_reason="discount_code_invalid")
    await db.execute(v.insert().values(**values))


async def ids(db):
    from db.deal_lane import observed_store_offers as o

    return sorted(row["deal_id"] for row in await db.fetch_all(o.select()))


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
    await offer(db, "checked")
    await verification(db, "checked", "unverified")
    await offer(db, "oldtext", observed_days=1, created_days=15)
    out = await r.run(now=NOW)
    texts = {row["deal_id"]: row["terms_text"] for row in await db.fetch_all(o.select())}
    assert texts == {"young": "Get 15% off with code X", "checked": None, "oldtext": None}
    assert out["cleared_terms_text"] == 2


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
    real = r._opted_out_seller_ids

    async def boom():
        raise RuntimeError("optout table gone SECRETCODE")

    monkeypatch.setattr(r, "_opted_out_seller_ids", boom)
    caplog.set_level(logging.WARNING)
    out = await r.run(now=NOW)
    assert out["errors"] == 1 and out["deleted_unverified"] == 1
    assert await ids(db) == []
    text = caplog.text
    assert "SECRETCODE" not in text and "secret-shop" not in text and "optout table gone" not in text
    assert "deleted_unverified" in text  # the counts are logged
    monkeypatch.setattr(r, "_opted_out_seller_ids", real)


async def test_nothing_raises_when_the_database_is_down(monkeypatch):
    from db.database import database

    async def down(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(database, "fetch_all", down)
    monkeypatch.setattr(database, "execute", down)
    out = await r.run(now=NOW)
    assert out["errors"] == 5



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
