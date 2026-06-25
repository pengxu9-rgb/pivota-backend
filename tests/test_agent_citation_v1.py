"""HTTP tests for the external citation read API (routes.agent_citation_v1)."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import pytest

os.environ.setdefault(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
)

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import agent_citation_v1 as cite


def _row(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "content_key": "ck_0123456789abcdef0123456789abcdef",
        "pivota_signature_id": None,
        "title": "Anuko Nourishing Hair Butter",
        "brand": "Anuko",
        "description": "A rich shea butter treatment for damaged hair. Mixed berry scent.",
        "bullet_points": ["Shea butter + green tea", "For damaged hair"],
        "usage_scenarios": ["Apply to damp hair"],
        "taxonomy_tags": ["haircare", "treatment"],
        "image_url": "https://img.example/anuko.jpg",
        "evidence_profile": {
            "claims": [
                {
                    "claim_text": "Nourishes damaged hair",
                    "source_type": "ingredient_mechanism",
                    "substantiation_status": "substantiated",
                }
            ],
            "review_state": "observed",
        },
        "required_disclaimers": [],
    }
    base.update(overrides)
    return base


class FakeDb:
    def __init__(self, row: Optional[Dict[str, Any]]) -> None:
        self._row = row

    async def fetch_one(self, query: Any, params: Any = None) -> Optional[Dict[str, Any]]:
        return self._row


@pytest.fixture
def client_for(monkeypatch: pytest.MonkeyPatch):
    def _make(row: Optional[Dict[str, Any]]) -> TestClient:
        monkeypatch.setenv("INDEX_ELIGIBLE_READ", "1")
        monkeypatch.setattr(cite, "database", FakeDb(row))
        app = FastAPI()
        app.include_router(cite.router)
        return TestClient(app)

    return _make


def test_citation_item_shape_and_invariants(client_for):
    res = client_for(_row()).get("/agent/v1/citation/ck_0123456789abcdef0123456789abcdef")
    assert res.status_code == 200
    body = res.json()
    assert body["content_key"] == "ck_0123456789abcdef0123456789abcdef"
    assert body["title"].startswith("Anuko")
    assert body["brand"] == "Anuko"
    # offer-free invariants (citation, not commerce)
    assert body["buyable"] is False
    assert body["offers"] is None
    assert body["catalog_track"] == "citation"
    # attribution — the moat
    assert body["attribution"]["source"] == "Pivota"
    assert (
        body["attribution"]["canonical_url"]
        == "https://agent.pivota.cc/products/ck_0123456789abcdef0123456789abcdef"
    )
    assert body["attribution"]["cite_as"] == "Pivota — agent.pivota.cc"
    assert body["attribution"]["attribution_required"] is True
    # substantiation — claims present, coverage disclosed (not faked)
    assert len(body["substantiation"]["claims"]) >= 1
    assert body["substantiation"]["verify_coverage"] is None
    # one-line summary an agent can quote
    assert body["summary"] and body["summary"].endswith(".")
    # cacheable
    assert "max-age" in res.headers.get("cache-control", "")


def test_no_merchant_private_or_commerce_fields_leak(client_for):
    res = client_for(_row(primary_merchant_id="merch_secret", price_min=42)).get(
        "/agent/v1/citation/ck_0123456789abcdef0123456789abcdef"
    )
    body = res.json()
    assert "merch_secret" not in str(body)
    assert "primary_merchant_id" not in body
    assert "price" not in body and "price_min" not in body
    assert body["offers"] is None


def test_404_when_row_missing(client_for):
    res = client_for(None).get("/agent/v1/citation/ck_ffffffffffffffffffffffffffffffff")
    assert res.status_code == 404


def test_rate_limit_returns_429_with_retry_after(client_for, monkeypatch: pytest.MonkeyPatch):
    client = client_for(_row())

    async def deny(key: str, tier: str = "standard"):
        return False, {"limit": 1000, "remaining": 0, "reset": 9999999999}

    monkeypatch.setattr(cite._limiter, "check_limit", deny)
    res = client.get("/agent/v1/citation/ck_0123456789abcdef0123456789abcdef")
    assert res.status_code == 429
    assert res.headers.get("Retry-After")


def test_unknown_id_shape_is_404(client_for):
    # A value that isn't a content_key / sig / pg / ext resolves to no SQL → 404.
    res = client_for(_row()).get("/agent/v1/citation/not-a-real-id")
    assert res.status_code == 404
