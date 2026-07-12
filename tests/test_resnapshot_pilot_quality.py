"""Tests for scripts/resnapshot_pilot_quality.py (Fix Plan G — T2).

Pins: pilot-scoped (only the given keys), reads the durable resolved_vertical +
llm_attributes into the readiness payload, captures before/after, dry-run writes
nothing, and the report summarizes the movement.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from scripts import resnapshot_pilot_quality as rs


def _ns(**kw) -> SimpleNamespace:
    base = {"keys_file": None, "keys": None, "dry_run": False}
    base.update(kw)
    return SimpleNamespace(**base)


class _FakeDB:
    def __init__(self, catalog: Dict[str, Dict[str, Any]],
                 before: Optional[Dict[str, Any]] = None):
        self.catalog = catalog
        self.before = before or {}
        self.is_connected = True
        self.inserts: List[Dict[str, Any]] = []

    async def connect(self):  # pragma: no cover
        return None

    async def disconnect(self):  # pragma: no cover
        return None

    async def fetch_one(self, sql, params):
        s = str(sql)
        if "FROM catalog_products" in s:
            return self.catalog.get(params["product_key"])
        if "FROM product_quality_snapshot" in s:
            return self.before.get(params["platform_product_id"])
        return None

    async def execute(self, sql, params=None):
        # full_quality_eval inserts here; record it.
        self.inserts.append({"sql": str(sql), "params": params})
        return None


def _catalog_row(pk: str, **over) -> Dict[str, Any]:
    base = {
        "product_key": pk, "merchant_id": "external_seed", "platform": "external_seed",
        "source_product_id": f"ext_{pk}",
        "title": "Snail Mucin Essence", "description": "x" * 80,
        "product_type": "Essence", "category": "Skincare",
        "category_path": "beauty/skincare/essence", "brand": "COSRX",
        "image_url": "https://img/1.jpg",
        "product_payload": {"price": 21.0},
        "resolved_vertical": "beauty",
        "llm_attributes": {
            "schema_version": "structural_depth.beauty.v1",
            "attributes": {"volume": "100 ml", "concerns": ["dryness"],
                           "key_ingredients": [{"label": "Snail Mucin"}],
                           "skin_type": ["dry"], "texture": "watery"},
        },
    }
    base.update(over)
    return base


def _stub_full_quality_eval(monkeypatch):
    """full_quality_eval writes via the real `database` singleton (not our
    FakeDB), so stub it to score the payload with the same preview_quality it
    uses and record that a write was requested."""
    from services import product_quality_service as svc
    calls = []

    async def _fake(*, merchant_id, platform, platform_product_id, geo_code,
                    payload, model_version="none", **_kw):
        calls.append({"platform_product_id": platform_product_id,
                      "model_version": model_version})
        return svc.preview_quality(payload)

    monkeypatch.setattr(rs, "full_quality_eval", _fake)
    return calls


@pytest.mark.asyncio
async def test_resnapshot_captures_before_after_and_writes(monkeypatch):
    calls = _stub_full_quality_eval(monkeypatch)
    db = _FakeDB(
        catalog={"p1": _catalog_row("p1")},
        before={"ext_p1": {"model_readiness_score": 0.0}},
    )
    report = await rs._drive(_ns(keys="p1"), db=db)
    r = report["results"][0]
    assert r["status"] == "ok"
    assert r["readiness_before"] == 0.0
    assert r["readiness_after"] > 80.0            # full structure + vertical + depth
    assert r["llm_attribute_field_count"] == 5
    assert report["resnapshotted"] == 1
    assert report["moved_up"] == 1
    # a re-snapshot write was requested, tagged with the pilot model_version
    assert calls and calls[0]["model_version"] == rs._MODEL_VERSION


@pytest.mark.asyncio
async def test_resnapshot_dry_run_writes_nothing():
    db = _FakeDB(catalog={"p1": _catalog_row("p1")})
    report = await rs._drive(_ns(keys="p1", dry_run=True), db=db)
    assert db.inserts == []
    assert report["dry_run"] is True
    assert report["results"][0]["readiness_after"] > 80.0


@pytest.mark.asyncio
async def test_resnapshot_only_touches_given_keys_and_reports_not_found(monkeypatch):
    _stub_full_quality_eval(monkeypatch)
    db = _FakeDB(catalog={"p1": _catalog_row("p1")})
    report = await rs._drive(_ns(keys="p1,pMISSING"), db=db)
    assert report["requested"] == 2
    assert report["resnapshotted"] == 1
    assert report["not_found"] == ["pMISSING"]


def test_row_to_product_parses_string_llm_attributes_and_price():
    prod = rs._row_to_product({
        "title": "T", "description": "d", "product_payload": json.dumps({"price": 9.0}),
        "resolved_vertical": "beauty",
        "llm_attributes": json.dumps({"attributes": {"volume": "50 ml"}}),
    })
    assert prod["price"] == 9.0
    assert prod["llm_attributes"]["attributes"]["volume"] == "50 ml"


def test_load_keys_dedups_and_splits(tmp_path):
    f = tmp_path / "keys.txt"
    f.write_text("a\nb, c\na\n")
    keys = rs._load_keys(_ns(keys_file=str(f), keys="d,b"))
    assert keys == ["a", "b", "c", "d"]
