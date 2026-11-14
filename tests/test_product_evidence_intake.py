"""Merchant evidence submission, verification boundary and store helpers."""
from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db.product_evidence as pe


# --- normalize_intake_claims -------------------------------------------------

def test_positioning_is_unverified_no_grade() -> None:
    out = pe.normalize_intake_claims([{"claim_text": "Designed for sensitive skin"}])
    assert out == [{
        "claim_text": "Designed for sensitive skin",
        "source_type": "merchant_positioning",
        "substantiation_status": "unverified",
    }]


def test_lab_report_with_source_is_a_submission_even_with_client_grade() -> None:
    out = pe.normalize_intake_claims([
        {"claim_text": "Clinically shown to reduce wrinkles",
         "source_type": "merchant_lab_report", "source_ref": "artifact_42",
         "evidence_grade": "a", "substantiation_status": "substantiated",
         "review_status": "verified", "reviewed_by": "staff"},
    ])
    assert out == [{
        "claim_text": "Clinically shown to reduce wrinkles",
        "source_type": "merchant_lab_report",
        "source_ref": "artifact_42",
        "substantiation_status": "unverified",
        "review_status": "submitted",
    }]


def test_substantiated_source_without_ref_downgrades_to_unverified() -> None:
    # A merchant can't self-substantiate by just picking the label.
    out = pe.normalize_intake_claims([
        {"claim_text": "Clinically proven", "source_type": "merchant_lab_report"},
    ])
    assert out[0]["substantiation_status"] == "unverified"
    assert out[0]["source_type"] == "merchant_positioning"
    assert "evidence_grade" not in out[0]


def test_supplied_review_url_does_not_substantiate() -> None:
    out = pe.normalize_intake_claims([
        {"claim_text": "Editor's pick", "source_type": "third_party_review",
         "source_ref": "https://press.example/review"},
    ])
    assert "evidence_grade" not in out[0]
    assert out[0]["substantiation_status"] == "unverified"
    assert out[0]["review_status"] == "submitted"


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


async def test_merchant_submission_cannot_assign_review_or_overwrite_evidence(monkeypatch) -> None:
    from routes import merchant_products as products

    reviewed = {
        "claim_text": "Supports hydration",
        "source_type": "merchant_lab_report",
        "source_ref": "art_1",
        "review_status": "verified",
        "substantiation_status": "substantiated",
        "evidence_grade": "a",
        "review_source_type": "third_party_test",
        "review_source_url": "https://source.example/report",
        "reviewed_by": "employee",
    }
    legacy = {
        "claim_text": "Legacy self-grade",
        "source_type": "editorial_press",
        "source_ref": "not-proof",
        "substantiation_status": "substantiated",
        "evidence_grade": "a",
    }
    discovery = {
        "claim_text": "Independent review candidate",
        "source_type": "third_party_review",
        "source_ref": "https://review.example/report",
        "substantiation_status": "unverified",
        "discovered_via": "web_crawl",
    }
    disclaimers = [{"code": "notice"}]
    db = _CaptureDB(fetch_row={"claims": [reviewed, legacy, discovery], "merchant_id": "m1",
                                "review_state": "observed", "required_disclaimers": disclaimers})

    @asynccontextmanager
    async def transaction(product_key):
        assert product_key == "pk1"
        yield db

    async def resolve(*args):
        assert args[0] == "m1"
        return "pk1", "ck1"

    async def noop():
        pass

    stored_fetch = pe.fetch_product_evidence_row

    async def fetch_row(product_key, **kwargs):
        kwargs["db"] = db
        return await stored_fetch(product_key, **kwargs)

    monkeypatch.setattr(pe, "ensure_product_evidence_tables", noop)
    monkeypatch.setattr(pe, "product_evidence_transaction", transaction)
    monkeypatch.setattr(pe, "fetch_product_evidence_row", fetch_row)
    monkeypatch.setattr(products, "database", db)
    monkeypatch.setattr(products, "_resolve_evidence_product", resolve)
    body = products.ProductEvidenceBody.model_validate({
        "review_state": "reviewed",
        "claims": [discovery, {"claim_text": "Claim using a public URL",
                               "source_type": "certification", "source_ref": "https://cert.example/report"}],
    })
    user = {"role": "merchant", "merchant_id": "m1"}
    result = await products.upsert_product_evidence_endpoint("url_audit", "p1", body, current_user=user)
    assert result == {"product_key": "pk1", "claims_written": 2, "substantiated": 0, "served_refresh": False}
    _, written = db.calls[0]
    assert written["rs"] == "observed"
    assert json.loads(written["disc"]) == disclaimers
    claims = json.loads(written["claims"])
    assert claims[0] == reviewed
    assert claims[2]["review_status"] == "submitted" and claims[2]["discovered_via"] == "web_crawl"
    assert claims[3]["review_status"] == "submitted" and claims[3]["substantiation_status"] == "unverified"
    db._fetch_row["claims"] = claims
    await products.upsert_product_evidence_endpoint("url_audit", "p1", body, current_user=user)
    assert len(db.calls) == 1

    visible = await products.get_product_evidence_endpoint("url_audit", "p1", current_user=user)
    assert visible["claims"][0]["review_status"] == "verified"
    assert "reviewed_by" not in visible["claims"][0]
    assert visible["claims"][1]["review_status"] == "submitted"
    assert "evidence_grade" not in visible["claims"][1]
    with pytest.raises(HTTPException) as error:
        await products.upsert_product_evidence_endpoint("url_audit", "p1", body, current_user={"role": "employee"})
    assert error.value.status_code == 403


async def test_lab_submission_requires_exact_owned_extraction_and_inspectable_url(monkeypatch):
    from routes import merchant_products as products

    artifact_records = []
    stored = {"claims": [], "merchant_id": "m1"}

    class FakeDB(_CaptureDB):
        async def fetch_one(self, sql, values=None):
            if isinstance(sql, str) and "FROM evidence_artifact" in sql:
                assert values == {"aid": "art_1", "pk": "pk1", "mid": "m1"}
                return artifact_records[-1] if artifact_records else None
            return stored

    db = FakeDB()

    @asynccontextmanager
    async def transaction(_key):
        yield db

    async def resolve(*_args):
        return "pk1", "ck1"

    async def noop():
        pass

    monkeypatch.setattr(pe, "ensure_product_evidence_tables", noop)
    monkeypatch.setattr(pe, "product_evidence_transaction", transaction)
    monkeypatch.setattr(products, "database", db)
    monkeypatch.setattr(products, "_resolve_evidence_product", resolve)
    user = {"role": "merchant", "merchant_id": "m1"}
    base = {"claim_text": "Supports hydration", "source_type": "merchant_lab_report", "source_ref": "art_1"}

    async def submit(claim):
        body = products.ProductEvidenceBody.model_validate({"claims": [claim]})
        return await products.upsert_product_evidence_endpoint("url_audit", "p1", body, current_user=user)

    citation = {**base, "public_source_url": "https://lab.example/report?variant=25"}
    for claim in [base, {**citation, "public_source_url": "https://lab.example/report?token=private"}, citation]:
        with pytest.raises(HTTPException) as error:
            await submit(claim)
        assert error.value.status_code == 422
    artifact_records.append({"extracted_claim_keys": '[]'})
    with pytest.raises(HTTPException) as error:
        await submit(citation)
    assert error.value.status_code == 422
    assert not db.calls

    artifact_records.append({"extracted_claim_keys": '["Supports hydration"]'})
    await submit(citation)
    written = json.loads(db.calls[0][1]["claims"])
    assert written[0]["public_source_url"] == citation["public_source_url"]
    assert written[0]["source_ref"] == "art_1"
    assert written[0]["review_status"] == "submitted"

    stored["claims"] = written
    monkeypatch.setattr(products, "MAX_PRODUCT_EVIDENCE_CLAIMS", 1)
    await submit(citation)
    with pytest.raises(HTTPException) as error:
        await submit({"claim_text": "Another claim"})
    assert error.value.status_code == 409
    assert len(db.calls) == 1
