"""P1 — substantiation grading service: an employee grades a submitted evidence
row; 'substantiated' advances the product attested -> substantiated and refreshes
the served PDP. flagged/rejected update the row but never advance."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException

import db.brand_attestation_evidence as evidence_db
import db.product_evidence as pe
from routes import substantiation_admin_routes as admin
import services.agent_pdp_view_assembler as asm
import services.claim_state as cs
import services.substantiation_grading_service as grading
from services.claim_safety import substantiated_claims


def _evidence_row():
    return {
        "id": 7,
        "merchant_id": "m1",
        "product_key": "m1|url_audit|x",
        "content_key": "ck_x",
        "evidence_ref": "lab.pdf",
        "grading_status": "submitted",
    }


def _wire(monkeypatch, row):
    calls = {"updated": None, "advanced": False, "refreshed": False}

    async def _get(eid):
        return row

    async def _update(eid, *, grading_status, graded_by, notes=None):
        calls["updated"] = (eid, grading_status, graded_by, notes)
        return True

    async def _advance(*, merchant_id, product_key):
        calls["advanced"] = True
        return True

    async def _refresh(ck, *, refresh_source, db=None):
        calls["refreshed"] = True
        return True

    monkeypatch.setattr(evidence_db, "get_evidence", _get)
    monkeypatch.setattr(evidence_db, "update_evidence_grade", _update)
    monkeypatch.setattr(cs, "advance_product_to_substantiated", _advance)
    monkeypatch.setattr(asm, "refresh_agent_pdp_view_for_content_key", _refresh)
    return calls


def test_substantiated_advances_and_refreshes(monkeypatch):
    calls = _wire(monkeypatch, _evidence_row())
    res = asyncio.run(
        grading.grade_evidence(7, graded_by="emp1", grade="substantiated", notes="lab ok")
    )
    assert res["status"] == "graded"
    assert res["grade"] == "substantiated"
    assert res["product_advanced"] is True
    assert calls["updated"][1] == "substantiated"
    assert calls["updated"][2] == "emp1"  # grader recorded
    assert calls["advanced"] is True
    assert calls["refreshed"] is True  # served PDP refreshed so agents see it


def test_flagged_updates_but_does_not_advance(monkeypatch):
    calls = _wire(monkeypatch, _evidence_row())
    res = asyncio.run(grading.grade_evidence(7, graded_by="emp1", grade="flagged"))
    assert res["status"] == "graded"
    assert res["product_advanced"] is False
    assert calls["updated"][1] == "flagged"
    assert calls["advanced"] is False  # only 'substantiated' advances the lifecycle
    assert calls["refreshed"] is False


def test_invalid_grade_rejected(monkeypatch):
    _wire(monkeypatch, _evidence_row())
    res = asyncio.run(grading.grade_evidence(7, graded_by="emp1", grade="approved"))
    assert res["status"] == "invalid_grade"


def test_missing_evidence(monkeypatch):
    _wire(monkeypatch, None)
    res = asyncio.run(
        grading.grade_evidence(7, graded_by="emp1", grade="substantiated")
    )
    assert res["status"] == "not_found"


def test_no_grader_is_forbidden():
    res = asyncio.run(grading.grade_evidence(7, graded_by="", grade="substantiated"))
    assert res["status"] == "forbidden"


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("ENABLE_SUBSTANTIATION_GRADING", raising=False)
    assert grading.grading_enabled() is False
    monkeypatch.setenv("ENABLE_SUBSTANTIATION_GRADING", "true")
    assert grading.grading_enabled() is True


async def test_claim_verification_binds_exact_submission_and_publishes_reviewed_source(monkeypatch):
    submitted, sibling = pe.normalize_intake_claims([
        {"claim_text": "Provides 4-hour hydration", "source_type": "merchant_lab_report", "source_ref": "art_a"},
        {"claim_text": "Provides 8-hour hydration", "source_type": "merchant_lab_report", "source_ref": "art_a"},
    ])
    written = []
    refreshed = []

    class FakeDB:
        async def fetch_one(self, sql, values):
            if "FROM catalog_products" in sql:
                assert values == {"key": "pk", "merchant_id": "m1"}
                return {"content_key": "ck"}
            assert values == {"pk": "pk", "geo": "default"}
            return {"claims": [submitted, sibling], "merchant_id": "m1"}

        async def execute(self, sql, values):
            written.append(json.loads(values["claims"]))

    @asynccontextmanager
    async def transaction(product_key):
        assert product_key == "pk"
        yield FakeDB()

    async def noop():
        pass

    async def refresh(content_key, *, refresh_source):
        assert written
        refreshed.append((content_key, refresh_source))
        return True

    monkeypatch.setattr(pe, "ensure_product_evidence_tables", noop)
    monkeypatch.setattr(pe, "product_evidence_transaction", transaction)
    monkeypatch.setattr(asm, "refresh_agent_pdp_view_for_content_key", refresh)
    base = {
        "merchant_id": "m1", "product_key": "pk", "claim_text": "Provides 4-hour hydration",
        "source_type": "merchant_lab_report", "source_ref": "art_a",
        "citation_url": "https://retailer.example/listing", "citation_type": "retailer_listing",
        "evidence_grade": "c", "verified_source_supports_product_and_claim": True,
    }
    body = admin.VerifyClaimBody.model_validate(base)
    staff = {"employee_id": "reviewer", "role": "employee"}
    monkeypatch.delenv("ENABLE_SUBSTANTIATION_GRADING", raising=False)
    with pytest.raises(HTTPException) as error:
        await admin.verify_product_claim(body, employee=staff)
    assert error.value.status_code == 404
    monkeypatch.setenv("ENABLE_SUBSTANTIATION_GRADING", "true")
    with pytest.raises(HTTPException) as error:
        await admin.verify_product_claim(body, employee={})
    assert error.value.status_code == 403
    with pytest.raises(HTTPException) as error:
        await admin.verify_product_claim(body, employee={**staff, "role": "outsourced"})
    assert error.value.status_code == 403
    for update, code in [
        ({"claim_text": "Invented wording"}, 404),
        ({"source_ref": "art_b"}, 404),
        ({"citation_url": "https://127.0.0.1/report"}, 422),
        ({"citation_url": "https://lab.example/report?api_key=private"}, 422),
        ({"citation_url": "https://lab.example/report#private"}, 422),
    ]:
        with pytest.raises(HTTPException) as error:
            await admin.verify_product_claim(
                admin.VerifyClaimBody.model_validate({**base, **update}),
                employee=staff,
            )
        assert error.value.status_code == code
    assert written == [] and refreshed == []

    result = await admin.verify_product_claim(body, employee=staff)
    assert result == {"status": "verified", "product_key": "pk", "served_refresh": True}
    assert written[0][0]["source_ref"] == "art_a"
    assert written[0][0]["reviewed_by"] == "reviewer"
    assert written[0][1] == sibling
    assert substantiated_claims(written[0]) == [{
        "claim_text": "Provides 4-hour hydration",
        "source_ref": "https://retailer.example/listing",
        "source_type": "retailer_listing",
        "evidence_grade": "c",
    }]
    assert refreshed == [("ck", "product_claim_review")]


async def test_staff_submission_read_returns_only_bound_pending_claims(monkeypatch):
    from db.database import database

    public_source_url = "https://report.example/document?variant=25"
    pending = {"claim_text": "Hydrates", "source_type": "merchant_lab_report", "source_ref": "art_1",
               "public_source_url": public_source_url, "review_status": "submitted"}

    async def fetch_one(sql, values):
        assert "merchant_id = :mid" in sql
        return {"exists": 1} if values == {"pk": "pk", "mid": "m1"} else None

    async def fetch_evidence(product_key, *, strict):
        assert product_key == "pk" and strict is True
        return {"merchant_id": "m1", "claims": [pending, {**pending, "review_status": "verified"}]}

    monkeypatch.setattr(database, "fetch_one", fetch_one)
    monkeypatch.setattr(pe, "fetch_product_evidence_row", fetch_evidence)
    monkeypatch.setenv("ENABLE_SUBSTANTIATION_GRADING", "true")
    staff = {"employee_id": "emp1", "role": "employee"}
    response = await admin.list_submitted_product_claims("m1", "pk", staff)
    assert response == {"product_key": "pk", "claims": [{k: v for k, v in pending.items() if k != "review_status"}]}
    with pytest.raises(HTTPException) as error:
        await admin.list_submitted_product_claims("m2", "pk", staff)
    assert error.value.status_code == 404
