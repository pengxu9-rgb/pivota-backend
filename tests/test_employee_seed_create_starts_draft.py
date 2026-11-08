"""A seed made in the employee portal starts as a DRAFT and is served only once published (FIX_PLAN D-11).

`POST /employee/products/external-seeds` used to insert every new seed as `status='active'`, and every
serving read keys on `status = 'active'`, so an unreviewed seed went live the moment it was saved. A new
seed is now `draft`; `PATCH /external-seeds/{id}` with `{"status": "active"}` publishes it. Existing rows
keep their status: a create that lands on a live seed for the same destination updates it in place, live.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

DEST = "https://shop.example/products/barrier-cream"


def _snapshot() -> SimpleNamespace:
    return SimpleNamespace(
        canonical_url=DEST, domain="shop.example", title="Barrier Cream", image_url=None,
        price_amount=24.0, price_currency="USD", availability="in_stock", evidence={},
    )


def _row(status: str) -> Dict[str, Any]:
    return {
        "id": f"seed_{status}", "external_product_id": "ext_existing", "market": "US", "tool": "*",
        "destination_url": DEST, "canonical_url": DEST, "domain": "shop.example", "title": "Barrier Cream",
        "image_url": None, "price_amount": 24.0, "price_currency": "USD", "availability": "in_stock",
        "seed_data": {"external_product_id": "ext_existing", "title": "Barrier Cream"}, "status": status,
        "notes": None, "attached_product_key": None, "attached_variant_id": None,
    }


def _run_create(monkeypatch, existing: Optional[List[Dict[str, Any]]] = None):
    import routes.employee_products as ep

    lookups: List[Dict[str, Any]] = []
    writes: List[Dict[str, Any]] = []
    supersedes: List[Dict[str, Any]] = []

    async def fetch_all(query, values=None):
        lookups.append({"sql": query, **(values or {})})
        return list(existing or [])

    async def execute_seed(query, values):
        writes.append({"sql": query, **values})

    async def execute(query, values=None):
        supersedes.append({"sql": query, **(values or {})})

    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "resolve_external_offer", AsyncMock(return_value=_snapshot()))
    monkeypatch.setattr(ep.database, "fetch_all", fetch_all)
    monkeypatch.setattr(ep.database, "execute", execute)
    monkeypatch.setattr(ep, "_execute_seed_data_stmt", execute_seed)
    monkeypatch.setattr(ep, "_derive_seed_seller_columns", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(ep, "_make_redirect_url", AsyncMock(return_value="https://api.example/r?token=t"))
    out = asyncio.run(
        ep.create_external_seed(
            ep.CreateExternalSeedRequest(destination_url=DEST, market="US"),
            request=SimpleNamespace(),
            current_user={"employee_id": "emp_test"},
        )
    )
    return out, lookups, writes, supersedes


def test_a_new_seed_is_inserted_as_a_draft(monkeypatch):
    out, _, writes, supersedes = _run_create(monkeypatch)
    (insert,) = writes
    assert "INSERT INTO external_product_seeds" in insert["sql"]
    assert insert["status"] == "draft"
    assert "'active'" not in insert["sql"], "the insert takes its status from the bound parameter"
    assert out["seed"]["status"] == "draft"
    assert supersedes == [], "a draft supersedes nothing"


def test_a_create_on_a_live_seed_updates_it_and_keeps_it_live(monkeypatch):
    out, _, writes, supersedes = _run_create(monkeypatch, existing=[_row("active")])
    (update,) = writes
    assert update["sql"].lstrip().startswith("UPDATE external_product_seeds")
    set_clause = update["sql"].split("WHERE")[0]
    assert "status" not in set_clause, "an existing row's status is never rewritten"
    assert out["seed"]["status"] == "active"
    (supersede,) = supersedes
    assert "SET status = 'disabled'" in supersede["sql"], "a live re-create still supersedes as before"


def test_saving_the_same_new_seed_again_edits_the_one_draft(monkeypatch):
    out, lookups, writes, supersedes = _run_create(monkeypatch, existing=[_row("draft")])
    (lookup,) = lookups
    assert "status IN ('active', :draft_status)" in lookup["sql"]
    assert lookup["draft_status"] == "draft"
    assert "ORDER BY (status = 'active') DESC" in lookup["sql"], "a live seed always wins over a draft"
    (update,) = writes
    assert update["sql"].lstrip().startswith("UPDATE external_product_seeds") and update["id"] == "seed_draft"
    assert out["seed"]["status"] == "draft"
    assert supersedes == [], "a draft must not take a served seed down before it is published"


class _Written(Exception):
    pass


def test_publishing_is_a_patch_to_active(monkeypatch):
    import routes.employee_products as ep

    seen: List[Dict[str, Any]] = []

    async def write(query, values=None):
        seen.append({"sql": query, **(values or {})})
        raise _Written

    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep.database, "fetch_one", AsyncMock(return_value=_row("draft")))
    monkeypatch.setattr(ep.database, "execute", write)
    monkeypatch.setattr(ep, "_execute_seed_data_stmt", write)
    with pytest.raises(_Written):
        asyncio.run(ep.update_external_seed("seed_draft", ep.UpdateExternalSeedRequest(status="active"),
                                            current_user={"employee_id": "emp_test"}))
    (stmt,) = seen
    assert "status = :status" in stmt["sql"] and stmt["status"] == "active"


def test_the_agent_detail_read_serves_only_active_seeds():
    # The premise a draft rests on: the serving read never returns a non-active seed.
    import inspect

    import routes.agent_api as agent_api

    src = inspect.getsource(agent_api._load_external_seed_product_by_product_id)
    assert src.count("WHERE status = 'active'") == 2
