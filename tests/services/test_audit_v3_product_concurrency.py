"""run_brand_report product_concurrency — the wedge latency fix.

Auditing the merchant's <=5 products sequentially blew past the client's
3-min timeout. The per-product work is independent, so the wedge opts into
bounded concurrency. These tests prove the knob (a) preserves per_product
ORDER, (b) keeps per-product failure isolation, and (c) actually raises
achievable parallelism — while the default (concurrency=1) path is unchanged.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services import agent_center_bd_report_service as bd


def _products(n: int):
    return [
        {"title": f"P{i}", "pdp_url": f"https://x.com/p/{i}", "product_type": "thing"}
        for i in range(1, n + 1)
    ]


@pytest.mark.asyncio
async def test_concurrency_preserves_order_and_completeness(monkeypatch):
    async def _fake_probe(**kwargs):
        await asyncio.sleep(0.01)
        return {
            "scan_mode": kwargs.get("scan_mode"), "provider": "gemini",
            "scores": {"visibility_score": 50}, "raw_runs": [], "findings": [],
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }

    monkeypatch.setattr(bd.llm_client, "probe", _fake_probe)
    out = await bd.run_brand_report(
        merchant_name="Brand X", merchant_domain="brandx.com",
        products=_products(5), provider="gemini",
        include_category_visibility=False, product_concurrency=3,
    )
    # gather preserves input order regardless of completion order.
    assert [p["product"]["title"] for p in out["per_product"]] == [
        "P1", "P2", "P3", "P4", "P5",
    ]
    assert out["aggregate"]["products_succeeded"] == 5
    assert out["aggregate"]["products_failed"] == 0


@pytest.mark.asyncio
async def test_concurrency_isolates_failures(monkeypatch):
    # Fail deterministically by PRODUCT (its pdp_url), not by global call
    # order — under concurrency the call order interleaves.
    async def _fake_probe(**kwargs):
        await asyncio.sleep(0.005)
        pdp = (kwargs.get("context") or {}).get("merchant_pdp_url") or ""
        if pdp.endswith("/p/2"):
            raise RuntimeError("upstream timeout")
        return {
            "scan_mode": kwargs.get("scan_mode"), "provider": "gemini",
            "scores": {"visibility_score": 50}, "raw_runs": [], "findings": [],
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }

    monkeypatch.setattr(bd.llm_client, "probe", _fake_probe)
    out = await bd.run_brand_report(
        merchant_name="Brand X", merchant_domain="brandx.com",
        products=_products(3), provider="gemini",
        include_category_visibility=False, product_concurrency=3,
    )
    assert out["aggregate"]["products_count"] == 3
    assert out["aggregate"]["products_failed"] == 1
    assert out["aggregate"]["products_succeeded"] == 2


@pytest.mark.asyncio
async def test_parallel_scan_modes_overlaps_probes(monkeypatch):
    inflight = {"now": 0, "max": 0}

    async def _fake_probe(**kwargs):
        inflight["now"] += 1
        inflight["max"] = max(inflight["max"], inflight["now"])
        await asyncio.sleep(0.02)
        inflight["now"] -= 1
        return {
            "scan_mode": kwargs.get("scan_mode"), "provider": "gemini",
            "scores": {"visibility_score": 50}, "raw_runs": [], "findings": [],
            "usage": {},
        }

    monkeypatch.setattr(bd.llm_client, "probe", _fake_probe)
    kw = dict(
        merchant_name="X", merchant_pdp_url="https://x.com/p/1",
        product_title="P", product_type="thing", provider="gemini",
    )

    inflight["max"] = 0
    await bd.run_bd_probes(**kw, parallel_scan_modes=False)
    assert inflight["max"] == 1  # default: one scan mode at a time

    inflight["max"] = 0
    await bd.run_bd_probes(**kw, parallel_scan_modes=True)
    assert inflight["max"] == 3  # vis + attribution + category overlap


@pytest.mark.asyncio
async def test_concurrency_knob_raises_parallelism(monkeypatch):
    inflight = {"now": 0, "max": 0}

    async def _fake_probe(**kwargs):
        inflight["now"] += 1
        inflight["max"] = max(inflight["max"], inflight["now"])
        await asyncio.sleep(0.02)
        inflight["now"] -= 1
        return {
            "scan_mode": kwargs.get("scan_mode"), "provider": "gemini",
            "scores": {"visibility_score": 50}, "raw_runs": [], "findings": [],
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }

    monkeypatch.setattr(bd.llm_client, "probe", _fake_probe)
    products = _products(5)

    inflight["max"] = 0
    await bd.run_brand_report(
        merchant_name="X", merchant_domain="x.com", products=products,
        provider="gemini", include_category_visibility=False,
        product_concurrency=1,  # sequential default
    )
    seq_max = inflight["max"]

    inflight["max"] = 0
    await bd.run_brand_report(
        merchant_name="X", merchant_domain="x.com", products=products,
        provider="gemini", include_category_visibility=False,
        product_concurrency=3,
    )
    conc_max = inflight["max"]

    assert seq_max == 1               # default path is fully sequential
    assert conc_max > seq_max         # the knob actually parallelizes
    assert conc_max <= 3              # ...within the requested bound
