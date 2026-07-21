"""Judge-lane CLI tests — vendor-brand parity between the judge and MINT lanes.

The anua anomaly (2026-07-17): the judge dry run fetched brand-official records
with brand=None, so proposals carried raw Shopify vendor brands ("Anua US",
"Anua Global") while every catalog row carries the item-side dominant brand
("Anua"). match_record keys on brand, so all 20 AUTO verdicts resolved to
"no catalog target" despite the officials being freshly minted. Two fixes:

  1. judge mode fetches officials under dominant_brand(mint) — parity with the
     MINT lane, so NEW proposals carry the catalog-brand form; and
  2. _attach_auto_verdicts falls back to the ITEM's brand when the official's
     vendor brand resolves no target — so EXISTING saved proposals attach
     without a DeepSeek re-judge.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Dict, List

import pytest

import scripts.judge_stylekorean_residue as judge_cli  # noqa: E402


def _auto_row(item_brand: str, item_title: str, official_brand: str, official_name: str) -> Dict[str, Any]:
    return {
        "bucket": "auto",
        "item": {"brand": item_brand, "title": item_title, "decision": "mint",
                 "url": "https://www.stylekorean.com/p/x", "price": "12.00", "currency": "USD",
                 "record": {"offers": [{"availability": "in_stock"}]}},
        "official": {"pdp": {"brand": official_brand, "product_name": official_name}},
        "verdict": {"confidence": 0.95},
    }


class NoOfferDB:
    async def fetch_all(self, sql: Any, values: Any = None) -> List[Any]:
        return []


@pytest.mark.asyncio
async def test_attach_falls_back_to_item_brand_on_vendor_drift(monkeypatch):
    """'Anua US' official vs 'Anua' catalog row — the fallback must resolve it."""
    our_rows = [{"product_key": "pk1", "content_key": "ck1", "brand": "Anua",
                 "title": "Heartleaf Pore Cleansing Oil Mild",
                 "pdp_scope": "multi_merchant_canonical"}]
    attached: List[List[Dict[str, Any]]] = []

    async def fake_load(brands: List[str]) -> List[Dict[str, Any]]:
        return our_rows

    async def fake_attach(items: List[Dict[str, Any]]):
        attached.append(items)
        return len(items), 0

    monkeypatch.setattr(judge_cli, "_load_our_rows", fake_load)
    monkeypatch.setattr(judge_cli, "_attach_offer_items", fake_attach)
    monkeypatch.setattr(judge_cli, "database", NoOfferDB())

    await judge_cli._attach_auto_verdicts([
        _auto_row("Anua", "Heartleaf Pore Control Cleansing Oil Mild 200ml",
                  "Anua US", "Heartleaf Pore Cleansing Oil Mild"),
    ])
    assert len(attached) == 1
    assert [c["matched_product_key"] for c in attached[0]] == ["pk1"]


@pytest.mark.asyncio
async def test_attach_still_reports_true_no_target(monkeypatch):
    """Both brand forms missing a catalog row → no target, nothing attached,
    never guessed."""
    async def fake_load(brands: List[str]) -> List[Dict[str, Any]]:
        return [{"product_key": "pk1", "content_key": "ck1", "brand": "Anua",
                 "title": "A Different Product Entirely", "pdp_scope": "merchant_owned"}]

    attached: List[List[Dict[str, Any]]] = []

    async def fake_attach(items: List[Dict[str, Any]]):
        attached.append(items)
        return len(items), 0

    monkeypatch.setattr(judge_cli, "_load_our_rows", fake_load)
    monkeypatch.setattr(judge_cli, "_attach_offer_items", fake_attach)
    monkeypatch.setattr(judge_cli, "database", NoOfferDB())

    await judge_cli._attach_auto_verdicts([
        _auto_row("Anua", "PDRN Capsule Mask (1ea)", "Anua US",
                  "PDRN Hyaluronic Acid Capsule 100 Serum Mask"),
    ])
    assert attached == [[]]


@pytest.mark.asyncio
async def test_no_clobber_applies_to_fallback_resolved_target(monkeypatch):
    """A target resolved via the item-brand FALLBACK must still be skipped when
    it already carries a StyleKorean offer from the ATTACH lane."""
    async def fake_load(brands: List[str]) -> List[Dict[str, Any]]:
        return [{"product_key": "pk1", "content_key": "ck1", "brand": "Anua",
                 "title": "Heartleaf Pore Cleansing Oil Mild",
                 "pdp_scope": "multi_merchant_canonical"}]

    attached: List[List[Dict[str, Any]]] = []

    async def fake_attach(items: List[Dict[str, Any]]):
        attached.append(items)
        return len(items), 0

    class HasOfferDB:
        async def fetch_all(self, sql: Any, values: Any = None) -> List[Dict[str, Any]]:
            return [{"product_key": "pk1"}]

    monkeypatch.setattr(judge_cli, "_load_our_rows", fake_load)
    monkeypatch.setattr(judge_cli, "_attach_offer_items", fake_attach)
    monkeypatch.setattr(judge_cli, "database", HasOfferDB())

    await judge_cli._attach_auto_verdicts([
        _auto_row("Anua", "Heartleaf Pore Control Cleansing Oil Mild 200ml",
                  "Anua US", "Heartleaf Pore Cleansing Oil Mild"),
    ])
    assert attached == [[]]


@pytest.mark.asyncio
async def test_judge_mode_fetches_officials_under_dominant_brand(tmp_path, monkeypatch):
    """Judge mode must fetch brand-official records with the plan's dominant
    brand (MINT-lane parity), not brand=None (raw vendor)."""
    plan_path = tmp_path / "plan.jsonl"
    with open(plan_path, "w") as f:
        for t in ("A", "B"):
            f.write(json.dumps({"decision": "mint", "brand": "Anua", "title": t}) + "\n")

    fetch_kwargs: List[Dict[str, Any]] = []

    async def fake_fetch(**kwargs: Any) -> List[Dict[str, Any]]:
        fetch_kwargs.append(kwargs)
        return []

    async def fake_judge(mint: Any, official: Any, threshold: float) -> Dict[str, List[Any]]:
        return {"auto": [], "review": [], "no_match": [], "no_candidates": []}

    monkeypatch.setattr(judge_cli, "fetch_official_records", fake_fetch)
    monkeypatch.setattr(judge_cli, "judge_residue_items", fake_judge)

    args = argparse.Namespace(
        plan=str(plan_path), brand_official_domain="anua.us",
        category_path="beauty/skincare", threshold=0.85, limit=0,
        out=None, apply_offers=False, apply_from=None,
    )
    rc = await judge_cli._drive(args)
    assert rc == 0
    assert fetch_kwargs[0]["brand"] == "Anua"
