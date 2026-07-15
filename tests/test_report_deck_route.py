"""HTTP-level tests for POST /url-readiness/{run_id}/deck (PR-4).

Coverage:
  - 404: run missing / other merchant's / wrong subject_type
  - 409: run not finished
  - free tier: preview_only — single watermarked slide, no LLM, 0 credits
  - paid tier + LLM usage: metered — credits = actual tokens x 1.6 (via
    credits_for_tokens), consume called once with the run-scoped
    idempotency key
  - paid tier, LLM down: full deck still ships, 'included', 0 credits
  - paid tier, empty wallet: 402 with credits_required
"""

from __future__ import annotations

import io
import os
from typing import Any, Dict

import pytest

os.environ.setdefault(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
)

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.merchant_audit_routes as mar
from services.merchant_credit_balance_service import InsufficientCreditsError
from tests.services.test_report_summary_builder import _brand_report
from utils.auth import get_current_merchant

pptx = pytest.importorskip("pptx")


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(mar.router)
    app.dependency_overrides[get_current_merchant] = lambda: "m-1"
    return TestClient(app)


def _row(status: str = "succeeded", merchant: str = "m-1") -> Dict[str, Any]:
    return {
        "run_id": "r-1",
        "merchant_id": merchant,
        "subject_type": "merchant_url",
        "status": status,
        "report_jsonb": _brand_report(),
        "partial_result_jsonb": {},
    }


def _slide_count(body: bytes) -> int:
    return len(pptx.Presentation(io.BytesIO(body)).slides)


@pytest.fixture()
def patched(monkeypatch: pytest.MonkeyPatch):
    """Default happy-path patches; tests override per-case."""
    state: Dict[str, Any] = {"row": _row(), "paid": False, "consumed": []}

    async def fake_fetch(*, run_id: str):
        return state["row"]

    async def fake_paid(merchant_id: str, **_kw):
        return state["paid"]

    async def fake_exec(summary):
        return state.get("exec")

    async def fake_consume(merchant_id, operation_type, idempotency_key, **kw):
        state["consumed"].append(
            {
                "merchant_id": merchant_id,
                "operation_type": operation_type,
                "idempotency_key": idempotency_key,
                **kw,
            }
        )
        exc = state.get("consume_raises")
        if exc:
            raise exc
        return {"credits": kw.get("credits"), "category": "execution"}

    monkeypatch.setattr(mar, "fetch_audit_run_by_id", fake_fetch)
    monkeypatch.setattr(mar, "merchant_is_paid_tier", fake_paid)
    monkeypatch.setattr(mar, "generate_executive_summary", fake_exec)
    monkeypatch.setattr(mar, "consume_credits", fake_consume)
    return state


def test_404_for_other_merchants_run(patched):
    patched["row"] = _row(merchant="someone-else")
    res = _client().post("/api/merchant-center/audit/url-readiness/r-1/deck")
    assert res.status_code == 404


def test_409_when_run_not_finished(patched):
    patched["row"] = _row(status="running")
    res = _client().post("/api/merchant-center/audit/url-readiness/r-1/deck")
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "audit_not_finished"


def test_free_tier_gets_watermarked_preview_and_no_charge(patched):
    patched["paid"] = False
    res = _client().post("/api/merchant-center/audit/url-readiness/r-1/deck")
    assert res.status_code == 200
    assert res.headers["x-pivota-billing-mode"] == "preview_only"
    assert res.headers["x-pivota-credits-charged"] == "0"
    assert "attachment" in res.headers["content-disposition"]
    assert _slide_count(res.content) == 1  # preview = cover only
    assert patched["consumed"] == []  # free tier is never debited


def test_paid_tier_meters_actual_tokens_at_1_6x(patched):
    patched["paid"] = True
    # 3k in / 500 out on deepseek -> under a cent of COGS x1.6 -> ceil -> 1 credit
    patched["exec"] = (["State: AI does not recommend the brand."], 3000, 500)
    res = _client().post("/api/merchant-center/audit/url-readiness/r-1/deck")
    assert res.status_code == 200
    assert res.headers["x-pivota-billing-mode"] == "metered"
    assert res.headers["x-pivota-credits-charged"] == "1"
    assert _slide_count(res.content) == 5  # full deck incl. exec summary
    (call,) = patched["consumed"]
    assert call["operation_type"] == "report_deck_export"
    assert call["idempotency_key"] == "report_deck:r-1"
    assert call["credits"] == 1
    assert call["usd_cogs"] > 0


def test_paid_tier_llm_down_ships_full_deck_unbilled(patched):
    patched["paid"] = True
    patched["exec"] = None  # no key / provider empty
    res = _client().post("/api/merchant-center/audit/url-readiness/r-1/deck")
    assert res.status_code == 200
    assert res.headers["x-pivota-billing-mode"] == "included"
    assert res.headers["x-pivota-credits-charged"] == "0"
    assert _slide_count(res.content) == 4  # full deck, no exec slide
    assert patched["consumed"] == []


def test_paid_tier_empty_wallet_402(patched):
    patched["paid"] = True
    patched["exec"] = (["bullet"], 3000, 500)
    patched["consume_raises"] = InsufficientCreditsError("m-1", "execution", 1, 0)
    res = _client().post("/api/merchant-center/audit/url-readiness/r-1/deck")
    assert res.status_code == 402
    detail = res.json()["detail"]
    assert detail["code"] == "insufficient_credits"
    assert detail["credits_required"] == 1


def test_renderer_unavailable_503_never_debits(patched, monkeypatch):
    # Review P1 (round 2): the deck must render BEFORE any debit — a missing
    # python-pptx on the serving image 503s with no money moved.
    patched["paid"] = True
    patched["exec"] = (["bullet"], 3000, 500)
    monkeypatch.setattr(mar, "build_report_deck", lambda *a, **k: None)
    res = _client().post("/api/merchant-center/audit/url-readiness/r-1/deck")
    assert res.status_code == 503
    assert res.json()["detail"]["code"] == "deck_renderer_unavailable"
    assert patched["consumed"] == []


def test_404_for_wrong_subject_type(patched):
    patched["row"] = {**_row(), "subject_type": "merchant"}
    res = _client().post("/api/merchant-center/audit/url-readiness/r-1/deck")
    assert res.status_code == 404


def test_409_when_summary_unavailable(patched):
    row = _row()
    row["report_jsonb"] = {}  # no scores, no narrative -> nothing renderable
    patched["row"] = row
    res = _client().post("/api/merchant-center/audit/url-readiness/r-1/deck")
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "summary_unavailable"
