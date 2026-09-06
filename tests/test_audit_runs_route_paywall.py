"""`GET /api/audits/{run_id}` must apply the same paywall as its sibling.

WHY THIS EXISTS. Two routes serve audit content. `merchant_audit_routes`'
poll and share view strip the paid "what to do" layer for free-tier owners;
this one did not — either return. A free-tier merchant whose portal showed
`selection_gap: null` + `locked_counts` could fetch the same run id here and
receive it in full, along with `where_you_can_win`, `win_plan` and
`merchant_narrative.prioritized_actions`.

Both flags are live in production (`AUDIT_ACTIONS_PAYWALL_ENABLED=true`), so
this was a real bypass, not a hypothetical one. Adding `revenue_recovery` to
`MERCHANT_ALLOWED_AUDIENCES` widened it: that projection's `stages[].actions`
carry full action content through the same unpaywalled return.

Ownership is enforced above both returns (404 on mismatch), so the paywall is
keyed to the run's OWNER — the same merchant, read from the row.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.audit_runs_routes as arr
import routes.merchant_audit_routes as mar
from utils.auth import get_current_merchant


def _paid_layer() -> Dict[str, Any]:
    return {
        "brand_rollup": {
            "avg_visibility": 40,
            "selection_gap": {
                "version": 1, "available": True,
                "gaps": [{"query": "PAID-QUERY best drone",
                          "matched_products": [{"product_key": "PAID-SKU"}]}],
                "lost_queries_without_product": [], "won_queries": [],
                "counts": {"lost_queries": 1},
            },
            "where_you_can_win": {"niches": [{"query": "PAID-NICHE"}]},
        },
        "merchant_narrative": {
            "headline_story": "FREE-STORY visible on Gemini.",
            "prioritized_actions": [{"headline": "PAID-ACTION fix the PDP"}],
        },
        # Real reports carry these at the report TOP LEVEL as well as inside
        # brand_rollup — _shape_url_audit_response reads
        # `brand_rollup.get(...) or report.get(...)`. Modelling only the
        # nested copy let a mutant that drops report_jsonb from the strip's
        # home list survive.
        "where_you_can_win": {"niches": [{"query": "PAID-TOPLEVEL-NICHE"}]},
        "selection_gap": {"version": 1, "available": True,
                          "gaps": [{"query": "PAID-TOPLEVEL-GAP"}],
                          "counts": {"lost_queries": 1}},
        "win_plan": {"available": True, "sku_plans": [{"sku": "PAID-PLAN"}]},
    }


def _row() -> Dict[str, Any]:
    """Every AuditRunDetail field, sourced from the model rather than guessed —
    a partial row would fail validation for reasons unrelated to the paywall."""
    from routes.audit_runs_routes import AuditRunDetail
    row: Dict[str, Any] = {n: None for n in AuditRunDetail.model_fields}
    row.update({
        "run_id": "r-1",
        "merchant_id": "m-1",
        "subject_type": "merchant_url",
        "status": "succeeded",
        "stage": "completed",
        "product_keys": [],
        "verdict_labels": [],
        "audited_via_pivota_canonical": [],
        "report_jsonb": _paid_layer(),
    })
    row.update(_paid_layer())
    return row


@pytest.fixture()
def wired(monkeypatch):
    state = {"row": _row(), "tier": "free", "projection": None}

    async def fake_fetch(*, run_id: str):
        return state["row"]

    async def fake_balance(merchant_id, **_kw):
        return {"plan_tier": state["tier"]}

    async def fake_projection(*, audit_run_id, audience):
        return {"payload_jsonb": state["projection"]} if state["projection"] else None

    # Patched at the SOURCE modules: audit_runs_routes imports both names
    # locally inside the handler, so they are not attributes of `arr` and a
    # module-level patch here would silently do nothing.
    # DIFFERENT targets on purpose. fetch_audit_run_by_id is a module-level
    # import, so it is an attribute of `arr`. fetch_projection is imported
    # INSIDE the handler, so it is not — patching `arr` for that one silently
    # does nothing and the real DB call runs.
    import db.audit_evidence as ae_db
    monkeypatch.setattr(arr, "fetch_audit_run_by_id", fake_fetch)
    monkeypatch.setattr(ae_db, "fetch_projection", fake_projection)
    monkeypatch.setattr(mar, "get_balance", fake_balance)
    monkeypatch.setattr(mar, "_ACTIONS_PAYWALL_ENABLED", True)
    return state


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(arr.router)
    app.dependency_overrides[get_current_merchant] = lambda: "m-1"
    return TestClient(app)


def _body(res) -> str:
    import json
    return json.dumps(res.json(), default=str)


def test_the_canonical_shape_is_paywalled_for_a_free_owner(wired):
    """The bypass a free merchant reaches simply by omitting ?audience."""
    res = _client().get("/api/audits/r-1")
    assert res.status_code == 200
    blob = _body(res)
    for locked in ("PAID-QUERY", "PAID-SKU", "PAID-NICHE", "PAID-ACTION",
                   "PAID-PLAN", "PAID-TOPLEVEL-NICHE", "PAID-TOPLEVEL-GAP"):
        assert locked not in blob, f"{locked} leaked through the canonical shape"
    # The free layer survives — this is a paywall, not a blackout.
    assert "FREE-STORY" in blob


def test_the_canonical_shape_is_intact_for_a_paid_owner(wired):
    """The positive counterpart: without it, a route that nulled everything
    unconditionally would pass the test above."""
    wired["tier"] = "pro"
    blob = _body(_client().get("/api/audits/r-1"))
    assert "PAID-QUERY" in blob and "PAID-ACTION" in blob


def test_the_revenue_recovery_audience_is_paywalled(wired):
    """C2 added this audience to MERCHANT_ALLOWED_AUDIENCES, and its
    stages[].actions carry full action content through the same return."""
    wired["projection"] = {
        "audience": "revenue_recovery",
        "stages": [
            {"stage": "get_selected", "status": "MEASURED",
             "findings": [{"type": "category_visibility_low",
                           "severity": "high", "summary": "FREE-FINDING"}],
             "actions": [{"title": "PAID-ACTION rewrite the PDP",
                          "first_move": "PAID-MOVE"}]},
        ],
    }
    blob = _body(_client().get("/api/audits/r-1?audience=revenue_recovery"))
    assert "PAID-ACTION" not in blob and "PAID-MOVE" not in blob


def test_the_revenue_recovery_audience_is_intact_for_a_paid_owner(wired):
    wired["tier"] = "pro"
    wired["projection"] = {
        "audience": "revenue_recovery",
        "stages": [{"stage": "get_selected", "status": "MEASURED",
                    "findings": [],
                    "actions": [{"title": "PAID-ACTION rewrite the PDP"}]}],
    }
    blob = _body(_client().get("/api/audits/r-1?audience=revenue_recovery"))
    assert "PAID-ACTION" in blob


def test_an_internal_audience_is_still_refused(wired):
    """The pre-existing P0-4 guard must survive this change."""
    res = _client().get("/api/audits/r-1?audience=internal_ops")
    assert res.status_code == 403


# ---- a claimed funnel run must not leak into a merchant's own audit views ---
#
# A claimed funnel run is a new row class: status='succeeded',
# stage='completed', subject_type='public_funnel', report_jsonb NULL. It
# carries the claimer's merchant_id, so every generic merchant-scoped reader
# would list it as one of their audits.

def test_the_generic_readers_exclude_the_funnel_lane():
    """Asserted at the SQL these build, because the alternative is a fixture
    per reader and the risk is that a future reader forgets."""
    import inspect
    import db.merchant_audit_runs as m

    for fn in (m.count_runs_in_window, m.audit_status_counts_in_window):
        src = inspect.getsource(fn)
        assert "public_funnel" in src or "SUBJECT_TYPE_PUBLIC_FUNNEL" in src, (
            f"{fn.__name__} counts funnel runs as the merchant's own audits"
        )

    src = inspect.getsource(m.recent_runs_for_merchant)
    assert "SUBJECT_TYPE_PUBLIC_FUNNEL" in src, (
        "recent_runs_for_merchant feeds the history list, the trend inputs "
        "and the tasks lookup — unscoped it must still exclude the funnel lane"
    )
