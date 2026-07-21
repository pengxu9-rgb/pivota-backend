"""Public share momentum payload — charts parity for the shared audit view.

The shared report page renders the same Momentum + engine-split charts as the
authed AI-Visibility report, but it can't call the authed /tracking + /history
endpoints. GET /api/public/audit-share/{token} therefore carries:

  visibility_tracking     — the merchant_url tracking series, minimized for
                            the open web (run ids nulled, no per-SKU series,
                            no merchant id)
  prior_brand_dimensions  — the immediately-previous run's dumbbell baseline

Both are best-effort: a history failure ships the share body without them,
never a 500.

Kept OUT of tests/test_report_deck_route.py on purpose — that file skips
wholesale when python-pptx isn't installed, and nothing here needs pptx.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict

import pytest

os.environ.setdefault(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
)

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.merchant_audit_routes as mar
from tests.services.test_report_summary_builder import _brand_report
from utils.auth import get_current_merchant


def _row(status: str = "succeeded", merchant: str = "m-1") -> Dict[str, Any]:
    return {
        "run_id": "r-1",
        "merchant_id": merchant,
        "subject_type": "merchant_url",
        "status": status,
        "report_jsonb": _brand_report(),
        "partial_result_jsonb": {},
    }


def _share_client(monkeypatch) -> TestClient:
    monkeypatch.setattr(mar, "_SHARE_LINKS_ENABLED", True)
    app = FastAPI()
    app.include_router(mar.router)
    app.include_router(mar.public_share_router)
    app.dependency_overrides[get_current_merchant] = lambda: "m-1"
    return TestClient(app)


class _FakeShareDB:
    def __init__(self):
        self.rows = {}

    async def fetch_one(self, query, values=None):
        q = " ".join(str(query).split())
        if "FROM audit_share_tokens" in q and "run_id = :r" in q:
            for t, r in self.rows.items():
                if r["run_id"] == values["r"] and not r["revoked"]:
                    return {"token": t, "expires_at": "2026-08-14"}
            return None
        if "WHERE token = :t" in q:
            r = self.rows.get(values["t"])
            return {"run_id": r["run_id"]} if r and not r["revoked"] else None
        return None

    async def execute(self, query, values=None):
        q = " ".join(str(query).split())
        if q.startswith("INSERT INTO audit_share_tokens"):
            self.rows[values["t"]] = {"run_id": values["r"], "revoked": False}
        if q.startswith("UPDATE audit_share_tokens"):
            for r in self.rows.values():
                if r["run_id"] == values["r"]:
                    r["revoked"] = True


@pytest.fixture()
def share_env(monkeypatch: pytest.MonkeyPatch):
    state: Dict[str, Any] = {"row": _row()}

    async def fake_fetch(*, run_id: str):
        return state["row"]

    monkeypatch.setattr(mar, "fetch_audit_run_by_id", fake_fetch)
    import db.database as dbmod

    monkeypatch.setattr(dbmod, "database", _FakeShareDB())
    return state


def _history_row(run_id, day, vis, dims=None):
    return {
        "run_id": run_id,
        "requested_at": datetime(2026, 7, day, 12, 0, tzinfo=timezone.utc),
        "visibility": vis,
        "attribution": vis - 5,
        "category_visibility": None,
        "report_jsonb": {
            "brand_rollup": {"dimensions": dims or {}},
        },
    }


def test_share_public_read_carries_momentum_payload(share_env, monkeypatch):
    import db.merchant_audit_runs as runs_db

    prior_dims = {"identity": {"median": 41, "band": "partial"}}
    rows = [
        _history_row("r-a", 1, 20),
        _history_row("r-b", 8, 30, dims=prior_dims),
        _history_row("r-1", 15, 45, dims={"identity": {"median": 60}}),
    ]

    async def fake_history(*, merchant_id, limit, subject_type):
        assert subject_type == "merchant_url"
        return rows

    monkeypatch.setattr(runs_db, "score_history_for_merchant", fake_history)
    client = _share_client(monkeypatch)
    token = client.post(
        "/api/merchant-center/audit/url-readiness/r-1/share"
    ).json()["token"]
    body = client.get(f"/api/public/audit-share/{token}").json()

    tracking = body["visibility_tracking"]
    assert [p["date"][:10] for p in tracking["points"]] == [
        "2026-07-01", "2026-07-08", "2026-07-15",
    ]
    assert all(p["run_id"] is None for p in tracking["points"])
    assert [p["scores"]["visibility"] for p in tracking["points"]] == [20, 30, 45]
    assert "per_sku" not in tracking and "merchant_id" not in tracking
    # prior run = the row immediately before the shared run (r-b)
    assert body["prior_brand_dimensions"] == prior_dims


def test_share_prior_falls_back_to_older_run_when_window_misses(share_env, monkeypatch):
    # The shared run is older than the 50-run history window: the baseline is
    # the newest row strictly OLDER than it — never a newer run.
    import db.merchant_audit_runs as runs_db

    share_env["row"] = {
        **_row(),
        "requested_at": datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc).isoformat(),
    }
    older_dims = {"identity": {"median": 33}}
    rows = [
        _history_row("r-old", 2, 18, dims=older_dims),
        _history_row("r-newer", 18, 50, dims={"identity": {"median": 70}}),
    ]

    async def fake_history(**_kw):
        return rows

    monkeypatch.setattr(runs_db, "score_history_for_merchant", fake_history)
    client = _share_client(monkeypatch)
    token = client.post(
        "/api/merchant-center/audit/url-readiness/r-1/share"
    ).json()["token"]
    body = client.get(f"/api/public/audit-share/{token}").json()
    assert body["prior_brand_dimensions"] == older_dims


def test_share_public_read_survives_momentum_failure(share_env, monkeypatch):
    import db.merchant_audit_runs as runs_db

    async def broken_history(**_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(runs_db, "score_history_for_merchant", broken_history)
    client = _share_client(monkeypatch)
    token = client.post(
        "/api/merchant-center/audit/url-readiness/r-1/share"
    ).json()["token"]
    res = client.get(f"/api/public/audit-share/{token}")
    assert res.status_code == 200
    body = res.json()
    assert "visibility_tracking" not in body
    assert "prior_brand_dimensions" not in body
    assert body["report_summary"]["contract_version"]
