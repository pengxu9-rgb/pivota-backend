"""One suppressed catalog row must not abort a merchant's whole universal sync.
Driven on the REAL `ingest_standard_products` against a REAL Postgres.

THE FILENAME IS LOAD-BEARING (`.github/workflows/postgres-dialect-gate.yml`
globs `tests/test_*_postgres.py`).

THE DEFECT (prod, since 2026-10-03). Every universal sync of the Wix merchant
merch_e68c20b0189746d0 failed with

    catalog upsert failed table=catalog_offers pk=offer_id:
    invalid_live_catalog_offer_link_or_price

and wrote nothing: the catalog went two weeks without an update, and each
cycle cost the load balancer ~8 5xx.

a10aab409 put `guard_catalog_offer_rows(..., require_live_links=True)` into
`_upsert_by_pk` as the last line before a catalog_offers write, and made it
RAISE. The sync re-ingests every product the platform still lists, and its SKU
and product upserts deliberately KEEP a non-stale suppression someone else set
(`_preserve_non_stale_suppression`). So a variant whose row was suppressed by
another writer reaches the offer write with a non-live parent, the boundary
raises, and the one-transaction ingest rolls back every other product with it.

What prod held (read-only census 2026-10-19): all 20 of that merchant's
catalog_products carried `suppression_reason='sync_lane_pilot_retired_20260729'`,
a hand retirement from 2026-07-29; its 433 SKUs were live and 0 offers lacked a
price. So every offer failed the live-link check (the guard reports a non-live
parent as `orphan_no_sku`), and the first one aborted the sync. The retirement
is deliberate data, so the fix is in the writer, not the rows.

The sync's own pre-check (`validate_catalog_offer_rows(existing_sku_keys={sku_key})`)
skipped-and-counted a priceless offer but never asked the live-link question,
so the boundary was the first thing to see it. The sync now asks that question
itself and skips the offer, counting it like the other skip reasons.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"),
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

_SCHEMA = f"universal_sync_suppressed_{os.getpid()}"

MERCHANT = "merch_wix_suppressed_gate"
HEALTHY_PID = "wix-prod-healthy"
SUPPRESSED_PID = "wix-prod-suppressed"


@pytest.fixture(scope="module")
def engine():
    from sqlalchemy import create_engine, text

    import services.catalog_sync_service  # noqa: F401  (pulls every table into metadata)
    from db.database import metadata

    url = os.environ["DATABASE_URL"]
    raw = create_engine(url, future=True)
    with raw.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
        c.execute(text(f"CREATE SCHEMA {_SCHEMA}"))
    eng = create_engine(url, future=True, connect_args={"options": f"-csearch_path={_SCHEMA}"})
    with eng.begin() as c:
        metadata.create_all(bind=c)
        # Same model/prod drift as tests/test_catalog_child_row_merchant_scope_postgres.py.
        c.execute(text("ALTER TABLE catalog_products ALTER COLUMN pivota_signature_minted_at TYPE TIMESTAMPTZ"))
        c.execute(text(pathlib.Path("db/migrations/045_product_groups.sql").read_text(encoding="utf-8")))
    yield eng
    with raw.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))


@pytest.fixture(autouse=True)
def clean(engine):
    from sqlalchemy import text

    with engine.begin() as c:
        c.execute(
            text(
                "TRUNCATE catalog_offers, catalog_skus, catalog_products, catalog_merchants,"
                " catalog_field_facts, product_group_members, writer_audit_log"
            )
        )
    yield


def _drive(coro_factory):
    """Bind every module's `database` to the test schema (see the child-row test)."""
    import databases

    import db.database as dbmod

    url = os.environ["DATABASE_URL"]
    if "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://")
    db = databases.Database(url, server_settings={"search_path": _SCHEMA})

    original = dbmod.database
    patched = [mod for mod in list(sys.modules.values()) if getattr(mod, "database", None) is original]
    assert patched, "no module held db.database.database — the patch would be a no-op"

    async def _go():
        await db.connect()
        try:
            return await coro_factory()
        finally:
            await db.disconnect()

    for mod in patched:
        mod.database = db
    dbmod.database = db
    try:
        return asyncio.run(_go())
    finally:
        for mod in patched:
            mod.database = original
        dbmod.database = original


def _product(pid: str, title: str, price: float) -> dict:
    return {
        "id": pid,
        "product_id": pid,
        "platform": "wix",
        "merchant_id": MERCHANT,
        "title": title,
        "description": "A plush toy.",
        "vendor": "Tsingtao Bear",
        "product_type": "Toys",
        "price": price,
        "currency": "EUR",
        "inventory_quantity": 4,
        "image_url": "https://static.wixstatic.com/media/a.jpg",
        "images": ["https://static.wixstatic.com/media/a.jpg"],
        "variants": [{"id": f"{pid}-v1", "title": "Default Title", "price": price, "inventory_quantity": 4}],
    }


def _ingest(price: float = 25.0) -> dict:
    import services.catalog_sync_service as sync

    return _drive(
        lambda: sync.ingest_standard_products(
            merchant_id=MERCHANT,
            platform="wix",
            product_payloads=[
                _product(HEALTHY_PID, "Healthy Bear", price),
                _product(SUPPRESSED_PID, "Withdrawn Bear", price),
            ],
            # The writer the prod route uses; it is in GUARDED_OFFER_WRITERS.
            source_system="universal_product_sync",
            source_ref="suppressed-row-gate",
        )
    )


def _rows(engine, sql: str) -> list:
    from sqlalchemy import text

    with engine.begin() as c:
        return [tuple(r) for r in c.execute(text(sql))]


def _exec(engine, sql: str) -> None:
    from sqlalchemy import text

    with engine.begin() as c:
        c.execute(text(sql))


def _offer_prices(engine) -> dict:
    return dict(
        _rows(
            engine,
            "SELECT s.source_product_id, o.list_price FROM catalog_offers o"
            " JOIN catalog_skus s ON s.sku_key = o.sku_key",
        )
    )


@pytest.mark.parametrize(
    "suppress_sql",
    [
        # Another writer withdrew the PRODUCT (timestamp + reason).
        "UPDATE catalog_products SET suppression_reason='withdrawn', suppressed_at=NOW()"
        f" WHERE source_product_id='{SUPPRESSED_PID}'",
        # Another writer withdrew the SKU by reason alone — the state the
        # `suppression_reason_without_timestamp` invariant counts.
        "UPDATE catalog_skus SET suppression_reason='withdrawn'"
        f" WHERE source_product_id='{SUPPRESSED_PID}'",
    ],
    ids=["product_suppressed", "sku_reason_only"],
)
def test_a_suppressed_row_is_skipped_and_counted_not_an_aborted_sync(engine, suppress_sql):
    first = _ingest(price=25.0)
    assert first["offers_ingested"] == 2, first
    assert _offer_prices(engine) == {HEALTHY_PID: 25, SUPPRESSED_PID: 25}

    _exec(engine, suppress_sql)

    # THE PROD REPRO: before the fix this raised
    # RuntimeError("catalog upsert failed table=catalog_offers ...
    # invalid_live_catalog_offer_link_or_price") and rolled back the healthy
    # product's price change with it.
    second = _ingest(price=31.0)

    assert second["offers_ingested"] == 1, second
    assert second["offers_skipped"] == 1, second
    # The guard's own vocabulary: with require_live_links a SKU or product that
    # exists but is suppressed is ORPHAN_NO_SKU, as under live_only.
    assert second["offer_skip_reasons"] == {"orphan_no_sku": 1}, second
    # The healthy product moved; the suppressed one was left exactly as it was.
    assert _offer_prices(engine) == {HEALTHY_PID: 31, SUPPRESSED_PID: 25}
    # The other writer's suppression survived the re-sync.
    assert _rows(
        engine,
        "SELECT count(*) FROM catalog_products p JOIN catalog_skus s ON s.product_key = p.product_key"
        f" WHERE s.source_product_id='{SUPPRESSED_PID}'"
        " AND (p.suppression_reason IS NOT NULL OR s.suppression_reason IS NOT NULL)",
    ) == [(1,)]
    # And the skip is on the audit trail, not just in the return value.
    audit = _rows(
        engine,
        "SELECT applied_rows, skipped_rows FROM writer_audit_log"
        " WHERE writer_name='universal_product_sync' ORDER BY id DESC LIMIT 1",
    )
    assert audit == [(1, 1)], audit
