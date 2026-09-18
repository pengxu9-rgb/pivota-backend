"""`catalog_merchants.metadata_json` is not one writer's column.

The same defect PR #1857 fixed for `status`, one column over, and this one was
LIVE on tenant merchants. `services/catalog_sync_service._upsert_by_pk` applies
its WHOLE payload on UPDATE, and `upsert_catalog_merchant` put
`"metadata_json": metadata_json or {}` in that payload unconditionally. Two live
callers reach it against EXISTING rows —

  * `services/audit_index_intake.upsert_audited_sku_to_index`, every URL audit
    run from the merchant portal, passing `{"ingested_from": "url_audit_intake"}`;
  * `ingest_standard_products`, every catalog sync, passing
    `{"ingested_from": <source_system>}`

— so each of them REPLACED the entire JSONB column with a single key.

What that destroyed, and why it is a serving defect rather than lost provenance:
`services/brand_claim_service.set_merchant_brand_direct` stamps
`metadata_json.brand_relationship='brand_direct'` after a DNS/email ownership
proof, deliberately via an ATOMIC server-side JSONB merge whose docstring says
it avoids read-modify-write so "a concurrent writer to metadata_json can't be
clobbered (B2)". That careful write was then undone wholesale by the next
catalog sync. It is called on `claim["merchant_id"]` — a TENANT merchant id,
exactly the population the audit and ingest paths reach. And it is the ONLY
value `services/offer_classification.classify_offer_type` trusts to return
'brand_direct': `services/pivot_query_service` reads it back on three serving
paths (`bm.metadata_json->>'brand_relationship'`), so losing the key silently
demotes a verified brand's offers on public recall.

The same write also erased the ADR-009 D2 mint stamp `services/seller_identity.py`
puts on an observed seller-of-record (`observed` / `minted_by` / `adr` /
`brand_identity` / `seller_identity`).

The fix MERGES rather than preserving, because unlike `status` this module does
own one of the column's keys (`ingested_from`) and must keep being able to write
it. `test_the_callers_own_key_still_lands_on_a_row_that_already_exists` is the
half that a blanket `preserve_on_update=("metadata_json",)` would fail.

These tests EXECUTE and every assertion is a DB re-read, matching
`tests/test_catalog_merchant_status_not_clobbered.py`. Three of them drive the
REAL callers rather than a hand-rolled stand-in, because the delivering line is
the call site, not the helper — and each asserts the row's own fields moved as
well, so a swallowed exception inside those best-effort `try:` blocks cannot
masquerade as "the metadata was preserved".

The SQL here runs on SQLite, where the merge compiles to `json_patch`. The
production Postgres expression (`||`) and the arc through the real
`set_merchant_brand_direct` are pinned separately in
`tests/test_catalog_merchant_metadata_merge_postgres.py`.
"""

from __future__ import annotations

import json
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
from services.catalog_sync_service import upsert_catalog_merchant
from tests.model_schema import ensure_model_tables

_PREFIX = "cmmeta"

# What `services/brand_claim_service.set_merchant_brand_direct` leaves behind.
_BRAND_DIRECT = {"brand_relationship": "brand_direct"}

# What `services/seller_identity.ensure_observed_seller` stamps at mint.
_OBSERVED_STAMP = {
    "observed": True,
    "minted_by": "seller_identity.ensure_observed_seller",
    "adr": "ADR-009-D2",
    "brand_identity": {"brand": "statfixture", "etld1": "statfixture.com"},
}


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


async def _reset() -> None:
    for table in (
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


async def _seed_merchant(merchant_id: str, metadata: Optional[Dict[str, Any]]) -> None:
    await database.execute(
        """
        INSERT INTO catalog_merchants
            (merchant_id, merchant_name, primary_platform, status, indexable,
             source_system, source_ref, metadata_json)
        VALUES (:m, :name, 'shopify', 'active', 1, 'seeded_by_test',
                'seed.example', :md)
        """,
        {
            "m": merchant_id,
            "name": merchant_id,
            "md": None if metadata is None else json.dumps(metadata),
        },
    )


async def _merchant_row(merchant_id: str) -> Optional[Dict[str, Any]]:
    row = await database.fetch_one(
        "SELECT * FROM catalog_merchants WHERE merchant_id = :m", {"m": merchant_id}
    )
    return None if row is None else dict(row)


async def _metadata(merchant_id: str) -> Optional[Dict[str, Any]]:
    """The column, decoded. The raw-SQL read above bypasses SQLAlchemy's JSON
    type, so on SQLite the value arrives as the stored TEXT."""
    row = await _merchant_row(merchant_id)
    if row is None:
        return None
    raw = row.get("metadata_json")
    if raw is None:
        return None
    return json.loads(raw) if isinstance(raw, (str, bytes)) else dict(raw)


def _audit_product() -> Dict[str, Any]:
    return {
        "title": "Heartleaf 77% Soothing Toner",
        "pdp_url": "https://www.statfixture.com/products/heartleaf-toner",
        "vendor": "StatFixture",
        "product_type": "Toner",
    }


# ---------------------------------------------------------------------------
# 1. The column itself: written whole on INSERT, merged key-by-key on UPDATE
# ---------------------------------------------------------------------------


async def test_a_sync_does_not_erase_a_verified_brand_claim():
    """The clobber, at the smallest scale that shows it. Reproduced during
    review of PR #1857: seed `{"brand_relationship": "brand_direct"}`, run the
    audit-shaped upsert, re-read -> `{"ingested_from": "url_audit_intake"}`."""
    merchant = f"{_PREFIX}_claimed"
    await _seed_merchant(merchant, dict(_BRAND_DIRECT))

    await upsert_catalog_merchant(
        merchant_id=merchant,
        merchant_name=None,
        primary_platform="url_audit",
        source_system="url_audit_intake",
        source_ref="statfixture.com",
        metadata_json={"ingested_from": "url_audit_intake"},
    )

    md = await _metadata(merchant)
    assert md["brand_relationship"] == "brand_direct"
    # ...and the UPDATE really ran: everything the caller DOES own moved.
    # Without this the test would also pass if the upsert had been a no-op.
    row = await _merchant_row(merchant)
    assert row["source_system"] == "url_audit_intake"
    assert row["source_ref"] == "statfixture.com"
    assert row["primary_platform"] == "url_audit"


async def test_the_callers_own_key_still_lands_on_a_row_that_already_exists():
    """Merging, not preserving. `ingested_from` is this module's own key, and a
    blanket `preserve_on_update=("metadata_json",)` — the treatment `status`
    got in #1857 — would make it unwritable on every row that already exists.
    This is the assertion that distinguishes the two fixes."""
    merchant = f"{_PREFIX}_ownkey"
    await _seed_merchant(merchant, dict(_BRAND_DIRECT))

    await upsert_catalog_merchant(
        merchant_id=merchant,
        merchant_name=None,
        primary_platform="shopify",
        source_system="catalog_reconcile",
        source_ref="statfixture.com",
        metadata_json={"ingested_from": "catalog_reconcile"},
    )

    assert await _metadata(merchant) == {
        "brand_relationship": "brand_direct",
        "ingested_from": "catalog_reconcile",
    }


async def test_a_second_sync_overwrites_only_its_own_key():
    """Shallow REPLACE per key, not append: re-syncing from a different source
    must move `ingested_from` to the new value rather than keeping both."""
    merchant = f"{_PREFIX}_resync"
    await _seed_merchant(
        merchant, {**_BRAND_DIRECT, "ingested_from": "url_audit_intake"}
    )

    await upsert_catalog_merchant(
        merchant_id=merchant,
        merchant_name=None,
        primary_platform="shopify",
        source_system="products_cache",
        source_ref="statfixture.com",
        metadata_json={"ingested_from": "products_cache"},
    )

    assert await _metadata(merchant) == {
        "brand_relationship": "brand_direct",
        "ingested_from": "products_cache",
    }


async def test_an_observed_seller_mint_stamp_survives_a_sync():
    """The other namespace in this column: the ADR-009 D2 stamp
    `services/seller_identity.py` writes when it mints an observed
    seller-of-record. Nested values must survive intact, not be flattened."""
    merchant = f"{_PREFIX}_merch_obs_deadbeefcafe0001"
    await _seed_merchant(merchant, dict(_OBSERVED_STAMP))

    await upsert_catalog_merchant(
        merchant_id=merchant,
        merchant_name=None,
        primary_platform="shopify",
        source_system="products_cache",
        source_ref="statfixture.com",
        metadata_json={"ingested_from": "products_cache"},
    )

    md = await _metadata(merchant)
    assert md["observed"] is True
    assert md["minted_by"] == "seller_identity.ensure_observed_seller"
    assert md["adr"] == "ADR-009-D2"
    assert md["brand_identity"] == {
        "brand": "statfixture",
        "etld1": "statfixture.com",
    }
    assert md["ingested_from"] == "products_cache"


async def test_a_caller_with_nothing_to_say_does_not_blank_the_column():
    """`metadata_json or {}` at the call site turns "said nothing" into `{}`,
    and `{}` written whole is still a clobber. `seller_identity`'s two call
    sites are the only ones that pass a dict on purpose; every other caller
    that omits the argument must leave the column exactly as it found it."""
    merchant = f"{_PREFIX}_silent"
    await _seed_merchant(merchant, dict(_BRAND_DIRECT))

    await upsert_catalog_merchant(
        merchant_id=merchant,
        merchant_name=None,
        primary_platform="url_audit",
        source_system="url_audit_intake",
        source_ref="statfixture.com",
    )

    assert await _metadata(merchant) == _BRAND_DIRECT
    assert (await _merchant_row(merchant))["source_system"] == "url_audit_intake"


async def test_a_caller_with_nothing_to_say_leaves_a_null_column_null():
    """The empty patch is DROPPED from the UPDATE, not merged as a no-op. On a
    row that already has keys the two are indistinguishable — `x || {}` and
    `json_patch(x, {})` both return `x` — so the difference is only observable
    on a legacy NULL row, where merging would quietly rewrite NULL to `{}`.
    Pinned here because without it, "drop the empty patch" is a branch no test
    can fail on."""
    merchant = f"{_PREFIX}_silentnull"
    await _seed_merchant(merchant, None)

    await upsert_catalog_merchant(
        merchant_id=merchant,
        merchant_name=None,
        primary_platform="url_audit",
        source_system="url_audit_intake",
        source_ref="statfixture.com",
    )

    row = await _merchant_row(merchant)
    assert row["metadata_json"] is None
    assert row["source_system"] == "url_audit_intake"


async def test_a_null_column_becomes_an_object_rather_than_failing():
    """`metadata_json` is nullable and legacy rows have NULL in it. The merge
    must COALESCE, not return NULL — `NULL || patch` is NULL on Postgres, which
    would turn the clobber into an erasure on exactly the oldest rows."""
    merchant = f"{_PREFIX}_nullmd"
    await _seed_merchant(merchant, None)

    await upsert_catalog_merchant(
        merchant_id=merchant,
        merchant_name=None,
        primary_platform="shopify",
        source_system="catalog_reconcile",
        source_ref="statfixture.com",
        metadata_json={"ingested_from": "catalog_reconcile"},
    )

    assert await _metadata(merchant) == {"ingested_from": "catalog_reconcile"}


async def test_mint_still_writes_the_whole_dict():
    """Merging on UPDATE must not turn into never-writing on INSERT. A fresh
    observed seller is born carrying its full ADR-009 D2 stamp."""
    merchant = f"{_PREFIX}_merch_obs_mint000000000001"

    await upsert_catalog_merchant(
        merchant_id=merchant,
        merchant_name="StatFixture",
        primary_platform="external_crawl",
        source_system="seller_identity",
        source_ref="statfixture.com",
        status="observed",
        metadata_json=dict(_OBSERVED_STAMP),
    )

    assert await _metadata(merchant) == _OBSERVED_STAMP
    assert (await _merchant_row(merchant))["status"] == "observed"


async def test_mint_with_no_metadata_still_lands_an_empty_object():
    """The pre-existing shape for a caller that says nothing on a NEW row:
    `{}`, not NULL. Pinned so the drop-on-update path cannot leak into the
    insert and silently change what a fresh row looks like."""
    merchant = f"{_PREFIX}_bareminted"

    await upsert_catalog_merchant(
        merchant_id=merchant,
        merchant_name="Bare Co",
        primary_platform="url_audit",
        source_system="url_audit_intake",
        source_ref="statfixture.com",
    )

    assert await _metadata(merchant) == {}


# ---------------------------------------------------------------------------
# 2. The helper's contract
# ---------------------------------------------------------------------------


async def test_the_merge_is_opt_in_per_call_site():
    """`_upsert_by_pk` serves many tables and most of their JSON columns are
    single-owner, where replace is the right answer. A blanket "always merge"
    would make a stale key impossible to REMOVE — pinned on
    `catalog_sync_jobs.stats_json`, which each run of a job rewrites wholesale
    and whose counters must not accumulate across runs."""
    from services.catalog_sync_service import _upsert_by_pk

    job_id = f"{_PREFIX}_job"
    base = {
        "job_id": job_id,
        "merchant_id": f"{_PREFIX}_jobmerchant",
        "connector": "shopify",
        "mode": "full",
        "status": "completed",
    }
    await _upsert_by_pk(
        catalog_sync_jobs, "job_id", {**base, "stats_json": {"products_failed": 7}}
    )
    await _upsert_by_pk(
        catalog_sync_jobs, "job_id", {**base, "stats_json": {"products_ingested": 3}}
    )

    row = await database.fetch_one(
        "SELECT stats_json FROM catalog_sync_jobs WHERE job_id = :j", {"j": job_id}
    )
    raw = dict(row)["stats_json"]
    stats = json.loads(raw) if isinstance(raw, (str, bytes)) else dict(raw)
    assert stats == {"products_ingested": 3}


async def test_a_declared_column_absent_from_the_payload_is_not_invented():
    """A declared field the payload does not carry is simply not there to merge.
    Pinned with a name that is not a column at all — a typo in a call site's
    tuple must be inert, not an UPDATE that fails on an unknown column."""
    from services.catalog_sync_service import _upsert_by_pk

    merchant = f"{_PREFIX}_absent"
    await _seed_merchant(merchant, dict(_BRAND_DIRECT))

    await _upsert_by_pk(
        catalog_merchants,
        "merchant_id",
        {"merchant_id": merchant, "source_system": "later_writer"},
        merge_json_on_update=("metadata_json", "no_such_column"),
    )

    assert await _metadata(merchant) == _BRAND_DIRECT
    assert (await _merchant_row(merchant))["source_system"] == "later_writer"


async def test_a_non_dict_payload_is_dropped_rather_than_written_through():
    """A shape the merge cannot express must fail SAFE. Falling back to a
    whole-column write would silently reintroduce the exact clobber this guard
    exists to prevent, on the one call site that opted into protection."""
    from services.catalog_sync_service import _upsert_by_pk

    merchant = f"{_PREFIX}_nondict"
    await _seed_merchant(merchant, dict(_BRAND_DIRECT))

    await _upsert_by_pk(
        catalog_merchants,
        "merchant_id",
        {
            "merchant_id": merchant,
            "source_system": "later_writer",
            "metadata_json": json.dumps({"ingested_from": "stringified"}),
        },
        merge_json_on_update=("metadata_json",),
    )

    assert await _metadata(merchant) == _BRAND_DIRECT
    assert (await _merchant_row(merchant))["source_system"] == "later_writer"


async def test_a_brand_claim_landing_mid_upsert_is_not_reverted():
    """Merging in the DATABASE, not in Python.

    `_upsert_by_pk` SELECTs the existing row, then UPDATEs. The audit path is
    not inside a transaction, so a `brand_claim_service.set_merchant_brand_direct`
    write can land in between — and a Python-side merge would read the
    pre-claim dict, add `ingested_from`, and write the whole thing back,
    reverting the claim. That is the B2 hazard `set_merchant_brand_direct`'s own
    docstring names, and it is why the fix is a server-side expression rather
    than a read-modify-write.

    The race is made deterministic by handing the upsert a STALE snapshot while
    the real row has already moved on; everything downstream is the real code,
    and the assertion is a DB re-read.
    """
    import services.catalog_sync_service as css

    merchant = f"{_PREFIX}_race"
    await _seed_merchant(merchant, {})

    real_fetch = css._fetch_one_by_pk

    async def _stale_read(table, pk_name, pk_value):
        row = await real_fetch(table, pk_name, pk_value)
        if row is not None and pk_value == merchant:
            # The brand claim verifies here — after our SELECT, before our
            # UPDATE. `row` is now the stale snapshot the upsert reasons on.
            # Same shallow-merge shape as set_merchant_brand_direct, spelled
            # for SQLite.
            await database.execute(
                "UPDATE catalog_merchants "
                "   SET metadata_json = json_patch(COALESCE(metadata_json, '{}'), :p) "
                " WHERE merchant_id = :m",
                {"p": json.dumps(_BRAND_DIRECT), "m": merchant},
            )
        return row

    monkeypatch_target = css
    original = monkeypatch_target._fetch_one_by_pk
    monkeypatch_target._fetch_one_by_pk = _stale_read
    try:
        await upsert_catalog_merchant(
            merchant_id=merchant,
            merchant_name=None,
            primary_platform="url_audit",
            source_system="url_audit_intake",
            source_ref="statfixture.com",
            metadata_json={"ingested_from": "url_audit_intake"},
        )
    finally:
        monkeypatch_target._fetch_one_by_pk = original

    assert await _metadata(merchant) == {
        "brand_relationship": "brand_direct",
        "ingested_from": "url_audit_intake",
    }


# ---------------------------------------------------------------------------
# 3. The two real call sites — the delivering lines, driven for real
# ---------------------------------------------------------------------------


async def test_url_audit_intake_does_not_erase_a_verified_brand_claim():
    """Drives `services/audit_index_intake.upsert_audited_sku_to_index`, the
    call site that reaches `upsert_catalog_merchant` with
    `{"ingested_from": "url_audit_intake"}`. Its merchant upsert sits inside a
    best-effort `try:`, so this also asserts the row's own fields moved —
    otherwise a swallowed exception would read as a pass."""
    from services.audit_index_intake import upsert_audited_sku_to_index

    merchant = f"{_PREFIX}_audit"
    await _seed_merchant(merchant, dict(_BRAND_DIRECT))

    content_key = await upsert_audited_sku_to_index(merchant, _audit_product())
    assert content_key  # the seed itself landed

    md = await _metadata(merchant)
    assert md["brand_relationship"] == "brand_direct"
    assert md["ingested_from"] == "url_audit_intake"
    row = await _merchant_row(merchant)
    assert row["source_system"] == "url_audit_intake"
    assert row["source_ref"] == "statfixture.com"
    assert row["primary_platform"] == "url_audit"


async def test_ingest_standard_products_does_not_erase_a_verified_brand_claim():
    """Drives `ingest_standard_products`, the other call site. An empty payload
    is enough: the merchant upsert runs BEFORE the product loop, which is
    precisely why every sync replaced the column."""
    from services.catalog_sync_service import ingest_standard_products

    merchant = f"{_PREFIX}_ingest"
    await _seed_merchant(merchant, dict(_BRAND_DIRECT))

    await ingest_standard_products(
        merchant_id=merchant,
        platform="shopify",
        product_payloads=[],
        source_system="catalog_reconcile",
        source_ref=f"catalog_reconcile:{merchant}:shopify",
    )

    md = await _metadata(merchant)
    assert md["brand_relationship"] == "brand_direct"
    assert md["ingested_from"] == "catalog_reconcile"
    row = await _merchant_row(merchant)
    assert row["source_system"] == "catalog_reconcile"
    assert row["source_ref"] == f"catalog_reconcile:{merchant}:shopify"


async def test_ingest_standard_products_does_not_erase_an_observed_mint_stamp():
    """The other namespace, on the reachable call site.

    `routes/catalog_routes.py` takes `merchant_id` from the request body or the
    path under a bare `Depends(get_current_user)` with no tenant scoping, and
    runs it through `run_catalog_sync_job` -> `sync_products_cache_to_catalog`
    -> here, so an observed seller's id genuinely arrives at this line."""
    from services.catalog_sync_service import ingest_standard_products

    merchant = f"{_PREFIX}_merch_obs_deadbeefcafe0002"
    await _seed_merchant(merchant, dict(_OBSERVED_STAMP))

    await ingest_standard_products(
        merchant_id=merchant,
        platform="shopify",
        product_payloads=[],
        source_system="products_cache",
        source_ref="catalog_sync_job:probe",
    )

    md = await _metadata(merchant)
    assert md["minted_by"] == "seller_identity.ensure_observed_seller"
    assert md["adr"] == "ADR-009-D2"
    assert md["brand_identity"]["etld1"] == "statfixture.com"
    assert md["ingested_from"] == "products_cache"
    assert (await _merchant_row(merchant))["source_system"] == "products_cache"
