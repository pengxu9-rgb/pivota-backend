"""Stage-A lean timeout rescue (SEED_STAGE_A_LEAN_TIMEOUT_RESCUE, default ON).

Stage-A's WHERE arms include the trgm-indexed seed_data->derived->recall JSON
paths; GIN trgm is lossy, so every flagged candidate is heap-rechecked and the
recheck detoasts the whole TOASTed seed_data blob. Dense verticals (beauty)
fill the LIMIT early and finish; SPARSE verticals can't terminate early and
time out — prod-measured 2026-07-13: "bone conduction headphones" 4.7s against
the stage-A budget, 0 rows, while the lean inline-column shape returns all 3
Mojawa products in ~0.5s. That is the "external-seed recall lane dead for
non-beauty verticals" bug.

The rescue re-runs stage A ONCE with the lean inline-column WHERE
(fast_multiterm + lean_where_min_tokens=1, no COUNT twin) — but only when
stage A timed out with ZERO rows, a state that previously served nothing, so
it cannot regress a query that currently serves.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")
os.chdir(REPO_ROOT)

from routes import agent_api


def _seed_row(external_product_id: str = "mojawa_us_8129594163442") -> Dict[str, Any]:
    return {
        "id": f"external_brand_crawl::{external_product_id}",
        "external_product_id": external_product_id,
        "market": "US",
        "tool": "external_brand_crawl",
        "destination_url": "https://mojawa.com/products/bone-conduction-headphone",
        "canonical_url": "https://mojawa.com/products/bone-conduction-headphone",
        "domain": "mojawa.com",
        "title": "HaptiFit Terra Bone Conduction Headphone",
        "price_amount": 229.99,
        "price_currency": "USD",
        "availability": "in_stock",
        "attached_product_key": f"prod::merch_obs_022b65d47a58b87a::external_seed::{external_product_id}",
        "seed_data": {"title": "HaptiFit Terra Bone Conduction Headphone", "brand": "Mojawa"},
        "status": "active",
    }


class _FetchScript:
    """Scripted stand-in for fetch_external_seed_rows keyed on call shape."""

    def __init__(self, *, stage_a: Dict[str, Any], rescue: Optional[Dict[str, Any]] = None):
        self.stage_a = stage_a
        self.rescue = rescue
        self.calls: List[Dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        base = {"rows": [], "total_count": 0, "query_ms": 10, "query_timeout": False, "table_missing": False}
        if kwargs.get("fast_multiterm") and kwargs.get("lean_where_min_tokens") == 1:
            return {**base, **(self.rescue or {})}
        if len(self.calls) == 1:
            return {**base, **self.stage_a}
        # stage B / broad fallback: nothing new.
        return base

    @property
    def rescue_calls(self) -> List[Dict[str, Any]]:
        return [c for c in self.calls if c.get("fast_multiterm") and c.get("lean_where_min_tokens") == 1]


async def _fake_build(*, req: Any, seed_row: Dict[str, Any], allowed_domains: Any = None, metrics_out: Any = None) -> Dict[str, Any]:
    return {
        "id": seed_row["external_product_id"],
        "product_id": seed_row["external_product_id"],
        "merchant_id": "external_seed",
        "title": seed_row["title"],
        "source": "external_seed",
    }


def _run_loader(monkeypatch: pytest.MonkeyPatch, script: _FetchScript, **env: str) -> Dict[str, Any]:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(agent_api, "fetch_external_seed_rows", script)
    monkeypatch.setattr(agent_api, "_build_external_seed_product", _fake_build)
    metrics: Dict[str, Any] = {}
    products = asyncio.run(
        agent_api._load_external_seed_products_for_search(
            req=None,
            query="bone conduction headphones",
            limit=8,
            metrics_out=metrics,
        )
    )
    return {"products": products, "metrics": metrics, "script": script}


def test_stage_a_timeout_with_zero_rows_triggers_lean_rescue(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _FetchScript(
        stage_a={"rows": [], "query_timeout": True},
        rescue={"rows": [_seed_row()], "query_timeout": False},
    )
    out = _run_loader(monkeypatch, script)

    assert len(script.rescue_calls) == 1
    rescue_call = script.rescue_calls[0]
    # The rescue is the lean, no-COUNT shape.
    assert rescue_call["fast_multiterm"] is True
    assert rescue_call["lean_where_min_tokens"] == 1
    assert rescue_call["include_total_count"] is False

    assert out["metrics"]["stage_a_lean_rescue_attempted"] is True
    assert out["metrics"]["stage_a_lean_rescue_rows"] == 1
    assert out["metrics"]["stage_a_rows"] == 1
    # The rescued rows flow through to built products.
    assert [p["product_id"] for p in out["products"]] == ["mojawa_us_8129594163442"]


def test_no_rescue_when_stage_a_returns_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _FetchScript(stage_a={"rows": [_seed_row()], "query_timeout": False})
    out = _run_loader(monkeypatch, script)

    assert script.rescue_calls == []
    assert out["metrics"]["stage_a_lean_rescue_attempted"] is False
    assert [p["product_id"] for p in out["products"]] == ["mojawa_us_8129594163442"]


def test_no_rescue_on_timeout_with_partial_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timed-out stage A that still returned rows keeps today's behavior."""
    script = _FetchScript(stage_a={"rows": [_seed_row()], "query_timeout": True})
    out = _run_loader(monkeypatch, script)

    assert script.rescue_calls == []
    assert [p["product_id"] for p in out["products"]] == ["mojawa_us_8129594163442"]


def test_kill_switch_disables_rescue(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _FetchScript(
        stage_a={"rows": [], "query_timeout": True},
        rescue={"rows": [_seed_row()], "query_timeout": False},
    )
    out = _run_loader(monkeypatch, script, SEED_STAGE_A_LEAN_TIMEOUT_RESCUE="false")

    assert script.rescue_calls == []
    assert out["metrics"]["stage_a_lean_rescue_attempted"] is False
    assert out["products"] == []
    assert out["metrics"]["skip_reason"] == "query_timeout"
