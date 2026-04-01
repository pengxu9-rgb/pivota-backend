from __future__ import annotations

import pytest

from readiness.models import MerchantReadinessSnapshot, MerchantSourceDataset
from readiness.service import (
    build_readiness_snapshot,
    get_readiness_snapshot_cache_metrics,
    invalidate_readiness_snapshot_cache,
    reset_readiness_snapshot_cache_observability,
)


@pytest.fixture(autouse=True)
def _reset_snapshot_cache():
    reset_readiness_snapshot_cache_observability()


@pytest.mark.asyncio
async def test_build_readiness_snapshot_uses_cache(monkeypatch):
    call_counts = {
        "dataset": 0,
        "snapshot": 0,
    }

    async def fake_load_dataset(merchant_id: str):
        assert merchant_id == "merch_test"
        call_counts["dataset"] += 1
        return MerchantSourceDataset(
            merchant_id=merchant_id,
            merchant_name="Test Merchant",
            evaluation_reference_time="2026-04-01T00:00:00Z",
        )

    def fake_build_snapshot(dataset: MerchantSourceDataset, channel: str = "ucp"):
        call_counts["snapshot"] += 1
        return MerchantReadinessSnapshot(
            merchant_id=dataset.merchant_id,
            merchant_name=dataset.merchant_name,
            channel=channel,
            generated_at="2026-04-01T00:00:00Z",
            merchant_alpha_mode="real_merchant_alpha",
            readiness_score=80,
            products=[],
        )

    monkeypatch.setattr("readiness.service.load_merchant_source_dataset", fake_load_dataset)
    monkeypatch.setattr("readiness.service.build_merchant_snapshot", fake_build_snapshot)

    first = await build_readiness_snapshot("merch_test", channel="ucp")
    second = await build_readiness_snapshot("merch_test", channel="ucp")

    assert first.generated_at == second.generated_at
    assert call_counts["dataset"] == 1
    assert call_counts["snapshot"] == 1

    metrics = get_readiness_snapshot_cache_metrics()
    assert metrics["hits"] == 1
    assert metrics["misses"] == 1
    assert metrics["stores"] == 1
    assert metrics["entries"] == 1


@pytest.mark.asyncio
async def test_build_readiness_snapshot_force_refresh_invalidates_cache(monkeypatch):
    build_count = {"value": 0}

    async def fake_load_dataset(_merchant_id: str):
        return MerchantSourceDataset(
            merchant_id="merch_test",
            merchant_name="Test Merchant",
            evaluation_reference_time="2026-04-01T00:00:00Z",
        )

    def fake_build_snapshot(dataset: MerchantSourceDataset, channel: str = "ucp"):
        build_count["value"] += 1
        return MerchantReadinessSnapshot(
            merchant_id=dataset.merchant_id,
            merchant_name=dataset.merchant_name,
            channel=channel,
            generated_at=f"2026-04-01T00:00:0{build_count['value']}Z",
            merchant_alpha_mode="real_merchant_alpha",
            readiness_score=80,
            products=[],
        )

    monkeypatch.setattr("readiness.service.load_merchant_source_dataset", fake_load_dataset)
    monkeypatch.setattr("readiness.service.build_merchant_snapshot", fake_build_snapshot)

    first = await build_readiness_snapshot("merch_test", channel="ucp")
    second = await build_readiness_snapshot("merch_test", channel="ucp", force_refresh=True)

    assert first.generated_at != second.generated_at
    assert build_count["value"] == 2
    assert invalidate_readiness_snapshot_cache("merch_test", channel="ucp") == 1
