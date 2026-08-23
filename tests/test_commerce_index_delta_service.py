from datetime import datetime, timezone

import pytest
from sqlalchemy.dialects import postgresql

from services import commerce_index_delta_service as module
from services.commerce_index_v2 import FieldObservation


@pytest.mark.asyncio
async def test_changed_taxonomy_creates_only_affected_publication_jobs(monkeypatch) -> None:
    writes = []

    async def fake_execute(query, values=None):
        writes.append(str(query.compile(dialect=postgresql.dialect())))

    monkeypatch.setattr(module.database, "execute", fake_execute)
    observation = FieldObservation(
        entity_type="product",
        entity_id="product_123",
        field_family="taxonomy",
        field_key="category",
        value="Moisturizer",
        source_system="shopify_products_sync",
        source_kind="merchant_api",
        source_ref="shopify:event:123",
        observed_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
        confidence=1.0,
    )

    result = await module.record_field_change_and_publications(
        merchant_id="merchant_123",
        observation=observation,
        previous_value="Serum",
        source_id="source_shopify_123",
    )

    assert result["changed"] is True
    assert len(result["job_ids"]) == 3
    assert len(writes) == 4
    assert all("INSERT INTO" in query for query in writes)


@pytest.mark.asyncio
async def test_unchanged_observation_does_not_write_a_delta(monkeypatch) -> None:
    async def should_not_write(*_args, **_kwargs):
        raise AssertionError("unchanged facts must not create publication work")

    monkeypatch.setattr(module.database, "execute", should_not_write)
    observation = FieldObservation(
        entity_type="sku",
        entity_id="sku_123",
        field_family="inventory",
        field_key="quantity",
        value=8,
        source_system="merchant_erp",
        source_kind="pim_erp_pos",
        observed_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
        confidence=1.0,
    )

    result = await module.record_field_change_and_publications(
        merchant_id="merchant_123", observation=observation, previous_value=8
    )

    assert result == {"changed": False, "reason": "value_unchanged", "change_id": None, "job_ids": []}


@pytest.mark.asyncio
async def test_changed_observation_uses_idempotent_change_and_job_inserts(monkeypatch) -> None:
    writes = []

    async def fake_execute(query, values=None):
        writes.append(str(query.compile(dialect=postgresql.dialect())))

    monkeypatch.setattr(module.database, "execute", fake_execute)
    observation = FieldObservation(
        entity_type="product",
        entity_id="product_123",
        field_family="content",
        field_key="description",
        value="Updated seller description",
        source_system="shopify_products_sync",
        source_kind="merchant_api",
        source_ref="shopify:event:123",
        observed_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
        confidence=1.0,
    )

    await module.record_field_change_and_publications(
        merchant_id="merchant_123", observation=observation, previous_value="Old description"
    )

    assert len(writes) == 4
    assert all("ON CONFLICT" in query and "DO NOTHING" in query for query in writes)
