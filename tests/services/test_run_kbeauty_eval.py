from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import run_kbeauty_decision_grade_eval as runner


class _Offer:
    def __init__(self, data):
        self._data = data

    def model_dump(self):
        return self._data


class _OffersResponse:
    def __init__(self, best):
        self.best_us_offer = best


async def _fake_payload(product_key, sku_key):
    return {
        "category_kind": "skincare",
        "concerns": ["dryness"],
        "active_ingredients": [{"label": "Niacinamide"}],
        "skincare_format": "serum",
        "evidence_profile": {
            "claims": [
                {"claim_text": "hydrates", "source_ref": "s1", "substantiation_status": "substantiated"}
            ]
        },
        "required_disclaimers": [],
    }


async def _fake_offers(request):
    return _OffersResponse(_Offer({"is_first_party": True, "price": "29.00"}))


async def _fake_resolve(product_key, *, db=None):
    return f"{product_key}::sku"


def test_drive_aggregates_pivota_vs_native(monkeypatch):
    monkeypatch.setattr(runner, "_resolve_representative_sku_key", _fake_resolve)
    monkeypatch.setattr(runner, "_fetch_beauty_vertical_payload", _fake_payload)
    monkeypatch.setattr(runner, "resolve_pivot_offers", _fake_offers)
    fake_db = types.SimpleNamespace(is_connected=True)
    args = types.SimpleNamespace(product_keys=["pk1", "pk2"])

    report = asyncio.run(runner._drive(args, db=fake_db))

    agg = report["aggregate"]
    assert agg["n"] == 2
    assert agg["errors"] == 0
    assert agg["pivota_decision_grade_rate"] == 1.0
    assert agg["pivota_overall_avg"] > agg["native_overall_avg"]
    assert agg["avg_overall_advantage"] > 0
    assert report["per_sku"][0]["pivota_decision_grade"] is True


def test_drive_breaks_down_by_category(monkeypatch):
    async def _payload_by_key(product_key, sku_key):
        if product_key == "hair1":
            return {
                "category_kind": "haircare",
                "concerns": ["color-treated"],
                "active_ingredients": [{"label": "Rice Protein"}],
                "haircare_format": "shampoo",
                "vegan_status": "verified",
                "cruelty_free_status": "verified",
                "evidence_profile": {
                    "claims": [
                        {"claim_text": "strengthens", "source_ref": "s1", "substantiation_status": "substantiated"}
                    ]
                },
                "required_disclaimers": [],
            }
        return await _fake_payload(product_key, sku_key)

    monkeypatch.setattr(runner, "_resolve_representative_sku_key", _fake_resolve)
    monkeypatch.setattr(runner, "_fetch_beauty_vertical_payload", _payload_by_key)
    monkeypatch.setattr(runner, "resolve_pivot_offers", _fake_offers)
    fake_db = types.SimpleNamespace(is_connected=True)
    args = types.SimpleNamespace(product_keys=["pk1", "hair1"])

    report = asyncio.run(runner._drive(args, db=fake_db))

    assert set(report["by_category"]) == {"skincare", "haircare"}
    assert report["by_category"]["skincare"]["n"] == 1
    assert report["by_category"]["haircare"]["n"] == 1
    assert report["by_category"]["haircare"]["avg_overall_advantage"] > 0
    assert report["aggregate"]["n"] == 2


def test_drive_captures_per_sku_errors(monkeypatch):
    async def _boom(product_key, sku_key):
        raise RuntimeError("no such product")

    monkeypatch.setattr(runner, "_resolve_representative_sku_key", _fake_resolve)
    monkeypatch.setattr(runner, "_fetch_beauty_vertical_payload", _boom)
    monkeypatch.setattr(runner, "resolve_pivot_offers", _fake_offers)
    fake_db = types.SimpleNamespace(is_connected=True)
    args = types.SimpleNamespace(product_keys=["bad"])

    report = asyncio.run(runner._drive(args, db=fake_db))
    assert report["aggregate"]["n"] == 0
    assert report["aggregate"]["errors"] == 1
    assert "error" in report["per_sku"][0]


def test_resolve_representative_sku_key_queries_ingredient_row():
    class _DB:
        def __init__(self, row):
            self._row = row
            self.params = None

        async def fetch_one(self, query, params):
            self.params = params
            assert "beauty_sku_ingredients" in query
            return self._row

    found = _DB({"sku_key": "pk1::sku7"})
    sku = asyncio.run(runner._resolve_representative_sku_key("pk1", db=found))
    assert sku == "pk1::sku7"
    assert found.params == {"product_key": "pk1"}

    # No ingredient row -> None (nothing to load), not a crash.
    missing = _DB(None)
    assert asyncio.run(runner._resolve_representative_sku_key("pk2", db=missing)) is None


def test_assemble_record_passes_resolved_sku_key_into_payload(monkeypatch):
    captured = {}

    async def _capturing_payload(product_key, sku_key):
        captured["sku_key"] = sku_key
        # active_ingredients load only because a real sku_key was resolved.
        return {
            "category_kind": "skincare",
            "concerns": ["dryness"],
            "active_ingredients": [{"label": "Niacinamide"}],
            "skincare_format": "serum",
            "evidence_profile": {
                "claims": [{"claim_text": "hydrates", "source_ref": "s1", "substantiation_status": "substantiated"}]
            },
            "required_disclaimers": [],
        }

    monkeypatch.setattr(runner, "_resolve_representative_sku_key", _fake_resolve)
    monkeypatch.setattr(runner, "_fetch_beauty_vertical_payload", _capturing_payload)
    monkeypatch.setattr(runner, "resolve_pivot_offers", _fake_offers)

    rec = asyncio.run(runner._assemble_record("pk1", db=types.SimpleNamespace()))
    assert captured["sku_key"] == "pk1::sku"  # resolved, not None
    assert rec["active_ingredients"] == [{"label": "Niacinamide"}]
