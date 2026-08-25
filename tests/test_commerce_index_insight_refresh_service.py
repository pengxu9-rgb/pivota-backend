import pytest

from services import commerce_index_insight_refresh_service as module


@pytest.mark.asyncio
async def test_delta_becomes_review_request_without_publishing_insights(monkeypatch) -> None:
    writes = []

    async def fake_claim(**_kwargs):
        return {
            "job_id": "job_1", "change_id": "change_1", "merchant_id": "merchant_1",
            "scope_json": {"entity_type": "product", "entity_id": "product_1", "field_path": "content.title"},
        }

    async def fake_complete(**_kwargs):
        return True

    async def fake_execute(query, values):
        writes.append((query, values))

    monkeypatch.setattr(module, "claim_next_publication_job", fake_claim)
    monkeypatch.setattr(module, "complete_publication_job", fake_complete)
    monkeypatch.setattr(module.database, "execute", fake_execute)

    result = await module.request_next_insight_refresh(worker_id="worker_1")

    assert result["status"] == "pending_review"
    assert "aurora_product_intel_kb" not in writes[0][0]
    assert writes[0][1]["review_policy"] == module.REVIEW_POLICY
