import pytest

from services import commerce_index_checkout_validation_service as module


@pytest.mark.asyncio
async def test_price_delta_creates_live_quote_requirement_without_creating_a_quote(monkeypatch) -> None:
    writes = []

    async def fake_claim(**_kwargs):
        return {
            "job_id": "job_1", "change_id": "change_1", "merchant_id": "merchant_1",
            "scope_json": {"entity_type": "offer", "entity_id": "offer_1", "field_path": "pricing.merchant_effective_price"},
        }

    async def fake_complete(**_kwargs):
        return True

    async def fake_execute(query, values):
        writes.append((query, values))

    monkeypatch.setattr(module, "claim_next_publication_job", fake_claim)
    monkeypatch.setattr(module, "complete_publication_job", fake_complete)
    monkeypatch.setattr(module.database, "execute", fake_execute)

    result = await module.request_next_checkout_validation(worker_id="worker_1")

    assert result["status"] == "requires_live_quote"
    assert "catalog_quote_snapshots" not in writes[0][0]
    assert writes[0][1]["validation_policy"] == module.VALIDATION_POLICY
