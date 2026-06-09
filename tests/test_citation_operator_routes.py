"""Route tests for the citation operator HTTP surface.

A minimal app mounts just the router; `get_current_merchant` is overridden to a
fixed merchant, and the service layer is mocked. No DB, no LLM, no real auth.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.citation_operator_routes as rt
from utils.auth import get_current_merchant


def _client(merchant_id: str = "m1") -> TestClient:
    app = FastAPI()
    app.include_router(rt.router)
    app.dependency_overrides[get_current_merchant] = lambda: merchant_id
    return TestClient(app)


def _async(value):
    async def _f(*args, **kwargs):
        return value
    return _f


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------

def test_scan_runs_and_returns_summary(monkeypatch) -> None:
    captured = {}
    async def fake_scan(**kwargs):
        captured.update(kwargs)
        return {"billing_mode": "metered",
                "providers": {"chatgpt": {"channel_mix": {"community": 2}, "targets": []}}}
    monkeypatch.setattr(rt.cos, "run_citation_scan", fake_scan)

    resp = _client().post("/api/citation-operator/scan", json={
        "queries": ["is collagen worth it", "does it work"], "merchant_brand": "BB Lab"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["merchant_id"] == "m1"
    assert body["billing_mode"] == "metered"
    assert body["providers"]["chatgpt"]["channel_mix"]["community"] == 2
    # merchant_id comes from the JWT, not the body.
    assert captured["merchant_id"] == "m1"
    assert captured["providers"] == ["gemini", "chatgpt"]


def test_dashboard_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(rt.dash, "overview", _async(
        {"providers": {"chatgpt": {"visibility_score": 33}}, "has_data": True}))
    monkeypatch.setattr(rt.dash, "progress", _async(
        {"funnel": {"draft": 1}, "confirmed_count": 0, "confirmed": []}))
    resp = _client().get("/api/citation-operator/dashboard")
    assert resp.status_code == 200
    body = resp.json()
    assert body["overview"]["providers"]["chatgpt"]["visibility_score"] == 33
    assert body["progress"]["funnel"]["draft"] == 1


def test_dashboard_trend_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(rt.dash, "trend", _async({"provider": "chatgpt", "points": []}))
    resp = _client().get("/api/citation-operator/dashboard/trend?provider=chatgpt")
    assert resp.status_code == 200
    assert resp.json()["provider"] == "chatgpt"


def test_dashboard_trend_rejects_bad_provider() -> None:
    resp = _client().get("/api/citation-operator/dashboard/trend?provider=perplexity")
    assert resp.status_code == 400


def test_run_due_rechecks_admin_endpoint(monkeypatch) -> None:
    from utils.auth import require_admin_or_key
    called = {}
    async def fake_run(**kw):
        called.update(kw)
        return {"processed": 3, "results": []}
    monkeypatch.setattr(rt.cos, "run_due_rechecks", fake_run)

    app = FastAPI()
    app.include_router(rt.router)
    # Bypass admin auth (its enforcement is tested in utils.auth); assert wiring.
    app.dependency_overrides[require_admin_or_key] = lambda: {"role": "admin"}
    resp = TestClient(app).post("/api/citation-operator/admin/run-due-rechecks?limit=25")
    assert resp.status_code == 200
    assert resp.json()["processed"] == 3
    assert called["limit"] == 25


def test_scan_rejects_too_many_queries() -> None:
    resp = _client().post("/api/citation-operator/scan",
                          json={"queries": [f"q{i}" for i in range(9)]})
    assert resp.status_code == 400
    assert "at most" in resp.json()["detail"]


def test_scan_rejects_unknown_provider() -> None:
    resp = _client().post("/api/citation-operator/scan",
                          json={"queries": ["q"], "providers": ["perplexity"]})
    assert resp.status_code == 400
    assert "unsupported provider" in resp.json()["detail"]


# --------------------------------------------------------------------------
# targets + draft (ownership)
# --------------------------------------------------------------------------

def test_list_targets(monkeypatch) -> None:
    monkeypatch.setattr(rt.cos, "list_targets",
                        _async([{"target_id": "t1", "channel": "community"}]))
    resp = _client().get("/api/citation-operator/targets?channel=community")
    assert resp.status_code == 200
    assert resp.json()["count"] == 1


def test_draft_happy_path(monkeypatch) -> None:
    monkeypatch.setattr(rt.cos, "get_target", _async(
        {"target_id": "t1", "merchant_id": "m1", "channel": "community",
         "subreddit": "SkincareAddiction", "question_text": "is it worth it", "sku_key": "sku1"}))
    gen_args = {}
    async def fake_gen(**kwargs):
        gen_args.update(kwargs)
        return {"billing_mode": "metered", "credits": 1, "draft_content": "hi",
                "action_id": "cact_1"}
    monkeypatch.setattr(rt.cds, "generate_draft", fake_gen)

    resp = _client().post("/api/citation-operator/targets/t1/draft",
                          json={"action_type": "reddit_reply", "brand": "BB Lab"})
    assert resp.status_code == 200
    assert resp.json()["billing_mode"] == "metered"
    # Context assembled from target; sku_key threaded through.
    assert gen_args["sku_key"] == "sku1"
    assert gen_args["context"]["subreddit"] == "SkincareAddiction"
    assert gen_args["context"]["question"] == "is it worth it"


def test_draft_404_when_target_not_owned(monkeypatch) -> None:
    monkeypatch.setattr(rt.cos, "get_target", _async(None))  # not found / not owned
    resp = _client().post("/api/citation-operator/targets/t1/draft",
                          json={"action_type": "reddit_reply"})
    assert resp.status_code == 404


def test_draft_400_on_bad_action_type() -> None:
    resp = _client().post("/api/citation-operator/targets/t1/draft",
                          json={"action_type": "spam_everywhere"})
    assert resp.status_code == 400
    assert "unsupported action_type" in resp.json()["detail"]


# --------------------------------------------------------------------------
# actions: posted + recheck (tracking loop)
# --------------------------------------------------------------------------

def test_mark_posted(monkeypatch) -> None:
    states = iter([
        {"action_id": "a1", "merchant_id": "m1", "status": "draft", "target_id": "t1"},
        {"action_id": "a1", "merchant_id": "m1", "status": "measuring", "target_id": "t1"},
    ])
    async def fake_get(**kw):
        return next(states)
    monkeypatch.setattr(rt.cos, "get_action", fake_get)
    monkeypatch.setattr(rt.cos, "mark_action_posted", _async(None))

    resp = _client().post("/api/citation-operator/actions/a1/posted",
                          json={"posted_url": "https://reddit.com/r/x/y"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "measuring"


def test_posted_404_when_not_owned(monkeypatch) -> None:
    monkeypatch.setattr(rt.cos, "get_action", _async(None))
    resp = _client().post("/api/citation-operator/actions/a1/posted",
                          json={"posted_url": "https://x"})
    assert resp.status_code == 404


def test_recheck_uses_target_queries(monkeypatch) -> None:
    monkeypatch.setattr(rt.cos, "get_action", _async(
        {"action_id": "a1", "merchant_id": "m1", "target_id": "t1"}))
    monkeypatch.setattr(rt.cos, "get_target", _async(
        {"target_id": "t1", "merchant_id": "m1",
         "question_text": "is it worth it; does it work"}))
    rc_args = {}
    async def fake_recheck(**kw):
        rc_args.update(kw)
        return {"status": "confirmed", "brand_cited": True}
    monkeypatch.setattr(rt.cos, "recheck_action", fake_recheck)

    resp = _client().post("/api/citation-operator/actions/a1/recheck", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "confirmed"
    # Queries recovered from the target's stored question_text (split on ';').
    assert rc_args["queries"] == ["is it worth it", "does it work"]
    # Re-probe is scoped to the JWT merchant (data-layer ownership).
    assert rc_args["merchant_id"] == "m1"


def test_recheck_caps_query_count(monkeypatch) -> None:
    monkeypatch.setattr(rt.cos, "get_action", _async(
        {"action_id": "a1", "merchant_id": "m1", "target_id": "t1"}))
    monkeypatch.setattr(rt.cos, "get_target", _async({"target_id": "t1", "merchant_id": "m1"}))
    called = {"recheck": False}
    async def fake_recheck(**kw):
        called["recheck"] = True
        return {}
    monkeypatch.setattr(rt.cos, "recheck_action", fake_recheck)

    resp = _client().post("/api/citation-operator/actions/a1/recheck",
                          json={"queries": [f"q{i}" for i in range(9)]})
    assert resp.status_code == 400          # uncapped fan-out is rejected...
    assert called["recheck"] is False       # ...before any LLM probe runs


def test_recheck_400_when_no_queries(monkeypatch) -> None:
    monkeypatch.setattr(rt.cos, "get_action", _async(
        {"action_id": "a1", "merchant_id": "m1", "target_id": "t1"}))
    monkeypatch.setattr(rt.cos, "get_target", _async(
        {"target_id": "t1", "merchant_id": "m1", "question_text": ""}))  # nothing to recover
    resp = _client().post("/api/citation-operator/actions/a1/recheck", json={})
    assert resp.status_code == 400


def test_scan_rejects_oversized_query() -> None:
    resp = _client().post("/api/citation-operator/scan",
                          json={"queries": ["x" * (rt.MAX_QUERY_CHARS + 1)]})
    assert resp.status_code == 400
    assert str(rt.MAX_QUERY_CHARS) in resp.json()["detail"]


def test_auth_required_without_token() -> None:
    """No dependency override -> the real get_current_merchant must reject."""
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(rt.router)
    resp = TestClient(app).get("/api/citation-operator/targets")
    assert resp.status_code in (401, 403)
