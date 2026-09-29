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
        for name in ("catalog_products", "catalog_offers"):
            table = dbmod.metadata.tables[name]
            await admin.execute(str(sa.schema.CreateTable(table).compile(dialect=postgresql.dialect())))
        # Not on the shared metadata (mig 044): the two columns the finder reads, in this private schema only.
        await admin.execute("CREATE TABLE external_product_seeds (id TEXT PRIMARY KEY, attached_product_key TEXT, "
                            "status TEXT)")
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


async def _insert(admin, key, url, *, suppressed=False, source_ref=None):
    # source_product_id is NOT the product key on real rows (it is the store's product id): nothing may join on it.
    await admin.execute(
        """INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title,
                                         canonical_url, source_domain, suppressed_at, suppression_reason, source_ref)
           VALUES ($1, 'm_legacy', 'external_seed', 'shopify:9000001', 'Legacy', $2, NULL, $3, $4, $5)""",
        key, url, datetime(2026, 9, 1, tzinfo=timezone.utc) if suppressed else None,
        "stale" if suppressed else None, source_ref,
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
    # The live owner conflicts; the suppressed one has no seed or offer left, so its chain is retired (2026-09-29).
    assert by_key[LEGACY_LIVE]["kind"] == "conflict" and by_key[LEGACY_SUPPRESSED]["kind"] == "retired_owner"
    assert by_key[LEGACY_LIVE]["suppressed"] is False
    assert by_key[LEGACY_SUPPRESSED]["suppressed"] is True
    assert by_key[LEGACY_SUPPRESSED]["suppression_reason"] == "stale"
    assert {f["listing"] for f in findings} == {"ohlolly.com/products/haruharu-wonder-serum-mist"}

    assert await _snapshot(admin) == before

    # The apply guard reads the same rows and refuses before any write.
    with pytest.raises(ValueError, match="retailer_listing_migration_required"):
        await writer._refuse_parallel_retailer_listings(plan, database)
    assert await _snapshot(admin) == before


async def _seed(admin, key, status, *, attached=True):
    """attached=False: the seed that CREATED the row (catalog_products.source_ref) but was never attached to it."""
    seed_id = f"seed:{key}:{status}"
    await admin.execute("INSERT INTO external_product_seeds (id, attached_product_key, status) VALUES ($1, $2, $3)",
                        seed_id, key if attached else None, status)
    return seed_id


async def _offer(admin, key, *, suppressed):
    await admin.execute(
        """INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, currency, suppressed_at)
           VALUES ($1, $1, $2, 'm_legacy', 'USD', $3)""",
        f"offer:{key}:{suppressed}", key, datetime(2026, 9, 1, tzinfo=timezone.utc) if suppressed else None)


@pytest.mark.parametrize("live_link", ["active_seed", "ACTIVE_seed", "source_ref_seed", "live_offer"])
async def test_real_sql_a_suppressed_owner_with_any_live_link_still_refuses(catalog, live_link):
    """Suppressing the product row is not retiring its chain: an active seed (attached, or the row's own
    source_ref seed; status in any case) or a live offer on it still refuses."""
    from services.catalog_enrichment_agent import apply as writer

    database, admin = catalog
    plan = _plan()
    status = {"active_seed": "active", "ACTIVE_seed": "ACTIVE", "source_ref_seed": "active"}.get(live_link, "inactive")
    seed_id = await _seed(admin, LEGACY_SUPPRESSED, status, attached=live_link != "source_ref_seed")
    await _insert(admin, LEGACY_SUPPRESSED, "https://ohlolly.com/products/haruharu-wonder-serum-mist",
                  suppressed=True, source_ref=seed_id)
    await _offer(admin, LEGACY_SUPPRESSED, suppressed=live_link != "live_offer")
    with pytest.raises(ValueError, match=f"retailer_listing_migration_required: existing product {LEGACY_SUPPRESSED}"):
        await writer._refuse_parallel_retailer_listings(plan, database)


async def test_real_sql_a_fully_retired_chain_is_reported_not_refused(catalog):
    """cocomo.sg, 2026-09-29: product suppressed with a reason, every seed inactive, every offer suppressed."""
    from scripts import onboard_curated_brands as cli
    from services.catalog_enrichment_agent import apply as writer

    database, admin = catalog
    plan = _plan()
    await _insert(admin, LEGACY_SUPPRESSED, "https://ohlolly.com/products/haruharu-wonder-serum-mist",
                  suppressed=True)
    await _seed(admin, LEGACY_SUPPRESSED, "inactive")
    await _offer(admin, LEGACY_SUPPRESSED, suppressed=True)
    [finding] = await writer.find_legacy_retailer_listing_owners(plan, cli._SelectOnlyHandle(database))
    assert finding["kind"] == "retired_owner" and finding["legacy_product_key"] == LEGACY_SUPPRESSED
    await writer._refuse_parallel_retailer_listings(plan, database)  # admitted: no raise


@pytest.mark.parametrize("reason", ["stale_after_sync", "d2_same_url"])
async def test_real_sql_a_suppression_its_own_lane_lifts_is_not_a_retirement(catalog, reason):
    """Review of #2448: catalog_sync clears `stale_after_sync` on the next re-sync; identity_resolution.revert_run
    revives its `d2_*` rows with their seeds. A new listing admitted onto either URL would be its twin by then."""
    from services import catalog_sync_service, identity_resolution
    from services.catalog_enrichment_agent import apply as writer

    assert catalog_sync_service.STALE_AFTER_SYNC in writer.SELF_REVIVING_SUPPRESSION_REASONS
    assert "suppression_reason LIKE 'd2" in identity_resolution.REVERT_ROWS_SQL
    database, admin = catalog
    plan = _plan()
    await _insert(admin, LEGACY_SUPPRESSED, "https://ohlolly.com/products/haruharu-wonder-serum-mist",
                  suppressed=True)
    await admin.execute("UPDATE catalog_products SET suppression_reason = $2 WHERE product_key = $1",
                        LEGACY_SUPPRESSED, reason)
    await _seed(admin, LEGACY_SUPPRESSED, "inactive")
    await _offer(admin, LEGACY_SUPPRESSED, suppressed=True)
    with pytest.raises(ValueError, match="retailer_listing_migration_required"):
        await writer._refuse_parallel_retailer_listings(plan, database)


async def test_real_sql_a_suppression_without_a_reason_is_not_a_retirement(catalog):
    from services.catalog_enrichment_agent import apply as writer

    database, admin = catalog
    plan = _plan()
    await _insert(admin, LEGACY_SUPPRESSED, "https://ohlolly.com/products/haruharu-wonder-serum-mist",
                  suppressed=True)
    await admin.execute("UPDATE catalog_products SET suppression_reason = '  ' WHERE product_key = $1",
                        LEGACY_SUPPRESSED)
    with pytest.raises(ValueError, match="retailer_listing_migration_required"):
        await writer._refuse_parallel_retailer_listings(plan, database)


async def test_cli_dry_run_report_over_real_sql(catalog, monkeypatch):
    from scripts import onboard_curated_brands as cli

    database, admin = catalog
    plan = _plan()
    await _insert(admin, LEGACY_SUPPRESSED, "https://ohlolly.com/products/haruharu-wonder-serum-mist",
                  suppressed=True)
    await _seed(admin, LEGACY_SUPPRESSED, "active")  # a live link: still the conflict it always was
    before = await _snapshot(admin)
    monkeypatch.setattr(cli, "_preflight_database", lambda: (database, None))
    report = await cli._legacy_listing_report(plan, check=True)
    assert report["status"] == "conflicts"
    assert (report["conflict_count"], report["suppressed_conflict_count"]) == (1, 1)
    assert report["conflicts"][0]["legacy_owners"][0]["product_key"] == LEGACY_SUPPRESSED
    assert report["retired_owner_count"] == 0
    assert database.is_connected  # a handle the preflight did not open is left open for its owner
    assert await _snapshot(admin) == before
    # ...and once that seed is inactive too, the chain is retired: the dry run says clear, and counts it.
    await admin.execute("UPDATE external_product_seeds SET status = 'inactive'")
    report = await cli._legacy_listing_report(plan, check=True)
    assert (report["status"], report["retired_owner_count"], report["apply_would_refuse"]) == ("clear", 1, False)


async def test_real_sql_a_live_owner_with_a_leftover_reason_is_not_retired(catalog):
    """An un-suppressed row can keep an old suppression_reason (a revert that cleared only suppressed_at); with no
    seed or offer it is still a LIVE row owning the URL."""
    from services.catalog_enrichment_agent import apply as writer

    database, admin = catalog
    plan = _plan()
    await _insert(admin, LEGACY_LIVE, "https://ohlolly.com/products/haruharu-wonder-serum-mist")
    await admin.execute("UPDATE catalog_products SET suppression_reason = 'reverted' WHERE product_key = $1",
                        LEGACY_LIVE)
    with pytest.raises(ValueError, match=f"retailer_listing_migration_required: existing product {LEGACY_LIVE}"):
        await writer._refuse_parallel_retailer_listings(plan, database)


async def test_real_sql_live_retailer_listing_owner_matches_the_url_not_the_host(catalog):
    """withdraw_catalog_rows --revert asks it before reviving a retired chain (review of #2448)."""
    from services.catalog_enrichment_agent import apply as writer

    database, admin = catalog
    url = "https://ohlolly.com/products/haruharu-wonder-serum-mist"
    await _insert(admin, "ext:retailer:live", url + "?variant=45000000000001")
    await _insert(admin, "ext:retailer:gone", "https://www.ohlolly.com/products/other", suppressed=True)
    await _insert(admin, "ext:retailer:other", "https://ohlolly.com/products/other-thing")
    await _insert(admin, "ext:legacy-live", "https://ohlolly.com/products/legacy-only")  # live, but not a listing
    assert await writer.live_retailer_listing_owner(database, "https://www.ohlolly.com" + url[19:] + "/") \
        == "ext:retailer:live"
    assert await writer.live_retailer_listing_owner(database, "https://ohlolly.com/products/other") is None
    assert await writer.live_retailer_listing_owner(database, "https://ohlolly.com/products/legacy-only") is None
    assert await writer.live_retailer_listing_owner(database, None) is None
    assert await writer.live_retailer_listing_owner(database, "https://[bad/products/x") is None
