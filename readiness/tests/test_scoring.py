from __future__ import annotations

import json
from pathlib import Path

import pytest

from readiness.service import build_readiness_snapshot


_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _dump(model):
    return model.model_dump() if hasattr(model, "model_dump") else model.dict()


def _summary(snapshot):
    ready_variants = [
        variant.variant_id
        for product in snapshot.products
        for variant in product.variants
        if variant.channel_coverage.get("ucp") == "ready"
    ]
    blocked_variants = [
        variant.variant_id
        for product in snapshot.products
        for variant in product.variants
        if variant.channel_coverage.get("ucp") != "ready"
    ]
    return {
        "merchant_id": snapshot.merchant_id,
        "merchant_alpha_mode": snapshot.merchant_alpha_mode,
        "readiness_score": snapshot.readiness_score,
        "capability_status": snapshot.capability_status,
        "ready_variant_ids": ready_variants,
        "blocked_variant_ids": blocked_variants,
        "source_of_truth": snapshot.source_of_truth,
    }


@pytest.mark.asyncio
async def test_snapshot_scores_and_blocked_variant() -> None:
    snapshot = await build_readiness_snapshot("synthetic-demo-merchant", channel="ucp")

    assert snapshot.merchant_id == "synthetic-demo-merchant"
    assert snapshot.domain_scores["reviews_confidence"] == 0
    assert snapshot.channel_coverage[0].ready_variant_count == 3
    assert snapshot.channel_coverage[0].blocked_variant_count == 1

    blocked = [
        variant
        for product in snapshot.products
        for variant in product.variants
        if variant.checkout.status == "blocked"
    ]
    assert len(blocked) == 1
    assert blocked[0].variant_id == "var_serum_refill"
    assert "inventory_stale" in blocked[0].checkout.blockers
    assert "missing_shipping_profile" in blocked[0].checkout.blockers


@pytest.mark.asyncio
async def test_snapshot_matches_golden_fixture() -> None:
    snapshot = await build_readiness_snapshot("synthetic-demo-merchant", channel="ucp")
    golden = json.loads((_FIXTURES / "golden_readiness_report_ucp.json").read_text(encoding="utf-8"))

    assert _summary(snapshot) == golden
