from __future__ import annotations

import json
from pathlib import Path

import pytest

from readiness.models import CapabilityStatus, MerchantReadinessSnapshot, ReadyProduct, ReadyVariant
from readiness.service import build_channel_export, build_export_summary_response, build_readiness_snapshot


_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _dump(model):
    return model.model_dump() if hasattr(model, "model_dump") else model.dict()


def _summary(report):
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
async def test_ucp_export_filters_blocked_variants() -> None:
    report = await build_channel_export("synthetic-demo-merchant", channel="ucp")

    assert report.channel == "ucp"
    assert len(report.offers) == 3
    offer_ids = {offer["offer_id"] for offer in report.offers}
    assert "ucp:synthetic-demo-merchant:prod_peptide_serum:var_serum_refill" not in offer_ids


@pytest.mark.asyncio
async def test_ucp_export_matches_golden_fixture() -> None:
    report = await build_channel_export("synthetic-demo-merchant", channel="ucp")
    golden = json.loads((_FIXTURES / "golden_ucp_export.json").read_text(encoding="utf-8"))

    assert _summary(report) == golden


@pytest.mark.asyncio
async def test_acp_export_filters_blocked_variants() -> None:
    report = await build_channel_export("synthetic-demo-merchant", channel="acp")

    assert report.channel == "acp"
    assert report.export_version == "readiness_acp_export.v1"
    assert report.servable_product_count >= 1
    assert report.servable_variant_count >= 1
    offer_ids = {offer["offer_id"] for offer in report.offers}
    assert "acp:synthetic-demo-merchant:prod_peptide_serum:var_serum_refill" not in offer_ids


@pytest.mark.asyncio
async def test_export_summary_includes_visible_attribute_coverage() -> None:
    snapshot = await build_readiness_snapshot("synthetic-demo-merchant", channel="ucp")
    summary = build_export_summary_response(snapshot, channel="ucp")

    assert summary["servable_product_count_by_category"]["serum"] >= 1
    assert summary["visible_attribute_coverage"]["product_category"]["cleanser"] >= 1
    assert summary["visible_attribute_coverage"]["skin_concern"]["hydrating"] >= 1
    assert "ingredient_coverage_by_category" in summary
    assert "shade_coverage_by_category" in summary
    assert set(summary["ingredient_coverage_by_category"].keys()) == {"serum", "moisturizer", "cleanser", "toner"}
    assert set(summary["shade_coverage_by_category"].keys()) == {"foundation", "lipstick", "blush", "gloss"}


def test_export_summary_tracks_structured_ingredient_and_shade_coverage() -> None:
    snapshot = MerchantReadinessSnapshot(
        merchant_id="merch_test_1",
        merchant_name="Merchant Test",
        channel="ucp",
        generated_at="2026-03-22T00:00:00Z",
        readiness_score=80,
        products=[
            ReadyProduct(
                product_id="prod_serum_1",
                title="Niacinamide Serum",
                category="Serum",
                visible_attributes={"product_category": ["serum"]},
                ingredient_ids=["niacinamide"],
                variants=[
                    ReadyVariant(
                        variant_id="var_serum_1",
                        title="Default",
                        price={"amount": 29.0, "currency": "USD"},
                        inventory={"quantity": 8, "availability": "in_stock"},
                        discovery=CapabilityStatus(capability="discovery", status="ready", score=100),
                        checkout=CapabilityStatus(capability="checkout", status="ready", score=100),
                        channel_coverage={"ucp": "ready"},
                    )
                ],
            ),
            ReadyProduct(
                product_id="prod_foundation_1",
                title="Soft Focus Foundation",
                category="Foundation",
                variants=[
                    ReadyVariant(
                        variant_id="var_foundation_1",
                        title="Shade 210",
                        visible_option_labels=["shade_210"],
                        price={"amount": 39.0, "currency": "USD"},
                        inventory={"quantity": 6, "availability": "in_stock"},
                        discovery=CapabilityStatus(capability="discovery", status="ready", score=100),
                        checkout=CapabilityStatus(capability="checkout", status="ready", score=100),
                        channel_coverage={"ucp": "ready"},
                    )
                ],
            ),
        ],
    )

    summary = build_export_summary_response(snapshot, channel="ucp")

    assert summary["ingredient_coverage_by_category"]["serum"] == 1
    assert summary["shade_coverage_by_category"]["foundation"] == 1
