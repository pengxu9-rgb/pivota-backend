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
    """NULLABILITY MIRRORS PRODUCTION EXACTLY — `db/migrations/058_catalog_core.sql`.

    These tables are created by whichever test module reaches them first in a
    full-suite run, so a fixture that relaxes a NOT NULL passes in isolation and
    dies the moment the real DDL wins the race. That is not hypothetical: the
    first push of this PR omitted `catalog_skus.merchant_id NOT NULL` and CI's
    `sweep` failed on every insert — the SAME defect the previous PR shipped with
    `merchant_stores.name`. Keep every NOT NULL here, and have the seed helpers
    supply all of them.
    """
    ddl = [
        """
        CREATE TABLE IF NOT EXISTS catalog_products (
            product_key VARCHAR(255) PRIMARY KEY,
            merchant_id VARCHAR(64) NOT NULL,
            platform VARCHAR(64) NOT NULL,
            source_product_id VARCHAR(128) NOT NULL,
            catalog_track VARCHAR(32) NOT NULL DEFAULT 'internal_merchant',
            truth_tier VARCHAR(32) NOT NULL DEFAULT 'primary',
            readiness_tier VARCHAR(32) NOT NULL DEFAULT 'commerce_ready',
            source_system VARCHAR(64),
            source_ref VARCHAR(255),
            title TEXT NOT NULL,
            description TEXT,
            brand VARCHAR(255),
            product_type VARCHAR(255),
            category VARCHAR(255),
            category_path TEXT,
            canonical_url TEXT,
            image_url TEXT,
            product_payload TEXT,
            freshness_json TEXT,
            pdp_scope TEXT,
            pdp_lifecycle_stage TEXT,
            sync_status TEXT DEFAULT 'live',
            content_key TEXT,
            suppressed_at TIMESTAMP,
            suppression_reason TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS catalog_skus (
            sku_key VARCHAR(255) PRIMARY KEY,
            product_key VARCHAR(255) NOT NULL,
            merchant_id VARCHAR(64) NOT NULL,
            platform VARCHAR(64) NOT NULL,
            source_product_id VARCHAR(128) NOT NULL,
            source_variant_id VARCHAR(128) NOT NULL,
            sku VARCHAR(128),
            barcode VARCHAR(128),
            title TEXT NOT NULL,
            currency VARCHAR(16),
            image_url TEXT,
            visible_attributes TEXT,
            visible_option_labels TEXT,
            ingredient_ids TEXT,
            sku_payload TEXT,
            readiness_tier VARCHAR(32) NOT NULL DEFAULT 'commerce_ready',
            suppressed_at TIMESTAMP,
            suppression_reason TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS catalog_offers (
            offer_id VARCHAR(255) PRIMARY KEY,
            sku_key VARCHAR(255) NOT NULL,
            product_key VARCHAR(255) NOT NULL,
            merchant_id VARCHAR(64) NOT NULL,
            catalog_track VARCHAR(32) NOT NULL DEFAULT 'internal_merchant',
            truth_tier VARCHAR(32) NOT NULL DEFAULT 'primary',
            readiness_tier VARCHAR(32) NOT NULL DEFAULT 'commerce_ready',
            offer_mode VARCHAR(32) NOT NULL DEFAULT 'merchant_checkout',
            channel VARCHAR(64) NOT NULL DEFAULT 'default',
            availability VARCHAR(32) NOT NULL DEFAULT 'unknown',
            inventory_quantity INTEGER,
            currency VARCHAR(16),
            list_price NUMERIC(12, 2),
            merchant_effective_price NUMERIC(12, 2),
            estimated_best_price NUMERIC(12, 2),
            price_confidence NUMERIC(5, 2),
            source_system VARCHAR(64),
            source_ref VARCHAR(255),
            offer_type TEXT,
            market TEXT,
            is_first_party BOOLEAN,
            source_domain TEXT,
            why_buy_direct TEXT,
            offer_payload TEXT,
            suppressed_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS catalog_merchants (
            merchant_id VARCHAR(64) PRIMARY KEY,
            merchant_name VARCHAR(255),
            primary_platform VARCHAR(64),
            status VARCHAR(32) NOT NULL DEFAULT 'active',
            indexable BOOLEAN NOT NULL DEFAULT TRUE,
            source_system VARCHAR(64),
            source_ref VARCHAR(255),
            metadata_json TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
    ]
    for stmt in ddl:
        await database.execute(stmt)
    # Whichever module wins the create race, this file must be able to INSERT.
    # The list is EVERY column the seed helpers write plus every column the lane
    # SELECTs — not just the ones added by later migrations. Narrower was not
    # enough: with `tests/test_index_pipeline_state_service.py` running first
    # (it precedes this file alphabetically and creates catalog_products from a
    # 13-column DDL), the seeds died on `no column named catalog_track`. CI is
    # safe today only because some earlier module happens to run
    # `metadata.create_all` and pytest-randomly is not installed — i.e. by
    # ordering luck, which is not a property worth depending on.
    for table, column, coltype in (
        ("catalog_products", "catalog_track", "VARCHAR(32) DEFAULT 'internal_merchant'"),
        ("catalog_products", "truth_tier", "VARCHAR(32) DEFAULT 'primary'"),
        ("catalog_products", "readiness_tier", "VARCHAR(32) DEFAULT 'commerce_ready'"),
        ("catalog_products", "source_system", "VARCHAR(64)"),
        ("catalog_products", "product_type", "VARCHAR(255)"),
        ("catalog_products", "category", "VARCHAR(255)"),
        ("catalog_products", "category_path", "TEXT"),
        ("catalog_products", "canonical_url", "TEXT"),
        ("catalog_products", "image_url", "TEXT"),
        ("catalog_products", "freshness_json", "TEXT"),
        ("catalog_products", "pdp_scope", "TEXT"),
        ("catalog_products", "pdp_lifecycle_stage", "TEXT"),
        ("catalog_products", "sync_status", "TEXT DEFAULT 'live'"),
        ("catalog_products", "content_key", "TEXT"),
        ("catalog_products", "suppressed_at", "TIMESTAMP"),
        ("catalog_products", "suppression_reason", "TEXT"),
        ("catalog_skus", "merchant_id", "VARCHAR(64)"),
        ("catalog_skus", "platform", "VARCHAR(64)"),
        ("catalog_skus", "source_product_id", "VARCHAR(128)"),
        ("catalog_skus", "readiness_tier", "VARCHAR(32) DEFAULT 'commerce_ready'"),
        ("catalog_skus", "visible_attributes", "TEXT"),
        ("catalog_skus", "visible_option_labels", "TEXT"),
        ("catalog_skus", "ingredient_ids", "TEXT"),
        ("catalog_skus", "image_url", "TEXT"),
        ("catalog_skus", "barcode", "VARCHAR(128)"),
        ("catalog_skus", "suppressed_at", "TIMESTAMP"),
        ("catalog_skus", "suppression_reason", "TEXT"),
        ("catalog_offers", "catalog_track", "VARCHAR(32) DEFAULT 'internal_merchant'"),
        ("catalog_offers", "truth_tier", "VARCHAR(32) DEFAULT 'primary'"),
        ("catalog_offers", "readiness_tier", "VARCHAR(32) DEFAULT 'commerce_ready'"),
        ("catalog_offers", "offer_mode", "VARCHAR(32) DEFAULT 'merchant_checkout'"),
        ("catalog_offers", "channel", "VARCHAR(64) DEFAULT 'default'"),
        ("catalog_offers", "availability", "VARCHAR(32) DEFAULT 'unknown'"),
        ("catalog_offers", "inventory_quantity", "INTEGER"),
        ("catalog_offers", "currency", "VARCHAR(16)"),
        ("catalog_offers", "list_price", "NUMERIC"),
        ("catalog_offers", "merchant_effective_price", "NUMERIC"),
        ("catalog_offers", "estimated_best_price", "NUMERIC"),
        ("catalog_offers", "price_confidence", "NUMERIC"),
        ("catalog_offers", "source_system", "VARCHAR(64)"),
        ("catalog_offers", "offer_payload", "TEXT"),
        ("catalog_offers", "suppressed_at", "TIMESTAMP"),
        ("catalog_offers", "offer_type", "TEXT"),
        ("catalog_offers", "market", "TEXT"),
        ("catalog_offers", "is_first_party", "BOOLEAN"),
        ("catalog_offers", "source_domain", "TEXT"),
        ("catalog_offers", "why_buy_direct", "TEXT"),
        ("catalog_merchants", "indexable", "BOOLEAN NOT NULL DEFAULT TRUE"),
        ("catalog_merchants", "metadata_json", "TEXT"),
        ("catalog_merchants", "primary_platform", "VARCHAR(64)"),
        ("catalog_products", "created_at", "TIMESTAMP"),
        ("catalog_products", "updated_at", "TIMESTAMP"),
        ("catalog_skus", "created_at", "TIMESTAMP"),
        ("catalog_skus", "updated_at", "TIMESTAMP"),
        ("catalog_offers", "created_at", "TIMESTAMP"),
        ("catalog_offers", "updated_at", "TIMESTAMP"),
        ("catalog_merchants", "created_at", "TIMESTAMP"),
        ("catalog_merchants", "updated_at", "TIMESTAMP"),
    ):
        try:
            await database.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        except Exception as e:  # noqa: BLE001
            # Only "already exists" is expected. Anything else is a real schema
            # problem and must not be swallowed into a confusing INSERT failure
            # ten lines later.
            if "duplicate column" not in str(e).lower():
                raise


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
        INSERT INTO catalog_merchants
            (merchant_id, merchant_name, status, indexable, metadata_json,
             created_at, updated_at)
        VALUES (:m, :n, :s, :i, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
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
    # Every NOT NULL column in the production DDL is supplied explicitly —
    # relying on a fixture-only default is what broke the first push.
    await database.execute(
        """
        INSERT INTO catalog_products
            (product_key, merchant_id, platform, source_product_id, catalog_track,
             truth_tier, readiness_tier, title, brand, pdp_lifecycle_stage,
             sync_status, created_at, updated_at)
        VALUES (:k, :m, 'shopify', :spi, 'internal_merchant', 'primary',
                'commerce_ready', :t, 'TestBrand', 'published', 'live',
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        {"k": key, "m": owner, "spi": f"src-{key}", "t": title},
    )
    await database.execute(
        """
        INSERT INTO catalog_skus
            (sku_key, product_key, merchant_id, platform, source_product_id,
             source_variant_id, sku, title, readiness_tier,
             suppressed_at, suppression_reason, created_at, updated_at)
        VALUES (:sk, :k, :m, 'shopify', :spi, :v, :sku, :t, 'commerce_ready',
                :sa, :sr, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        {
            "sk": f"{key}::sku",
            "k": key,
            "m": owner,
            "spi": f"src-{key}",
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
            (offer_id, sku_key, product_key, merchant_id, catalog_track, truth_tier,
             readiness_tier, offer_mode, channel, availability, currency, list_price,
             suppressed_at, created_at, updated_at)
        VALUES (:o, :sk, :k, :seller, 'internal_merchant', 'primary', 'commerce_ready',
                'merchant_checkout', 'default', 'in_stock', 'USD', 19.99, NULL,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        {"o": f"{key}::offer", "sk": f"{key}::sku", "k": key, "seller": seller},
    )


async def _recall(query: str = "Hydrating Serum", merchant_id: Optional[str] = None) -> List[str]:
    rows = await svc._fetch_canonical_search_rows(
        query=query, merchant_id=merchant_id, limit=20
    )
    return [str(r.get("product_key")) for r in rows if str(r.get("product_key", "")).startswith(_PREFIX)]


async def _recall_offers(
    query: str = "Hydrating Serum", merchant_id: Optional[str] = None
) -> List[str]:
    """Offer-grain view of the same call.

    `_recall` returns product_keys only, which makes it structurally incapable
    of catching a regression that drops the SURVIVING offer while keeping the
    product — the row grain this lane actually emits is (product, offer).
    """
    rows = await svc._fetch_canonical_search_rows(
        query=query, merchant_id=merchant_id, limit=20
    )
    return sorted(
        str(r.get("offer_id")) for r in rows if str(r.get("offer_id", "")).startswith(_PREFIX)
    )


async def _extra_offer(key: str, *, seller: str, suffix: str) -> None:
    """A SECOND offer on the SAME sku, from a different seller — the shape H1 is
    really about, and the one the product-grain tests cannot distinguish."""
    await database.execute(
        """
        INSERT INTO catalog_offers
            (offer_id, sku_key, product_key, merchant_id, catalog_track, truth_tier,
             readiness_tier, offer_mode, channel, availability, currency, list_price,
             suppressed_at, created_at, updated_at)
        VALUES (:o, :sk, :k, :seller, 'internal_merchant', 'primary', 'commerce_ready',
                'merchant_checkout', 'default', 'in_stock', 'USD', 24.99, NULL,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        {"o": f"{key}::offer::{suffix}", "sk": f"{key}::sku", "k": key, "seller": seller},
    )


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


@pytest.mark.parametrize(
    "status,expected_serving",
    [
        ("inactive", False),
        ("INACTIVE", False),   # the gate lowercases, as every status read does
        ("Inactive", False),
        ("active", True),
        ("observed", True),
        ("", True),            # empty string is not 'inactive' — do not guess
        (" inactive ", True),  # NOT gated: no btrim, matching the pre-existing
                               # m-clause. Pinned as KNOWN, not endorsed — if a
                               # writer ever pads the column this is a hole, and
                               # the fix belongs on both clauses at once.
    ],
)
@pytest.mark.asyncio
async def test_seller_status_matching_is_case_insensitive_and_inactive_only(
    status: str, expected_serving: bool
):
    await _merchant(f"{_PREFIX}_owner")
    await _merchant(f"{_PREFIX}_seller", status=status)
    await _product_with_offer(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_seller")

    served = await _recall() == [f"{_PREFIX}_p1"]
    assert served is expected_serving


# NOTE — there is deliberately NO test for "row exists, indexable IS NULL".
# `db/catalog.py:28` declares `indexable` NOT NULL (server_default true) and prod
# agrees, so that state is unreachable: the COALESCE's only reachable branch is
# the JOIN MISS, which `test_seller_with_no_catalog_merchants_row_still_serves`
# covers. A test for it existed briefly and CI killed it — it could only be
# written by making this fixture laxer than production, which is precisely the
# defect this file has now paid for twice.


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


@pytest.mark.asyncio
async def test_one_sku_two_sellers_only_the_gated_offer_is_dropped():
    """THE shape H1 is really about: one sku, two sellers, one retired.

    The product must survive carrying ONLY the healthy seller's offer. Getting
    this wrong in either direction is a real outage — dropping the product
    removes a live listing, keeping both republishes the retired seller's price.
    Asserted at OFFER grain because the product-grain helper cannot see the
    difference."""
    await _merchant(f"{_PREFIX}_owner")
    await _merchant(f"{_PREFIX}_bad", status="inactive")
    await _merchant(f"{_PREFIX}_good")
    await _product_with_offer(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_good")
    await _extra_offer(f"{_PREFIX}_p1", seller=f"{_PREFIX}_bad", suffix="bad")

    assert await _recall() == [f"{_PREFIX}_p1"], "the product must not vanish"
    assert await _recall_offers() == [f"{_PREFIX}_p1::offer"], "only the healthy offer survives"


@pytest.mark.asyncio
async def test_one_sku_two_healthy_sellers_both_offers_survive():
    """The control for the test above — the gate must not collapse a
    multi-seller sku down to one row when nobody is gated."""
    await _merchant(f"{_PREFIX}_owner")
    await _merchant(f"{_PREFIX}_a")
    await _merchant(f"{_PREFIX}_b")
    await _product_with_offer(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_a")
    await _extra_offer(f"{_PREFIX}_p1", seller=f"{_PREFIX}_b", suffix="b")

    assert await _recall_offers() == [f"{_PREFIX}_p1::offer", f"{_PREFIX}_p1::offer::b"]


@pytest.mark.asyncio
async def test_one_sku_all_sellers_gated_removes_the_product():
    await _merchant(f"{_PREFIX}_owner")
    await _merchant(f"{_PREFIX}_bad1", status="inactive")
    await _merchant(f"{_PREFIX}_bad2", indexable=False)
    await _product_with_offer(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_bad1")
    await _extra_offer(f"{_PREFIX}_p1", seller=f"{_PREFIX}_bad2", suffix="bad2")

    assert await _recall() == []
    assert await _recall_offers() == []


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
