"""The paywall as the ENDPOINTS apply it, not as the helpers compose.

WHY THIS FILE EXISTS. tests/test_audit_actions_paywall.py drives
`_strip_actions_for_free_tier` and `_redact_shared_report` directly and proves
they compose correctly. It does not prove either ROUTE calls them — measured:
deleting `await _apply_actions_paywall(...)` from the authed poll
(routes/merchant_audit_routes.py:2564) or from the public share read (:2885)
survives the entire 13,700-test suite. That is the "simulating a route leaves
its delivering line untested" shape, one line away from serving the paid layer
to a free-tier merchant and to anyone holding a share URL.

Both flags are ON in production (`AUDIT_ACTIONS_PAYWALL_ENABLED=true`,
`AUDIT_SHARE_LINKS_ENABLED=true` on web and worker), so the delivering line is
live, not hypothetical.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.merchant_audit_routes as mar
from utils.auth import get_current_merchant


def _selection_gap() -> Dict[str, Any]:
    return {
        "version": 1,
        "available": True,
        "gaps": [
            {
                "query": "best beginner drone under 300",
                "evidence": {"grounded_responses": 0,
                             "responses_citing_your_product": 0,
                             "engines": ["gemini"]},
                "matched_products": [
                    {"product_key": "x1-drone", "title": "X1 Drone",
                     "matched_terms": ["drone"], "matched_form": "drone",
                     "match_reason": 'Your "X1 Drone" is a drone.'},
                ],
            }
        ],
        "lost_queries_without_product": [],
        "won_queries": [],
        "counts": {"catalog_products_indexed": 1, "lost_queries": 1,
                   "lost_queries_with_matched_product": 1, "won_queries": 0},
    }


def _row() -> Dict[str, Any]:
    gap = _selection_gap()
    return {
        "run_id": "r-1",
        "merchant_id": "m-1",
        "subject_type": "merchant_url",
        "status": "succeeded",
        "report_jsonb": {
            "brand_rollup": {"avg_visibility": 40, "selection_gap": gap},
            "per_sku_reports": [],
            "merchant_narrative": {"headline_story": "Invisible on ChatGPT."},
        },
        "partial_result_jsonb": {},
    }


@pytest.fixture()
def wired(monkeypatch):
    """Paywall ON, share links ON, owner is FREE tier — production's shape."""
    state = {"row": _row(), "tier": "free"}

    async def fake_fetch(*, run_id: str):
        return state["row"]

    async def fake_balance(merchant_id, **_kw):
        return {"plan_tier": state["tier"]}

    monkeypatch.setattr(mar, "fetch_audit_run_by_id", fake_fetch)
    monkeypatch.setattr(mar, "get_balance", fake_balance)
    monkeypatch.setattr(mar, "_ACTIONS_PAYWALL_ENABLED", True)
    monkeypatch.setattr(mar, "_SHARE_LINKS_ENABLED", True)
    return state


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(mar.router)
    app.include_router(mar.public_share_router)
    app.dependency_overrides[get_current_merchant] = lambda: "m-1"
    return TestClient(app)


def _gap_text(payload: Any) -> str:
    """The gap names a real product and a real query. Search the WHOLE body
    rather than one key, so a section surviving under any nesting is caught."""
    import json
    return json.dumps(payload)


def test_the_authed_poll_locks_the_gap_for_a_free_owner(wired):
    res = _client().get("/api/merchant-center/audit/url-readiness/r-1")
    assert res.status_code == 200
    body = res.json()
    assert body.get("actions_locked") is True, (
        "the poll did not run the paywall at all"
    )
    assert body.get("selection_gap") is None
    assert (body.get("brand_rollup") or {}).get("selection_gap") is None
    assert "best beginner drone under 300" not in _gap_text(body)
    assert "x1-drone" not in _gap_text(body)
    # ...and the lock still tells the merchant how much is behind it.
    assert body["locked_counts"]["selection_gap"] == 1


def test_the_authed_poll_serves_the_gap_to_a_paid_owner(wired):
    """The positive counterpart: without it, a paywall that nulls the section
    unconditionally would pass every assertion above."""
    wired["tier"] = "pro"
    body = _client().get("/api/merchant-center/audit/url-readiness/r-1").json()
    assert body.get("actions_locked") is not True
    assert body["selection_gap"]["gaps"][0]["query"] == (
        "best beginner drone under 300"
    )


def test_the_public_share_read_locks_the_gap_for_a_free_owner(wired, monkeypatch):
    """The share view is keyed to the OWNER's tier — a free owner's link must
    not hand the paid layer to anyone holding the URL."""
    class _FakeShareDB:
        def __init__(self):
            self.rows = {}

        async def fetch_one(self, query, values=None):
            q = " ".join(str(query).split())
            if "FROM audit_share_tokens" in q and "run_id = :r" in q:
                for t, r in self.rows.items():
                    if r["run_id"] == values["r"] and not r["revoked"]:
                        return {"token": t, "expires_at": "2026-12-01"}
                return None
            if "WHERE token = :t" in q:
                r = self.rows.get(values["t"])
                return {"run_id": r["run_id"]} if r and not r["revoked"] else None
            return None

        async def execute(self, query, values=None):
            q = " ".join(str(query).split())
            if q.startswith("INSERT INTO audit_share_tokens"):
                self.rows[values["t"]] = {"run_id": values["r"], "revoked": False}

    import db.database as dbmod
    monkeypatch.setattr(dbmod, "database", _FakeShareDB())

    client = _client()
    token = client.post(
        "/api/merchant-center/audit/url-readiness/r-1/share"
    ).json()["token"]
    body = client.get(f"/api/public/audit-share/{token}").json()

    assert body["shared_view"] is True
    assert body.get("selection_gap") is None
    assert (body.get("brand_rollup") or {}).get("selection_gap") is None
    assert "best beginner drone under 300" not in _gap_text(body)
    assert "x1-drone" not in _gap_text(body)
