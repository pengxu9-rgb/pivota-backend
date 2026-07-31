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
    assert await _by_sku(f"{_PREFIX}_p1::sku") == [f"{_PREFIX}_p1::offer"]


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
    assert await _by_sku(f"{_PREFIX}_p1::sku") == []


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
    assert await _by_sku(f"{_PREFIX}_p1::sku") == []


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
    assert await _by_sku(f"{_PREFIX}_p1::sku") == []


@pytest.mark.asyncio
async def test_case_insensitive_inactive_matching():
    await _merchant(f"{_PREFIX}_owner", status="INACTIVE")
    await _merchant(f"{_PREFIX}_seller")
    await _seed(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_seller")

    assert await _by_product(f"{_PREFIX}_p1") == []
    assert await _by_sku(f"{_PREFIX}_p1::sku") == []


@pytest.mark.asyncio
async def test_the_sku_door_is_a_subset_of_the_product_door():
    """The doors drifted apart once already — recall gained three gates in #1650
    while these kept none.

    The invariant is CONTAINMENT, not equality: the product door returns every
    offer under every sku of the key, the sku door only one sku's. An earlier
    version asserted `==`, which held only because every fixture was
    single-sku — it would have started failing the moment someone added a
    second sku, and it proved nothing about the gates in the meantime.
    """
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
        product_offers = set(await _by_product(key))
        sku_offers = set(await _by_sku(f"{key}::sku"))
        assert sku_offers <= product_offers, key
        # ...and on a single-sku key they must be equal, so containment cannot
        # be satisfied by the sku door simply returning nothing.
        assert sku_offers == product_offers, key


@pytest.mark.parametrize(
    "status,indexable,serves",
    [
        ("active", True, True),
        ("observed", True, True),      # 346 of 483 prod merchants
        ("OBSERVED", True, True),      # case-folding must not darken them
        ("inactive", True, False),
        ("INACTIVE", True, False),     # case-folded on BOTH doors
        ("Inactive", True, False),
        ("active", False, False),      # indexable leg, on BOTH doors
    ],
)
@pytest.mark.asyncio
async def test_seller_status_and_indexable_on_both_doors(status, indexable, serves):
    """Every seller-leg variant, asserted on BOTH doors.

    Five sku-lane mutants survived the first sweep because 8 of 15 tests
    asserted `_by_product` only — including the `observed` control, so an
    `= 'active'` allow-list on the sku lane (which would darken 346 prod
    merchants) went undetected. Seller-status case-folding was pinned on
    neither door.
    """
    await _merchant(f"{_PREFIX}_owner")
    await _merchant(f"{_PREFIX}_seller", status=status, indexable=indexable)
    await _seed(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_seller")

    expected = [f"{_PREFIX}_p1::offer"] if serves else []
    assert await _by_product(f"{_PREFIX}_p1") == expected
    assert await _by_sku(f"{_PREFIX}_p1::sku") == expected


@pytest.mark.parametrize(
    "status,indexable,serves",
    [
        ("observed", True, True),
        ("OBSERVED", True, True),
        ("INACTIVE", True, False),
        ("active", False, False),
    ],
)
@pytest.mark.asyncio
async def test_owner_status_and_indexable_on_both_doors(status, indexable, serves):
    """Owner leg, mirror of the above. Seller is healthy and DISTINCT so the
    seller leg cannot cover for the owner leg — the flaw that let two mutants
    survive the first sweep."""
    await _merchant(f"{_PREFIX}_owner", status=status, indexable=indexable)
    await _merchant(f"{_PREFIX}_seller")
    await _seed(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_seller")

    expected = [f"{_PREFIX}_p1::offer"] if serves else []
    assert await _by_product(f"{_PREFIX}_p1") == expected
    assert await _by_sku(f"{_PREFIX}_p1::sku") == expected


@pytest.mark.asyncio
async def test_a_partially_gated_key_keeps_its_survivors():
    """Per-row gating, not per-key. A product with two skus where ONE is
    suppressed must still resolve via the healthy sku — a regression that turned
    a partial gate into a whole-key gate would silently delete live listings,
    and prod has 0 partially-gated keys today so production would not tell us.
    """
    await _merchant(f"{_PREFIX}_owner")
    await _seed(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_owner")
    # second sku on the same product, suppressed
    await database.execute(
        """
        INSERT INTO catalog_skus
            (sku_key, product_key, merchant_id, platform, source_product_id,
             source_variant_id, sku, title, readiness_tier, suppressed_at,
             created_at, updated_at)
        VALUES (:sk, :k, :m, 'shopify', :spi, 'var2', 'sku2', 'Hydrating Serum',
                'commerce_ready', '2026-07-31 00:00:00', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        {"sk": f"{_PREFIX}_p1::sku2", "k": f"{_PREFIX}_p1", "m": f"{_PREFIX}_owner",
         "spi": f"src-{_PREFIX}_p1"},
    )
    await database.execute(
        """
        INSERT INTO catalog_offers
            (offer_id, sku_key, product_key, merchant_id, catalog_track, truth_tier,
             readiness_tier, offer_mode, channel, availability, currency, list_price,
             suppressed_at, created_at, updated_at)
        VALUES (:o, :sk, :k, :m, 'internal_merchant', 'primary', 'commerce_ready',
                'merchant_checkout', 'default', 'in_stock', 'USD', 29.99, NULL,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        {"o": f"{_PREFIX}_p1::offer2", "sk": f"{_PREFIX}_p1::sku2", "k": f"{_PREFIX}_p1",
         "m": f"{_PREFIX}_owner"},
    )

    assert await _by_product(f"{_PREFIX}_p1") == [f"{_PREFIX}_p1::offer"]
    assert await _by_sku(f"{_PREFIX}_p1::sku") == [f"{_PREFIX}_p1::offer"]
    assert await _by_sku(f"{_PREFIX}_p1::sku2") == []


@pytest.mark.asyncio
async def test_a_sku_with_one_gated_seller_keeps_the_other_offer():
    await _merchant(f"{_PREFIX}_owner")
    await _merchant(f"{_PREFIX}_dead", status="inactive")
    await _seed(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_owner")
    await database.execute(
        """
        INSERT INTO catalog_offers
            (offer_id, sku_key, product_key, merchant_id, catalog_track, truth_tier,
             readiness_tier, offer_mode, channel, availability, currency, list_price,
             suppressed_at, created_at, updated_at)
        VALUES (:o, :sk, :k, :m, 'internal_merchant', 'primary', 'commerce_ready',
                'merchant_checkout', 'default', 'in_stock', 'USD', 29.99, NULL,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        {"o": f"{_PREFIX}_p1::offer_dead", "sk": f"{_PREFIX}_p1::sku",
         "k": f"{_PREFIX}_p1", "m": f"{_PREFIX}_dead"},
    )

    assert await _by_product(f"{_PREFIX}_p1") == [f"{_PREFIX}_p1::offer"]
    assert await _by_sku(f"{_PREFIX}_p1::sku") == [f"{_PREFIX}_p1::offer"]


# ---------------------------------------------------------------------------
# The QUOTE door — `POST /v1/pivot/quote`, the transactional one.
#
# The first version of these tests was vacuous: 39 of 48 mutants survived, and
# deleting the ENTIRE offer-seller leg from all three fetchers left every test
# green. Cause: one test fired THREE legs at once (inactive owner AND product
# suppression), so no single-leg deletion could change the outcome — the fourth
# instance in this arc of "the fixture makes several gates fire, so none of them
# is attributable". Each case below fires EXACTLY ONE leg, on ALL THREE
# fetchers.
# ---------------------------------------------------------------------------


_QUOTE_FETCHERS = ("_fetch_offer_row", "_fetch_default_offer_for_sku",
                   "_fetch_default_offer_for_product")


async def _quote_lookups(key: str) -> Dict[str, Any]:
    """All three quote fetchers for one seeded key, by name."""
    return {
        "_fetch_offer_row": await svc._fetch_offer_row(f"{key}::offer"),
        "_fetch_default_offer_for_sku": await svc._fetch_default_offer_for_sku(f"{key}::sku"),
        "_fetch_default_offer_for_product": await svc._fetch_default_offer_for_product(key),
    }


@pytest.mark.parametrize(
    "leg",
    ["owner_status", "owner_indexable", "seller_status", "seller_indexable",
     "product_suppressed_at", "product_suppression_reason",
     "sku_suppressed_at", "sku_suppression_reason"],
)
@pytest.mark.asyncio
async def test_each_gate_leg_alone_closes_all_three_quote_fetchers(leg: str):
    """One leg at a time. Deleting any single leg from any single fetcher must
    fail here — that is the property the previous version did not have."""
    owner, seller = f"{_PREFIX}_owner", f"{_PREFIX}_seller"
    await _merchant(owner, status="inactive" if leg == "owner_status" else "active",
                    indexable=leg != "owner_indexable")
    await _merchant(seller, status="inactive" if leg == "seller_status" else "active",
                    indexable=leg != "seller_indexable")
    await _seed(
        f"{_PREFIX}_p1",
        owner=owner,
        seller=seller,
        product_suppressed_at="2026-07-31 00:00:00" if leg == "product_suppressed_at" else None,
        product_suppression_reason="d2_tier3_judge" if leg == "product_suppression_reason" else None,
        sku_suppressed_at="2026-07-31 00:00:00" if leg == "sku_suppressed_at" else None,
        sku_suppression_reason="merge_loser" if leg == "sku_suppression_reason" else None,
    )

    results = await _quote_lookups(f"{_PREFIX}_p1")
    for name in _QUOTE_FETCHERS:
        assert results[name] is None, f"{name} still quotes with leg={leg} firing"


@pytest.mark.asyncio
async def test_all_three_quote_fetchers_serve_a_healthy_key():
    """The positive control the one-leg cases are measured against."""
    await _merchant(f"{_PREFIX}_owner")
    await _seed(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_owner")

    results = await _quote_lookups(f"{_PREFIX}_p1")
    for name in _QUOTE_FETCHERS:
        assert results[name] is not None, f"{name} refused a healthy key"


@pytest.mark.parametrize("status", ["observed", "OBSERVED", "active"])
@pytest.mark.asyncio
async def test_quote_fetchers_keep_non_inactive_sellers(status: str):
    """`observed` is 346 of 483 prod merchants; an `= 'active'` allow-list on
    the SELL path would refuse to quote most of the corpus."""
    await _merchant(f"{_PREFIX}_owner", status=status)
    await _merchant(f"{_PREFIX}_seller", status=status)
    await _seed(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_seller")

    results = await _quote_lookups(f"{_PREFIX}_p1")
    for name in _QUOTE_FETCHERS:
        assert results[name] is not None, f"{name} refused a {status} merchant"


@pytest.mark.asyncio
async def test_all_three_quote_fetchers_keep_seedless_sellers():
    """NULL-keeping COALESCE matters most here: a bare `= 'active'` on either
    merchant alias would refuse to quote every external_seed offer. The earlier
    version of this test omitted _fetch_default_offer_for_sku, which is why all
    four of its COALESCE mutants survived."""
    await _seed(f"{_PREFIX}_p1", owner=f"{_PREFIX}_noseed", seller=f"{_PREFIX}_noseller")

    results = await _quote_lookups(f"{_PREFIX}_p1")
    for name in _QUOTE_FETCHERS:
        assert results[name] is not None, f"{name} dropped a seedless seller"


@pytest.mark.asyncio
async def test_quote_falls_through_to_a_surviving_offer_not_to_none():
    """`LIMIT 1` picks AFTER the predicate. A sku whose newest offer is from a
    gated seller must quote the older surviving offer, not refuse — and the
    caller silently gets a different seller at a different price, which is worth
    pinning so a future change cannot turn it into a refusal unnoticed."""
    await _merchant(f"{_PREFIX}_owner")
    await _merchant(f"{_PREFIX}_dead", status="inactive")
    await _seed(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_owner")
    await database.execute(
        """
        INSERT INTO catalog_offers
            (offer_id, sku_key, product_key, merchant_id, catalog_track, truth_tier,
             readiness_tier, offer_mode, channel, availability, currency, list_price,
             suppressed_at, created_at, updated_at)
        VALUES (:o, :sk, :k, :m, 'internal_merchant', 'primary', 'commerce_ready',
                'merchant_checkout', 'default', 'in_stock', 'USD', 99.99, NULL,
                CURRENT_TIMESTAMP, '2099-01-01 00:00:00')
        """,
        {"o": f"{_PREFIX}_p1::offer_dead", "sk": f"{_PREFIX}_p1::sku",
         "k": f"{_PREFIX}_p1", "m": f"{_PREFIX}_dead"},
    )

    row = await svc._fetch_default_offer_for_sku(f"{_PREFIX}_p1::sku")
    assert row is not None and row["offer_id"] == f"{_PREFIX}_p1::offer"


# ---------------------------------------------------------------------------
# The raw-source-id branch of preview_pivot_quote — a full bypass of everything
# above until this PR: it fabricates an item from caller-supplied ids with no
# DB lookup at all.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raw_source_ids_are_refused_when_the_catalog_row_is_withdrawn():
    await _merchant(f"{_PREFIX}_owner")
    await _seed(
        f"{_PREFIX}_p1",
        owner=f"{_PREFIX}_owner",
        seller=f"{_PREFIX}_owner",
        product_suppression_reason="step5_campaign_clone_dup",
    )

    assert await svc._source_ids_are_withdrawn(
        f"{_PREFIX}_owner", f"src-{_PREFIX}_p1", f"var-{_PREFIX}_p1"
    ) is True


@pytest.mark.asyncio
async def test_raw_source_ids_are_allowed_when_the_catalog_row_is_healthy():
    await _merchant(f"{_PREFIX}_owner")
    await _seed(f"{_PREFIX}_p1", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_owner")

    assert await svc._source_ids_are_withdrawn(
        f"{_PREFIX}_owner", f"src-{_PREFIX}_p1", f"var-{_PREFIX}_p1"
    ) is False


@pytest.mark.asyncio
async def test_raw_source_ids_for_an_UNINDEXED_item_are_allowed():
    """The over-filtering case, and the reason the rule is 'all known rows are
    gated' rather than 'a clean row exists'. This branch exists so a caller can
    quote a variant the index has never ingested; requiring a catalog row would
    break exactly what it is for."""
    assert await svc._source_ids_are_withdrawn(
        f"{_PREFIX}_owner", "never-ingested-product", "never-ingested-variant"
    ) is False


@pytest.mark.asyncio
async def test_raw_source_ids_with_a_clean_sibling_row_are_allowed():
    """One withdrawn row and one healthy row for the same source ids: the ids
    still resolve to something we serve, so the quote proceeds.

    The sibling must sit on a DIFFERENT platform — `catalog_products` has a
    unique index on (merchant_id, platform, source_product_id), so two rows
    sharing source ids under one merchant is only reachable across platforms.
    A first version of this test ignored that and hit the constraint; the
    lookup is platform-agnostic by design, so the cross-platform shape is the
    real one anyway."""
    await _merchant(f"{_PREFIX}_owner")
    await _seed(
        f"{_PREFIX}_dead",
        owner=f"{_PREFIX}_owner",
        seller=f"{_PREFIX}_owner",
        product_suppression_reason="d2_tier3_judge",
    )
    await database.execute(
        """
        UPDATE catalog_products SET source_product_id = :spi, platform = 'wix'
        WHERE product_key = :k
        """,
        {"spi": "shared-src", "k": f"{_PREFIX}_dead"},
    )
    await database.execute(
        "UPDATE catalog_skus SET source_variant_id = :v WHERE product_key = :k",
        {"v": "shared-var", "k": f"{_PREFIX}_dead"},
    )
    await _seed(f"{_PREFIX}_live", owner=f"{_PREFIX}_owner", seller=f"{_PREFIX}_owner")
    await database.execute(
        "UPDATE catalog_products SET source_product_id = :spi WHERE product_key = :k",
        {"spi": "shared-src", "k": f"{_PREFIX}_live"},
    )
    await database.execute(
        "UPDATE catalog_skus SET source_variant_id = :v WHERE product_key = :k",
        {"v": "shared-var", "k": f"{_PREFIX}_live"},
    )

    assert await svc._source_ids_are_withdrawn(
        f"{_PREFIX}_owner", "shared-src", "shared-var"
    ) is False
