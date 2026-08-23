import pytest

from db import commerce_index_publication_jobs as module


@pytest.mark.asyncio
async def test_claim_uses_skip_locked_and_returns_the_claimed_job() -> None:
    captured = {}

    class FakeDB:
        async def fetch_one(self, query, values):
            captured["query"] = query
            captured["values"] = values
            return {"job_id": "job_1", "target": "relation_graph", "attempts": 1}

    result = await module.claim_next_publication_job(
        target="relation_graph", worker_id="worker_a", db=FakeDB()
    )

    assert result == {"job_id": "job_1", "target": "relation_graph", "attempts": 1}
    assert "FOR UPDATE SKIP LOCKED" in captured["query"]
    assert captured["values"]["worker_id"] == "worker_a"


@pytest.mark.asyncio
async def test_failure_requeues_only_the_lease_holder_job() -> None:
    captured = {}

    class FakeDB:
        async def fetch_one(self, query, values):
            captured["query"] = query
            captured["values"] = values
            return {"job_id": "job_1"}

    completed = await module.complete_publication_job(
        job_id="job_1", worker_id="worker_a", error_message="temporary downstream error", db=FakeDB()
    )

    assert completed is True
    assert "claimed_by = :worker_id" in captured["query"]
    assert captured["values"]["status"] == "pending"
    assert captured["values"]["error_message"] == "temporary downstream error"
