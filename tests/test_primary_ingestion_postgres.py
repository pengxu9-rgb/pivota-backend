"""Real PostgreSQL writer, SKU/offer joins, queue completion and repeat identity.

Reuses the existing isolated-row SKU dialect fixture. Source extraction is mocked;
actual plan SQL, row guards, queue SQL and primary canonical SELECT execute.
"""

import copy
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from services import catalog_onboard_worker as worker, curated_brand_feed as feed
from services.catalog_enrichment_agent import apply as writer
from tests.services.test_curated_gtin_handoff import captured_records
from tests.test_sku_identity_upserts_postgres import (
    db,
    PK,
    MERCHANT,
    VID,
    VID2,
    _planned_pdp,
    _planned_sku,
    _planned_offer,
    _plan,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"), reason="requires test Postgres"
)
SOURCE = "test_primary_ingestion_review"


@pytest.fixture
async def primary_db(db):
    import asyncpg

    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        await c.execute((Path(__file__).parents[1] / "db/migrations/158_catalog_onboard_queue.sql").read_text())
    finally:
        await c.close()
    await db.execute("ALTER TABLE product_group_members ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now()")
    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS test_primary_pgm_identity ON product_group_members(merchant_id, platform, platform_product_id)")
    # Expand the narrow seed fixture using the production writer bind columns.
    # Never drop shared dialect-test tables or erase another fixture's rows.
    from tests.test_sku_identity_upserts_postgres import _full_row

    for column in _full_row(writer._SEED_UPSERT_SQL):
        kind = {"seed_data": "jsonb", "price_amount": "double precision"}.get(column, "text")
        await db.execute(f"ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS {column} {kind}")
    await db.execute("ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now()")
    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS test_primary_seed_id ON external_product_seeds(id)")
    await db.execute(
        "INSERT INTO catalog_merchants (merchant_id,merchant_name,status,indexable) VALUES (:m,'Primary Review','active',true)",
        {"m": MERCHANT},
    )
    try:
        yield db
    finally:
        await db.execute("DELETE FROM catalog_onboard_queue WHERE source=:s", {"s": SOURCE})


def plan(*, broken=False):
    pdp = _planned_pdp()
    pdp.update(
        category_path="beauty/makeup/lip/lipstick",
        category="lipstick",
        product_type="lipstick",
        brand="Primary Review",
        pivota_signature_id="sig_0123456789abcdef0123456789abcdef",
        gtin="08809530070499",
        pdp_lifecycle_stage="published",
    )
    skus = [_planned_sku(vid=VID, barcode="08809530070499"), _planned_sku(vid=VID2, barcode="08809530070499")]
    if broken:
        for s in skus if broken == "all" else skus[:1]:
            s["title"] = None  # Real Postgres NOT NULL failure, not mocked counts.
    return _plan(skus, [_planned_offer(sku_key=s["sku_key"]) for s in skus], pdps=[pdp])


async def install_job(db, monkeypatch, planned, *, batch=False, records=None):
    monkeypatch.setattr(
        worker,
        "records_for_brand",
        AsyncMock(
            return_value=feed.CuratedRecordBatch(
                records if records is not None else [{"pdp": {"currency": "USD"}}],
                crawl_report={"status": "complete", "pages": 1},
            )
        ),
    )
    if planned is not None:
        monkeypatch.setattr(worker, "ingest_validated_jsonl", lambda _: copy.deepcopy(planned))
    real = writer.apply_ingest_plan

    async def apply(*args, **kwargs):
        # These legacy fixtures isolate writer/SKU/queue SQL. The fresh primary
        # handoff is exercised without mocks in test_curated_primary_readiness_postgres.
        kwargs["primary_readiness"] = False
        return await real(*args, batch=batch, **kwargs)

    monkeypatch.setattr(worker, "apply_ingest_plan", apply)
    await worker.enqueue_curated_brands(
        [{"domain": "brand.example", "category_path": "beauty/makeup/lip/lipstick"}],
        priority_rank={},
        source=SOURCE,
        db=db,
    )
    job = await db.fetch_one("SELECT id FROM catalog_onboard_queue WHERE source=:s", {"s": SOURCE})

    async def claim(*args, **kwargs):
        row = await db.fetch_one(
            "UPDATE catalog_onboard_queue SET status='processing', attempts=attempts+1 WHERE id=:id RETURNING *",
            {"id": job["id"]},
        )
        return [dict(row)]

    monkeypatch.setattr(worker.q, "claim_batch", claim)
    return job["id"]


@pytest.mark.parametrize("batch", [False, True])
async def test_real_primary_write_and_repeat_preserve_usable_native_chain(primary_db, monkeypatch, batch):
    from services import pivot_query_service as primary

    db = primary_db
    job = await install_job(db, monkeypatch, plan(), batch=batch)
    first = await worker.process_queue(apply=True, db=db)
    assert first["done"] == 1 and first["failed"] == 0
    keys = []
    for _ in range(2):
        rows = await primary._fetch_canonical_search_rows(
            query="Primary Review", merchant_id=None, limit=10, require_signature=True
        )
        rows = [r for r in rows if r["product_key"] == PK]
        assert {r["source_variant_id"] for r in rows} == {VID, VID2}
        assert all(r["offer_merchant_id"] == MERCHANT and r["currency"] == "USD" for r in rows)
        assert {r["barcode"] for r in rows} == {"08809530070499"}
        assert {r["category"] for r in rows} == {"lipstick"}
        keys.append(sorted((r["product_key"], r["sku_key"], r["offer_id"]) for r in rows))
        if len(keys) == 1:
            assert (await worker.process_queue(apply=True, db=db))["done"] == 1
    assert keys[0] == keys[1]
    assert await db.fetch_val("SELECT count(*) FROM catalog_skus WHERE product_key=:p", {"p": PK}) == 2
    assert await db.fetch_val("SELECT count(*) FROM catalog_offers WHERE product_key=:p", {"p": PK}) == 2
    state = await db.fetch_one("SELECT status,result_jsonb FROM catalog_onboard_queue WHERE id=:id", {"id": job})
    result = json.loads(state["result_jsonb"])
    assert state["status"] == "done" and result["primary_ingestion"]["status"] == "applied"
    assert result["primary_ingestion"]["primary_search_verified"] is False


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("broken", ["all", "one"])
async def test_real_sku_failure_cannot_finish_queue_or_leave_orphan_offers(primary_db, monkeypatch, batch, broken):
    db = primary_db
    job = await install_job(db, monkeypatch, plan(broken=broken), batch=batch)
    outcome = await worker.process_queue(apply=True, db=db)
    assert outcome["done"] == 0 and outcome["failed"] == 1
    state = await db.fetch_one("SELECT status,error,result_jsonb FROM catalog_onboard_queue WHERE id=:id", {"id": job})
    assert state["status"] == "pending" and state["result_jsonb"] is None
    assert (
        "incomplete_primary_writes" in state["error"] and f'"offers": {0 if broken == "all" else 1}' in state["error"]
    )
    assert await db.fetch_val("SELECT count(*) FROM catalog_offers WHERE product_key=:p", {"p": PK}) == (
        0 if broken == "all" else 1
    )
    assert await db.fetch_val("SELECT count(*) FROM catalog_skus WHERE product_key=:p", {"p": PK}) == (
        0 if broken == "all" else 1
    )


async def test_real_mapper_plan_writer_primary_reader_retains_two_retailers(primary_db, monkeypatch):
    from services import pivot_query_service as primary
    from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl

    records = captured_records()
    planned = ingest_validated_jsonl(records)
    keys = [p["product_key"] for p in planned["pdps"]]
    seller_ids = [m["merchant_id"] for m in planned["merchants"]]
    db = primary_db
    try:
        # Unlike the executor-failure fixture, the real pure planner runs here.
        await install_job(db, monkeypatch, None, records=records)
        assert (await worker.process_queue(apply=True, db=db))["done"] == 1
        before = []
        for iteration in range(2):
            rows = await primary._fetch_canonical_search_rows(
                query="A'PIEU", merchant_id=None, limit=30, require_signature=True
            )
            rows = [r for r in rows if r["product_key"] in keys]
            native = [r for r in rows if r["source_variant_id"] in {"43603819692287", "41807436316855"}]
            assert len(native) == 2
            assert {r["offer_source_domain"] for r in native} == {"eyurs.com", "asianbeautyessentials.com"}
            assert {r["barcode"] for r in native} == {"8809530070499"}
            assert {r["currency"] for r in native} == {"USD"}
            assert {r["category"] for r in native} == {"oil"}
            assert all(len(r["pivota_signature_id"]) == 36 for r in native)
            before.append(sorted((r["product_key"], r["sku_key"], r["offer_id"]) for r in rows))
            if iteration == 0:
                assert (await worker.process_queue(apply=True, db=db))["done"] == 1
        assert before[0] == before[1] and len(before[0]) == 4
        assert (
            await db.fetch_val(
                "SELECT count(*) FROM catalog_products WHERE product_key=ANY(:k) AND gtin=:g",
                {"k": keys, "g": "08809530070499"},
            )
            == 2
        )
        assert (
            await db.fetch_val(
                "SELECT count(*) FROM external_product_seeds WHERE attached_product_key=ANY(:k)", {"k": keys}
            )
            == 2
        )
    finally:
        for table in ("catalog_offers", "catalog_skus", "catalog_products"):
            await db.execute(f"DELETE FROM {table} WHERE product_key=ANY(:k)", {"k": keys})
        await db.execute("DELETE FROM external_product_seeds WHERE attached_product_key=ANY(:k)", {"k": keys})
        await db.execute("DELETE FROM catalog_merchants WHERE merchant_id=ANY(:m)", {"m": seller_ids})


@pytest.mark.parametrize("batch_mode", [False, True])
async def test_enabled_identity_two_identical_native_listings_replay_without_seller_collapse(primary_db, monkeypatch, batch_mode):
    """Real planner + resolver + SQL + canonical SELECT; no recovered PDP barcodes."""
    from services import intake_identity as identity, pivot_query_service as primary
    from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl
    from tests.services.test_retailer_adversarial_acceptance import record

    db = primary_db
    # Production identity-event and group membership shape for the enabled path.
    import asyncpg
    connection = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        await connection.execute((Path(__file__).parents[1] / "db/migrations/177_intake_identity_events.sql").read_text())
    finally:
        await connection.close()
    await db.execute("ALTER TABLE product_group_members ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now()")
    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS test_primary_pgm_identity ON product_group_members(merchant_id, platform, platform_product_id)")
    monkeypatch.setenv("ENABLE_INTAKE_IDENTITY_ENRICHMENT", "1")
    records = [record(host) for host in ("first-review.example", "second-review.example")]
    planned = ingest_validated_jsonl(records)
    keys = [p["product_key"] for p in planned["pdps"]]
    sellers = [m["merchant_id"] for m in planned["merchants"]]
    outcomes = []
    real_resolve = identity.resolve_or_attach_content_identity

    async def observe(**kwargs):
        outcome = await real_resolve(**kwargs)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(identity, "resolve_or_attach_content_identity", observe)
    try:
        await install_job(db, monkeypatch, None, records=records, batch=batch_mode)
        snapshots = []
        for _ in range(2):
            assert (await worker.process_queue(apply=True, db=db))["done"] == 1
            rows = await primary._fetch_canonical_search_rows(query="A'PIEU", merchant_id=None, limit=30, require_signature=True)
            rows = [dict(row) for row in rows if row["product_key"] in keys]
            native = [row for row in rows if row["source_variant_id"] == "45000000000001"]
            assert len(native) == 2
            assert {row["offer_source_domain"] for row in native} == {"first-review.example", "second-review.example"}
            snapshots.append(sorted((row["product_key"], row["sku_key"], row["offer_id"]) for row in rows))
            owners = await db.fetch_all("SELECT p.merchant_id AS owner, s.merchant_id AS sku_owner FROM catalog_products p JOIN catalog_skus s USING(product_key) WHERE p.product_key=ANY(:keys)", {"keys": keys})
            assert len(owners) == 4
            assert all(row["owner"] == row["sku_owner"] for row in owners)
            assert len({row["owner"] for row in owners}) == 2
        assert snapshots[0] == snapshots[1] and len(snapshots[0]) == 4
        assert len(outcomes) == 4
        assert all(outcome["evidence"]["evidence"].get("reason") != "error" for outcome in outcomes)
        assert any(outcome["evidence"].get("matcher") == "gtin_match" for outcome in outcomes)
        assert len({outcome["content_key"] for outcome in outcomes}) == 1
    finally:
        for table in ("catalog_offers", "catalog_skus", "catalog_products"):
            await db.execute(f"DELETE FROM {table} WHERE product_key=ANY(:k)", {"k": keys})
        await db.execute("DELETE FROM external_product_seeds WHERE attached_product_key=ANY(:k)", {"k": keys})
        await db.execute("DELETE FROM intake_identity_events WHERE product_key=ANY(:k)", {"k": keys})
        await db.execute("DELETE FROM product_group_members WHERE merchant_id=ANY(:s)", {"s": sellers})
        await db.execute("DELETE FROM catalog_merchants WHERE merchant_id=ANY(:s)", {"s": sellers})


@pytest.mark.parametrize("batch_mode", [False, True])
async def test_primary_group_write_failure_cannot_complete_or_publish_children(primary_db, monkeypatch, batch_mode):
    from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl
    from services.catalog_enrichment_agent.primary_ingestion import require_primary_plan, require_primary_apply, PrimaryIngestionIncomplete
    from tests.services.test_retailer_adversarial_acceptance import record

    db = primary_db
    planned = ingest_validated_jsonl([record("group-failure-review.example")])
    report = require_primary_plan(planned)
    keys = [p["product_key"] for p in planned["pdps"]]
    sellers = [m["merchant_id"] for m in planned["merchants"]]

    class RefuseGroupWrite:
        is_connected = True
        def __getattr__(self, name):
            return getattr(db, name)
        async def execute(self, query, values=None):
            if "INSERT INTO product_group_members" in str(query):
                raise RuntimeError("test injected membership write denial")
            return await db.execute(query, values)

    try:
        counts = await writer.apply_ingest_plan(planned, batch_label="group-failure-review", db=RefuseGroupWrite(), batch=batch_mode)
        assert counts["pdps"] == 1
        assert counts["product_groups_failed"] == 1
        assert counts["skus"] == counts["offers"] == counts["seeds"] == 0
        with pytest.raises(PrimaryIngestionIncomplete):
            require_primary_apply(report, counts)
        assert await db.fetch_val("SELECT count(*) FROM catalog_offers WHERE product_key=ANY(:k)", {"k": keys}) == 0
        assert await db.fetch_val("SELECT count(*) FROM product_group_members WHERE merchant_id=ANY(:s)", {"s": sellers}) == 0
    finally:
        for table in ("catalog_offers", "catalog_skus", "catalog_products"):
            await db.execute(f"DELETE FROM {table} WHERE product_key=ANY(:k)", {"k": keys})
        await db.execute("DELETE FROM external_product_seeds WHERE attached_product_key=ANY(:k)", {"k": keys})
        await db.execute("DELETE FROM catalog_merchants WHERE merchant_id=ANY(:s)", {"s": sellers})
