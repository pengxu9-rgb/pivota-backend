"""Phase 2b: merchant evidence intake — the grade/substantiation mapping + the
store write/read helpers. The grade mapping is the load-bearing decision (it
converges on the serving gate's a/b/c letters), so it's tested thoroughly."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db.product_evidence as pe


# --- normalize_intake_claims (the grade mapping) -----------------------------

def test_positioning_is_unverified_no_grade() -> None:
    out = pe.normalize_intake_claims([{"claim_text": "Designed for sensitive skin"}])
    assert out == [{
        "claim_text": "Designed for sensitive skin",
        "source_type": "merchant_positioning",
        "substantiation_status": "unverified",
    }]


def test_lab_report_with_source_is_substantiated_grade_a() -> None:
    out = pe.normalize_intake_claims([
        {"claim_text": "Clinically shown to reduce wrinkles",
         "source_type": "merchant_lab_report", "source_ref": "artifact_42"},
    ])
    assert out[0]["substantiation_status"] == "substantiated"
    assert out[0]["evidence_grade"] == "a"
    assert out[0]["source_ref"] == "artifact_42"


def test_substantiated_source_without_ref_downgrades_to_unverified() -> None:
    # A merchant can't self-substantiate by just picking the label.
    out = pe.normalize_intake_claims([
        {"claim_text": "Clinically proven", "source_type": "merchant_lab_report"},
    ])
    assert out[0]["substantiation_status"] == "unverified"
    assert out[0]["source_type"] == "merchant_positioning"
    assert "evidence_grade" not in out[0]


def test_third_party_review_is_grade_b() -> None:
    out = pe.normalize_intake_claims([
        {"claim_text": "Editor's pick", "source_type": "third_party_review",
         "source_ref": "https://press.example/review"},
    ])
    assert out[0]["evidence_grade"] == "b"
    assert out[0]["substantiation_status"] == "substantiated"


def test_empty_text_and_unknown_source() -> None:
    out = pe.normalize_intake_claims([
        {"claim_text": "  "},                                  # dropped
        {"claim_text": "X", "source_type": "weird_source"},    # unknown -> unverified
        "not-a-dict",
    ])
    assert len(out) == 1
    assert out[0]["substantiation_status"] == "unverified"


def test_normalize_non_list_is_empty() -> None:
    assert pe.normalize_intake_claims(None) == []
    assert pe.normalize_intake_claims("x") == []


# --- upsert / fetch (fake DB) ------------------------------------------------

class _CaptureDB:
    def __init__(self, fetch_row=None):
        self.calls = []
        self._fetch_row = fetch_row

    async def execute(self, sql, values):
        self.calls.append((sql, values))

    async def fetch_one(self, *a, **k):
        return self._fetch_row


async def test_upsert_serializes_claims(monkeypatch) -> None:
    async def _noop():
        return None
    monkeypatch.setattr(pe, "ensure_product_evidence_tables", _noop)
    db = _CaptureDB()
    claims = [{"claim_text": "A", "source_type": "merchant_positioning",
               "substantiation_status": "unverified"}]
    await pe.upsert_product_evidence("pk1", merchant_id="m1", claims=claims, db=db)
    assert len(db.calls) == 1
    _, values = db.calls[0]
    assert values["pk"] == "pk1" and values["mid"] == "m1"
    assert json.loads(values["claims"])[0]["claim_text"] == "A"


async def test_fetch_row_coerces_json_claims(monkeypatch) -> None:
    async def _noop():
        return None
    monkeypatch.setattr(pe, "ensure_product_evidence_tables", _noop)
    row = {"product_key": "pk1", "merchant_id": "m1",
           "claims": json.dumps([{"claim_text": "A"}]),  # JSONB may decode as str
           "review_state": "observed", "required_disclaimers": None, "updated_at": None}
    out = await pe.fetch_product_evidence_row("pk1", db=_CaptureDB(fetch_row=row))
    assert out["claims"] == [{"claim_text": "A"}]
    assert out["review_state"] == "observed"
