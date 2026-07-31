"""H1 + H3 (#1648) — the cross-merchant recall lane must gate the OFFER SELLER
and read catalog_skus suppression.

These tests EXECUTE `_fetch_canonical_search_rows` against a real DB. Every
existing test of this lane monkeypatches the function or `database.fetch_all`,
which is why three source gates could be missing from it for months while the
suite stayed green — a mock cannot tell you a WHERE clause is absent.

H1 — every gate on this lane joins `m` on `p.merchant_id`, the merchant who OWNS
the canonical row. The merchant whose price and availability the lane PUBLISHES
is `o.merchant_id` (alias `bm`). They differ for 3,423 of 14,867 unsuppressed
offers on prod (2026-07-31). So a retired merchant's offer hanging off a sku
under an `external_seed`-owned canonical row passed every gate.

H3 — the recall CTE joins `catalog_skus` and ignored its suppression columns.

BOTH FIRE ZERO TIMES ON PROD TODAY (0 offers have a gated seller; 0 suppressed
skus sit under an unsuppressed product). That makes these tests the entire proof
that the gates are real rather than decorative, so each one is written to FAIL
if its clause is deleted — verified by mutation, and separately demonstrated on
prod Postgres in a rolled-back transaction (recorded in the PR).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from db.database import database
from services import pivot_query_service as svc


_PREFIX = "h1h3"


async def _connect_if_needed() -> bool:
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    return was_connected


async def _ensure_schema() -> None:
    ddl = [
        """
        CREATE TABLE IF NOT EXISTS catalog_products (
            product_key TEXT PRIMARY KEY,
            merchant_id TEXT NOT NULL,
            platform TEXT,
            source_product_id TEXT,
            catalog_track TEXT,
            truth_tier TEXT,
            readiness_tier TEXT,
            source_system TEXT,
            title TEXT,
            description TEXT,
            brand TEXT,
            product_type TEXT,
            category TEXT,
            category_path TEXT,
            canonical_url TEXT,
            image_url TEXT,
            pdp_scope TEXT,
            pdp_lifecycle_stage TEXT,
            sync_status TEXT DEFAULT 'live',
            freshness_json TEXT,
            content_key TEXT,
            suppressed_at TIMESTAMP,
            suppression_reason TEXT,
            updated_at TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS catalog_skus (
            sku_key TEXT PRIMARY KEY,
            product_key TEXT NOT NULL,
            source_variant_id TEXT,
            sku TEXT,
            barcode TEXT,
            title TEXT,
            visible_attributes TEXT,
            visible_option_labels TEXT,
            ingredient_ids TEXT,
            image_url TEXT,
            suppressed_at TIMESTAMP,
            suppression_reason TEXT,
            updated_at TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS catalog_offers (
            offer_id TEXT PRIMARY KEY,
            sku_key TEXT NOT NULL,
            product_key TEXT,
            merchant_id TEXT,
            catalog_track TEXT,
            truth_tier TEXT,
            readiness_tier TEXT,
            offer_mode TEXT,
            availability TEXT,
            inventory_quantity INTEGER,
            currency TEXT,
            list_price NUMERIC,
            merchant_effective_price NUMERIC,
            estimated_best_price NUMERIC,
            price_confidence TEXT,
            source_system TEXT,
            offer_type TEXT,
            market TEXT,
            is_first_party BOOLEAN,
            source_domain TEXT,
            why_buy_direct TEXT,
            offer_payload TEXT,
            suppressed_at TIMESTAMP,
            updated_at TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS catalog_merchants (
            merchant_id TEXT PRIMARY KEY,
            merchant_name TEXT,
            primary_platform TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            indexable BOOLEAN DEFAULT TRUE,
            metadata_json TEXT,
            updated_at TIMESTAMP
        )
        """,
    ]
    for stmt in ddl:
        await database.execute(stmt)


async def _reset() -> None:
    for table, col in (
        ("catalog_offers", "offer_id"),
        ("catalog_skus", "sku_key"),
        ("catalog_products", "product_key"),
        ("catalog_merchants", "merchant_id"),
    ):
        await database.execute(f"DELETE FROM {table} WHERE {col} LIKE :p", {"p": f"{_PREFIX}%"})


async def _merchant(merchant_id: str, *, status: str = "active", indexable: bool = True) -> None:
    await database.execute(
        """
        INSERT INTO catalog_merchants (merchant_id, merchant_name, status, indexable, metadata_json)
        VALUES (:m, :n, :s, :i, '{}')
        """,
        {"m": merchant_id, "n": merchant_id, "s": status, "i": 1 if indexable else 0},
    )


async def _product_with_offer(
    key: str,
    *,
    owner: str,
    seller: str,
    title: str = "Hydrating Serum",
    sku_suppressed_at: Optional[str] = None,
    sku_suppression_reason: Optional[str] = None,
) -> None:
    """One canonical row owned by `owner`, carrying one offer sold by `seller`.

    owner != seller is the whole H1 shape — every pre-existing gate reads the
    owner, so a gated SELLER walked straight through.
    """
    await database.execute(
        """
        INSERT INTO catalog_products
            (product_key, merchant_id, platform, source_product_id, title, brand,
             pdp_lifecycle_stage, sync_status, updated_at)
        VALUES (:k, :m, 'shopify', :spi, :t, 'TestBrand', 'published', 'live', CURRENT_TIMESTAMP)
        """,
        {"k": key, "m": owner, "spi": f"src-{key}", "t": title},
    )
    await database.execute(
        """
        INSERT INTO catalog_skus
            (sku_key, product_key, source_variant_id, sku, title,
             suppressed_at, suppression_reason, updated_at)
        VALUES (:sk, :k, :v, :sku, :t, :sa, :sr, CURRENT_TIMESTAMP)
        """,
        {
            "sk": f"{key}::sku",
            "k": key,
            "v": f"var-{key}",
            "sku": f"sku-{key}",
            "t": title,
            "sa": sku_suppressed_at,
            "sr": sku_suppression_reason,
        },
    )
    await database.execute(
        """
        INSERT INTO catalog_offers
            (offer_id, sku_key, product_key, merchant_id, availability, currency,
             list_price, suppressed_at, updated_at)
        VALUES (:o, :sk, :k, :seller, 'in_stock', 'USD', 19.99, NULL, CURRENT_TIMESTAMP)
        """,
        {"o": f"{key}::offer", "sk": f"{key}::sku", "k": key, "seller": seller},
    )


async def _recall(query: str = "Hydrating Serum", merchant_id: Optional[str] = None) -> List[str]:
    rows = await svc._fetch_canonical_search_rows(
        query=query, merchant_id=merchant_id, limit=20
    )
    return [str(r.get("product_key")) for r in rows if str(r.get("product_key", "")).startswith(_PREFIX)]


@pytest.fixture(autouse=True)
async def _db():
    was_connected = await _connect_if_needed()
    await _ensure_schema()
    await _reset()
    try:
        yield
    finally:
        await _reset()
        if not was_connected:
            await database.disconnect()


# ---------------------------------------------------------------------------
# H1 — gate the offer SELLER
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_baseline_offer_from_a_healthy_seller_serves():
    """Positive control. Every negative below is only meaningful because this
    exact shape — owner != seller — serves when the seller is healthy."""
    await _merchant(f"{_PREFIX}_owner")
    await _merchant(f"{_PREFIX}_seller")
    await _product_with_offer(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_seller")

    assert await _recall() == [f"{_PREFIX}_p1"]


@pytest.mark.asyncio
async def test_offer_from_a_deactivated_seller_is_gated():
    """THE H1 FAILURE SCENARIO. Retired merchant R's offer hangs off a sku under
    a canonical row owned by an active, indexable merchant — so every gate that
    reads the OWNER passes, and R's price surfaced in cross-merchant recall."""
    await _merchant(f"{_PREFIX}_owner")                       # active, indexable
    await _merchant(f"{_PREFIX}_seller", status="inactive")   # retired SELLER
    await _product_with_offer(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_seller")

    assert await _recall() == []


@pytest.mark.asyncio
async def test_offer_from_a_non_indexable_seller_is_gated():
    await _merchant(f"{_PREFIX}_owner")
    await _merchant(f"{_PREFIX}_seller", indexable=False)
    await _product_with_offer(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_seller")

    assert await _recall() == []


@pytest.mark.asyncio
async def test_seller_with_no_catalog_merchants_row_still_serves():
    """NULL-KEEPING COALESCE — the trap this arc has nearly paid for twice.
    741 unsuppressed offers on prod have a seller with NO catalog_merchants row
    (external seeds). A bare `bm.status = 'active'` deletes every one of them
    from search. Prior art: tests/test_pivota_canonical_routes.py:674."""
    await _merchant(f"{_PREFIX}_owner")
    # deliberately NO catalog_merchants row for the seller
    await _product_with_offer(
        f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_seedseller"
    )

    assert await _recall() == [f"{_PREFIX}_p1"]


@pytest.mark.asyncio
async def test_seller_with_observed_status_still_serves():
    """'observed' is 346 of 483 prod merchants. The gate excludes 'inactive'
    ONLY — it must not become an allow-list of 'active'."""
    await _merchant(f"{_PREFIX}_owner")
    await _merchant(f"{_PREFIX}_seller", status="observed")
    await _product_with_offer(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_seller")

    assert await _recall() == [f"{_PREFIX}_p1"]


@pytest.mark.asyncio
async def test_merchant_scoped_recall_still_sees_its_own_gated_rows():
    """Merchant-scoped queries skip every source gate by design, so the operator
    dashboard can still surface withdrawn rows for cleanup."""
    await _merchant(f"{_PREFIX}_owner")
    await _merchant(f"{_PREFIX}_seller", status="inactive")
    await _product_with_offer(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_seller")

    assert await _recall(merchant_id=f"{_PREFIX}_owner") == [f"{_PREFIX}_p1"]


@pytest.mark.asyncio
async def test_a_healthy_sellers_offer_survives_alongside_a_gated_one():
    """Both-ways in ONE query: the gate must remove the retired seller's row and
    keep the healthy one, not simply empty the result set."""
    await _merchant(f"{_PREFIX}_owner")
    await _merchant(f"{_PREFIX}_bad", status="inactive")
    await _merchant(f"{_PREFIX}_good")
    await _product_with_offer(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_bad")
    await _product_with_offer(f"{_PREFIX}_p2", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_good")

    assert await _recall() == [f"{_PREFIX}_p2"]


# ---------------------------------------------------------------------------
# H3 — read catalog_skus suppression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suppressed_sku_is_gated():
    await _merchant(f"{_PREFIX}_owner")
    await _product_with_offer(
        f"{_PREFIX}_p1",
        owner=f"{_PREFIX}_owner",
        seller=f"{_PREFIX}_owner",
        sku_suppressed_at="2026-07-31 00:00:00",
    )

    assert await _recall() == []


@pytest.mark.asyncio
async def test_reason_only_suppressed_sku_is_gated():
    """The split-column shape: the step5-generation writers set
    `suppression_reason` without `suppressed_at` (2,332 such product rows were
    backfilled on 2026-07-30). Gating both columns is what stops a future
    reason-only sku writer from re-opening the same hole."""
    await _merchant(f"{_PREFIX}_owner")
    await _product_with_offer(
        f"{_PREFIX}_p1",
        owner=f"{_PREFIX}_owner",
        seller=f"{_PREFIX}_owner",
        sku_suppression_reason="merge_duplicate_canonicals_loser",
    )

    assert await _recall() == []


@pytest.mark.asyncio
async def test_merchant_scoped_recall_still_sees_its_suppressed_skus():
    await _merchant(f"{_PREFIX}_owner")
    await _product_with_offer(
        f"{_PREFIX}_p1",
        owner=f"{_PREFIX}_owner",
        seller=f"{_PREFIX}_owner",
        sku_suppressed_at="2026-07-31 00:00:00",
    )

    assert await _recall(merchant_id=f"{_PREFIX}_owner") == [f"{_PREFIX}_p1"]
