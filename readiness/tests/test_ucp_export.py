from __future__ import annotations

import json
from pathlib import Path

import pytest

from readiness.service import build_channel_export


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
