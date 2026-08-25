"""`catalog_merchants.status` is not this module's column to write.

`services/catalog_sync_service._upsert_by_pk` applies its WHOLE payload on
UPDATE, and `upsert_catalog_merchant` used to declare `status: str = "active"`.
Two live callers reach it with that default against EXISTING rows —
`services/audit_index_intake.py` (every URL audit run from the merchant portal)
and `ingest_standard_products` (every catalog sync) — so every content re-sync
silently re-asserted 'active' over whatever the lifecycle writers had decided.

Why it mattered enough to fix rather than absorb: PR #1852 made
`DELETE /merchant/integrations/store/{store_id}` flip the merchant to 'inactive'
when the LAST store is detached, so it stops serving on public recall. That
transition is TERMINAL by construction —
`store_lifecycle_service.reconcile_catalog_merchant_statuses` drives off
`SELECT DISTINCT merchant_id FROM merchant_stores`, so a merchant with zero
store rows is invisible to the hourly sweep and nothing re-derives it. Every
other transition this clobber touched was repaired within a tick; this one was
not. `test_a_url_audit_cannot_undo_the_last_store_detach` is that whole arc.

These tests EXECUTE and every assertion is a DB re-read, matching
`tests/test_store_lifecycle_reconciliation.py`. Two of them drive the REAL
callers (`upsert_audited_sku_to_index`, `ingest_standard_products`) rather than
a hand-rolled stand-in, because the delivering line is the call site, not the
helper. Each of those asserts the row's CONTENT fields moved as well, so a
swallowed exception inside those best-effort `try:` blocks cannot masquerade as
"status was preserved".

The SQL here runs on SQLite: it proves the branches, not the Postgres plan.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from db.catalog import (
    catalog_merchants,
    catalog_offers,
    catalog_products,
    catalog_skus,
    catalog_sync_jobs,
    writer_audit_log,
)
from db.database import database
from db.merchant_onboarding import merchant_onboarding
from services import store_lifecycle_service as lifecycle
from services.catalog_sync_service import upsert_catalog_merchant
from tests.model_schema import ensure_model_tables

_PREFIX = "cmstat"


async def _ensure_schema() -> None:
    await ensure_model_tables(
        (
            catalog_merchants,
            catalog_products,
            catalog_skus,
            catalog_offers,
            catalog_sync_jobs,
            writer_audit_log,
            merchant_onboarding,
        )
    )
    # merchant_stores has NO SQLAlchemy model (main.py creates it, schema_guard
    # widens it), so it stays hand-written. Kept BYTE-IDENTICAL to
    # tests/test_store_lifecycle_reconciliation.py::_ensure_schema — a laxer copy
    # would pass in isolation and die when the other module wins the create race.
    await database.execute(
        """
        CREATE TABLE IF NOT EXISTS merchant_stores (
            store_id VARCHAR(50) PRIMARY KEY,
            merchant_id VARCHAR(50) NOT NULL,
            platform VARCHAR(50) NOT NULL,
            name VARCHAR(255) NOT NULL,
            domain VARCHAR(255),
            api_key TEXT,
            status VARCHAR(50) DEFAULT 'connected',
            connected_at TIMESTAMP,
            last_sync TIMESTAMP,
            product_count INTEGER DEFAULT 0,
            is_primary BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            upstream_probe_at TIMESTAMP,
            upstream_probe_status TEXT,
            upstream_probe_http_status INTEGER,
            upstream_probe_failures INTEGER NOT NULL DEFAULT 0
        )
        """
    )


async def _reset() -> None:
    for table in (
        "merchant_stores",
        "catalog_offers",
        "catalog_skus",
        "catalog_products",
        "catalog_merchants",
        "catalog_sync_jobs",
    ):
        await database.execute(
            f"DELETE FROM {table} WHERE merchant_id LIKE :p", {"p": f"{_PREFIX}%"}
        )


@pytest.fixture(autouse=True)
async def _db():
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await _ensure_schema()
    await _reset()
    try:
        yield
    finally:
        await _reset()
        if not was_connected:
            await database.disconnect()


async def _seed_merchant(merchant_id: str, status: str) -> None:
    await database.execute(
        """
        INSERT INTO catalog_merchants
            (merchant_id, merchant_name, primary_platform, status, indexable,
             source_system, source_ref)
        VALUES (:m, :name, 'shopify', :status, 1, 'seeded_by_test', 'seed.example')
        """,
        {"m": merchant_id, "name": merchant_id, "status": status},
    )


async def _merchant_row(merchant_id: str) -> Optional[Dict[str, Any]]:
    row = await database.fetch_one(
        "SELECT * FROM catalog_merchants WHERE merchant_id = :m", {"m": merchant_id}
    )
    return dict(row) if row is not None else None


async def _status(merchant_id: str) -> Optional[str]:
    row = await _merchant_row(merchant_id)
    return None if row is None else str(row.get("status"))


def _audit_product() -> Dict[str, Any]:
    return {
        "title": "Heartleaf 77% Soothing Toner",
        "pdp_url": "https://www.statfixture.com/products/heartleaf-toner",
        "vendor": "StatFixture",
        "product_type": "Toner",
    }


# ---------------------------------------------------------------------------
# 1. The column itself: minted on INSERT, untouched on UPDATE
# ---------------------------------------------------------------------------


async def test_mint_lands_active_when_no_status_is_given():
    """The default must still MINT 'active' — url_audit merchants have no
    storefront, and the by-signature PDP read INNER-JOINs on status='active'.
    Preserving-on-update must not turn into never-writing."""
    merchant = f"{_PREFIX}_mint"

    await upsert_catalog_merchant(
        merchant_id=merchant,
        merchant_name="Mint Co",
        primary_platform="url_audit",
        source_system="url_audit_intake",
        source_ref="statfixture.com",
    )

    assert await _status(merchant) == "active"


async def test_default_status_does_not_reactivate_an_inactive_merchant():
    """The #1852 clobber, at the smallest scale that shows it."""
    merchant = f"{_PREFIX}_inactive"
    await _seed_merchant(merchant, "inactive")

    await upsert_catalog_merchant(
        merchant_id=merchant,
        merchant_name=None,
        primary_platform="url_audit",
        source_system="url_audit_intake",
        source_ref="statfixture.com",
        metadata_json={"ingested_from": "url_audit_intake"},
    )

    row = await _merchant_row(merchant)
    assert row["status"] == "inactive"
    # ...and the UPDATE really ran: everything the caller DOES own moved.
    # Without this the test would also pass if the upsert had been a no-op.
    assert row["source_system"] == "url_audit_intake"
    assert row["source_ref"] == "statfixture.com"
    assert row["primary_platform"] == "url_audit"


async def test_default_status_does_not_graduate_an_observed_seller():
    """`status='observed'` (ADR-009 D2) means "crawl-sourced, deliberately not
    servable as the public citation artifact until graduation". A content
    re-sync is not a graduation decision. `catalog_enrichment_agent/apply.py`
    already refuses the clobbering upsert for exactly this reason."""
    merchant = f"{_PREFIX}_observed"
    await _seed_merchant(merchant, "observed")

    await upsert_catalog_merchant(
        merchant_id=merchant,
        merchant_name=None,
        primary_platform="shopify",
        source_system="catalog_reconcile",
        source_ref="statfixture.com",
    )

    row = await _merchant_row(merchant)
    assert row["status"] == "observed"
    assert row["source_system"] == "catalog_reconcile"


async def test_an_explicit_status_still_moves_an_existing_row():
    """Preservation is for callers that said nothing. A caller that NAMES a
    status is a lifecycle/identity writer and keeps its authority."""
    merchant = f"{_PREFIX}_explicit"
    await _seed_merchant(merchant, "active")

    await upsert_catalog_merchant(
        merchant_id=merchant,
        merchant_name=None,
        primary_platform="url_audit",
        source_system="seller_identity",
        source_ref="statfixture.com",
        status="observed",
    )

    assert await _status(merchant) == "observed"


async def test_an_explicit_status_is_honoured_on_mint():
    """`seller_identity.ensure_observed_seller`'s shape: a fresh observed seller
    must be born 'observed', not 'active'."""
    merchant = f"{_PREFIX}_obs_mint"

    await upsert_catalog_merchant(
        merchant_id=merchant,
        merchant_name="StatFixture",
        primary_platform="external_crawl",
        source_system="seller_identity",
        source_ref="statfixture.com",
        status="observed",
    )

    assert await _status(merchant) == "observed"


async def test_other_upsert_by_pk_tables_still_move_their_status():
    """The preservation is OPT-IN per call site, and must stay that way:
    `catalog_sync_jobs` / `catalog_sync_events` go through the same
    `_upsert_by_pk` and move `status` on purpose. A blanket 'never update
    status' guard would freeze every sync job at 'pending'."""
    from services.catalog_sync_service import _upsert_by_pk

    job_id = f"{_PREFIX}_job"
    base = {
        "job_id": job_id,
        "merchant_id": f"{_PREFIX}_jobmerchant",
        "connector": "shopify",
        "mode": "full",
    }
    await _upsert_by_pk(catalog_sync_jobs, "job_id", {**base, "status": "pending"})
    await _upsert_by_pk(catalog_sync_jobs, "job_id", {**base, "status": "completed"})

    row = await database.fetch_one(
        "SELECT status FROM catalog_sync_jobs WHERE job_id = :j", {"j": job_id}
    )
    assert str(dict(row)["status"]) == "completed"


async def test_a_declared_field_absent_from_the_payload_is_not_invented():
    """`_preserve_caller_declared_fields` copies the existing value only for
    fields the payload actually carries — the same `if field in payload` shape
    its two siblings use. Pinned with a name that is not a column at all,
    because that is the only case with an observable difference: for a real
    column the copied value equals what is already stored. Without the check the
    helper would inject the key and the UPDATE would fail on an unknown column.
    """
    from services.catalog_sync_service import _upsert_by_pk

    merchant = f"{_PREFIX}_absent"
    await _seed_merchant(merchant, "inactive")

    await _upsert_by_pk(
        catalog_merchants,
        "merchant_id",
        {"merchant_id": merchant, "source_system": "later_writer"},
        preserve_on_update=("status", "no_such_column"),
    )

    row = await _merchant_row(merchant)
    assert row["status"] == "inactive"
    assert row["source_system"] == "later_writer"


# ---------------------------------------------------------------------------
# 2. The two real call sites — the delivering lines, driven for real
# ---------------------------------------------------------------------------


async def test_url_audit_intake_does_not_reactivate_a_detached_merchant():
    """Drives `services/audit_index_intake.upsert_audited_sku_to_index`, the
    call site that reaches `upsert_catalog_merchant` with no `status`. Its
    merchant upsert sits inside a best-effort `try:`, so this also asserts the
    row's content moved — otherwise a swallowed exception would read as a pass.
    """
    from services.audit_index_intake import upsert_audited_sku_to_index

    merchant = f"{_PREFIX}_audit"
    await _seed_merchant(merchant, "inactive")

    content_key = await upsert_audited_sku_to_index(merchant, _audit_product())
    assert content_key  # the seed itself landed

    row = await _merchant_row(merchant)
    assert row["status"] == "inactive"
    assert row["source_system"] == "url_audit_intake"
    assert row["source_ref"] == "statfixture.com"
    assert row["primary_platform"] == "url_audit"


async def test_ingest_standard_products_does_not_reactivate_a_detached_merchant():
    """Drives `ingest_standard_products`, the other default-status call site.
    An empty payload is enough: the merchant upsert runs before the product
    loop, which is precisely why every sync re-asserted 'active'."""
    from services.catalog_sync_service import ingest_standard_products

    merchant = f"{_PREFIX}_ingest"
    await _seed_merchant(merchant, "inactive")

    await ingest_standard_products(
        merchant_id=merchant,
        platform="shopify",
        product_payloads=[],
        source_system="catalog_reconcile",
        source_ref=f"catalog_reconcile:{merchant}:shopify",
    )

    row = await _merchant_row(merchant)
    assert row["status"] == "inactive"
    assert row["source_system"] == "catalog_reconcile"
    assert row["source_ref"] == f"catalog_reconcile:{merchant}:shopify"


# ---------------------------------------------------------------------------
# 3. The whole arc: PR #1852's transition is the one with no backstop
# ---------------------------------------------------------------------------


async def test_a_url_audit_cannot_undo_the_last_store_detach():
    """Merchant detaches their LAST store -> 'inactive' (#1852) -> runs one URL
    audit from the portal. Before the fix the audit put them back to 'active'
    with ZERO merchant_stores rows, and the sweep could never see them again."""
    from services.audit_index_intake import upsert_audited_sku_to_index

    merchant = f"{_PREFIX}_detach"
    await _seed_merchant(merchant, "active")

    # The DELETE /merchant/integrations/store/{store_id} shape: the row is HARD
    # deleted, then the route calls the write-through with last_store_removed.
    out = await lifecycle.sync_catalog_merchant_status(
        merchant, reason="store_deleted", last_store_removed=True
    )
    assert out["changed"] is True
    assert await _status(merchant) == "inactive"

    await upsert_audited_sku_to_index(merchant, _audit_product())

    assert await _status(merchant) == "inactive"

    # And the reason this one is terminal rather than self-healing: the hourly
    # sweep drives off merchant_stores, which has no row for this merchant.
    summary = await lifecycle.reconcile_catalog_merchant_statuses()
    assert not any(
        t.get("merchant_id") == merchant for t in summary.get("transitions") or []
    )
