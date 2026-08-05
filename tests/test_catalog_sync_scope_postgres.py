"""Path-A sync must never clobber a pdp_scope promotion — driven on the REAL
`_upsert_by_pk` against the REAL `db.catalog.catalog_products` table.

THE FILENAME IS LOAD-BEARING (`.github/workflows/postgres-dialect-gate.yml`
globs `tests/test_*_postgres.py`).

THE DEFECT (PR #1680 round-11 review, Finding 2, driven): the Path-A products
payload carries `pdp_scope='merchant_owned'` / `pdp_scope_source='merchant_sync'`
unconditionally, and `_upsert_by_pk` applies the full payload on UPDATE of an
existing row. Every re-sync therefore silently reverted any promotion — the D3
cron's, the recovery writer's — to `merchant_owned`, an AFFIRMED state outside
the `WHERE pdp_scope='unverified'` gate every promotion writer checks: the
promotion never came back, and nothing logged that it was gone. The comment on
the payload claimed "initial write"; the update path made that claim false.

THE RULE (docs/PDP_SCOPE_REDESIGN.md): ingest lanes may SEED the scope column
at birth; scope transitions on live rows belong exclusively to the governance
writers. `_preserve_existing_scope` enforces it in the same shape as the
suppression-field preservation that already lived in this upsert.

Prod exposure measured 0 at fix time (2026-08-05: no canonical rows on sync
platforms) — the mechanism was fixed BEFORE it ever had a cohort, which is the
cheap moment.
"""

from __future__ import annotations

import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"),
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

_SCHEMA = f"sync_scope_test_{os.getpid()}"


@pytest.fixture(scope="module")
def engine():
    from sqlalchemy import create_engine, text

    from db.catalog import catalog_products

    eng = create_engine(
        os.environ["DATABASE_URL"], future=True,
        connect_args={"options": f"-csearch_path={_SCHEMA}"})
    raw = create_engine(os.environ["DATABASE_URL"], future=True)
    with raw.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
        c.execute(text(f"CREATE SCHEMA {_SCHEMA}"))
    with eng.begin() as c:
        # The REAL table metadata — not a hand-typed lookalike. If the real
        # DDL and this test ever disagree, the test must lose, loudly.
        catalog_products.create(bind=c)
    yield eng
    with raw.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))


def _db_for_schema():
    import databases

    url = os.environ["DATABASE_URL"]
    if "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://")
    return databases.Database(url, server_settings={"search_path": _SCHEMA})


def _drive(module, coro_factory):
    db = _db_for_schema()

    async def _go():
        await db.connect()
        try:
            return await coro_factory()
        finally:
            await db.disconnect()

    original = module.database
    module.database = db
    try:
        return asyncio.run(_go())
    finally:
        module.database = original


def _path_a_payload(pk: str, title: str) -> dict:
    """The scope-relevant shape of the Path-A products payload: required
    columns plus the unconditional scope stamp."""
    from datetime import datetime, timezone

    return {
        "product_key": pk,
        "merchant_id": "acme_shop",
        "platform": "shopify",
        "source_product_id": pk.rsplit("::", 1)[-1],
        "title": title,
        "pdp_scope": "merchant_owned",
        "pdp_scope_source": "merchant_sync",
        "pdp_scope_set_at": datetime.now(timezone.utc).replace(tzinfo=None),
    }


def test_birth_stamps_scope_and_resync_preserves_a_promotion(engine):
    """The full lifecycle, in order, against the real upsert:
      1. first sync INSERTs the row — the scope SEED applies (merchant_owned,
         merchant_sync): birth stamping must keep working;
      2. a governance writer promotes the row (canonical, d3_promotion_cron);
      3. a re-sync UPDATE arrives with the same unconditional payload — the
         promotion MUST survive (scope, source, AND set_at all preserved)
         while the sync's real business (here: the title) still lands."""
    import services.catalog_sync_service as sync
    from sqlalchemy import text

    from db.catalog import catalog_products

    pk = "prod::acme_shop::shopify::sku1"

    with engine.begin() as c:
        c.execute(text("DELETE FROM catalog_products"))

    _drive(sync, lambda: sync._upsert_by_pk(
        catalog_products, "product_key", _path_a_payload(pk, "First title")))

    with engine.begin() as c:
        born = c.execute(text(
            "SELECT pdp_scope, pdp_scope_source FROM catalog_products"
            " WHERE product_key = :pk"), {"pk": pk}).one()
    assert born.pdp_scope == "merchant_owned", "birth seeding broke"
    assert born.pdp_scope_source == "merchant_sync"

    with engine.begin() as c:
        c.execute(text(
            "UPDATE catalog_products SET pdp_scope = 'multi_merchant_canonical',"
            " pdp_scope_source = 'd3_promotion_cron',"
            " pdp_scope_set_at = now() WHERE product_key = :pk"), {"pk": pk})
        promoted_at = c.execute(text(
            "SELECT pdp_scope_set_at FROM catalog_products"
            " WHERE product_key = :pk"), {"pk": pk}).scalar()

    _drive(sync, lambda: sync._upsert_by_pk(
        catalog_products, "product_key", _path_a_payload(pk, "Resynced title")))

    with engine.begin() as c:
        after = c.execute(text(
            "SELECT title, pdp_scope, pdp_scope_source, pdp_scope_set_at"
            " FROM catalog_products WHERE product_key = :pk"), {"pk": pk}).one()
    assert after.title == "Resynced title", (
        "the update itself must still apply — preservation is per-field, "
        "not a skipped write")
    assert after.pdp_scope == "multi_merchant_canonical", (
        "re-sync clobbered the promotion back to merchant_owned — the exact "
        "silent one-way door of round-11 Finding 2")
    assert after.pdp_scope_source == "d3_promotion_cron", (
        "provenance clobbered: the row would grep as a sync write")
    assert after.pdp_scope_set_at == promoted_at, (
        "pdp_scope_set_at re-stamped — the promotion timestamp is part of "
        "the record")


def test_resync_preserves_scope_and_suppression_together(engine):
    """The two preservation guards act on the SAME payload dict in sequence —
    prove they compose: a suppressed, promoted row keeps BOTH its suppression
    and its scope through a re-sync that tries to overwrite both."""
    import services.catalog_sync_service as sync
    from sqlalchemy import text

    from db.catalog import catalog_products

    pk = "prod::acme_shop::shopify::sku2"

    with engine.begin() as c:
        c.execute(text("DELETE FROM catalog_products"))

    _drive(sync, lambda: sync._upsert_by_pk(
        catalog_products, "product_key", _path_a_payload(pk, "T")))
    with engine.begin() as c:
        c.execute(text(
            "UPDATE catalog_products SET pdp_scope = 'multi_merchant_canonical',"
            " pdp_scope_source = 'd3_promotion_cron', pdp_scope_set_at = now(),"
            " suppressed_at = now(), suppression_reason = 'manual_review'"
            " WHERE product_key = :pk"), {"pk": pk})

    payload = _path_a_payload(pk, "T2")
    payload["suppressed_at"] = None
    payload["suppression_reason"] = None
    _drive(sync, lambda: sync._upsert_by_pk(
        catalog_products, "product_key", payload))

    with engine.begin() as c:
        row = c.execute(text(
            "SELECT pdp_scope, suppressed_at, suppression_reason"
            " FROM catalog_products WHERE product_key = :pk"), {"pk": pk}).one()
    assert row.pdp_scope == "multi_merchant_canonical"
    assert row.suppressed_at is not None and row.suppression_reason == "manual_review", (
        "suppression preservation regressed — the neighbor guard broke")
