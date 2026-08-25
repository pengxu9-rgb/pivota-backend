"""Production-dialect gate for the `catalog_merchants.metadata_json` merge.

`services/catalog_sync_service._json_merge_expression` is dialect-split: on
Postgres it emits `COALESCE(col, '{}') || CAST(:p AS JSONB)`, on SQLite
`json_patch(COALESCE(col, '{}'), :p)`. `tests/test_catalog_merchant_metadata_not_clobbered.py`
runs the SQLite half and proves the branches; only Postgres can answer the three
questions that actually decide whether prod is fixed:

  * Does the emitted SQL RUN? SQLite's `||` is string concatenation, so a
    dialect mix-up produces a plausible-looking TEXT value there and a hard
    error (or, worse, a silently valid concatenation) in prod. The SQLite suite
    would go green either way — it never compiles the `||` form at all.
  * Is the merged value readable through the expression that actually serves
    it? The whole point of preserving the key is that
    `services/pivot_query_service` reads it back with
    `bm.metadata_json->>'brand_relationship'` on three serving paths, so the
    gate asserts through that exact expression rather than through a decoded
    dict — the assertion and the serving read are then the same question.
  * Are Postgres' merge semantics what the docstring claims? `||` is a SHALLOW
    replace that STORES a null; RFC 7396 `json_patch` deep-merges and DELETES on
    null. The SQLite suite cannot see that difference, so the claim is pinned
    here rather than asserted from memory.

The headline test drives the REAL `services/brand_claim_service.set_merchant_brand_direct`
— itself Postgres-only SQL — so the arc under test is the production one end to
end: a brand verifies ownership, then a catalog sync runs, and the verified
relationship is still there afterwards.

🚨 THIS FILE SHARES ONE DATABASE WITH THE OTHER GATE MODULES. Additive,
order-proof DDL only (#1651): the schema comes from `tests/model_schema.py`
(derived from `db/catalog.py`), and teardown DELETEs this file's own prefixed
rows rather than dropping anything.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

_PREFIX = "cmmetapg"

_BRAND_DIRECT = "brand_direct"


@pytest.fixture(autouse=True)
async def _db():
    import db.database as dbmod

    # `db.database` binds its singleton to whatever DATABASE_URL held when it was
    # FIRST imported. If a SQLite-bound test module got there first in this
    # process, skip LOUDLY rather than run Postgres-specific SQL against SQLite
    # and report a green that proves nothing.
    if "postgres" not in str(dbmod.DATABASE_URL):
        pytest.skip(
            "db.database is bound to a non-postgres URL (an earlier test imported "
            "it first). Run this file with a Postgres DATABASE_URL."
        )

    from db.catalog import catalog_merchants
    from db.merchant_onboarding import merchant_onboarding
    from tests.model_schema import ensure_model_tables

    database = dbmod.database
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    # merchant_onboarding rides along because `upsert_catalog_merchant` falls
    # back to `_resolve_merchant_name` whenever the caller passes
    # `merchant_name=None` — which both live call sites do.
    await ensure_model_tables((catalog_merchants, merchant_onboarding))
    await _reset()
    try:
        yield
    finally:
        await _reset()
        if not was_connected:
            await database.disconnect()


async def _reset() -> None:
    from db.database import database

    await database.execute(
        "DELETE FROM catalog_merchants WHERE merchant_id LIKE :p", {"p": f"{_PREFIX}%"}
    )


async def _seed_merchant(merchant_id: str, metadata: Optional[Dict[str, Any]]) -> None:
    from db.database import database

    await database.execute(
        """
        INSERT INTO catalog_merchants
            (merchant_id, merchant_name, primary_platform, status, indexable,
             source_system, source_ref, metadata_json)
        VALUES (:m, :name, 'shopify', 'active', TRUE, 'seeded_by_test',
                'seed.example', CAST(:md AS JSONB))
        """,
        {
            "m": merchant_id,
            "name": merchant_id,
            "md": None if metadata is None else json.dumps(metadata),
        },
    )


async def _metadata(merchant_id: str) -> Optional[Dict[str, Any]]:
    from db.database import database

    row = await database.fetch_one(
        "SELECT metadata_json FROM catalog_merchants WHERE merchant_id = :m",
        {"m": merchant_id},
    )
    if row is None:
        return None
    raw = dict(row)["metadata_json"]
    if raw is None:
        return None
    return json.loads(raw) if isinstance(raw, (str, bytes)) else dict(raw)


async def _brand_relationship_as_the_serving_path_reads_it(
    merchant_id: str,
) -> Optional[str]:
    """Byte-for-byte the expression `services/pivot_query_service` uses on its
    three serving paths (`bm.metadata_json->>'brand_relationship'`). A merge
    that produced valid JSON TEXT rather than JSONB would fail HERE and nowhere
    else in the suite."""
    from db.database import database

    row = await database.fetch_one(
        "SELECT bm.metadata_json->>'brand_relationship' AS brand_relationship "
        "  FROM catalog_merchants bm WHERE bm.merchant_id = :m",
        {"m": merchant_id},
    )
    return None if row is None else dict(row)["brand_relationship"]


# ---------------------------------------------------------------------------
# The production arc, end to end
# ---------------------------------------------------------------------------


async def test_a_sync_after_a_real_brand_claim_keeps_the_claim():
    """Both halves are the real production writers: `set_merchant_brand_direct`
    (Postgres `||` merge, run after a DNS/email ownership proof) and then
    `upsert_catalog_merchant` (every URL audit and every catalog sync). Before
    the fix the second erased the first."""
    from services.brand_claim_service import set_merchant_brand_direct
    from services.catalog_sync_service import upsert_catalog_merchant

    merchant = f"{_PREFIX}_claimarc"
    await _seed_merchant(merchant, {})

    assert await set_merchant_brand_direct(merchant) is True
    assert await _metadata(merchant) == {"brand_relationship": _BRAND_DIRECT}

    await upsert_catalog_merchant(
        merchant_id=merchant,
        merchant_name=None,
        primary_platform="url_audit",
        source_system="url_audit_intake",
        source_ref="statfixture.com",
        metadata_json={"ingested_from": "url_audit_intake"},
    )

    assert await _metadata(merchant) == {
        "brand_relationship": _BRAND_DIRECT,
        "ingested_from": "url_audit_intake",
    }


async def test_the_merged_column_is_still_jsonb_to_the_serving_read():
    """Preserving the key is not enough if the column stops being JSONB — the
    serving path reads it with `->>`, which needs a real JSONB value."""
    from services.catalog_sync_service import upsert_catalog_merchant

    merchant = f"{_PREFIX}_serving"
    await _seed_merchant(merchant, {"brand_relationship": _BRAND_DIRECT})

    await upsert_catalog_merchant(
        merchant_id=merchant,
        merchant_name=None,
        primary_platform="shopify",
        source_system="catalog_reconcile",
        source_ref="statfixture.com",
        metadata_json={"ingested_from": "catalog_reconcile"},
    )

    assert await _brand_relationship_as_the_serving_path_reads_it(merchant) == (
        _BRAND_DIRECT
    )


async def test_a_null_metadata_column_merges_rather_than_annihilating():
    """`NULL || anything` is NULL in Postgres — strictly worse than the clobber,
    since it would ERASE the column on exactly the oldest rows. The COALESCE is
    load-bearing here in a way SQLite's `json_patch(NULL, p)` only hints at."""
    from services.catalog_sync_service import upsert_catalog_merchant

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


async def test_production_semantics_are_shallow_replace_and_keep_a_null():
    """The documented divergence from the SQLite spelling, pinned on the dialect
    that decides it.

    `||` replaces a nested object WHOLESALE and STORES a null value; RFC 7396
    `json_patch` would deep-merge the object and DELETE the key. No caller
    writes either shape today — which is exactly why this needs a test rather
    than a comment: nothing else in the suite would notice if the two spellings
    drifted apart.
    """
    from services.catalog_sync_service import upsert_catalog_merchant

    merchant = f"{_PREFIX}_semantics"
    await _seed_merchant(
        merchant,
        {
            "brand_relationship": _BRAND_DIRECT,
            "brand_identity": {"brand": "statfixture", "etld1": "statfixture.com"},
            "doomed": "still here",
        },
    )

    await upsert_catalog_merchant(
        merchant_id=merchant,
        merchant_name=None,
        primary_platform="shopify",
        source_system="catalog_reconcile",
        source_ref="statfixture.com",
        metadata_json={"brand_identity": {"etld1": "other.example"}, "doomed": None},
    )

    md = await _metadata(merchant)
    # Untouched key survives — the property the whole fix exists for.
    assert md["brand_relationship"] == _BRAND_DIRECT
    # Nested object REPLACED, not deep-merged: 'brand' is gone.
    assert md["brand_identity"] == {"etld1": "other.example"}
    # Null is STORED, not treated as a delete: the key is still present.
    assert "doomed" in md
    assert md["doomed"] is None


async def test_the_mint_path_still_writes_the_whole_object():
    """Merging on UPDATE must not leak into the INSERT — a fresh observed seller
    is born carrying its full ADR-009 D2 stamp, as JSONB."""
    from services.catalog_sync_service import upsert_catalog_merchant

    merchant = f"{_PREFIX}_merch_obs_mint00000001"
    stamp = {
        "observed": True,
        "minted_by": "seller_identity.ensure_observed_seller",
        "adr": "ADR-009-D2",
        "brand_identity": {"brand": "statfixture", "etld1": "statfixture.com"},
    }

    await upsert_catalog_merchant(
        merchant_id=merchant,
        merchant_name="StatFixture",
        primary_platform="external_crawl",
        source_system="seller_identity",
        source_ref="statfixture.com",
        status="observed",
        metadata_json=dict(stamp),
    )

    assert await _metadata(merchant) == stamp
