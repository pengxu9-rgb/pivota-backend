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
            " canonical_url text, suppressed_at timestamptz)")
        cur.execute(
            "CREATE TABLE external_product_seeds (id text PRIMARY KEY, status text, domain text,"
            " attached_product_key text)")
        cur.execute(
            "INSERT INTO catalog_products VALUES"
            " ('pk_soap', 'ck_soap', 'm_lav', 'https://lasavonneriedupilonduroy.com/products/soap', NULL),"
            " ('pk_gone', 'ck_gone', 'm_up', 'https://upcirclebeauty.com/products/gone', now()),"
            " ('pk_nourl', 'ck_nourl', 'm_up', NULL, NULL),"
            " ('pk_other', 'ck_other', 'm_x', 'https://another-store.com/products/serum', NULL),"
            " ('pk_unnamed', 'ck_unnamed', 'm_up', 'https://upcirclebeauty.com/products/unnamed', NULL)")
        cur.execute(
            "INSERT INTO external_product_seeds VALUES"
            " ('eps_soap', 'Active ', 'lasavonneriedupilonduroy.com', 'pk_soap'),"
            " ('eps_soap_twin', 'active', 'lasavonneriedupilonduroy.com', 'pk_soap'),"
            " ('eps_retired', 'retired', 'lasavonneriedupilonduroy.com', 'pk_soap'),"
            " ('eps_gone', 'active', 'upcirclebeauty.com', 'pk_gone'),"
            " ('eps_nourl', 'active', 'upcirclebeauty.com', 'pk_nourl'),"
            " ('eps_other', 'active', 'upcirclebeauty.com', 'pk_other'),"
            " ('eps_unnamed', 'active', 'upcirclebeauty.com', 'pk_unnamed')")
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


def _candidates(db_url, seed_ids):
    from databases import Database

    from scripts.capture_us_market_offers import SEED_CANDIDATES_SQL

    async def go():
        db = Database(db_url)
        await db.connect()
        try:
            return [dict(r) for r in await db.fetch_all(SEED_CANDIDATES_SQL, {"seed_ids": seed_ids})]
        finally:
            await db.disconnect()

    return asyncio.run(go())


def test_named_active_seeds_select_their_unsuppressed_products_once(db_url):
    rows = _candidates(db_url, ["eps_soap", "eps_soap_twin", "eps_retired", "eps_gone", "eps_nourl", "eps_other"])
    assert [(r["product_key"], r["source_domain"]) for r in rows] == [
        ("pk_soap", "lasavonneriedupilonduroy.com"),   # two active seeds, one row; the retired one adds none
        ("pk_other", "upcirclebeauty.com"),            # selected here, refused by select_capturable below
    ]
    assert "pk_unnamed" not in {r["product_key"] for r in rows}  # an active seed nobody named


def test_a_seed_on_another_stores_product_is_refused_not_priced(db_url):
    from scripts.capture_us_market_offers import select_capturable

    by_domain, skipped = select_capturable(_candidates(db_url, ["eps_soap", "eps_other"]))
    assert {d: [r["product_key"] for r in rows] for d, rows in by_domain.items()} == {
        "lasavonneriedupilonduroy.com": ["pk_soap"]}
    assert skipped == [("pk_other", "domain_mismatch")]
