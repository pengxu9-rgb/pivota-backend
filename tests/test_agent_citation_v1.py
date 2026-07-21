"""HTTP tests for the external citation read API (routes.agent_citation_v1)."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

import services.pivot_query_service as pqs
from routes import agent_citation_v1 as cite

_CK_A = "ck_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_CK_B = "ck_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _citable_rows():
    return [
        {
            "content_key": _CK_A,
            "product_title": "Anuko Nourishing Hair Butter",
            "product_description": "Shea butter treatment. For damaged hair.",
            "brand": "Anuko",
            "product_image_url": "https://img.example/anuko.jpg",
        },
        {
            "content_key": _CK_B,
            "product_title": "SKIN1004 Centella Ampoule",
            "product_description": "Centella ampoule.",
            "brand": "SKIN1004",
            "product_image_url": None,
        },
    ]


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


# ── /search ─────────────────────────────────────────────────────────────────


def test_search_inform_returns_citation_items(client_for, monkeypatch: pytest.MonkeyPatch):
    client = client_for(_row())

    async def fake_fetch(*, query, merchant_id, limit):
        return _citable_rows()

    monkeypatch.setattr(pqs, "_index_eligible_recall_enabled", lambda: True)
    monkeypatch.setattr(pqs, "_fetch_citable_canonical_rows", fake_fetch)
    res = client.get("/agent/v1/citation/search?q=hair+butter&intent=inform")
    assert res.status_code == 200
    body = res.json()
    assert body["intent"] == "inform"
    assert body["count"] == 2
    item = body["items"][0]
    # same offer-free CitationItem shape as the single-item read
    assert item["buyable"] is False
    assert item["offers"] is None
    assert item["catalog_track"] == "citation"
    assert item["attribution"]["source"] == "Pivota"
    assert item["attribution"]["canonical_url"] == f"https://agent.pivota.cc/products/{_CK_A}"
    assert item["title"] == "Anuko Nourishing Hair Butter"
    assert item["brand"] == "Anuko"
    # recall rows are light → substantiation empty (full detail via single-item)
    assert item["substantiation"]["claims"] == []


def test_search_shop_intent_suppresses_citations(client_for, monkeypatch: pytest.MonkeyPatch):
    client = client_for(_row())
    monkeypatch.setattr(pqs, "_index_eligible_recall_enabled", lambda: True)
    res = client.get("/agent/v1/citation/search?q=hair&intent=shop")
    assert res.status_code == 200
    body = res.json()
    assert body["intent"] == "shop"
    assert body["items"] == []


def test_search_returns_empty_when_recall_flag_off(client_for, monkeypatch: pytest.MonkeyPatch):
    client = client_for(_row())
    monkeypatch.setattr(pqs, "_index_eligible_recall_enabled", lambda: False)
    res = client.get("/agent/v1/citation/search?q=hair&intent=inform")
    assert res.status_code == 200
    assert res.json()["items"] == []


def test_search_empty_query_returns_empty(client_for):
    res = client_for(_row()).get("/agent/v1/citation/search?q=")
    assert res.status_code == 200
    assert res.json()["items"] == []


# ── B④-P1 attribution telemetry wiring ───────────────────────────────────────


@pytest.fixture
def capture_logs(monkeypatch: pytest.MonkeyPatch):
    """Capture the fields each handler hands to telemetry, bypassing the real
    fire-and-forget task (deterministic, no event-loop race)."""
    calls: list[Dict[str, Any]] = []
    monkeypatch.setattr(cite, "_spawn_log", lambda **f: calls.append(f))
    return calls


def test_single_read_hit_logs_telemetry(client_for, capture_logs):
    ck = "ck_0123456789abcdef0123456789abcdef"
    res = client_for(_row()).get(f"/agent/v1/citation/{ck}")
    assert res.status_code == 200
    assert len(capture_logs) == 1
    ev = capture_logs[0]
    assert ev["endpoint"] == "item"
    assert ev["status"] == cite.STATUS_HIT
    assert ev["requested_id"] == ck
    assert ev["content_key"] == ck


def test_single_read_miss_logs_telemetry(client_for, capture_logs):
    ck = "ck_ffffffffffffffffffffffffffffffff"
    res = client_for(None).get(f"/agent/v1/citation/{ck}")
    assert res.status_code == 404
    assert len(capture_logs) == 1
    ev = capture_logs[0]
    assert ev["endpoint"] == "item"
    assert ev["status"] == cite.STATUS_MISS
    assert ev["requested_id"] == ck
    assert ev["content_key"] is None


def test_agent_header_captured_in_telemetry(client_for, capture_logs):
    client_for(_row()).get(
        "/agent/v1/citation/ck_0123456789abcdef0123456789abcdef",
        headers={"X-Pivota-Agent": "openai-chatgpt/1.0"},
    )
    assert capture_logs[-1]["agent"] == "openai-chatgpt/1.0"


def test_search_hit_logs_result_count(client_for, capture_logs, monkeypatch: pytest.MonkeyPatch):
    async def fake_fetch(*, query, merchant_id, limit):
        return _citable_rows()

    monkeypatch.setattr(pqs, "_index_eligible_recall_enabled", lambda: True)
    monkeypatch.setattr(pqs, "_fetch_citable_canonical_rows", fake_fetch)
    client_for(_row()).get("/agent/v1/citation/search?q=hair&intent=inform")
    ev = capture_logs[-1]
    assert ev["endpoint"] == "search"
    assert ev["status"] == cite.STATUS_HIT
    assert ev["result_count"] == 2
    assert ev["query"] == "hair"


def test_search_shop_logs_suppressed(client_for, capture_logs, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pqs, "_index_eligible_recall_enabled", lambda: True)
    client_for(_row()).get("/agent/v1/citation/search?q=hair&intent=shop")
    assert capture_logs[-1]["status"] == cite.STATUS_SUPPRESSED


def test_search_recall_off_logs_disabled(client_for, capture_logs, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pqs, "_index_eligible_recall_enabled", lambda: False)
    client_for(_row()).get("/agent/v1/citation/search?q=hair&intent=inform")
    assert capture_logs[-1]["status"] == cite.STATUS_DISABLED


def test_telemetry_disabled_by_default_spawns_no_write(client_for, monkeypatch: pytest.MonkeyPatch):
    # Real _spawn_log with the flag OFF must never reach log_citation_read.
    monkeypatch.delenv("CITATION_READ_TELEMETRY", raising=False)
    reached: list = []
    monkeypatch.setattr(cite, "log_citation_read", lambda **f: reached.append(f))
    res = client_for(_row()).get("/agent/v1/citation/ck_0123456789abcdef0123456789abcdef")
    assert res.status_code == 200
    assert reached == []


async def test_spawn_log_schedules_write_when_flag_on(monkeypatch: pytest.MonkeyPatch):
    # Flag ON: _spawn_log schedules the best-effort write coroutine (tested
    # directly to control the loop, avoiding a TestClient scheduling race).
    monkeypatch.setenv("CITATION_READ_TELEMETRY", "1")
    seen: list[Dict[str, Any]] = []

    async def fake_log(**fields):
        seen.append(fields)

    monkeypatch.setattr(cite, "log_citation_read", fake_log)
    cite._spawn_log(endpoint="item", status=cite.STATUS_HIT, content_key="ck_z")
    await asyncio.sleep(0)  # let the scheduled task run
    assert seen and seen[0]["endpoint"] == "item"
    assert seen[0]["content_key"] == "ck_z"


async def test_spawn_log_noop_when_flag_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CITATION_READ_TELEMETRY", raising=False)
    seen: list = []

    async def fake_log(**fields):
        seen.append(fields)

    monkeypatch.setattr(cite, "log_citation_read", fake_log)
    cite._spawn_log(endpoint="item", status=cite.STATUS_HIT)
    await asyncio.sleep(0)
    assert seen == []

