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


# ── Wave-3 B2: share links ───────────────────────────────────────────────────


def _share_client(monkeypatch, *, enabled=True):
    monkeypatch.setattr(mar, "_SHARE_LINKS_ENABLED", enabled)
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


def test_share_mint_public_read_and_revoke(patched, monkeypatch):
    fake_db = _FakeShareDB()
    import db.database as dbmod
    monkeypatch.setattr(dbmod, "database", fake_db)
    client = _share_client(monkeypatch)

    minted = client.post("/api/merchant-center/audit/url-readiness/r-1/share")
    assert minted.status_code == 200
    token = minted.json()["token"]
    assert minted.json()["share_path"] == f"/share/r/{token}"

    # idempotent: second mint returns the same live token
    again = client.post("/api/merchant-center/audit/url-readiness/r-1/share")
    assert again.json()["token"] == token

    # public read: no auth, redacted, noindex
    pub = client.get(f"/api/public/audit-share/{token}")
    assert pub.status_code == 200
    assert pub.headers["x-robots-tag"] == "noindex, nofollow"
    body = pub.json()
    assert body["shared_view"] is True
    assert "custom_prompts" not in body and "brand_report" not in body
    assert "merchant_context" not in body
    assert body["report_summary"]["contract_version"]

    # revoke kills the public read
    assert client.delete("/api/merchant-center/audit/url-readiness/r-1/share").status_code == 200
    assert client.get(f"/api/public/audit-share/{token}").status_code == 404


def test_share_disabled_flag_404s_everything(patched, monkeypatch):
    client = _share_client(monkeypatch, enabled=False)
    assert client.post("/api/merchant-center/audit/url-readiness/r-1/share").status_code == 404
    assert client.get("/api/public/audit-share/whatever").status_code == 404


def test_share_public_read_leaks_no_email_anywhere(patched, monkeypatch):
    # Security review round 2: the REAL builder shapes carry full
    # pitch_recipient dicts on outreach_moves AND pitch_targets, in both
    # where_youre_losing and merchant_narrative.where_youre_losing. The
    # public body must contain NO email string anywhere, ever.
    import re

    fake_db = _FakeShareDB()
    import db.database as dbmod
    monkeypatch.setattr(dbmod, "database", fake_db)
    row = _row()
    losing = row["report_jsonb"]["merchant_narrative"]["where_youre_losing"]
    losing["outreach_moves"] = [
        {
            "host": "soundguys.com",
            "pitch_email": "pr@soundguys.com",
            "pitch_recipient": {"email": "pr@soundguys.com", "note": "x"},
            "pitch_state": "draft_ready",
        }
    ]
    losing["pitch_targets"] = [
        {
            "host": "forbes.com",
            "pitch_recipient": {
                "email": "vetted@forbes.com",
                "submission_url": "https://forbes.com/tips",
            },
        }
    ]
    patched["row"] = row
    client = _share_client(monkeypatch)
    token = client.post("/api/merchant-center/audit/url-readiness/r-1/share").json()["token"]
    res = client.get(f"/api/public/audit-share/{token}")
    raw = res.text
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", raw), raw[:400]
    body = res.json()
    # the structural rows survive (host + state), only routing data is gone
    moves = body["where_youre_losing"]["outreach_moves"]
    assert moves[0]["host"] == "soundguys.com"
    assert "pitch_recipient" not in moves[0] and "pitch_email" not in moves[0]
    assert "pitch_recipient" not in body["where_youre_losing"]["pitch_targets"][0]
    # allowlist: nothing outside the approved key set
    allowed = set(mar._SHARE_ALLOWED_TOP_KEYS) | {"shared_view"}
    assert set(body.keys()) <= allowed, set(body.keys()) - allowed


def test_share_revoke_requires_flag(patched, monkeypatch):
    client = _share_client(monkeypatch, enabled=False)
    assert client.delete("/api/merchant-center/audit/url-readiness/r-1/share").status_code == 404
