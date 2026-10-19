"""One suppressed catalog row must not end a merchant's universal sync.
Driven on the REAL `ingest_standard_products` against a REAL Postgres.

THE FILENAME IS LOAD-BEARING (`.github/workflows/postgres-dialect-gate.yml`
globs `tests/test_*_postgres.py`).

THE DEFECT (prod, since 2026-10-03). Every universal sync of the Wix merchant
merch_e68c20b0189746d0 failed with

    catalog upsert failed table=catalog_offers pk=offer_id:
    invalid_live_catalog_offer_link_or_price

the catalog went two weeks without an update, and each cycle cost the load
balancer ~8 5xx.

a10aab409 put `guard_catalog_offer_rows(..., require_live_links=True)` into
`_upsert_by_pk` as the last line before a catalog_offers write, and made it
RAISE. The sync re-ingests every product the platform still lists, and its SKU
and product upserts deliberately KEEP a non-stale suppression someone else set
(`_preserve_non_stale_suppression`). So a variant whose row another writer
suppressed reaches the offer write with a non-live parent and the boundary
raises. Each product has its OWN transaction, so products processed earlier
kept their writes; the raise rolled back that one product and ended the ingest
there — every LATER product, the writer-audit row and the job bookkeeping never
ran.

What prod held (read-only census 2026-10-19): all 20 of that merchant's
catalog_products carried `suppression_reason='sync_lane_pilot_retired_20260729'`,
a hand retirement from 2026-07-29; its 433 SKUs were live and 0 offers lacked a
price. So every offer failed the live-link check (the guard reports a non-live
parent as `orphan_no_sku`), and the FIRST product ended the sync. The
retirement is deliberate data, so the fix is in the writer, not the rows.

The fix skips and counts ONLY a non-live parent. Any other boundary reason —
a zero or missing price on a lane with no pre-check of its own — still fails
the sync exactly as before: a quiet skip there would leave the old price
standing on the offer.
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


def _ingest(source_system: str, *, healthy_price: float, suppressed_price: float) -> dict:
    import services.catalog_sync_service as sync

    return _drive(
        lambda: sync.ingest_standard_products(
            merchant_id=MERCHANT,
            platform="wix",
            # The suppressed product comes FIRST: on main its raise ended the
            # ingest before the healthy product was ever reached.
            product_payloads=[
                _product(SUPPRESSED_PID, "Withdrawn Bear", suppressed_price),
                _product(HEALTHY_PID, "Healthy Bear", healthy_price),
            ],
            source_system=source_system,
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


def _offer_id(engine, pid: str) -> str:
    return _rows(
        engine,
        "SELECT o.offer_id FROM catalog_offers o JOIN catalog_skus s ON s.sku_key = o.sku_key"
        f" WHERE s.source_product_id='{pid}'",
    )[0][0]


# `universal_product_sync` is the prod writer and runs the audit pre-check;
# `products_cache` (run_catalog_sync_job) has no pre-check and no audit, so
# the boundary is the only thing that sees its rows.
LANES = ["universal_product_sync", "products_cache"]


@pytest.mark.parametrize("source_system", LANES)
@pytest.mark.parametrize(
    "suppress_sql",
    [
        # Another writer withdrew the PRODUCT (timestamp + reason) — the prod shape.
        "UPDATE catalog_products SET suppression_reason='withdrawn', suppressed_at=NOW()"
        f" WHERE source_product_id='{SUPPRESSED_PID}'",
        # Another writer withdrew the SKU by reason alone — the state the
        # `suppression_reason_without_timestamp` invariant counts.
        "UPDATE catalog_skus SET suppression_reason='withdrawn'"
        f" WHERE source_product_id='{SUPPRESSED_PID}'",
    ],
    ids=["product_suppressed", "sku_reason_only"],
)
def test_a_suppressed_parent_is_skipped_and_counted_not_the_end_of_the_sync(
    engine, caplog, suppress_sql, source_system
):
    first = _ingest(source_system, healthy_price=25.0, suppressed_price=25.0)
    assert first["offers_ingested"] == 2, first
    assert _offer_prices(engine) == {HEALTHY_PID: 25, SUPPRESSED_PID: 25}
    suppressed_offer = _offer_id(engine, SUPPRESSED_PID)

    _exec(engine, suppress_sql)

    # THE PROD REPRO: before the fix this raised
    # RuntimeError("catalog upsert failed table=catalog_offers ...
    # invalid_live_catalog_offer_link_or_price") on the suppressed product and
    # the healthy product after it was never ingested.
    caplog.clear()
    with caplog.at_level("WARNING", logger="services.catalog_sync_service"):
        second = _ingest(source_system, healthy_price=31.0, suppressed_price=31.0)

    assert second["products_ingested"] == 2, second
    assert second["offers_ingested"] == 1, second
    assert second["offers_skipped"] == 1, second
    # The guard's own vocabulary: with require_live_links a SKU or product that
    # exists but is suppressed is ORPHAN_NO_SKU, as under live_only.
    assert second["offer_skip_reasons"] == {"orphan_no_sku": 1}, second
    assert second["offer_skip_sample"] == [suppressed_offer], second
    # The healthy product moved; the suppressed one was left exactly as it was.
    assert _offer_prices(engine) == {HEALTHY_PID: 31, SUPPRESSED_PID: 25}
    # The other writer's suppression survived the re-sync.
    assert _rows(
        engine,
        "SELECT count(*) FROM catalog_products p JOIN catalog_skus s ON s.product_key = p.product_key"
        f" WHERE s.source_product_id='{SUPPRESSED_PID}'"
        " AND (p.suppression_reason IS NOT NULL OR s.suppression_reason IS NOT NULL)",
    ) == [(1,)]
    # ONE summary line per sync, not one per offer.
    skip_lines = [r for r in caplog.records if "non-live parent" in r.getMessage()]
    assert len(skip_lines) == 1, [r.getMessage() for r in skip_lines]
    assert "count=1" in skip_lines[0].getMessage()
    assert suppressed_offer in skip_lines[0].getMessage()

    audit = _rows(
        engine,
        "SELECT applied_rows, skipped_rows FROM writer_audit_log"
        f" WHERE writer_name='{source_system}' ORDER BY id DESC LIMIT 1",
    )
    if source_system == "universal_product_sync":
        # The skip is on the audit trail, not just in the return value.
        assert audit == [(1, 1)], audit
    else:
        assert audit == [], audit


def test_a_zero_price_on_a_lane_without_the_pre_check_still_fails_the_sync(engine):
    """The skip is for a non-live parent ONLY. `products_cache` runs no price
    pre-check, so the boundary is what refuses a zero price there — and that must
    stay a failure: skipping it would leave the old 25.00 standing on the offer
    while the sync reported success."""
    first = _ingest("products_cache", healthy_price=25.0, suppressed_price=25.0)
    assert first["offers_ingested"] == 2, first

    with pytest.raises(RuntimeError, match="invalid_live_catalog_offer_link_or_price"):
        _ingest("products_cache", healthy_price=31.0, suppressed_price=0.0)

    # The zero-priced product's transaction rolled back; nothing was skipped quietly.
    assert _offer_prices(engine)[SUPPRESSED_PID] == 25
