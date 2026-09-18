"""The legacy listing finder's SQL on real PostgreSQL: the apply guard and the dry-run preflight.

Both doors share `apply._LEGACY_LISTING_OWNERS_SQL`. This pins, on the real dialect, that it
returns suppressed owners (the guard does not exclude them), folds `www.` and scheme, never reads
another host, and that the dry-run path through the CLI's SELECT-only handle changes no row.
Isolated schema built from the production model; never run against production.
"""
import os
import uuid
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgres"), reason="requires test PostgreSQL"
)

LEGACY_LIVE = "prod::external_seed::external_seed::ext_bf55156550aa86a7eb921ff2"
LEGACY_SUPPRESSED = "prod::external_seed::external_seed::ext_suppressed"


@pytest.fixture
async def catalog(request):
    import asyncpg
    import databases
    import sqlalchemy as sa
    from sqlalchemy.dialects import postgresql
    import db.database as dbmod
    import db.catalog  # noqa: F401 — registers catalog_products on the shared metadata

    url = os.environ["DATABASE_URL"]
    schema = "legacy_listing_preflight_" + uuid.uuid4().hex
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    await admin.execute(f'SET search_path TO "{schema}"')
    database = None
    try:
        table = dbmod.metadata.tables["catalog_products"]
        await admin.execute(str(sa.schema.CreateTable(table).compile(dialect=postgresql.dialect())))
        database = databases.Database(url, server_settings={"search_path": schema})
        await database.connect()
        yield database, admin
    finally:
        if database is not None:
            await database.disconnect()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


def _plan():
    from services import curated_brand_feed as feed
    from services.catalog_enrichment_agent import ingestion as ing

    raw = {
        "id": 9000001, "handle": "haruharu-wonder-serum-mist", "title": "Wonder Serum Mist",
        "vendor": "Haruharu Wonder", "product_type": "Serum", "images": [{"src": "https://cdn.example/m.jpg"}],
        "variants": [{"id": 45000000000001, "barcode": "8809530070499", "price": "20.00", "available": True}],
    }
    record = feed.shopify_product_to_record(raw, domain="ohlolly.com", category_path="beauty", currency="USD",
                                            source_role="retailer", emit_native_variants=True)
    return ing.ingest_validated_jsonl([record])


async def _insert(admin, key, url, *, suppressed=False):
    await admin.execute(
        """INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title,
                                         canonical_url, source_domain, suppressed_at, suppression_reason)
           VALUES ($1, 'm_legacy', 'external_seed', $1, 'Legacy', $2, NULL, $3, $4)""",
        key, url, datetime(2026, 9, 1, tzinfo=timezone.utc) if suppressed else None,
        "stale" if suppressed else None,
    )


async def _snapshot(admin):
    return [tuple(r) for r in await admin.fetch("SELECT * FROM catalog_products ORDER BY product_key")]


async def test_real_sql_finds_live_and_suppressed_owners_and_writes_nothing(catalog):
    from scripts import onboard_curated_brands as cli
    from services.catalog_enrichment_agent import apply as writer

    database, admin = catalog
    plan = _plan()
    planned = plan["pdps"][0]
    assert planned["product_key"].startswith("ext:retailer:")
    await _insert(admin, LEGACY_LIVE, "https://www.ohlolly.com/products/haruharu-wonder-serum-mist/")
    await _insert(admin, LEGACY_SUPPRESSED, "http://ohlolly.com/products/haruharu-wonder-serum-mist?v=1",
                  suppressed=True)
    await _insert(admin, "legacy::unrelated", "https://ohlolly.com/products/something-else", suppressed=True)
    await _insert(admin, "legacy::other-host", "https://other.example/products/haruharu-wonder-serum-mist")
    before = await _snapshot(admin)

    findings = await writer.find_legacy_retailer_listing_owners(plan, cli._SelectOnlyHandle(database))
    by_key = {f["legacy_product_key"]: f for f in findings}
    assert set(by_key) == {LEGACY_LIVE, LEGACY_SUPPRESSED}
    assert {f["kind"] for f in findings} == {"conflict"}
    assert by_key[LEGACY_LIVE]["suppressed"] is False
    assert by_key[LEGACY_SUPPRESSED]["suppressed"] is True
    assert by_key[LEGACY_SUPPRESSED]["suppression_reason"] == "stale"
    assert {f["listing"] for f in findings} == {"ohlolly.com/products/haruharu-wonder-serum-mist"}

    assert await _snapshot(admin) == before

    # The apply guard reads the same rows and refuses before any write.
    with pytest.raises(ValueError, match="retailer_listing_migration_required"):
        await writer._refuse_parallel_retailer_listings(plan, database)
    assert await _snapshot(admin) == before


async def test_real_sql_suppressed_owner_alone_still_refuses(catalog):
    from services.catalog_enrichment_agent import apply as writer

    database, admin = catalog
    plan = _plan()
    await _insert(admin, LEGACY_SUPPRESSED, "https://ohlolly.com/products/haruharu-wonder-serum-mist",
                  suppressed=True)
    with pytest.raises(ValueError, match=f"retailer_listing_migration_required: existing product {LEGACY_SUPPRESSED}"):
        await writer._refuse_parallel_retailer_listings(plan, database)


async def test_cli_dry_run_report_over_real_sql(catalog, monkeypatch):
    from scripts import onboard_curated_brands as cli

    database, admin = catalog
    plan = _plan()
    await _insert(admin, LEGACY_SUPPRESSED, "https://ohlolly.com/products/haruharu-wonder-serum-mist",
                  suppressed=True)
    before = await _snapshot(admin)
    monkeypatch.setattr(cli, "_preflight_database", lambda: (database, None))
    report = await cli._legacy_listing_report(plan, check=True)
    assert report["status"] == "conflicts"
    assert (report["conflict_count"], report["suppressed_conflict_count"]) == (1, 1)
    assert report["conflicts"][0]["legacy_owners"][0]["product_key"] == LEGACY_SUPPRESSED
    assert database.is_connected  # a handle the preflight did not open is left open for its owner
    assert await _snapshot(admin) == before
