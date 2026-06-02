"""T2b: POST /api/audits gates on merchant audit-readiness so a fresh merchant
doesn't get a false all-blocked first audit. `force=true` bypasses the gate.
Wires the previously-orphaned assess_merchant_audit_readiness (#722/#723).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import routes.audit_runs_routes as ar


def _patch(monkeypatch, *, ready, platform="shopify", calls=None):
    async def fake_platform(merchant_id, product_keys=None):
        if calls is not None:
            calls.append(("platform", merchant_id, tuple(product_keys or [])))
        return platform

    async def fake_assess(merchant_id, plat):
        if calls is not None:
            calls.append(("assess", merchant_id, plat))
        return {
            "ready": ready,
            "blocking_gaps": (
                [] if ready else
                ["product_quality_snapshot missing content_quality_score"]
            ),
            "counts": {
                "catalog_products": 4,
                "product_quality_snapshot": 4 if ready else 0,
            },
        }

    monkeypatch.setattr(ar, "_audit_readiness_platform", fake_platform)
    monkeypatch.setattr(ar, "assess_merchant_audit_readiness", fake_assess)


async def test_gate_blocks_when_not_ready(monkeypatch):
    from fastapi import HTTPException

    _patch(monkeypatch, ready=False)
    with pytest.raises(HTTPException) as ei:
        await ar._enforce_audit_readiness(
            merchant_id="m1", product_keys=["pk1"], force=False,
        )
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "merchant_not_audit_ready"
    assert "product_quality_snapshot" in str(ei.value.detail["blocking_gaps"])
    assert ei.value.detail["retry_after_seconds"] == 60
    assert ei.value.detail["platform"] == "shopify"


async def test_gate_passes_when_ready(monkeypatch):
    _patch(monkeypatch, ready=True)
    out = await ar._enforce_audit_readiness(
        merchant_id="m1", product_keys=["pk1"], force=False,
    )
    assert out is None  # no raise


async def test_force_bypasses_gate_without_probing(monkeypatch):
    calls = []
    _patch(monkeypatch, ready=False, calls=calls)
    # force=True -> no raise even though not ready, and the probe never runs.
    out = await ar._enforce_audit_readiness(
        merchant_id="m1", product_keys=["pk1"], force=True,
    )
    assert out is None
    assert calls == []  # short-circuits before any platform/probe lookup


async def test_platform_picks_dominant_from_catalog(monkeypatch):
    async def fake_fetch_all(query, *a, **k):
        return [{"platform": "shopify", "n": 9}, {"platform": "wix", "n": 2}]

    monkeypatch.setattr(ar.database, "fetch_all", fake_fetch_all)
    assert await ar._audit_readiness_platform("m1", ["pk1"]) == "shopify"


async def test_platform_defaults_to_shopify_when_empty(monkeypatch):
    async def fake_fetch_all(query, *a, **k):
        return []

    monkeypatch.setattr(ar.database, "fetch_all", fake_fetch_all)
    assert await ar._audit_readiness_platform("m1") == "shopify"
