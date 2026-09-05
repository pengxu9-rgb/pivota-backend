"""One product must not be able to spend the whole recall candidate budget.

THE DEFECT THIS CLOSES. `_fetch_canonical_search_rows` matches at (product x SKU)
grain and takes the top `candidate_limit` ROWS — 40 for a default limit=10 query.
Every curated/Path-C product carried exactly ONE `catalog_skus` row
(`<product_key>::canonical`) when that budget was chosen, so 40 rows meant 40
products and nobody had to think about it. The shade fold (one SKU per real
variant) breaks that assumption: a 60-shade lipstick's rows all match on the same
`p.title` / `p.brand`, so they score identically, cluster under
`ORDER BY rank_score DESC` and take every slot.

Measured on this fixture BEFORE the cap: a 60-SKU product beside 45 ordinary
one-SKU products returns **1 distinct product** for `lipstick`. With the cap: 29.

WHY A CAP AND NOT `DISTINCT ON (product_key)`. `_build_canonical_items` groups on
`sku_key`, so one result item IS one SKU. Collapsing to a single row per product
would silently drop the other variants of every ordinary multi-variant product
(a 5-shade foundation would surface once instead of five times) — a much larger
behaviour change than the starvation it fixes. The cap only bites the tail.

The test drives the REAL query (no hand-copied SQL — a copy cannot catch a change
to the statement the service actually sends) and asserts the property, not the
number: no single product may occupy more than the cap, and the ordinary products
must still be reachable.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG, reason="requires a real Postgres DATABASE_URL (postgres dialect gate)"
)

_FAT = "pk_fat_lipstick"
_FAT_SKUS = 60
_OTHERS = 45


@pytest.fixture(scope="module")
def pg_engine():
    import db.catalog  # noqa: F401
    from sqlalchemy import create_engine

    from db.database import metadata

    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine, checkfirst=True)
    yield engine
    engine.dispose()


def _seed(engine):
    """45 ordinary one-SKU products, then the fat one LAST so it wins the
    `p.updated_at DESC` tie-break — the shape where starvation actually bites."""
    from sqlalchemy import text

    with engine.begin() as conn:
        for t in ("catalog_offers", "catalog_skus", "catalog_products", "catalog_merchants"):
            conn.execute(text(f"DELETE FROM {t}"))
        conn.execute(text(
            "INSERT INTO catalog_merchants (merchant_id, merchant_name, primary_platform, status) "
            "VALUES ('m_cap', 'Cap Merchant', 'external_seed', 'active')"
        ))

        def product(pk, title, n_skus):
            conn.execute(text(
                "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id,"
                " title, brand, catalog_track, truth_tier, readiness_tier, pdp_scope,"
                " pdp_lifecycle_stage, source_system, updated_at)"
                " VALUES (:pk,'m_cap','external_seed',:pk,:t,'ACME','citation','primary',"
                "         'referral_only','multi_merchant_canonical','published','test',NOW())"
            ), {"pk": pk, "t": title})
            for i in range(n_skus):
                sk = f"{pk}::v{i}"
                conn.execute(text(
                    "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform,"
                    " source_product_id, source_variant_id, title, currency, updated_at)"
                    " VALUES (:sk,:pk,'m_cap','external_seed',:pk,:vid,:t,'USD',NOW())"
                ), {"sk": sk, "pk": pk, "vid": f"vid{i}", "t": f"{title} shade {i}"})
                conn.execute(text(
                    "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id,"
                    " catalog_track, truth_tier, readiness_tier, offer_mode, channel, availability,"
                    " currency, list_price, merchant_effective_price, updated_at)"
                    " VALUES (:oid,:sk,:pk,'m_cap','citation','primary','referral_only','redirect',"
                    "         'default','in_stock','USD',24,24,NOW())"
                ), {"oid": f"of:{sk}", "sk": sk, "pk": pk})

        for n in range(_OTHERS):
            product(f"pk_other_{n}", f"Other Lipstick {n}", 1)
        product(_FAT, "Fat Lipstick", _FAT_SKUS)


def _recall_rows(cap: int):
    """Run the REAL recall query with the cap set, in a fresh module import so the
    module-level constant is re-read."""
    os.environ["RECALL_MAX_SKUS_PER_PRODUCT"] = str(cap)
    for name in [k for k in list(sys.modules) if k.startswith("services.pivot_query_service")]:
        del sys.modules[name]
    pq = importlib.import_module("services.pivot_query_service")

    async def go():
        from db.database import database

        if not database.is_connected:
            await database.connect()
        try:
            return await pq._fetch_canonical_search_rows(query="lipstick", merchant_id=None, limit=10)
        finally:
            await database.disconnect()

    return pq, asyncio.run(go())


def test_one_product_cannot_take_the_whole_candidate_budget(pg_engine):
    _seed(pg_engine)
    pq, rows = _recall_rows(12)

    from collections import Counter

    per_product = Counter(str(r["product_key"]) for r in rows)
    assert per_product[_FAT] <= pq.RECALL_MAX_SKUS_PER_PRODUCT, (
        f"the {_FAT_SKUS}-SKU product contributed {per_product[_FAT]} candidate rows, "
        f"above the cap of {pq.RECALL_MAX_SKUS_PER_PRODUCT}"
    )
    # ...and the budget it no longer eats reaches other products.
    assert len(per_product) > 20, f"only {len(per_product)} distinct products survived the budget"
    assert any(k.startswith("pk_other_") for k in per_product)


def test_without_the_cap_the_fat_product_starves_every_other_one(pg_engine):
    """The counterfactual, so the assertion above cannot pass for a reason other
    than the cap (a fixture that never starves would make it vacuous)."""
    _seed(pg_engine)
    _, rows = _recall_rows(10_000)  # effectively uncapped == pre-fix behaviour

    products = {str(r["product_key"]) for r in rows}
    assert products == {_FAT}, (
        "expected the uncapped query to return ONLY the 60-SKU product; got "
        f"{len(products)} products — the fixture no longer reproduces the defect"
    )


def test_the_cap_keeps_ordinary_multi_variant_products_whole(pg_engine):
    """A cap, not a dedupe: a product with a handful of variants must still
    contribute every one of them, or search loses variants it surfaces today."""
    from sqlalchemy import text

    _seed(pg_engine)
    with pg_engine.begin() as conn:
        conn.execute(text(f"DELETE FROM catalog_offers WHERE product_key = '{_FAT}'"))
        conn.execute(text(f"DELETE FROM catalog_skus WHERE product_key = '{_FAT}'"))
        conn.execute(text(f"DELETE FROM catalog_products WHERE product_key = '{_FAT}'"))

    _, rows = _recall_rows(12)
    # every ordinary product has exactly one SKU; all of them should be present
    products = {str(r["product_key"]) for r in rows}
    assert len(products) >= 25, f"cap dropped ordinary products: only {len(products)} present"
