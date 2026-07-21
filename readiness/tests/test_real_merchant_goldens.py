from __future__ import annotations

import json
from pathlib import Path

import pytest

from readiness.service import build_channel_export, build_readiness_snapshot
from readiness.tests.conftest import ALPHA_MERCHANT_ID as DEFAULT_ALPHA_MERCHANT_ID
from readiness.tests.test_routes import _install_live_source_mocks


_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _snapshot_summary(snapshot):
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


def _export_summary(report):
    return {
        "merchant_id": report.merchant_id,
        "merchant_alpha_mode": report.merchant_alpha_mode,
        "readiness_score": report.readiness_score,
        "capability_status": report.capability_status,
        "offer_ids": [offer["offer_id"] for offer in report.offers],
        "validation_warnings": report.validation_warnings,
        "source_of_truth": report.source_of_truth,
    }


@pytest.mark.asyncio
async def test_real_merchant_snapshot_matches_golden(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_READINESS_REAL_MERCHANT_ALPHA", "true")
    monkeypatch.setenv("READINESS_ALPHA_MERCHANT_ID", DEFAULT_ALPHA_MERCHANT_ID)
    _install_live_source_mocks(monkeypatch, psp_enabled=True)

    snapshot = await build_readiness_snapshot(DEFAULT_ALPHA_MERCHANT_ID, channel="ucp")
    golden = json.loads(
        (_FIXTURES / "golden_real_merchant_readiness_report_ucp.json").read_text(encoding="utf-8")
    )

    assert _snapshot_summary(snapshot) == golden


@pytest.mark.asyncio
async def test_real_merchant_export_matches_golden(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_READINESS_REAL_MERCHANT_ALPHA", "true")
    monkeypatch.setenv("READINESS_ALPHA_MERCHANT_ID", DEFAULT_ALPHA_MERCHANT_ID)
    _install_live_source_mocks(monkeypatch, psp_enabled=True)

    report = await build_channel_export(DEFAULT_ALPHA_MERCHANT_ID, channel="ucp")
    golden = json.loads((_FIXTURES / "golden_real_merchant_ucp_export.json").read_text(encoding="utf-8"))

    assert _export_summary(report) == golden
