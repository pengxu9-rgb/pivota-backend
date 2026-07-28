"""The in-cluster rescore runner must not manage the shared connection pool.

The whole reason this endpoint exists rather than a call to
`scripts.backfill_external_seed_quality_rescore.run()` is that `run()` owns the
process's connection lifecycle — `database.connect()` / `disconnect()`, and a
`_reset_connection()` recovery path that calls `pool.terminate()` and sets
`backend._pool = None`. Correct for a one-shot CLI; catastrophic in the web
process, where it would tear the pool out from under every in-flight request.

`test_route_never_touches_the_shared_pool_lifecycle` is the guard that matters:
the behaviour tests below would all still pass if someone "simplified" the route
into calling `run()`, and the damage would only show up as production 500s under
concurrent traffic.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROUTE_PATH = (
    Path(__file__).resolve().parents[1]
    / "routes"
    / "admin_rescore_external_seed_quality.py"
)

ROW = {
    "product_key": "prod::merch_x::external_seed::abc",
    "source_product_id": "brand_us_123",
    "seed_id": "seed-1",
    "title": "Centella Calming Gel Cream",
    "description": "A soothing gel cream with centella for sensitive skin.",
    "brand": "ExampleBeauty",
    "product_type": None,
    "category_kind": "skincare",
    "image_url": "https://example.com/p.jpg",
    "price_amount": 24.0,
    "raw_inci": "Water, Glycerin, Centella Asiatica Extract",
    "pdp_details_sections": None,
}


# --- the guard that the behaviour tests cannot provide ---------------------

def test_route_never_touches_the_shared_pool_lifecycle():
    tree = ast.parse(ROUTE_PATH.read_text())

    banned_attrs = {"connect", "disconnect", "terminate"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in banned_attrs:
                owner = node.func.value
                name = getattr(owner, "id", None) or getattr(owner, "attr", None)
                assert name != "database", (
                    f"route calls database.{node.func.attr}() — this runs inside the "
                    f"web process and would break the shared pool for live traffic"
                )

    # Assert on CODE, not on the file text: this module's docstring explains
    # _reset_connection and wait_for at length, and a substring check would fire
    # on the explanation rather than on a real reference.
    referenced = {
        getattr(n, "id", None) or getattr(n, "attr", None)
        for n in ast.walk(tree)
        if isinstance(n, (ast.Name, ast.Attribute))
    }
    assert "_reset_connection" not in referenced, (
        "route references the CLI's pool-recycling recovery path; it terminates "
        "the shared pool and must never run in-process"
    )
    # The cancellation is what poisons the connection in the first place; on the
    # internal network the timeout it guards against does not arise.
    assert "wait_for" not in referenced, (
        "route uses asyncio.wait_for — its cancellation leaves the shared "
        "connection half-acquired, which is the defect _reset_connection exists "
        "to undo. Bounded `limit` is the safety property here, not a timeout."
    )


def test_route_reuses_the_cli_sql_rather_than_copying_it():
    src = ROUTE_PATH.read_text()
    assert "from scripts.backfill_external_seed_quality_rescore import" in src
    assert "FETCH" in src, "must import the CLI's FETCH; a second copy would drift"


def test_router_is_registered_in_main():
    main_src = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    assert "admin_rescore_external_seed_quality_router" in main_src
    assert "app.include_router(admin_rescore_external_seed_quality_router)" in main_src


# --- behaviour -------------------------------------------------------------

@pytest.fixture()
def patched(monkeypatch):
    import routes.admin_rescore_external_seed_quality as mod
    import scripts.backfill_external_seed_quality_rescore as cli
    import services.external_seed_servability as svc

    calls = {"servable": [], "flush": []}

    class FakeDB:
        async def fetch_all(self, *_a, **_k):
            return [ROW]

    monkeypatch.setattr(mod, "database", FakeDB())

    async def fake_rescored_ids():
        return set()

    async def fake_flush(keys):
        calls["flush"].append(list(keys))
        return len(keys)

    monkeypatch.setattr(cli, "_rescored_ids", fake_rescored_ids)
    monkeypatch.setattr(cli, "_flush_trust", fake_flush)

    async def fake_servable(**kw):
        calls["servable"].append(kw)
        return {"quality": True, "serving_eligible": True}

    monkeypatch.setattr(svc, "make_external_seed_servable", fake_servable)
    return mod, calls


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(patched):
    mod, calls = patched
    out = await mod._run_chunk(mod.RescoreRequest(mode="dry_run", limit=10))
    assert out["selected"] == 1
    assert out["wrote"] == 0 and out["promoted_to_public"] == 0
    assert calls["servable"] == [], "dry_run must not call the servability writer"
    assert out["sample"], "dry_run should show what it would do"


@pytest.mark.asyncio
async def test_apply_writes_and_flushes_trust(patched):
    mod, calls = patched
    out = await mod._run_chunk(mod.RescoreRequest(mode="apply", limit=10))
    assert out["wrote"] == 1 and out["eligible"] == 1
    assert out["promoted_to_public"] == 1
    assert len(calls["servable"]) == 1
    # The signals the whole plan turns on must reach the payload builder.
    kw = calls["servable"][0]
    assert kw["quality_payload"]["seed_data"]["inci_list"]
    assert kw["quality_payload"].get("global_category_id")


@pytest.mark.asyncio
async def test_unresolved_identity_counts_as_no_write(monkeypatch, patched):
    """`serving_eligible is None` means the snapshot landed under the FALLBACK
    merchant — invisible to the eligibility classifier. Counting it as a write
    would let _rescored_ids() mark the product done forever."""
    mod, calls = patched
    import services.external_seed_servability as svc

    async def unresolved(**kw):
        calls["servable"].append(kw)
        return {"quality": True, "serving_eligible": None}

    monkeypatch.setattr(svc, "make_external_seed_servable", unresolved)
    out = await mod._run_chunk(mod.RescoreRequest(mode="apply", limit=10))
    assert out["wrote"] == 0 and out["no_write"] == 1
    assert out["promoted_to_public"] == 0


@pytest.mark.asyncio
async def test_one_bad_row_does_not_end_the_chunk(monkeypatch, patched):
    mod, _ = patched
    import services.external_seed_servability as svc

    async def boom(**kw):
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(svc, "make_external_seed_servable", boom)
    out = await mod._run_chunk(mod.RescoreRequest(mode="apply", limit=10))
    assert out["failed"] == 1 and out["wrote"] == 0


def test_limit_is_capped():
    from pydantic import ValidationError

    import routes.admin_rescore_external_seed_quality as mod

    with pytest.raises(ValidationError):
        mod.RescoreRequest(limit=mod.MAX_LIMIT + 1)
    # include_eligible off by default => a rescore can only promote, never demote.
    assert mod.RescoreRequest().include_eligible is False
    assert mod.RescoreRequest().mode == "dry_run"
