"""H2 (#1648) — the by-key doors must carry the same source gates as recall.

`_fetch_canonical_rows_for_product` / `_for_sku` back `GET /v1/pivot/products/{key}`,
`GET /v1/pivot/skus/{key}` and `POST /v1/pivot/offers/resolve`. They carried NO
source gate at all — only `o.suppressed_at IS NULL`. Search stopped emitting
withdrawn keys after #1650/#1655, but any caller holding a key could still
resolve one here.

WHAT THIS IS NOT. The handoff for this item warned that these lanes "feed PDP
rendering: changing them can flip pages to 404". That is wrong, and it was worth
checking before writing a line: the public PDP renders through `get_pdp_v2` (the
gateway op agent-ui calls), gated on `index_pipeline_state.serving_eligible`.
These two functions are reached ONLY from `routes/pivot_routes.py`, every route
of which is `Depends(get_current_user)`. Verified empirically as well —
suppressed sigs' PDP status and MCP `get_product` resolution correlate perfectly
(404<->gated, 200<->resolves), i.e. both follow the IPS gate, not this lane.

Unlike H1/H3, this gate fires on a REAL cohort: 2,045 of the 14,749 rows these
lanes return on prod (1,534 product_keys, 2,040 sku_keys), every one an
intentional withdrawal — step5 dedupe, wrong-brand namesake, retired pilots,
test variants. So the risk here is the mirror image of H1/H3: not "does the gate
do anything" but "does it take anything it shouldn't". Hence a positive control
per gate leg, and the seedless-seller case pinned hardest.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from db.database import database
from services import pivot_query_service as svc


_PREFIX = "h2door"


async def _connect_if_needed() -> bool:
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    return was_connected


async def _ensure_schema() -> None:
    """Built from the model, so the fixture can be neither laxer nor richer than
    production. Hand-written DDL hid three real defects across #1653/#1655
    (`merchant_stores.name`, `catalog_skus.merchant_id`, and a test asserting a
    NULL `indexable` the schema forbids) — every one of them a fixture written
    from what the test wanted instead of from the schema the code runs against.
    """
    from sqlalchemy import create_engine

    from db.catalog import catalog_merchants, catalog_offers, catalog_products, catalog_skus
    from db.database import DATABASE_URL, metadata

    sync_url = DATABASE_URL.replace("sqlite+aiosqlite://", "sqlite://").replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    engine = create_engine(sync_url)
    try:
        metadata.create_all(
            engine,
            tables=[catalog_merchants, catalog_products, catalog_skus, catalog_offers],
            checkfirst=True,
        )
    finally:
        engine.dispose()


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
            (merchant_id, merchant_name, status, indexable, metadata_json, created_at, updated_at)
        VALUES (:m, :m, :s, :i, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        {"m": merchant_id, "s": status, "i": 1 if indexable else 0},
    )


async def _seed(
    key: str,
    *,
    owner: str,
    seller: str,
    product_suppressed_at: Optional[str] = None,
    product_suppression_reason: Optional[str] = None,
    sku_suppressed_at: Optional[str] = None,
    sku_suppression_reason: Optional[str] = None,
) -> None:
    await database.execute(
        """
        INSERT INTO catalog_products
            (product_key, merchant_id, platform, source_product_id, catalog_track,
             truth_tier, readiness_tier, title, brand, suppressed_at, suppression_reason,
             created_at, updated_at)
        VALUES (:k, :m, 'shopify', :spi, 'internal_merchant', 'primary', 'commerce_ready',
                'Hydrating Serum', 'TestBrand', :psa, :psr,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        {
            "k": key, "m": owner, "spi": f"src-{key}",
            "psa": product_suppressed_at, "psr": product_suppression_reason,
        },
    )
    await database.execute(
        """
        INSERT INTO catalog_skus
            (sku_key, product_key, merchant_id, platform, source_product_id,
             source_variant_id, sku, title, readiness_tier, suppressed_at,
             suppression_reason, created_at, updated_at)
        VALUES (:sk, :k, :m, 'shopify', :spi, :v, :sku, 'Hydrating Serum',
                'commerce_ready', :ssa, :ssr, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        {
            "sk": f"{key}::sku", "k": key, "m": owner, "spi": f"src-{key}",
            "v": f"var-{key}", "sku": f"sku-{key}",
            "ssa": sku_suppressed_at, "ssr": sku_suppression_reason,
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


async def _by_product(key: str) -> List[str]:
    rows = await svc._fetch_canonical_rows_for_product(key)
    return [str(r.get("offer_id")) for r in rows]


async def _by_sku(key: str) -> List[str]:
    rows = await svc._fetch_canonical_rows_for_sku(key)
    return [str(r.get("offer_id")) for r in rows]


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
# The positive controls come FIRST — this gate removes 2,045 real prod rows,
# so "took something it shouldn't" is the failure mode that matters here.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthy_product_key_still_resolves():
    await _merchant(f"{_PREFIX}_owner")
    await _seed(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_owner")

    assert await _by_product(f"{_PREFIX}_p1") == [f"{_PREFIX}_p1::offer"]


@pytest.mark.asyncio
async def test_healthy_sku_key_still_resolves():
    await _merchant(f"{_PREFIX}_owner")
    await _seed(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_owner")

    assert await _by_sku(f"{_PREFIX}_p1::sku") == [f"{_PREFIX}_p1::offer"]


@pytest.mark.asyncio
async def test_seedless_seller_and_owner_still_resolve():
    """NULL-keeping COALESCE on BOTH merchant aliases. external_seed rows have no
    catalog_merchants row; a bare `= 'active'` on either alias deletes them.
    Prod: 741 unsuppressed offers have a seller with no merchant row."""
    await _seed(f"{_PREFIX}_p1", owner=f"{_PREFIX}_noseed", seller=f"{_PREFIX}_noseller")

    assert await _by_product(f"{_PREFIX}_p1") == [f"{_PREFIX}_p1::offer"]
    assert await _by_sku(f"{_PREFIX}_p1::sku") == [f"{_PREFIX}_p1::offer"]


@pytest.mark.asyncio
async def test_observed_owner_and_seller_still_resolve():
    """'observed' is 346 of 483 prod merchants — the gate excludes 'inactive'
    only, and must never become an `= 'active'` allow-list."""
    await _merchant(f"{_PREFIX}_owner", status="observed")
    await _merchant(f"{_PREFIX}_seller", status="observed")
    await _seed(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_seller")

    assert await _by_product(f"{_PREFIX}_p1") == [f"{_PREFIX}_p1::offer"]


# ---------------------------------------------------------------------------
# Each gate leg, both doors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suppressed_product_row_is_gated_on_both_doors():
    """The live leak: 1,534 withdrawn product_keys were resolvable by key."""
    await _merchant(f"{_PREFIX}_owner")
    await _seed(
        f"{_PREFIX}_p1",
        owner=f"{_PREFIX}_owner",
        seller=f"{_PREFIX}_owner",
        product_suppressed_at="2026-07-31 00:00:00",
    )

    assert await _by_product(f"{_PREFIX}_p1") == []
    assert await _by_sku(f"{_PREFIX}_p1::sku") == []


@pytest.mark.asyncio
async def test_reason_only_suppressed_product_is_gated():
    """The step5 generation wrote `suppression_reason` without `suppressed_at`
    (2,332 rows backfilled 2026-07-30). Gating both columns is what stops a
    future reason-only writer re-opening this door."""
    await _merchant(f"{_PREFIX}_owner")
    await _seed(
        f"{_PREFIX}_p1",
        owner=f"{_PREFIX}_owner",
        seller=f"{_PREFIX}_owner",
        product_suppression_reason="step5_same_merchant_same_url_dup",
    )

    assert await _by_product(f"{_PREFIX}_p1") == []
    assert await _by_sku(f"{_PREFIX}_p1::sku") == []


@pytest.mark.asyncio
async def test_suppressed_sku_is_gated_on_both_doors():
    await _merchant(f"{_PREFIX}_owner")
    await _seed(
        f"{_PREFIX}_p1",
        owner=f"{_PREFIX}_owner",
        seller=f"{_PREFIX}_owner",
        sku_suppressed_at="2026-07-31 00:00:00",
    )

    assert await _by_product(f"{_PREFIX}_p1") == []
    assert await _by_sku(f"{_PREFIX}_p1::sku") == []


@pytest.mark.asyncio
async def test_reason_only_suppressed_sku_is_gated():
    await _merchant(f"{_PREFIX}_owner")
    await _seed(
        f"{_PREFIX}_p1",
        owner=f"{_PREFIX}_owner",
        seller=f"{_PREFIX}_owner",
        sku_suppression_reason="merge_duplicate_canonicals_loser",
    )

    assert await _by_product(f"{_PREFIX}_p1") == []


@pytest.mark.asyncio
async def test_deactivated_owner_is_gated_on_both_doors():
    """The SELLER here is deliberately healthy and DISTINCT from the owner.

    With owner == seller, dropping the owner leg entirely still leaves the
    seller leg gating the row, so the test passes against a mutant that removed
    the very clause it claims to defend — confirmed by mutation, twice.
    """
    await _merchant(f"{_PREFIX}_owner", status="inactive")
    await _merchant(f"{_PREFIX}_seller")
    await _seed(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_seller")

    assert await _by_product(f"{_PREFIX}_p1") == []
    assert await _by_sku(f"{_PREFIX}_p1::sku") == []


@pytest.mark.asyncio
async def test_non_indexable_owner_is_gated():
    # Healthy, distinct seller — see test_deactivated_owner_is_gated_on_both_doors.
    await _merchant(f"{_PREFIX}_owner", indexable=False)
    await _merchant(f"{_PREFIX}_seller")
    await _seed(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_seller")

    assert await _by_product(f"{_PREFIX}_p1") == []


@pytest.mark.asyncio
async def test_deactivated_offer_seller_is_gated():
    """H1's failure scenario, on the by-key door: a retired SELLER's offer
    hanging off a canonical row owned by an active, indexable merchant."""
    await _merchant(f"{_PREFIX}_owner")
    await _merchant(f"{_PREFIX}_seller", status="inactive")
    await _seed(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_seller")

    assert await _by_product(f"{_PREFIX}_p1") == []
    assert await _by_sku(f"{_PREFIX}_p1::sku") == []


@pytest.mark.asyncio
async def test_non_indexable_offer_seller_is_gated():
    await _merchant(f"{_PREFIX}_owner")
    await _merchant(f"{_PREFIX}_seller", indexable=False)
    await _seed(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_seller")

    assert await _by_product(f"{_PREFIX}_p1") == []


@pytest.mark.asyncio
async def test_case_insensitive_inactive_matching():
    await _merchant(f"{_PREFIX}_owner", status="INACTIVE")
    await _merchant(f"{_PREFIX}_seller")
    await _seed(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_seller")

    assert await _by_product(f"{_PREFIX}_p1") == []


@pytest.mark.asyncio
async def test_the_two_doors_agree_with_each_other():
    """The doors drifted apart once already — recall gained three gates in #1650
    while these kept none. Any row visible through one must be visible through
    the other, or the next divergence starts here."""
    await _merchant(f"{_PREFIX}_owner")
    await _merchant(f"{_PREFIX}_dead", status="inactive")
    await _seed(f"{_PREFIX}_ok", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_owner")
    await _seed(f"{_PREFIX}_bad", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_dead")
    await _seed(
        f"{_PREFIX}_supp",
        owner=f"{_PREFIX}_owner",
        seller=f"{_PREFIX}_owner",
        product_suppression_reason="d2_tier3_judge",
    )

    for key in (f"{_PREFIX}_ok", f"{_PREFIX}_bad", f"{_PREFIX}_supp"):
        assert await _by_product(key) == await _by_sku(f"{key}::sku"), key


@pytest.mark.asyncio
async def test_owner_and_seller_legs_are_independently_load_bearing():
    """Four combinations, so neither merchant leg can be deleted without a
    failure. owner==seller fixtures cannot prove this: they let one leg cover
    for the other, which is how two mutants survived the first sweep."""
    await _merchant(f"{_PREFIX}_good_owner")
    await _merchant(f"{_PREFIX}_good_seller")
    await _merchant(f"{_PREFIX}_dead_owner", status="inactive")
    await _merchant(f"{_PREFIX}_dead_seller", status="inactive")

    await _seed(f"{_PREFIX}_gg", owner=f"{_PREFIX}_good_owner", seller=f"{_PREFIX}_good_seller")
    await _seed(f"{_PREFIX}_dg", owner=f"{_PREFIX}_dead_owner", seller=f"{_PREFIX}_good_seller")
    await _seed(f"{_PREFIX}_gd", owner=f"{_PREFIX}_good_owner", seller=f"{_PREFIX}_dead_seller")
    await _seed(f"{_PREFIX}_dd", owner=f"{_PREFIX}_dead_owner", seller=f"{_PREFIX}_dead_seller")

    assert await _by_product(f"{_PREFIX}_gg") == [f"{_PREFIX}_gg::offer"]
    assert await _by_product(f"{_PREFIX}_dg") == [], "owner leg must gate on its own"
    assert await _by_product(f"{_PREFIX}_gd") == [], "seller leg must gate on its own"
    assert await _by_product(f"{_PREFIX}_dd") == []
