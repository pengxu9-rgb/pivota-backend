"""The W2 claimed-attach lookup in `_prepare_seller_of_record`, EXECUTED against
a brand_claims table built from db/brand_claims.py's own Table.

WHY A POSTGRES GATE. The lookup is wrapped in a best-effort `except Exception`
that logs a warning and answers "no claim" — the honest fallback for a wedged
connection, and a perfect hiding place for a wrong column name. From the day
it was written until 2026-09-04 the statement read `status` and `updated_at`;
brand_claims has `verification_status` and `verified_at`. Postgres refused it
on EVERY apply (`column "status" does not exist`, prod job
catalog-curated-brand-onboard-xlv56, flowerbeauty.com), the except branch set
`claimed = None`, and the ingest minted an observed `merch_obs_` merchant for
a domain a verified tenant already owned. No verified claim could ever attach
through this path.

tests/test_w2_retailer_seller_model.py cannot see that: its fake database
answers any SQL containing "FROM brand_claims" from a dict, so the column
names in the text are never parsed. Only a database built from the real
Table, on the dialect that ships, can refuse a column that is not there.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        .venv/bin/python -m pytest tests/test_w2_claimed_attach_query_postgres.py
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason=(
        "needs a Postgres DATABASE_URL — this is the production-dialect gate; "
        "see the module docstring for the one-line setup"
    ),
)

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_TABLES = ("brand_claims", "catalog_products", "catalog_merchants")


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to drop {_TABLES} in {dbname!r}")


@pytest.fixture(autouse=True)
async def _db():
    """Build the three tables the function touches from their SQLAlchemy Tables —
    the same declarations create_all builds prod from — so a column the query
    names but the Table does not declare fails here the way it fails there."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    from db.brand_claims import brand_claims
    from db.catalog import catalog_merchants, catalog_products
    from db.database import database

    _assert_throwaway_database()
    was = database.is_connected
    if not was:
        await database.connect()
    for name in _TABLES:
        await database.execute(f"DROP TABLE IF EXISTS {name} CASCADE")
    for table in (brand_claims, catalog_merchants, catalog_products):
        await database.execute(str(CreateTable(table).compile(dialect=postgresql.dialect())))
    yield database
    for name in _TABLES:
        await database.execute(f"DROP TABLE IF EXISTS {name} CASCADE")
    if not was and database.is_connected:
        await database.disconnect()


async def _claim(database, *, merchant_id: str, brand_domain: str, status: str) -> None:
    from db.brand_claims import STATUS_VERIFIED

    now = datetime.now(timezone.utc)
    await database.execute(
        "INSERT INTO brand_claims (claim_id, merchant_id, brand_domain, claim_method, "
        " verification_status, verified_at, created_at) "
        "VALUES (:id, :m, :d, 'dns_txt', :s, :v, :c)",
        {"id": str(uuid.uuid4()), "m": merchant_id, "d": brand_domain, "s": status,
         "v": now if status == STATUS_VERIFIED else None, "c": now},
    )


async def _tenant_merchant(database, merchant_id: str) -> None:
    await database.execute(
        "INSERT INTO catalog_merchants (merchant_id, merchant_name, status) "
        "VALUES (:m, :m, 'active')",
        {"m": merchant_id},
    )


def _plan(*, observed_id: str, registrable: str):
    product_key = "ext:flower beauty::abc12345"
    return {
        "pdps": [{"product_key": product_key, "merchant_id": observed_id}],
        "skus": [{"sku_key": product_key + "::canonical", "product_key": product_key,
                  "merchant_id": observed_id}],
        "merchants": [{
            "merchant_id": observed_id, "merchant_name": registrable,
            "primary_platform": "external_seed", "status": "observed",
            "source_system": "curated_brand_onboard", "source_ref": registrable,
            "metadata_json": "{}", "_ensure_only": True,
        }],
    }


async def _merchant_rows(database) -> set:
    rows = await database.fetch_all("SELECT merchant_id FROM catalog_merchants")
    return {dict(r)["merchant_id"] for r in rows}


OBSERVED = "merch_obs_0123456789abcdef"
TENANT = "merch_tenant_flowerbeauty"


async def test_a_verified_claim_attaches_the_rows_to_the_tenant(_db, caplog):
    """The prod failure, replayed: a verified claim for flowerbeauty.com whose
    tenant has a catalog_merchants row. The rows must follow the tenant and NO
    observed merchant may be minted. Before the fix the lookup raised, the
    warning below fired, and the observed row was written."""
    from services.catalog_enrichment_agent.apply import _prepare_seller_of_record

    await _claim(_db, merchant_id=TENANT, brand_domain="flowerbeauty.com", status="verified")
    await _tenant_merchant(_db, TENANT)
    plan = _plan(observed_id=OBSERVED, registrable="flowerbeauty.com")

    with caplog.at_level(logging.WARNING):
        out = await _prepare_seller_of_record(plan, _db)

    failed = [r.getMessage() for r in caplog.records if "claimed-attach lookup failed" in r.getMessage()]
    assert failed == [], f"the lookup itself failed (and was swallowed): {failed}"
    assert plan["pdps"][0]["merchant_id"] == TENANT
    assert plan["skus"][0]["merchant_id"] == TENANT
    assert out["merchants"] == []
    assert await _merchant_rows(_db) == {TENANT}, "an observed merchant was minted despite the claim"


async def test_a_subdomain_claim_attaches_by_registrable(_db):
    """The LIKE branch: the claim is on shop.flowerbeauty.com, the plan's
    registrable is flowerbeauty.com."""
    from services.catalog_enrichment_agent.apply import _prepare_seller_of_record

    await _claim(_db, merchant_id=TENANT, brand_domain="shop.flowerbeauty.com", status="verified")
    await _tenant_merchant(_db, TENANT)
    plan = _plan(observed_id=OBSERVED, registrable="flowerbeauty.com")

    await _prepare_seller_of_record(plan, _db)

    assert plan["pdps"][0]["merchant_id"] == TENANT
    assert await _merchant_rows(_db) == {TENANT}


async def test_a_pending_claim_does_not_attach(_db):
    """Positive counterpart for the status filter: an unverified claim must
    not hand a tenant someone else's rows. The observed merchant is minted."""
    from services.catalog_enrichment_agent.apply import _prepare_seller_of_record

    await _claim(_db, merchant_id=TENANT, brand_domain="flowerbeauty.com", status="pending")
    await _tenant_merchant(_db, TENANT)
    plan = _plan(observed_id=OBSERVED, registrable="flowerbeauty.com")

    out = await _prepare_seller_of_record(plan, _db)

    assert plan["pdps"][0]["merchant_id"] == OBSERVED
    assert plan["skus"][0]["merchant_id"] == OBSERVED
    assert out["merchants"] == []
    assert await _merchant_rows(_db) == {TENANT, OBSERVED}


async def test_the_newest_verified_claim_wins(_db):
    """Two tenants verified the same domain over time: the later verification
    is the seller of record (ORDER BY verified_at DESC)."""
    from services.catalog_enrichment_agent.apply import _prepare_seller_of_record

    await _claim(_db, merchant_id="merch_tenant_old", brand_domain="flowerbeauty.com", status="verified")
    await _db.execute("UPDATE brand_claims SET verified_at = verified_at - INTERVAL '30 days'")
    await _claim(_db, merchant_id=TENANT, brand_domain="flowerbeauty.com", status="verified")
    await _tenant_merchant(_db, TENANT)
    await _tenant_merchant(_db, "merch_tenant_old")
    plan = _plan(observed_id=OBSERVED, registrable="flowerbeauty.com")

    await _prepare_seller_of_record(plan, _db)

    assert plan["pdps"][0]["merchant_id"] == TENANT
