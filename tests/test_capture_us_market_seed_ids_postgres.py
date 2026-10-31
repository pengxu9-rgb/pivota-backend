"""`capture_us_market_offers --seed-ids`: which products a named set of seeds selects, on real Postgres.

2026-10-22: 108 US-filed seeds on EUR/GBP storefronts (rr1020) whose stores quote US buyers real USD
via Shopify Markets (usmkt1022). CANDIDATES_SQL cannot see them (their offers are labelled USD), so the
capture takes them by seed id. Pinned here: only ACTIVE named seeds, only unsuppressed attached
products with a canonical_url, the SEED's domain as the store, and select_capturable still refusing a
seed attached to another store's product.

PRIVATE DATABASE, created and dropped here.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"),
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

_DB_NAME = f"capture_seed_ids_{os.getpid()}"


def _private_url() -> str:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(os.environ["DATABASE_URL"])
    return urlunsplit(parts._replace(path=f"/{_DB_NAME}"))


@pytest.fixture(scope="module")
def db_url():
    import psycopg2

    admin = psycopg2.connect(os.environ["DATABASE_URL"])
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{_DB_NAME}"')
        cur.execute(f'CREATE DATABASE "{_DB_NAME}"')
    admin.close()
    conn = psycopg2.connect(_private_url())
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE catalog_products (product_key text PRIMARY KEY, content_key text, merchant_id text,"
            " canonical_url text, suppressed_at timestamptz, suppression_reason text)")
        cur.execute(
            "CREATE TABLE external_product_seeds (id text PRIMARY KEY, status text, domain text,"
            " attached_product_key text)")
        cur.execute(
            "CREATE TABLE catalog_offers (offer_id text PRIMARY KEY, sku_key text, channel text, market text,"
            " source_system text, suppressed_at timestamptz, suppression_reason text,"
            " suppression_metadata jsonb, updated_at timestamptz)")
        cur.execute(
            "INSERT INTO catalog_products VALUES"
            " ('pk_soap', 'ck_soap', 'm_lav', 'https://lasavonneriedupilonduroy.com/products/soap', NULL, NULL),"
            " ('pk_gone', 'ck_gone', 'm_up', 'https://upcirclebeauty.com/products/gone', now(), NULL),"
            " ('pk_reason', 'ck_reason', 'm_up', 'https://upcirclebeauty.com/products/reason', NULL, 'withdrawn'),"
            " ('pk_nourl', 'ck_nourl', 'm_up', NULL, NULL, NULL),"
            " ('pk_nock', NULL, 'm_up', 'https://upcirclebeauty.com/products/nock', NULL, NULL),"
            " ('pk_other', 'ck_other', 'm_x', 'https://another-store.com/products/serum', NULL, NULL),"
            " ('pk_retired_own', 'ck_ro', 'm_lav', 'https://lasavonneriedupilonduroy.com/products/old', NULL, NULL),"
            " ('pk_unnamed', 'ck_unnamed', 'm_up', 'https://upcirclebeauty.com/products/unnamed', NULL, NULL)")
        cur.execute(
            "INSERT INTO external_product_seeds VALUES"
            " ('eps_soap', 'Active ', ' LaSavonnerieDuPilonDuRoy.com ', 'pk_soap'),"
            " ('eps_soap_twin', 'active', 'lasavonneriedupilonduroy.com', 'pk_soap'),"
            " ('eps_retired', 'retired', 'lasavonneriedupilonduroy.com', 'pk_soap'),"
            " ('eps_retired_own', 'retired', 'lasavonneriedupilonduroy.com', 'pk_retired_own'),"
            " ('eps_gone', 'active', 'upcirclebeauty.com', 'pk_gone'),"
            " ('eps_reason', 'active', 'upcirclebeauty.com', 'pk_reason'),"
            " ('eps_nourl', 'active', 'upcirclebeauty.com', 'pk_nourl'),"
            " ('eps_nock', 'active', 'upcirclebeauty.com', 'pk_nock'),"
            " ('eps_unattached', 'active', 'upcirclebeauty.com', NULL),"
            " ('eps_other', 'active', 'upcirclebeauty.com', 'pk_other'),"
            " ('eps_unnamed', 'active', 'upcirclebeauty.com', 'pk_unnamed')")
        cur.execute(
            "INSERT INTO catalog_offers VALUES"
            " ('mirror_soap', 'pk_soap::canonical', 'external_referral', 'US', 'external_product_seeds_mirror_v1', NULL, NULL, NULL, NULL),"
            " ('enrich_soap', 'pk_soap::canonical', 'external_referral', 'US', 'catalog_enrichment_agent_v1', NULL, NULL, NULL, NULL),"
            " ('mirror_soap_gb', 'pk_soap::canonical', 'external_referral', 'GB', 'external_product_seeds_mirror_v1', NULL, NULL, NULL, NULL),"
            " ('dead_soap', 'pk_soap::canonical', 'external_referral', 'US', 'external_product_seeds_mirror_v1', now(), 'x', NULL, NULL),"
            " ('us_soap', 'pk_soap::canonical', 'external_referral', 'US', 'us_market_capture', NULL, NULL, NULL, NULL)")
    conn.close()
    try:
        yield _private_url()
    finally:
        admin = psycopg2.connect(os.environ["DATABASE_URL"])
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                        (_DB_NAME,))
            cur.execute(f'DROP DATABASE IF EXISTS "{_DB_NAME}"')
        admin.close()


def _run(db_url, sql, params):
    from databases import Database

    async def go():
        db = Database(db_url)
        await db.connect()
        try:
            return [dict(r) for r in await db.fetch_all(sql, params)]
        finally:
            await db.disconnect()

    return asyncio.run(go())


def _candidates(db_url, seed_ids):
    from scripts.capture_us_market_offers import SEED_CANDIDATES_SQL

    return _run(db_url, SEED_CANDIDATES_SQL, {"seed_ids": seed_ids})


_ALL = ["eps_soap", "eps_soap_twin", "eps_retired", "eps_retired_own", "eps_gone", "eps_reason", "eps_nourl",
        "eps_nock", "eps_unattached", "eps_other", "eps_nope"]


def test_named_active_seeds_select_their_live_products_once(db_url):
    rows = _candidates(db_url, _ALL)
    assert [(r["product_key"], r["source_domain"]) for r in rows] == [
        ("pk_soap", "lasavonneriedupilonduroy.com"),   # two active seeds, one row; the domain is normalised
        ("pk_other", "upcirclebeauty.com"),            # selected here, refused by select_capturable below
    ]
    keys = {r["product_key"] for r in rows}
    assert "pk_retired_own" not in keys    # M1: a retired seed on its OWN product selects nothing
    assert "pk_reason" not in keys         # suppressed by reason alone
    assert "pk_nock" not in keys           # no content_key: nothing could republish it
    assert "pk_unnamed" not in keys        # an active seed nobody named


def test_every_named_id_says_why_it_was_or_was_not_priced(db_url):
    from scripts.capture_us_market_offers import SEED_MATCH_SQL

    outcomes = {r["id"]: r["outcome"] for r in _run(db_url, SEED_MATCH_SQL, {"seed_ids": _ALL})}
    assert outcomes == {
        "eps_soap": "matched", "eps_soap_twin": "matched", "eps_other": "matched",
        "eps_retired": "not_active", "eps_retired_own": "not_active",
        "eps_gone": "product_suppressed", "eps_reason": "product_suppressed",
        "eps_nourl": "no_canonical_url", "eps_nock": "no_content_key",
        "eps_unattached": "unattached", "eps_nope": "missing",
    }


def test_a_seed_on_another_stores_product_is_refused_not_priced(db_url):
    from scripts.capture_us_market_offers import select_capturable

    by_domain, skipped = select_capturable(_candidates(db_url, ["eps_soap", "eps_other"]))
    assert {d: [r["product_key"] for r in rows] for d, rows in by_domain.items()} == {
        "lasavonneriedupilonduroy.com": ["pk_soap"]}
    assert skipped == [("pk_other", "domain_mismatch")]


def test_the_us_shelf_is_read_and_only_its_projected_sibling_retired(db_url):
    """The shelf is (sku_key, external_referral, US), live rows only, never the US offer itself; the
    retirement touches only the listed seed-projected ids and says what superseded them."""
    from scripts.capture_us_market_offers import RETIRE_SUPERSEDED_SQL, SHELF_SIBLINGS_SQL
    from services.seed_offer_currency import SEED_PROJECTED_OFFER_SOURCE_SYSTEMS

    sibs = _run(db_url, SHELF_SIBLINGS_SQL, {"sku_keys": ["pk_soap::canonical"], "offer_ids": ["us_soap"]})
    assert sorted(r["offer_id"] for r in sibs) == ["enrich_soap", "mirror_soap"]
    gone = _run(db_url, RETIRE_SUPERSEDED_SQL, {
        "offer_id": "us_soap", "sku_key": "pk_soap::canonical",
        "retire_offer_ids": ["mirror_soap", "enrich_soap", "dead_soap"],
        "projected_systems": list(SEED_PROJECTED_OFFER_SOURCE_SYSTEMS)})
    assert [r["offer_id"] for r in gone] == ["mirror_soap"]   # not the enrichment row, not a dead one
    after = {r["offer_id"]: r for r in _run(db_url, "SELECT offer_id, suppression_reason, suppression_metadata"
                                                    " FROM catalog_offers", {})}
    assert after["mirror_soap"]["suppression_reason"] == "superseded_by_us_market_capture"
    assert "us_soap" in str(after["mirror_soap"]["suppression_metadata"])
    assert after["enrich_soap"]["suppression_reason"] is None
    assert after["dead_soap"]["suppression_reason"] == "x"
