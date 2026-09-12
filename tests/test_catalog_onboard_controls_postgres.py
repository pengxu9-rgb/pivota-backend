"""Execute the queue's real partial unique index and failure/retry transition."""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from services import catalog_onboard_worker as worker

URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL.startswith("postgres"), reason="requires test Postgres")
SOURCE = "test_onboard_controls_review"


@pytest.fixture
async def queue_db():
    import asyncpg
    from databases import Database

    dbname = urlsplit(URL).path.rsplit("/", 1)[-1]
    if "test" not in dbname and dbname != "pivota_dialect_check":
        pytest.skip("synthetic queue tests require a test database")
    connection = await asyncpg.connect(URL)
    try:
        await connection.execute((Path(__file__).resolve().parents[1] /
                                  "db/migrations/158_catalog_onboard_queue.sql").read_text())
    finally:
        await connection.close()
    db = Database(URL)
    await db.connect()
    try:
        yield db
    finally:
        await db.execute("DELETE FROM catalog_onboard_queue WHERE source=:s", {"s": SOURCE})
        await db.disconnect()


def _job(brand):
    return {"domain": "queue-controls.example", "brand": brand, "only_vendors": [brand],
            "source_role": "retailer", "category_path": "beauty/skincare", "require_currency": "USD"}


async def test_two_brand_subsets_survive_and_exact_duplicate_is_idempotent(queue_db):
    jobs = [_job("COSRX"), _job("APIEU"), _job("COSRX")]
    count = await worker.enqueue_curated_brands(jobs, priority_rank={}, source=SOURCE, db=queue_db)
    assert count == 2
    rows = await queue_db.fetch_all(
        "SELECT payload FROM catalog_onboard_queue WHERE source=:s", {"s": SOURCE})
    assert {json.loads(r["payload"])["brand"] for r in rows} == {"COSRX", "APIEU"}
    assert await worker.enqueue_curated_brands(jobs, priority_rank={}, source=SOURCE, db=queue_db) == 0


async def test_incomplete_crawl_requeues_without_applying_or_marking_done(queue_db, monkeypatch):
    await worker.enqueue_curated_brands([_job("COSRX")], priority_rank={}, source=SOURCE, db=queue_db)
    row = await queue_db.fetch_one(
        "SELECT id FROM catalog_onboard_queue WHERE source=:s", {"s": SOURCE})
    item_id = row["id"]

    # Claim only this fixture, without consuming another test module's jobs.
    async def claim(*args, **kwargs):
        record = await queue_db.fetch_one(
            "UPDATE catalog_onboard_queue SET status='processing', attempts=attempts+1 "
            "WHERE id=:id RETURNING *", {"id": item_id})
        return [dict(record)]

    async def incomplete(**kwargs):
        raise RuntimeError("crawl incomplete: page 2 refused; no partial records")

    monkeypatch.setattr(worker.q, "claim_batch", claim)
    monkeypatch.setattr(worker, "records_for_brand", incomplete)
    monkeypatch.setattr(worker, "ingest_validated_jsonl", lambda _: pytest.fail("partial crawl reached plan"))
    summary = await worker.process_queue(apply=True, db=queue_db)
    assert summary == {"claimed": 1, "done": 0, "failed": 1, "skipped": 0}
    state = await queue_db.fetch_one(
        "SELECT status, attempts, result_jsonb, error FROM catalog_onboard_queue WHERE id=:id", {"id": item_id})
    assert state["status"] == "pending" and state["attempts"] == 1
    assert state["result_jsonb"] is None and "page 2" in state["error"]
    await worker.process_queue(apply=True, db=queue_db)
    await worker.process_queue(apply=True, db=queue_db)
    state = await queue_db.fetch_one(
        "SELECT status, attempts FROM catalog_onboard_queue WHERE id=:id", {"id": item_id})
    assert state["status"] == "failed" and state["attempts"] == 3
