"""A category_path that STOPS SHORT of the query's category must still be recallable,
and a trademark glyph must not eat the word it is glued to.

WHY. Prod, 2026-09-07: "Stila Stay All Day Liquid Lipstick" returned Pixi/Fenty/Kylie and
ZERO Stila, while stilacosmetics.com held 123 live, priced, serving-eligible rows
(index_pipeline_state: 123 serving_eligible, blocker_code 'none', avg quality 83.3). It was
never a ranking loss — nothing Stila was ever a candidate. Three doors, all shut:

  * the whole-phrase door wants the literal "stila stay all day liquid lipstick" inside ONE
    column, and the titles are "Stay All Day(R) Liquid Lipstick" with no brand prefix;
  * the category door wants 'beauty/makeup/lip/%', and all 124 Stila rows sit at exactly
    'beauty/makeup' — as do Tarte's 231 and Flower Beauty's 49, the rest of the same curated
    lane. The `missing_taxonomy` escape does not reach them: it fires only when the path IS
    NULL, so a coarse-but-correct ancestor was strictly WORSE than no taxonomy at all;
  * the brand-admit door reads p.brand / m.merchant_name only and ANDs every residual term,
    so it wanted a brand literally named "Stila Stay All Day".

These tests EXECUTE `_fetch_canonical_search_rows` against a real DB. A mock cannot tell you
a WHERE clause is absent — the same reason tests/test_recall_offer_seller_and_sku_gates.py,
whose fixtures this file follows, is written that way. Each test is written to FAIL if its
clause is deleted.
"""

from __future__ import annotations

from typing import List, Optional

import pytest

from db.catalog import (
    catalog_merchants,
    catalog_offers,
    catalog_products,
    catalog_skus,
)
from db.database import database
from services import pivot_query_service as svc
from tests.model_schema import ensure_model_tables

_PREFIX = "anctax"
# The live query, verbatim. category_path_prefix_for_query() resolves it to
# 'beauty/makeup/lip/'; _category_brand_anchor_terms() resolves it to
# ['stila', 'stay', 'all', 'day'].
_QUERY = "Stila Stay All Day Liquid Lipstick"


async def _connect_if_needed() -> bool:
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    return was_connected


async def _reset() -> None:
    for table, col in (
        ("catalog_offers", "offer_id"),
        ("catalog_skus", "sku_key"),
        ("catalog_products", "product_key"),
        ("catalog_merchants", "merchant_id"),
    ):
        await database.execute(f"DELETE FROM {table} WHERE {col} LIKE :p", {"p": f"{_PREFIX}%"})


async def _merchant(merchant_id: str) -> None:
    await database.execute(
        """
        INSERT INTO catalog_merchants
            (merchant_id, merchant_name, status, indexable, metadata_json,
             created_at, updated_at)
        VALUES (:m, :n, 'active', 1, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        {"m": merchant_id, "n": merchant_id},
    )


async def _row(
    key: str,
    *,
    merchant: str,
    title: str,
    brand: str,
    category_path: Optional[str],
    product_type: str = "makeup",
) -> None:
    """One canonical row with one sku and one live, priced offer."""
    await database.execute(
        """
        INSERT INTO catalog_products
            (product_key, merchant_id, platform, source_product_id, catalog_track,
             truth_tier, readiness_tier, title, brand, product_type, category_path,
             pdp_lifecycle_stage, sync_status, created_at, updated_at)
        VALUES (:k, :m, 'shopify', :spi, 'internal_merchant', 'primary',
                'commerce_ready', :t, :brand, :ptype, :category_path, 'published', 'live',
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        {"k": key, "m": merchant, "spi": f"src-{key}", "t": title, "brand": brand,
         "ptype": product_type, "category_path": category_path},
    )
    await database.execute(
        """
        INSERT INTO catalog_skus
            (sku_key, product_key, merchant_id, platform, source_product_id,
             source_variant_id, sku, title, readiness_tier, created_at, updated_at)
        VALUES (:sk, :k, :m, 'shopify', :spi, :v, :sku, :t, 'commerce_ready',
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        {"sk": f"{key}::sku", "k": key, "m": merchant, "spi": f"src-{key}",
         "v": f"var-{key}", "sku": f"sku-{key}", "t": title},
    )
    await database.execute(
        """
        INSERT INTO catalog_offers
            (offer_id, sku_key, product_key, merchant_id, catalog_track, truth_tier,
             readiness_tier, offer_mode, channel, availability, currency, list_price,
             suppressed_at, created_at, updated_at)
        VALUES (:o, :sk, :k, :m, 'internal_merchant', 'primary', 'commerce_ready',
                'merchant_checkout', 'default', 'in_stock', 'USD', 24.00, NULL,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        {"o": f"{key}::offer", "sk": f"{key}::sku", "k": key, "m": merchant},
    )


async def _recall(query: str = _QUERY) -> List[str]:
    rows = await svc._fetch_canonical_search_rows(query=query, merchant_id=None, limit=20)
    return [
        str(r.get("product_key"))
        for r in rows
        if str(r.get("product_key", "")).startswith(_PREFIX)
    ]


@pytest.fixture(autouse=True)
async def _db():
    was_connected = await _connect_if_needed()
    await ensure_model_tables(
        (catalog_merchants, catalog_products, catalog_skus, catalog_offers)
    )
    await _reset()
    try:
        yield
    finally:
        await _reset()
        if not was_connected:
            await database.disconnect()


# --------------------------------------------------------------------------------------
# ANCESTOR TAXONOMY
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_ancestor_path_with_category_evidence_is_recalled():
    """The live repro, at row grain: category_path 'beauty/makeup', title says 'Lipstick'."""
    await _merchant(f"{_PREFIX}-stila")
    await _row(
        f"{_PREFIX}-stay-all-day",
        merchant=f"{_PREFIX}-stila",
        title="Stay All Day® Liquid Lipstick - Beso",
        brand="Stila Cosmetics",
        category_path="beauty/makeup",
    )
    assert await _recall() == [f"{_PREFIX}-stay-all-day"]


@pytest.mark.asyncio
async def test_an_ancestor_path_without_category_evidence_is_not_recalled():
    """The bound on the widening. Same coarse path, same brand — but the row's own text
    never says lip, so 'beauty/makeup' must NOT become a wildcard for every lip query.

    Delete the evidence conjunct and this row joins every lip search in the catalog."""
    await _merchant(f"{_PREFIX}-stila")
    await _row(
        f"{_PREFIX}-mascara",
        merchant=f"{_PREFIX}-stila",
        title="Huge® Extreme Lash Mascara",
        brand="Stila Cosmetics",
        category_path="beauty/makeup",
    )
    assert await _recall() == []


@pytest.mark.asyncio
async def test_a_sibling_path_is_not_an_ancestor():
    """'beauty/skincare' is not an ancestor of 'beauty/makeup/lip/'. A prefix test that
    compared only the first segment, or used LIKE with the column as the pattern, would
    let this through on the strength of its title alone."""
    await _merchant(f"{_PREFIX}-other")
    await _row(
        f"{_PREFIX}-balm",
        merchant=f"{_PREFIX}-other",
        title="Overnight Lipstick Repair Treatment",
        brand="Somebody Else",
        category_path="beauty/skincare",
    )
    assert await _recall() == []


@pytest.mark.asyncio
async def test_a_null_path_is_still_not_admitted_by_the_ancestor_door():
    """The ancestor door requires an ASSERTED ancestor. A NULL path keeps exactly the
    behaviour it had — admitted only through the brand-gated missing_taxonomy escape —
    so this change cannot widen recall for untaxonomised rows.

    Honest about its own strength: unlike its three siblings, no single-clause mutation
    makes this one fail, because SQL's NULL propagation excludes the row a second time even
    with the `NULLIF(...) IS NOT NULL` guard deleted. It pins the BEHAVIOUR (an untaxonomised
    row is not swept in) rather than one conjunct, and it is the test that would catch a
    future rewrite of the prefix test into something NULL-tolerant, e.g. COALESCE(path,'')."""
    await _merchant(f"{_PREFIX}-null")
    await _row(
        f"{_PREFIX}-notaxo",
        merchant=f"{_PREFIX}-null",
        title="Some Other Brand Lipstick",
        brand="Untaxonomised Co",
        category_path=None,
    )
    assert await _recall() == []


@pytest.mark.asyncio
async def test_a_real_depth_path_still_outranks_an_ancestor_only_row():
    """The score is deliberately NOT widened: an ancestor-only row is admitted so it can
    compete, not promoted. With the brand anchor removed from the query, the depth row's
    +90 category score must still put it first."""
    await _merchant(f"{_PREFIX}-depth")
    await _row(
        f"{_PREFIX}-deep",
        merchant=f"{_PREFIX}-depth",
        title="Gloss Bomb Lipstick",
        brand="Depth Brand",
        category_path="beauty/makeup/lip/lipstick",
    )
    await _row(
        f"{_PREFIX}-shallow",
        merchant=f"{_PREFIX}-depth",
        title="Coarse Path Lipstick",
        brand="Depth Brand",
        category_path="beauty/makeup",
    )
    ordered = await _recall("lipstick")
    assert ordered[0] == f"{_PREFIX}-deep"
    assert f"{_PREFIX}-shallow" in ordered


# --------------------------------------------------------------------------------------
# TRADEMARK GLYPHS
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_registered_mark_does_not_swallow_the_word_it_is_glued_to():
    """`Day(R)` must still be the word `day` for the brand anchor.

    Both rows below are admitted by the ancestor door, so this test isolates the SCORE:
    only the row whose title carries all four anchor terms may lead. With the glyph left
    unfolded, "Stay All Day(R)" scores as if the query had never named the line, and the
    two rows tie on structure — which is how the anchor read 0 of 124 Stila rows on prod.
    """
    await _merchant(f"{_PREFIX}-glyph")
    await _row(
        f"{_PREFIX}-marked",
        merchant=f"{_PREFIX}-glyph",
        title="Stay All Day® Liquid Lipstick",
        brand="Stila Cosmetics",
        category_path="beauty/makeup",
    )
    await _row(
        f"{_PREFIX}-decoy",
        merchant=f"{_PREFIX}-glyph",
        title="Everyday Sheer Lipstick",
        brand="Stila Cosmetics",
        category_path="beauty/makeup",
    )
    ordered = await _recall()
    assert ordered[0] == f"{_PREFIX}-marked", ordered
    assert set(ordered) == {f"{_PREFIX}-marked", f"{_PREFIX}-decoy"}


def test_the_separator_list_carries_the_marks_merchants_actually_write():
    """A unit pin next to the SQL one: the glyphs are load-bearing, and a future edit that
    trims the list "because each entry is a replace()" should have to argue with this."""
    for glyph in ("®", "™", "©", "·"):
        assert glyph in svc._BRAND_IDENTITY_SEPARATORS
    expr = svc._brand_identity_expr("p.title")
    assert "'®'" in expr and "'·'" in expr


def test_category_evidence_phrases_come_from_the_query_not_a_word_list():
    """The hoisted helper is what makes the two taxonomy escapes agree."""
    phrases = svc._category_evidence_phrases(
        "stila stay all day liquid lipstick", "beauty/makeup/lip/"
    )
    assert phrases, "the query names a lip category; some span of it must prove that"
    assert all(
        svc.category_path_prefix_for_query(p) == "beauty/makeup/lip/" for p in phrases
    )
    # A query with no category term yields no evidence, so the escape stays shut.
    assert svc._category_evidence_phrases("show me murad products", "beauty/makeup/lip/") == []
