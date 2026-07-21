"""Tests for the merchant citation observations read (get-cited proof loop)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

import db.audit_evidence as ae
import routes.merchant_citations as mc
import utils.auth as auth


def _obs(**over: Any) -> Dict[str, Any]:
    base = {
        "observation_id": "obs",
        "audit_run_id": "run1",
        "content_key": "ck_a",
        "product_key": "pk_a",
        "provider": "chatgpt",
        "query": "best collagen",
        "axis": "outcome",
        "query_class": "exploratory",
        "cited_host": "agent.pivota.cc",
        "host_type": "first_party",
        "citation_role": "recommended",
        "first_party": True,
        "is_competitor": False,
        "evidence_url": "https://e/x",
        "observed_at": datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc),
    }
    base.update(over)
    return base


def _rows() -> List[Dict[str, Any]]:
    return [
        _obs(observation_id="o1", provider="chatgpt", citation_role="recommended", first_party=True,
             observed_at=datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)),
        _obs(observation_id="o2", provider="chatgpt", citation_role="mentioned", first_party=False,
             content_key="ck_b", observed_at=datetime(2026, 6, 26, 9, 0, tzinfo=timezone.utc)),
        _obs(observation_id="o3", provider="gemini", citation_role="recommended", first_party=False,
             content_key="ck_a", observed_at=datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc)),
    ]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    captured: Dict[str, Any] = {}

    async def fake_merchant(*, current_user):
        return "merch_test"

    async def fake_fetch(merchant_id, *, content_key=None, limit=200):
        captured["merchant_id"] = merchant_id
        captured["content_key"] = content_key
        captured["limit"] = limit
        return _rows()

    monkeypatch.setattr(auth, "get_current_merchant", fake_merchant)
    monkeypatch.setattr(mc, "fetch_citation_observations", fake_fetch)

    app = FastAPI()
    app.include_router(mc.router)
    app.dependency_overrides[mc.get_current_user] = lambda: {"sub": "u1"}
    return TestClient(app), captured


def test_lists_observations_with_summary(client):
    cl, captured = client
    res = cl.get("/merchant/citations")
    assert res.status_code == 200
    body = res.json()
    assert body["merchant_id"] == "merch_test"
    assert body["count"] == 3
    s = body["summary"]
    assert s["total"] == 3
    assert s["by_provider"] == {"chatgpt": 2, "gemini": 1}
    assert s["by_role"] == {"recommended": 2, "mentioned": 1}
    assert s["first_party"] == 1
    assert s["products_cited"] == 2  # ck_a, ck_b
    assert s["last_observed_at"] == "2026-06-26T09:00:00+00:00"  # the newest
    # projected observations carry iso timestamps + the role/provider
    o = body["observations"][0]
    assert o["observed_at"].startswith("2026-06-25")
    assert o["provider"] == "chatgpt"
    assert o["citation_role"] == "recommended"


def test_is_scoped_to_authenticated_merchant(client):
    cl, captured = client
    cl.get("/merchant/citations?content_key=ck_a&limit=50")
    # the read is driven by the authed merchant, NOT a client-supplied id
    assert captured["merchant_id"] == "merch_test"
    assert captured["content_key"] == "ck_a"
    assert captured["limit"] == 50


@pytest.mark.asyncio
async def test_fetch_returns_empty_for_blank_merchant(monkeypatch: pytest.MonkeyPatch):
    # Guard: no merchant ⇒ no query, empty result (never another merchant's rows).
    out = await ae.fetch_citation_observations("", content_key=None, limit=10)
    assert out == []
