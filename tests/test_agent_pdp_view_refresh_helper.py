"""Tests for refresh_agent_pdp_view_for_content_key — the canonical
fetch->assemble->upsert orchestration used by the catalog_sync auto-servable
hook (so a fresh internal-merchant SKU gets an APV row before recompute)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import agent_pdp_view_assembler as apv  # noqa: E402


class _FakeDB:
    def __init__(self) -> None:
        self.executes: List[Dict[str, Any]] = []

    async def execute(self, sql: str, params: Dict[str, Any]) -> None:
        self.executes.append({"sql": sql, "params": params})


def _patch_fetches(
    monkeypatch: pytest.MonkeyPatch,
    *,
    products: List[Dict[str, Any]],
) -> None:
    async def fake_products(content_key: str, *, db: Any = None):
        return products

    async def fake_skus(product_keys, *, db: Any = None):
        return []

    async def fake_offers(product_keys, *, db: Any = None):
        return []

    async def fake_seed(product_keys, *, db: Any = None):
        return None

    async def fake_evidence(product_keys, *, db: Any = None):
        return {}

    monkeypatch.setattr(apv, "fetch_products_for_key", fake_products)
    monkeypatch.setattr(apv, "fetch_skus_for_keys", fake_skus)
    monkeypatch.setattr(apv, "fetch_offers_for_keys", fake_offers)
    monkeypatch.setattr(apv, "fetch_external_seed_for_keys", fake_seed)
    monkeypatch.setattr(apv, "fetch_evidence_for_keys", fake_evidence)


@pytest.mark.asyncio
async def test_refresh_builds_and_upserts_when_title_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fetches(
        monkeypatch,
        products=[{
            "product_key": "pk-1",
            "merchant_id": "m1",
            "platform": "shopify",
            "source_product_id": "sp-1",
            "title": "Glow Serum",
            "description": "A long enough description for the agent PDP view row.",
            "brand": "AuraGlow",
            "image_url": "https://img.example/serum.jpg",
        }],
    )
    db = _FakeDB()
    built = await apv.refresh_agent_pdp_view_for_content_key(
        "ck-1", refresh_source="catalog_sync", db=db
    )
    assert built is True
    assert len(db.executes) == 1
    assert db.executes[0]["sql"] is apv.UPSERT_SQL
    assert db.executes[0]["params"]["content_key"] == "ck-1"
    assert db.executes[0]["params"]["refresh_source"] == "catalog_sync"


@pytest.mark.asyncio
async def test_refresh_projects_evidence_into_agent_pdp_view(monkeypatch: pytest.MonkeyPatch) -> None:
    # Graded claims authored on the canonical record must reach the agent view.
    _patch_fetches(
        monkeypatch,
        products=[{
            "product_key": "pk-1", "merchant_id": "m1", "platform": "shopify",
            "source_product_id": "sp-1", "title": "Glow Serum",
            "description": "A long enough description for the agent PDP view row.",
            "brand": "AuraGlow",
        }],
    )
    profile = {"claims": [{"claim_text": "Helps brighten", "source_type": "ingredient_mechanism",
                           "substantiation_status": "substantiated"}], "review_state": "observed"}
    disclaimers = [{"text": "FDA disclaimer"}]

    async def fake_evidence(product_keys, *, db: Any = None):
        return {"evidence_profile": profile, "required_disclaimers": disclaimers}

    monkeypatch.setattr(apv, "fetch_evidence_for_keys", fake_evidence)
    db = _FakeDB()
    built = await apv.refresh_agent_pdp_view_for_content_key("ck-1", refresh_source="catalog_sync", db=db)
    assert built is True
    params = db.executes[0]["params"]
    # JSONB columns are serialized to a JSON string by row_to_upsert_params.
    assert params["evidence_profile"] == apv.to_jsonb(profile)
    assert params["required_disclaimers"] == apv.to_jsonb(disclaimers)
    assert "Helps brighten" in params["evidence_profile"]


@pytest.mark.asyncio
async def test_refresh_returns_false_when_no_products(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fetches(monkeypatch, products=[])
    db = _FakeDB()
    built = await apv.refresh_agent_pdp_view_for_content_key(
        "ck-missing", refresh_source="catalog_sync", db=db
    )
    assert built is False
    assert db.executes == []


@pytest.mark.asyncio
async def test_refresh_returns_false_when_row_too_thin(monkeypatch: pytest.MonkeyPatch) -> None:
    # Products exist but no title → assemble_row returns None → no upsert.
    _patch_fetches(
        monkeypatch,
        products=[{
            "product_key": "pk-2",
            "merchant_id": "m1",
            "platform": "shopify",
            "source_product_id": "sp-2",
            "title": None,
        }],
    )
    db = _FakeDB()
    built = await apv.refresh_agent_pdp_view_for_content_key(
        "ck-2", refresh_source="catalog_sync", db=db
    )
    assert built is False
    assert db.executes == []
