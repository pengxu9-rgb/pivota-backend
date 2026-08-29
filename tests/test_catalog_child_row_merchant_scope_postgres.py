"""Two merchants may carry the SAME platform product. Catalog ingest must not
hard-fail the second one — driven on the REAL `ingest_standard_products` against
a REAL Postgres.

THE FILENAME IS LOAD-BEARING (`.github/workflows/postgres-dialect-gate.yml`
globs `tests/test_*_postgres.py`).

THE DEFECT (prod, 2026-08-29). Connecting one Shopify store
(ijaqit-v9.myshopify.com) to a SECOND merchant made `run_catalog_sync_job` fail
with

    duplicate key value violates unique constraint "beauty_shades_pkey"
    DETAIL: Key (shade_id)=(beauty_shade_66f740053949ff6302fc) already exists

and write ZERO catalog_products rows — the whole per-product transaction rolled
back on one child row.

The two scopes did not match. `product_key` is merchant-scoped
(`make_catalog_product_key(merchant_id, platform, source_pid)`) and the child
rows are replaced by a DELETE on that key, so the delete only ever cleared THIS
merchant's rows. But the child PRIMARY KEYS were derived from the platform's own
product id — `_stable_key("beauty_shade", product.id, ...)` — which the two
merchants share. Merchant B's INSERT therefore collided with merchant A's
surviving row. `beauty_content_assets` had the same shape;
`beauty_usage_guides.guide_id` already hashed `product_key` and never collided,
which is the shape the fix copies.

A SECOND, narrower defect the repro turned up: `beauty_content_assets` rows are
built from PRODUCT-level metadata but appended inside the VARIANT loop, so a
two-variant product hands the same asset id in twice within ONE run. That one
needs no second merchant — it aborted a single merchant's ingest on its own.

Deduping those two copies (#1940) stopped the abort but left the surviving row
stamped with the LAST variant's `sku_key`, so the tutorial surfaced on exactly
one of the product's variants. Product-level payloads are now derived once,
outside the loop, and written with `sku_key = NULL` — the product-level marker
`beauty_field_authoring` and `routes/merchant_products.py` already used — and
read through an `IS NULL` arm. Both halves are pinned below: one test that ONE
row is written, one that EVERY variant can read it.

WHY THIS TEST NEEDS POSTGRES: the failure IS a primary-key constraint. The
repo's fake-database tests (`tests/test_catalog_sync_service_integration.py`)
keep rows in dicts and would report both merchants succeeding against the
unfixed code.

Locally:
    DATABASE_URL=postgresql://postgres@127.0.0.1:55433/pivota_gate \\
      .venv/bin/python -m pytest tests/test_catalog_child_row_merchant_scope_postgres.py -q
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"),
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

_SCHEMA = f"beauty_child_scope_{os.getpid()}"

SOURCE_PRODUCT_ID = "gid://shopify/Product/8899"
MERCHANT_A = "ijaqit_merchant_a"
MERCHANT_B = "ijaqit_merchant_b"


@pytest.fixture(scope="module")
def engine():
    from sqlalchemy import create_engine, text

    # Import the module under test FIRST: it is what pulls every table it
    # touches into `db.database.metadata`. Creating from a narrower import
    # yields a schema the real ingest path immediately falls off.
    import services.catalog_sync_service  # noqa: F401
    from db.database import metadata

    url = os.environ["DATABASE_URL"]
    raw = create_engine(url, future=True)
    with raw.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
        c.execute(text(f"CREATE SCHEMA {_SCHEMA}"))
    eng = create_engine(
        url, future=True, connect_args={"options": f"-csearch_path={_SCHEMA}"}
    )
    with eng.begin() as c:
        # The REAL table metadata — not a hand-typed lookalike.
        metadata.create_all(bind=c)
        # db/catalog.py declares this column as a naive `DateTime` while its
        # migration (073_pivota_signature_minted_at.sql) and prod both have
        # TIMESTAMPTZ, and the ingest path writes an AWARE datetime into it. The
        # drift is invisible in prod and only bites a create_all() fixture, so
        # match prod here rather than "fix" the model from a test.
        c.execute(
            text(
                "ALTER TABLE catalog_products "
                "ALTER COLUMN pivota_signature_minted_at TYPE TIMESTAMPTZ"
            )
        )
        # product_group_members has no Table() declaration — the ingest path
        # stamps a singleton group membership through raw SQL. Apply the REAL
        # migration rather than typing DDL that could drift from it.
        c.execute(
            text(
                pathlib.Path("db/migrations/045_product_groups.sql").read_text(
                    encoding="utf-8"
                )
            )
        )
    yield eng
    with raw.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))


@pytest.fixture(autouse=True)
def clean(engine):
    from sqlalchemy import text

    with engine.begin() as c:
        c.execute(
            text(
                "TRUNCATE beauty_shades, beauty_content_assets, beauty_usage_guides,"
                " beauty_compatibility_rules, beauty_product_profiles,"
                " beauty_sku_ingredients, catalog_offers, catalog_skus,"
                " catalog_products, catalog_merchants, catalog_field_facts,"
                " product_group_members"
            )
        )
    yield


def _drive(coro_factory):
    """Run `coro_factory()` with every module's `database` bound to the test
    schema. The ingest path fans out across modules that each captured
    `db.database.database` at import time (`product_group_autogrouper` and
    friends), so patching only `catalog_sync_service.database` leaves them
    talking to an unconnected backend."""
    import databases

    import db.database as dbmod

    url = os.environ["DATABASE_URL"]
    if "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://")
    db = databases.Database(url, server_settings={"search_path": _SCHEMA})

    original = dbmod.database
    patched = [
        mod
        for mod in list(sys.modules.values())
        if getattr(mod, "database", None) is original
    ]
    assert patched, "no module held db.database.database — the patch would be a no-op"

    async def _go():
        await db.connect()
        try:
            return await coro_factory()
        finally:
            await db.disconnect()

    for mod in patched:
        mod.database = db
    dbmod.database = db
    try:
        return asyncio.run(_go())
    finally:
        for mod in patched:
            mod.database = original
        dbmod.database = original


def _payload(merchant_id: str, *, shades: bool = True, tutorials: bool = False) -> dict:
    """One platform product, as the SAME Shopify store hands it to whichever
    merchant it is connected to. Only `merchant_id` differs between the two
    merchants — the platform product id, variants and metadata are identical,
    which is the whole point."""
    metadata: dict = {"how_to_use": "Apply daily."}
    if tutorials:
        metadata["tutorials"] = [
            {"url": "https://cdn.example.com/t1.mp4", "title": "How to apply"}
        ]
    return {
        "id": SOURCE_PRODUCT_ID,
        "platform": "shopify",
        "merchant_id": merchant_id,
        "title": "Glow Serum Foundation",
        "description": "A lightweight serum foundation.",
        "vendor": "Ijaqit",
        "product_type": "Foundation",
        "price": 42.0,
        "currency": "USD",
        "inventory_quantity": 10,
        # retinol + salicylic_acid is the pair `_compatibility_rules_from_ingredients`
        # fires on, so beauty_compatibility_rules gets exercised too.
        "ingredient_ids": ["niacinamide", "retinol", "salicylic_acid"],
        "image_url": "https://cdn.example.com/a.jpg",
        "images": ["https://cdn.example.com/a.jpg"],
        "variants": [
            {
                "id": "v1",
                # StandardProduct derives a shade label from a cosmetic
                # product's variant title as well as from its options, and
                # exempts exactly Shopify's own "Default Title". So a
                # shade-free variant has to be titled that way, not merely
                # stripped of options.
                "title": "Rose Nude" if shades else "Default Title",
                "price": 42.0,
                "inventory_quantity": 5,
                **({"options": {"Shade": "Rose Nude"}} if shades else {}),
            },
            {
                "id": "v2",
                "title": "Warm Beige" if shades else "Default Title",
                "price": 42.0,
                "inventory_quantity": 5,
                **({"options": {"Shade": "Warm Beige"}} if shades else {}),
            },
        ],
        "platform_metadata": metadata,
    }


def _ingest(merchant_id: str, **kwargs) -> dict:
    import services.catalog_sync_service as sync

    return _drive(
        lambda: sync.ingest_standard_products(
            merchant_id=merchant_id,
            platform="shopify",
            product_payloads=[_payload(merchant_id, **kwargs)],
            source_system="products_cache",
            source_ref="child-scope-gate",
        )
    )


def _rows(engine, sql: str) -> list:
    from sqlalchemy import text

    with engine.begin() as c:
        return [tuple(r) for r in c.execute(text(sql))]


def test_second_merchant_carrying_the_same_platform_product_ingests(engine):
    """THE PROD REPRO. Merchant A ingests the store's product; merchant B then
    ingests the SAME platform product. Before the fix B raised
    `UniqueViolation: beauty_shades_pkey` and rolled back — zero
    catalog_products rows for B."""
    stats_a = _ingest(MERCHANT_A)
    stats_b = _ingest(MERCHANT_B)

    # The machinery actually ran for BOTH — a fixture that produced no shades
    # would pass the collision assertions below while proving nothing.
    for who, stats in (("A", stats_a), ("B", stats_b)):
        assert stats["products_ingested"] == 1, f"merchant {who}: {stats}"
        assert stats["products_failed"] == 0, f"merchant {who}: {stats}"
        assert stats["beauty_shades_upserted"] == 2, f"merchant {who}: {stats}"

    # The rollback signature: B's product row is the thing that went missing.
    assert sorted(
        _rows(engine, "SELECT merchant_id FROM catalog_products")
    ) == [(MERCHANT_A,), (MERCHANT_B,)]

    shades = _rows(
        engine,
        "SELECT merchant_id, shade_name, shade_id FROM beauty_shades"
        " ORDER BY merchant_id, shade_name",
    )
    assert [(m, n) for m, n, _ in shades] == [
        (MERCHANT_A, "Rose Nude"),
        (MERCHANT_A, "Warm Beige"),
        (MERCHANT_B, "Rose Nude"),
        (MERCHANT_B, "Warm Beige"),
    ]
    # Four rows means four distinct shade_ids; the defect was that the two
    # merchants derived the SAME two.
    assert len({sid for _, _, sid in shades}) == 4

    # beauty_compatibility_rules was NOT re-keyed by the fix and must not be:
    # its `compatibility_rule_id` hashes `sku_key`, which already embeds the
    # merchant-scoped `product_key`. Pinned so a later "fix it for symmetry"
    # has to explain itself.
    assert stats_a["beauty_compatibility_rules_upserted"] == 2, stats_a
    rules = _rows(
        engine,
        "SELECT merchant_id, compatibility_rule_id FROM beauty_compatibility_rules",
    )
    assert len(rules) == 4, rules
    assert len({rid for _, rid in rules}) == 4


def test_one_merchants_resync_does_not_touch_the_others_child_rows(engine):
    """The child rows are per-merchant state, not shared state. Re-syncing A
    with the shade options gone must clear A's rows and leave B's standing."""
    assert _ingest(MERCHANT_A)["beauty_shades_upserted"] == 2
    assert _ingest(MERCHANT_B)["beauty_shades_upserted"] == 2
    before_b = sorted(
        _rows(
            engine,
            f"SELECT shade_id FROM beauty_shades WHERE merchant_id = '{MERCHANT_B}'",
        )
    )
    assert len(before_b) == 2

    stats = _ingest(MERCHANT_A, shades=False)
    assert stats["products_ingested"] == 1, stats
    assert stats["beauty_shades_upserted"] == 0, stats

    assert _rows(
        engine,
        "SELECT merchant_id, count(*) FROM beauty_shades GROUP BY 1 ORDER BY 1",
    ) == [(MERCHANT_B, 2)]
    # Not just the count — the same two rows, so a delete-and-rewrite of B's
    # rows under A's sync could not pass this.
    assert (
        sorted(
            _rows(
                engine,
                f"SELECT shade_id FROM beauty_shades WHERE merchant_id = '{MERCHANT_B}'",
            )
        )
        == before_b
    )


def test_a_product_level_asset_seen_once_per_variant_writes_one_row(engine):
    """`beauty_content_assets` rows come from PRODUCT-level metadata but are
    collected inside the VARIANT loop, so a two-variant product offers the same
    asset id twice in one batch. That alone raised
    `duplicate key ... beauty_content_assets_pkey` — no second merchant needed."""
    stats = _ingest(MERCHANT_A, tutorials=True)

    assert stats["products_ingested"] == 1, stats
    assert stats["beauty_content_assets_upserted"] == 1, stats
    assert _rows(
        engine, "SELECT merchant_id, url FROM beauty_content_assets"
    ) == [(MERCHANT_A, "https://cdn.example.com/t1.mp4")]

    # And the cross-merchant half of the same derivation.
    _ingest(MERCHANT_B, tutorials=True)
    assets = _rows(engine, "SELECT merchant_id, asset_id FROM beauty_content_assets")
    assert len(assets) == 2
    assert len({aid for _, aid in assets}) == 2


def test_a_child_row_outside_the_delete_scope_does_not_abort_the_whole_ingest(engine):
    """Defence in depth for the blast radius, not for the collision.

    The DELETE clears one `product_key`; the write is keyed by primary key. Any
    row carrying an incoming id but sitting OUTSIDE that delete scope — residue
    written under the pre-fix derivation is the case that existed — used to
    raise out of a plain INSERT and take down the ingest of every product in the
    run. It must degrade to a row rewrite instead.
    """
    from sqlalchemy import text

    import services.catalog_sync_service as sync
    from models.standard_product import StandardProduct

    product = StandardProduct(**_payload(MERCHANT_A))
    key_a = sync.make_catalog_product_key(MERCHANT_A, "shopify", SOURCE_PRODUCT_ID)
    incoming_id = sync._extract_shades(key_a, product.variants[0])[0]["shade_id"]

    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO beauty_shades (shade_id, sku_key, product_key,"
                " merchant_id, shade_name) VALUES (:sid, 'sku::legacy',"
                " 'prod::legacy', 'legacy_merchant', 'Legacy')"
            ),
            {"sid": incoming_id},
        )

    stats = _ingest(MERCHANT_A)
    assert stats["products_ingested"] == 1, stats
    assert stats["beauty_shades_upserted"] == 2, stats
    assert _rows(
        engine,
        f"SELECT merchant_id, shade_name FROM beauty_shades"
        f" WHERE shade_id = '{incoming_id}'",
    ) == [(MERCHANT_A, "Rose Nude")]


def test_child_row_ids_are_derived_from_the_merchant_scoped_product_key():
    """The derivations themselves, with no database in the way: the same
    platform product under two merchants must produce different child ids.
    This is the line the two integration tests above exercise through the DB."""
    import services.catalog_sync_service as sync
    from models.standard_product import StandardProduct

    product = StandardProduct(**_payload(MERCHANT_A, tutorials=True))
    variant = product.variants[0]
    key_a = sync.make_catalog_product_key(MERCHANT_A, "shopify", SOURCE_PRODUCT_ID)
    key_b = sync.make_catalog_product_key(MERCHANT_B, "shopify", SOURCE_PRODUCT_ID)
    assert key_a != key_b

    shades_a = sync._extract_shades(key_a, variant)
    shades_b = sync._extract_shades(key_b, variant)
    assert [s["shade_name"] for s in shades_a] == ["Rose Nude"], shades_a
    assert shades_a[0]["shade_id"] != shades_b[0]["shade_id"]

    metadata = sync._json_dict(product.platform_metadata)
    assets_a = sync._extract_tutorial_assets(key_a, metadata)
    assets_b = sync._extract_tutorial_assets(key_b, metadata)
    assert len(assets_a) == 1, assets_a
    assert assets_a[0]["asset_id"] != assets_b[0]["asset_id"]

    # A merchant-DECLARED asset id is shared by the two merchants too — the
    # platform handed both stores the same metadata — so it is namespaced, not
    # taken verbatim.
    declared = {"tutorials": [{"url": "https://cdn.example.com/t1.mp4", "asset_id": "tut-1"}]}
    declared_a = sync._extract_tutorial_assets(key_a, declared)
    declared_b = sync._extract_tutorial_assets(key_b, declared)
    assert declared_a[0]["asset_id"] != "tut-1"
    assert declared_a[0]["asset_id"] != declared_b[0]["asset_id"]


def test_a_product_level_asset_reaches_every_variants_read_payload(engine):
    """The sibling half of the test above: that one pins that ONE row is
    written, this one pins WHO CAN READ IT.

    `platform_metadata.tutorials` and `how_to_use` describe the PRODUCT. Derived
    inside the variant loop they were stamped with a variant's `sku_key`, and
    the two tables then failed differently:

      * every variant derived the SAME `asset_id` (it hashes only
        `product_key`), so the dedupe in `_replace_child_rows_multi` left one
        row carrying the LAST variant's sku_key. `_fetch_beauty_vertical_payload`
        is called per-SKU with a concrete `sku_key`, so variant 1 got zero
        tutorials and variant 2 got one.
      * `guide_id` hashes `sku_key`, so the usage guide multiplied into one
        identical row per variant and never matched the `sku_key IS NULL` join
        `routes/merchant_products.py` uses for it.

    Both are written once now with `sku_key = NULL`, the product-level marker
    `beauty_field_authoring` already used, and read through an `IS NULL` arm.

    Note WHY that arm is needed: the pre-existing predicate
    `CAST(:sku_key AS text) IS NULL OR sku_key = ...` tests the PARAMETER, not
    the column, so writing a NULL sku_key without touching the read would have
    hidden the row from every per-SKU caller instead of fixing it.
    """
    import services.catalog_sync_service as sync
    import services.pivot_query_service as pq

    stats = _ingest(MERCHANT_A, tutorials=True)
    assert stats["products_ingested"] == 1, stats
    assert stats["beauty_content_assets_upserted"] == 1, stats
    assert stats["beauty_usage_guides_upserted"] == 1, stats

    # The stored marker: one product-level row per table, sku_key NULL.
    assert _rows(engine, "SELECT sku_key FROM beauty_content_assets") == [(None,)]
    assert _rows(engine, "SELECT sku_key FROM beauty_usage_guides") == [(None,)]
    # ... and it is the id `beauty_field_authoring` writes, so the merchant's
    # own how_to_use and ingest's converge on ONE row rather than racing two
    # NULL-sku rows past that IS NULL join.
    from services.beauty_field_authoring import product_usage_guide_id

    product_key = sync.make_catalog_product_key(MERCHANT_A, "shopify", SOURCE_PRODUCT_ID)
    assert _rows(engine, "SELECT guide_id FROM beauty_usage_guides") == [
        (product_usage_guide_id(product_key),)
    ]

    sku_keys = sorted(
        k
        for (k,) in _rows(
            engine,
            "SELECT sku_key FROM catalog_skus"
            f" WHERE product_key = '{product_key}'",
        )
    )
    # Two variants, or the read assertions below prove nothing.
    assert len(sku_keys) == 2, sku_keys

    for sku_key in sku_keys:
        payload = _drive(
            lambda sk=sku_key: pq._fetch_beauty_vertical_payload(product_key, sk)
        )
        assert [t["url"] for t in payload["tutorials"]] == [
            "https://cdn.example.com/t1.mp4"
        ], (sku_key, payload["tutorials"])
        assert payload["how_to_use"] == "Apply daily.", (sku_key, payload["how_to_use"])

    # The product-level read (no sku_key) still works — that is the arm the old
    # predicate served, and it must not have been traded away for the new one.
    product_payload = _drive(
        lambda: pq._fetch_beauty_vertical_payload(product_key, None)
    )
    assert [t["url"] for t in product_payload["tutorials"]] == [
        "https://cdn.example.com/t1.mp4"
    ], product_payload["tutorials"]

    # Per-SKU rows are still per-SKU: shades stay split across the two variants
    # (`beauty_shades.sku_key` is NOT NULL — that table is genuinely variant
    # scoped), so this is not "everything became product-level".
    for sku_key in sku_keys:
        payload = _drive(
            lambda sk=sku_key: pq._fetch_beauty_vertical_payload(product_key, sk)
        )
        assert len(payload["shades"]) == 1, (sku_key, payload["shades"])
