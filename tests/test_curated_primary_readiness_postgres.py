"""Fresh primary ingestion creates its own readiness artifacts on real PostgreSQL.

No quality, APV, index eligibility or trust rows are seeded. An isolated schema
contains production models/migrations, and only the public-source product input
and ancillary empty overlay services are fixtures. Never run against production.
"""
import copy
import json
import os
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgres"), reason="requires test PostgreSQL"
)
ROOT = Path(__file__).parents[1]


@pytest.fixture
async def readiness_db(monkeypatch, request):
    import asyncpg
    import databases
    import sqlalchemy as sa
    from sqlalchemy.dialects import postgresql
    import db.database as dbmod
    import db.catalog
    import db.product_quality
    import db.brand_claims
    import db.external_offers
    import db.product_evidence as evidence_db
    from services import agent_pdp_view_assembler as apv
    from services import outcome_aggregation_service, indexnow, pivot_query_service, catalog_offer_writer_guard

    url = os.environ["DATABASE_URL"]
    schema = "curated_readiness_test_" + uuid.uuid4().hex
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    await admin.execute(f'SET search_path TO "{schema}"')
    # Model tables used by the real writer/scorer/assembler; do not manufacture
    # eligibility rows or bypass any query or classifier.
    names = (
        "catalog_products", "catalog_skus", "catalog_offers", "catalog_merchants", "writer_audit_log",
        "agent_pdp_view", "beauty_product_profiles", "beauty_sku_ingredients", "beauty_usage_guides",
        "beauty_shades", "beauty_compatibility_rules", "beauty_content_assets", "product_quality_snapshot",
        "brand_claims", "external_offer_snapshots", "product_evidence", "evidence_artifact",
    )
    try:
        for name in names:
            table = dbmod.metadata.tables[name]
            await admin.execute(str(sa.schema.CreateTable(table).compile(dialect=postgresql.dialect())))
            for index in table.indexes:
                await admin.execute(str(sa.schema.CreateIndex(index).compile(dialect=postgresql.dialect())))
        for name in (
            "044_external_product_seeds.sql", "045_product_groups.sql", "098_index_pipeline_state.sql",
            "099_domain_extractor_baselines.sql", "134_catalog_source_quarantine.sql",
            "136_catalog_row_trust.sql", "158_catalog_onboard_queue.sql", "165_index_pipeline_state_index_eligible.sql",
            "169_external_product_seeds_seller_ref.sql", "177_intake_identity_events.sql",
            "181_content_canonical_election.sql", "188_catalog_products_pdp_will_render.sql",
        ):
            await admin.execute((ROOT / "db/migrations" / name).read_text())
        # Empty auxiliary identity/store tables: the primary input is unclaimed
        # external retail supply, so there are no connected stores or overrides.
        await admin.execute("""
          CREATE TABLE merchant_stores (store_id text PRIMARY KEY, merchant_id text, platform text,
            domain text, status text, is_primary boolean, last_sync timestamptz, created_at timestamptz);
          CREATE TABLE pdp_identity_listing (product_id text, merchant_id text, source_listing_ref text,
            identity_status text, identity_confidence numeric, live_read_enabled boolean,
            review_required boolean, sellable_item_group_id text, product_line_id text, review_family_id text);
          CREATE TABLE pdp_identity_override (id text, source_listing_ref text, action_type text,
            active boolean, updated_at timestamptz, created_at timestamptz);
        """)
        database = databases.Database(url, server_settings={"search_path": schema, "timezone": getattr(request, "param", "UTC")})
        await database.connect()
        # DB injection only: actual resolver, queries, scorer, assembler,
        # classifier and trust policy all execute on the isolated schema.
        from services import catalog_sync_service, seller_identity
        monkeypatch.setattr(catalog_sync_service, "database", database)
        monkeypatch.setattr(seller_identity, "database", database)
        monkeypatch.setattr(evidence_db, "database", database)
        monkeypatch.setattr(dbmod, "database", database)
        monkeypatch.setattr(pivot_query_service, "database", database)
        monkeypatch.setattr(catalog_offer_writer_guard, "database", database)
        monkeypatch.setenv("ENABLE_INTAKE_IDENTITY_ENRICHMENT", "1")
        monkeypatch.setattr(apv, "_fetch_enrichment_for_canonical", AsyncMock(return_value=None))
        monkeypatch.setattr(outcome_aggregation_service, "seller_trust_bulk", AsyncMock(return_value={}))
        monkeypatch.setattr(indexnow, "schedule_submit_url", lambda *_args, **_kwargs: None)
        try:
            yield database
        finally:
            await database.disconnect()
    finally:
        await admin.execute("RESET search_path")
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


def source_plan(*, available=True, return_record=False):
    """Explicit merchant observation, with no GTIN recovery or guessed identity."""
    from services.curated_brand_feed import shopify_product_to_record
    from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl
    product = {
        "id": 930000001, "handle": "primary-readiness-lip-oil", "vendor": "A'PIEU",
        "title": "A'PIEU Honey Milk Lip Oil", "product_type": "Lip Oil",
        "body_html": "<p>A nourishing lip oil with a glossy finish and comfortable texture. Apply a thin layer directly to clean lips using the included applicator. Reapply when desired. This product is for external use only. Avoid contact with eyes.</p>",
        "tags": ["Lip Oil", "Makeup"],
        "images": [{"src": "https://cdn.example/primary-lip-oil.jpg"}],
        "variants": [{"id": 450000093000001, "price": "10.00", "available": available, "title": "Original"}],
    }
    record = shopify_product_to_record(product, domain="readiness-review.example", category_path="beauty",
                                      currency="USD", source_role="retailer", emit_native_variants=True)
    if return_record:
        return record
    plan = ingest_validated_jsonl([record])
    assert plan["pdps"][0]["gtin"] is None
    assert plan["pdps"][0]["pdp_lifecycle_stage"] == "published"
    return plan


async def read_artifacts(database, key):
    product = dict(await database.fetch_one("SELECT * FROM catalog_products WHERE product_key=:k", {"k": key}))
    quality = [dict(row) for row in await database.fetch_all(
        "SELECT * FROM product_quality_snapshot WHERE merchant_id=:m AND platform=:p AND platform_product_id=:s ORDER BY id",
        {"m": product["merchant_id"], "p": product["platform"], "s": product["source_product_id"]})]
    ck = product["content_key"]
    return {
        "product": product,
        "quality": quality,
        "view": await database.fetch_one("SELECT * FROM agent_pdp_view WHERE content_key=:c", {"c": ck}),
        "index": await database.fetch_one("SELECT * FROM index_pipeline_state WHERE content_key=:c", {"c": ck}),
        "trust": await database.fetch_one("SELECT * FROM catalog_row_trust WHERE subject_type='product' AND subject_key=:k", {"k": key}),
    }


@pytest.mark.parametrize("batch", [False, True])
async def test_fresh_primary_apply_builds_quality_view_index_without_seeded_eligibility(readiness_db, batch):
    from services.catalog_enrichment_agent.apply import apply_ingest_plan
    db = readiness_db
    plan = source_plan()
    for table in ("catalog_products", "product_quality_snapshot", "agent_pdp_view", "index_pipeline_state", "catalog_row_trust"):
        assert await db.fetch_val(f"SELECT count(*) FROM {table}") == 0
    snapshots = []
    for _ in range(2):
        counts = await apply_ingest_plan(copy.deepcopy(plan), db=db, batch=batch, batch_label="readiness_pg", primary_readiness=True)
        assert counts["primary_readiness"]["status"] == "complete"
        state = await read_artifacts(db, plan["pdps"][0]["product_key"])
        assert state["quality"] and state["quality"][-1]["content_quality_score"] is not None
        assert state["view"] and state["view"]["refreshed_at"]
        assert state["index"] and state["index"]["index_eligible"] is True
        assert state["index"]["serving_eligible"] is True
        assert state["trust"]
        snapshots.append((state["product"]["product_key"],state["product"]["content_key"],state["product"]["pivota_signature_id"]))
    assert snapshots[0] == snapshots[1]
    assert await db.fetch_val("SELECT count(*) FROM catalog_skus") == 2
    assert await db.fetch_val("SELECT count(*) FROM catalog_offers") == 2
    assert await db.fetch_val("SELECT count(*) FROM agent_pdp_view") == 1
    assert await db.fetch_val("SELECT count(*) FROM index_pipeline_state") == 1


async def test_sold_out_source_keeps_quality_but_is_not_buyable(readiness_db):
    from services.catalog_enrichment_agent.apply import apply_ingest_plan
    db = readiness_db
    plan = source_plan(available=False)
    counts = await apply_ingest_plan(plan, db=db, batch=False, batch_label="readiness_pg", primary_readiness=True)
    assert counts["primary_readiness"]["status"] == "complete"
    state = await read_artifacts(db, plan["pdps"][0]["product_key"])
    assert state["quality"][-1]["content_quality_score"] >= 71.4
    assert state["index"]["index_eligible"] is True
    offers = await db.fetch_all("SELECT availability,inventory_quantity FROM catalog_offers")
    assert len(offers) == 2
    assert all(row["availability"] == "out_of_stock" and row["inventory_quantity"] == 0 for row in offers)


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("stage,table", [("quality", "product_quality_snapshot"), ("apv", "agent_pdp_view"),
                                        ("index_state", "index_pipeline_state"), ("trust", "catalog_row_trust")])
async def test_real_readiness_write_denial_is_primary_failure(readiness_db, batch, stage, table):
    from services.catalog_enrichment_agent.apply import apply_ingest_plan
    from services.catalog_enrichment_agent.primary_readiness import PrimaryReadinessIncomplete
    db = readiness_db
    await db.execute("CREATE FUNCTION deny_readiness_write() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'test readiness write denied'; END $$")
    await db.execute(f"CREATE TRIGGER deny_readiness BEFORE INSERT OR UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION deny_readiness_write()")
    with pytest.raises(PrimaryReadinessIncomplete) as refused:
        await apply_ingest_plan(source_plan(), db=db, batch=batch, batch_label="readiness_denied", primary_readiness=True)
    assert refused.value.report["failed_stage"] == stage
    assert refused.value.persisted_counts["pdps"] == 1
    assert refused.value.persisted_counts["offers"] == 2
    assert await db.fetch_val(f"SELECT count(*) FROM {table}") == 0


async def test_fresh_public_twenty_records_complete_real_handoff(readiness_db):
    from services.catalog_enrichment_agent.apply import apply_ingest_plan
    from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl
    db = readiness_db
    records = [json.loads(line) for line in (ROOT / "tests/fixtures/curated_primary_readiness_public_records.jsonl").read_text().splitlines()]
    assert len(records) == 20
    # Captured native bulk source, no optional PDP identity rescue.
    plan = ingest_validated_jsonl(records)
    assert len(plan["pdps"]) == 20
    assert all(p["gtin"] is None for p in plan["pdps"])
    counts = await apply_ingest_plan(plan, db=db, batch=False, batch_label="readiness_public20", primary_readiness=True)
    assert counts["primary_readiness"]["status"] == "complete"
    assert await db.fetch_val("SELECT count(*) FROM product_quality_snapshot") == 20
    assert await db.fetch_val("SELECT count(*) FROM agent_pdp_view") == 20
    assert await db.fetch_val("SELECT count(*) FROM index_pipeline_state") == 20
    assert await db.fetch_val("SELECT count(*) FROM catalog_row_trust") == 20
    rows = await db.fetch_all("SELECT cp.title, ips.content_quality_score, ips.index_eligible,ips.serving_eligible,ips.blocker_code FROM catalog_products cp JOIN index_pipeline_state ips USING(content_key) ORDER BY cp.title")
    print("PUBLIC20_READINESS=" + json.dumps([dict(row) for row in rows]))
    assert all(row["content_quality_score"] is not None for row in rows)
    assert all(row["blocker_code"] == "none" and row["index_eligible"] is True for row in rows)
    # Actual stock remains independent: eligibility never invents an in-stock offer.
    native = await db.fetch_all("SELECT o.availability FROM catalog_offers o JOIN catalog_skus s USING(sku_key) WHERE s.sku_key <> s.product_key || '::canonical'")
    assert len(native) == 20
    assert sum(row["availability"] == "in_stock" for row in native) == 4
    assert sum(row["availability"] == "out_of_stock" for row in native) == 16


@pytest.mark.parametrize("readiness_db", ["America/Los_Angeles"], indirect=True)
async def test_primary_freshness_uses_actual_database_session_timezone(readiness_db):
    from services.catalog_enrichment_agent.apply import apply_ingest_plan
    counts = await apply_ingest_plan(source_plan(), db=readiness_db, batch=False,
                                    batch_label="readiness_timezone", primary_readiness=True)
    assert counts["primary_readiness"]["status"] == "complete"


async def test_queue_cannot_finish_after_real_quality_write_failure_and_retry_finishes(readiness_db, monkeypatch):
    from services import curated_brand_feed as feed, catalog_onboard_worker as worker
    from db import catalog_onboard_queue as queue
    db = readiness_db
    monkeypatch.setattr(worker, "records_for_brand", AsyncMock(return_value=feed.CuratedRecordBatch(
        [source_plan(return_record=True)], crawl_report={"status": "complete", "pages": 1})))
    job = await queue.enqueue(kind="curated_brand", dedup_key="readiness-real-pg", db=db, payload={
        "domain": "readiness-review.example", "brand": "A'PIEU", "only_vendors": ["A'PIEU"],
        "category_path": "beauty", "source_role": "retailer", "require_currency": "USD",
        "emit_real_variants": True,
    })
    assert job
    await db.execute("CREATE FUNCTION deny_quality() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'test quality storage denied'; END $$")
    await db.execute("CREATE TRIGGER deny_quality BEFORE INSERT ON product_quality_snapshot FOR EACH ROW EXECUTE FUNCTION deny_quality()")
    first = await worker.process_queue(limit=1, apply=True, db=db)
    assert first["done"] == 0 and first["failed"] == 1
    state = await db.fetch_one("SELECT status,error FROM catalog_onboard_queue WHERE id=:id", {"id": job})
    assert state["status"] == "pending" and "primary_readiness_incomplete" in state["error"]
    assert await db.fetch_val("SELECT count(*) FROM index_pipeline_state") == 0
    await db.execute("DROP TRIGGER deny_quality ON product_quality_snapshot")
    second = await worker.process_queue(limit=1, apply=True, db=db)
    assert second["done"] == 1 and second["failed"] == 0
    state = await db.fetch_one("SELECT status,result_jsonb FROM catalog_onboard_queue WHERE id=:id", {"id": job})
    assert state["status"] == "done"
    result = json.loads(state["result_jsonb"])
    assert result["applied"]["primary_readiness"]["status"] == "complete"
    assert await db.fetch_val("SELECT count(*) FROM catalog_products") == 1
    assert await db.fetch_val("SELECT count(*) FROM catalog_skus") == 2
    assert await db.fetch_val("SELECT count(*) FROM catalog_offers") == 2


async def test_changed_source_with_same_snapshot_timestamp_uses_latest_real_score(readiness_db):
    from services.catalog_enrichment_agent.apply import apply_ingest_plan
    from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl
    db = readiness_db
    first_plan = source_plan()
    record = source_plan(return_record=True)
    record["offers"][0]["image_url"] = ""
    second_plan = ingest_validated_jsonl([record])
    key = first_plan["pdps"][0]["product_key"]
    assert second_plan["pdps"][0]["product_key"] == key
    async with db.transaction():
        await apply_ingest_plan(first_plan, db=db, batch=False, batch_label="source_before", primary_readiness=True)
        await apply_ingest_plan(second_plan, db=db, batch=False, batch_label="source_after", primary_readiness=True)
        state = await read_artifacts(db, key)
        scores = state["quality"]
        assert len(scores) == 2 and scores[0]["snapshot_date"] == scores[1]["snapshot_date"]
        assert scores[1]["content_quality_score"] < scores[0]["content_quality_score"]
        assert state["index"]["content_quality_score"] == pytest.approx(scores[1]["content_quality_score"])
        assert state["index"]["index_eligible"] is False
