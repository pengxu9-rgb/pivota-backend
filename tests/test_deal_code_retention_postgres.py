"""Retention of crawled codes (services/deal_code_retention.py) and the writer's opt-out predicate, executed on real
Postgres. Retention runs on EVERY refresh run, so its SQL must hold on the production dialect: TIMESTAMPTZ comparisons,
the correlated MAX() and EXISTS subqueries, `LIKE ... ESCAPE` for the deal_id prefix, and `'www.' || lower(...)` with a
bound host. The rules themselves: tests/test_deal_code_retention.py (SQLite).

    DATABASE_URL=postgresql://pgtest@127.0.0.1:55433/pivota_retention_dialect_check \\
        .venv/bin/python -m pytest tests/test_deal_code_retention_postgres.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")]

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_MIG_275 = Path(__file__).resolve().parent.parent / "db" / "migrations" / "275_deal_lane_tables.sql"
_TABLES = ("observed_store_offer_verifications", "observed_store_offers", "deal_lane_stops", "deal_lane_optout")
_PREFIX = "dcr-gate-"
# The gate shares ONE database across files; another file may have created catalog_products with a narrower schema.
_CATALOG_DDL = "ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS source_domain text"


@pytest.fixture
async def db(monkeypatch):
    from sqlalchemy import create_engine

    from db.catalog import catalog_products
    from db.database import database, metadata
    from db.sql_migrations import split_statements

    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to write deal rows in {dbname!r}; throwaway only")
    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine, tables=[catalog_products], checkfirst=True)
    engine.dispose()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    for stmt in split_statements(_MIG_275.read_text()):  # IF NOT EXISTS throughout
        await database.execute(stmt)
    await database.execute(_CATALOG_DDL)

    async def _clean():
        for table in _TABLES:
            await database.execute(f"DELETE FROM {table}")
        await database.execute("DELETE FROM catalog_products WHERE product_key LIKE :p", {"p": _PREFIX + "%"})

    await _clean()
    from services import deal_code_capture

    deal_code_capture.reset_stats()  # a new run: the writer rereads the denylist
    monkeypatch.delenv("DEAL_CODE_RETENTION_PAUSED", raising=False)
    try:
        yield database
    finally:
        await _clean()
        if not was_connected:
            await database.disconnect()


async def _catalog_row(db, i, *, merchant, source_domain):
    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, source_domain)"
        " VALUES (:k, :m, 'shopify', :s, 't', :d)",
        {"k": f"{_PREFIX}{i}", "m": merchant, "s": str(i), "d": source_domain},
    )


async def test_every_rule_runs_on_postgres_and_deletes_exactly_what_it_names(db):
    from services import deal_code_retention as r
    from tests.test_deal_code_retention import NOW, ids, offer, verification

    await offer(db, "fresh", observed_days=13)
    await offer(db, "stale", observed_days=15)
    await offer(db, "oldfail", observed_days=1)
    await verification(db, "oldfail", "failed", updated_days=8)
    await offer(db, "recentfail", observed_days=1)
    await verification(db, "recentfail", "failed", updated_days=6)
    await offer(db, "lately", observed_days=40)
    await verification(db, "lately", "checkout_verified", expires_days=-29)
    await offer(db, "longgone", observed_days=40)
    await verification(db, "longgone", "checkout_verified", expires_days=-31)
    await offer(db, "handentered", observed_days=60, created_days=60, crawl=False)
    out = await r.run(now=NOW)
    assert "errors" not in out, out
    assert out["deleted_unverified"] == 1 and out["deleted_refused"] == 1 and out["deleted_applied_expired"] == 1
    assert await ids(db) == ["fresh", "handentered", "lately", "recentfail"]
    assert (await r.run(now=NOW)).get("errors") is None  # idempotent, still no error


async def test_bar_text_clearing_and_the_literal_prefix_on_postgres(db):
    from db.deal_lane import observed_store_offer_key, observed_store_offers as o
    from services import deal_code_retention as r
    from tests.test_deal_code_retention import NOW, offer, verification
    from datetime import timedelta

    await offer(db, "young")
    await offer(db, "checked")
    await verification(db, "checked", "failed")
    key = observed_store_offer_key(seller_id="glow.example", market="US", code="LOOKALIKE", platform_rule_ref=None, program_id=None)
    await db.execute(o.insert().values(deal_id="dlcXlookalike", deal_key=key, seller_id="glow.example", market="US",
                                       code="LOOKALIKE", kind="percent", value=10, source="merchant_published",
                                       terms_text="bar text", observed_at=NOW - timedelta(days=60),
                                       created_at=NOW - timedelta(days=60)))
    out = await r.run(now=NOW)
    assert "errors" not in out, out
    texts = {row["deal_id"]: row["terms_text"] for row in await db.fetch_all(o.select())}
    assert texts == {"dlc_young": "Get 15% off with code X", "dlc_checked": None, "dlcXlookalike": "bar text"}


@pytest.mark.parametrize("optout_id", ["glow.example", "WWW.Glow.Example", "MERCH_OBS_GLOW"])
async def test_the_opt_out_deletes_one_sellers_rows_on_postgres(db, optout_id):
    from db.deal_lane import deal_lane_optout
    from services import deal_code_retention as r
    from tests.test_deal_code_retention import NOW, ids, offer

    await _catalog_row(db, 1, merchant="merch_obs_glow", source_domain="www.Glow.example")
    await _catalog_row(db, 2, merchant="merch_obs_other", source_domain="other.example")
    await offer(db, "glow1")
    await offer(db, "glow2")
    await offer(db, "other", seller="other.example")
    await db.execute(deal_lane_optout.insert().values(seller_id=optout_id, requested_at=NOW, recorded_by="test"))
    out = await r.run(now=NOW)
    assert "errors" not in out, out
    assert out["deleted_optout"] == 2
    assert await ids(db) == ["other"]


@pytest.mark.parametrize("optout_id,hit", [
    ("glow.example", True), ("www.glow.example", True), ("Merch_Obs_Glow", True), ("merch_obs_other", False),
    ("glow.exampl", False),
])
async def test_the_writers_opt_out_check_binds_its_host_on_postgres(db, optout_id, hit):
    from db.deal_lane import deal_lane_optout
    from services import deal_code_capture_writer as w
    from tests.test_deal_code_retention import NOW

    await _catalog_row(db, 1, merchant="merch_obs_glow", source_domain="WWW.glow.example")
    await _catalog_row(db, 2, merchant="merch_obs_other", source_domain="other.example")
    await db.execute(deal_lane_optout.insert().values(seller_id=optout_id, requested_at=NOW, recorded_by="test"))
    assert await w._opted_out("glow.example") is hit


# The rules' exact boundaries and the verification lifecycle, executed on Postgres (TIMESTAMPTZ) with this file's `db`
# fixture. Only cases that write no catalog_products rows: those are shared across gate files.
from tests.test_deal_code_retention import (  # noqa: E402,F401
    test_a_code_refused_twice_is_kept_7_days_after_the_LAST_refusal,
    test_a_delete_re_checks_its_condition,
    test_a_failed_check_stamped_with_verified_at_but_no_discount_follows_the_refusal_clock,
    test_a_wildcard_opt_out_names_the_host_and_its_subdomains,
    test_an_explicit_platform_tenant_covers_its_own_subdomains,
    test_an_expires_at_decades_out_is_clamped_to_the_verification_ttl,
    test_an_underscore_in_a_host_is_matched_literally,
    test_the_latest_of_several_applied_variants_sets_the_clock,
    test_a_lapsed_verification_keeps_the_30_day_grace_whatever_its_state_now,
    test_a_recent_refusal_keeps_a_row_not_seen_for_more_than_14_days,
    test_a_refused_row_once_applied_follows_the_applied_clock_not_the_refusal_clock,
    test_an_expired_verification_counts_as_applied_even_without_verified_at,
    test_an_applied_row_with_a_refused_variant_follows_the_applied_clock,
    test_an_opted_out_host_covers_its_subdomains_and_padded_sellers_but_not_look_alikes_or_platforms,
    test_the_applied_boundary_is_exactly_30_days_after_the_last_valid_until,
    test_the_bar_text_boundary_is_exactly_14_days_after_creation,
    test_the_opt_out_trims_the_id_and_matches_a_www_stored_seller,
    test_the_refused_boundary_is_exactly_7_days_after_the_last_refusal,
    test_the_unverified_boundary_is_exactly_14_days,
    # review of be#2735 delta 3 (P3-R4, P3-R5)
    test_a_failed_check_with_no_discount_is_not_applied,
    test_a_requeued_row_that_kept_its_amount_but_no_expiry_keeps_its_grace,
    test_an_expired_row_without_timestamps_runs_on_updated_at_to_the_second,
    test_an_expired_row_without_verified_at_runs_on_its_past_expiry,
    test_a_timestamp_in_the_future_never_keeps_a_row_forever,
    test_the_expiry_only_leg_boundary_is_exactly_30_days,
    # review of be#2742 delta (Q07, the updated_at horizon)
    test_an_expiry_exactly_one_ttl_ahead_is_trusted_and_a_microsecond_later_is_not,
    test_an_updated_at_past_the_horizon_is_no_evidence,
)


async def test_the_opt_out_rule_stays_cheap_at_scale(db):
    """Review of be#2735, P2-1: 30k catalog rows, 2k crawl rows and 3 opt-outs took 44 s with the correlated predicate.
    The denied host set is read once, so the whole run stays well under a few seconds."""
    import time

    from services import deal_code_retention as r
    from tests.test_deal_code_retention import NOW

    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, source_domain)"
        " SELECT :p || g, 'dcr_m' || (g % 3000), 'shopify', 'dcr' || g, 't', 'h' || (g % 3000) || '.example'"
        " FROM generate_series(1, 30000) AS g",
        {"p": _PREFIX},
    )
    await db.execute(
        "INSERT INTO observed_store_offers (deal_id, deal_key, seller_id, market, code, kind, value, source, observed_at)"
        " SELECT 'dlc_perf' || g, md5('perf' || g), 's' || g || '.example', 'US', 'C' || g, 'percent', 10,"
        " 'merchant_published', :now FROM generate_series(1, 2000) AS g",
        {"now": NOW},
    )
    for optout in ("dcr_m5", "merch_nomatch", "nomatch.example"):
        await db.execute("INSERT INTO deal_lane_optout (seller_id, requested_at, recorded_by) VALUES (:s, :t, 'test')",
                         {"s": optout, "t": NOW})
    started = time.monotonic()
    out = await r.run(now=NOW)
    elapsed = time.monotonic() - started
    assert "errors" not in out, out
    assert not any(n for k, n in out.items() if k.startswith(("deleted_", "cleared_"))), out
    assert out.get("optout_ids_unmapped") == 1  # merch_nomatch: no catalog rows, reported
    assert elapsed < 5, f"retention took {elapsed:.1f}s"


async def test_the_canonical_url_fallback_reads_each_id_on_postgres(db, monkeypatch):
    """Review of be#2735, P3-N2: each NULL-domain id reads its own distinct canonical URLs (DISTINCT + ORDER BY +
    LIMIT on Postgres); a big id never starves a small one; the hosts block writes and delete nothing."""
    from services import deal_code_capture_writer as w
    from services import deal_code_retention as r
    from tests.test_deal_code_retention import NOW, ids, offer

    monkeypatch.setattr(w, "CANONICAL_HOSTS_PER_ID", 2)
    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, canonical_url)"
        " SELECT :p || 'big' || g, 'merch_big', 'shopify', 'big' || g, 't', 'https://big.example/p/' || g"
        " FROM generate_series(1, 50) AS g",
        {"p": _PREFIX},
    )
    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, canonical_url)"
        " VALUES (:k, 'merch_small', 'shopify', 'small1', 't', 'https://small.example/p/1')",
        {"k": _PREFIX + "small"},
    )
    for mid in ("merch_big", "merch_small"):
        await db.execute("INSERT INTO deal_lane_optout (seller_id, requested_at, recorded_by) VALUES (:s, :t, 'test')",
                         {"s": mid, "t": NOW})
    deny = await w.read_denyset()
    assert deny.blocks("big.example") and deny.blocks("small.example") and not deny.blocks("other.example")
    assert not deny.delete
    await offer(db, "small", seller="small.example")
    out = await r.run(now=NOW)
    assert await ids(db) == ["small"]
    assert out["optout_ids_canonical_only"] == 2



async def test_the_canonical_read_takes_distinct_hosts_on_postgres(db, monkeypatch):
    """Review of be#2735 delta 3, P3-R1(b)(c): on Postgres the host is taken out of the URL in SQL (DISTINCT host,
    ORDER BY host, LIMIT per id), so marketplace listings that sort first never hide the store's own hosts, a store on
    a shared host is its own host, and the first hosts in host order are kept."""
    from services import deal_code_capture_writer as w
    from tests.test_deal_code_retention import NOW

    monkeypatch.setattr(w, "CANONICAL_HOSTS_PER_ID", 4)
    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, canonical_url)"
        " SELECT :p || 'mk' || g, 'merch_mix', 'shopify', 'mk' || g, 't', 'https://www.amazon.com/dp/' || lpad(g::text, 4, '0')"
        " FROM generate_series(1, 300) AS g",
        {"p": _PREFIX},
    )
    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, canonical_url)"
        " SELECT :p || 'own' || g, 'merch_mix', 'shopify', 'own' || g, 't', 'https://a-own.example/p/' || g"
        " FROM generate_series(1, 300) AS g",
        {"p": _PREFIX},
    )
    rows = [("z", "merch_mix", "HTTPS://user@Shop.Z-Own.Example:8443/p?x=1"), ("sq", "merch_sq", "https://brand1.square.site/p"),
            ("o1", "merch_order", "https://d.example/p"), ("o2", "merch_order", "https://c.example/p"),
            ("o3", "merch_order", "https://b.example/p"), ("o4", "merch_order", "https://a.example/p"),
            ("o5", "merch_order", "https://e.example/p"),
            # review of be#2742, P2-B: shapes the Python parse reads, so the SQL host must read them too
            ("ws", "merch_ws", " https://ws-own.example/p"), ("ns", "merch_ns", "nos-own.example/p"),
            ("at", "merch_at", "https://a@b@at-own.example/p"),
            # review of be#2742, P2-B': a scheme-less host:port is the host, not a scheme; tab, CR, LF and space in the
            # authority are dropped as urlsplit drops them
            ("p1", "merch_p1", "sp.example:8080/p"), ("p2", "merch_p2", "sp2.example:8080"),
            ("p3", "merch_p3", "www.nos2.example:443"), ("t1", "merch_t1", "https://ta\tb.example/p"),
            ("t2", "merch_t2", "https://nl\n.example/p"), ("t3", "merch_t3", "https://cr\r.example/p"),
            ("t4", "merch_t4", "https:// sp3.example/p"), ("t5", "merch_t5", "https://user@ sp5.example/p"),
            ("t6", "merch_t6", "//proto-rel.example/p"),  # Python reads no host here; SQL reading one fails closed
            ("m1", "merch_pmix", "sp4.example:8080/p"), ("m2", "merch_pmix", "https://own4.example/p")]
    for key, mid, url in rows:
        await db.execute(
            "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, canonical_url)"
            " VALUES (:k, :m, 'shopify', :k, 't', :u)", {"k": _PREFIX + key, "m": mid, "u": url})
    await db.execute(  # many distinct hosts, so an unordered DISTINCT (hash) would not return the first by name
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, canonical_url)"
        " SELECT :p || 'ord' || g, 'merch_order', 'shopify', 'ord' || g, 't', 'https://h' || md5(g::text) || '.example/p'"
        " FROM generate_series(1, 3000) AS g",
        {"p": _PREFIX},
    )
    await db.execute(  # one id past the URL cap: only the SQL host read finds its own host behind 5,100 listings
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, canonical_url)"
        " SELECT :p || 'deep' || g, 'merch_deep', 'shopify', 'deep' || g, 't', 'https://www.amazon.com/dp/d' || g"
        " FROM generate_series(1, 5100) AS g",
        {"p": _PREFIX},
    )
    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, canonical_url)"
        " VALUES (:k, 'merch_deep', 'shopify', 'deepown', 't', 'https://zz-own.example/p')", {"k": _PREFIX + "deepown"})
    shape_ids = ("merch_p1", "merch_p2", "merch_p3", "merch_t1", "merch_t2", "merch_t3", "merch_t4", "merch_t5",
                 "merch_t6", "merch_pmix")
    for mid in ("merch_mix", "merch_sq", "merch_order", "merch_ws", "merch_ns", "merch_at", "merch_deep") + shape_ids:
        await db.execute("INSERT INTO deal_lane_optout (seller_id, requested_at, recorded_by) VALUES (:s, :t, 'test')",
                         {"s": mid, "t": NOW})
    # With sort plans off, Postgres answers an unordered DISTINCT by hashing; only the ORDER BY keeps the first hosts.
    # (SET LOCAL inside a transaction: the pool resets a plain SET when the connection goes back.)
    async with db.transaction(force_rollback=True):
        await db.execute("SET LOCAL enable_sort = off")
        assert await db.fetch_val("SHOW enable_sort") == "off"
        deny = await w.read_denyset()
    assert deny.blocks("a-own.example") and deny.blocks("shop.z-own.example") and not deny.blocks("amazon.com")
    assert deny.blocks("brand1.square.site") and not deny.blocks("other.square.site")
    assert all(deny.blocks(f"{c}.example") for c in "abcd") and not deny.blocks("e.example")
    assert deny.blocks("ws-own.example") and deny.blocks("nos-own.example") and deny.blocks("at-own.example")
    assert deny.blocks("zz-own.example")
    for host in ("sp.example", "sp2.example", "nos2.example", "tab.example", "nl.example", "cr.example", "sp3.example",
                 "sp5.example", "proto-rel.example", "sp4.example", "own4.example"):
        assert deny.blocks(host), host
    assert not deny.delete
    assert deny.counts == {"optout_ids_canonical_only": 7 + len(shape_ids), "optout_ids_too_broad": 1}
