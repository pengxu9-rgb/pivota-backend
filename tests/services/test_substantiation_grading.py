"""P1 — substantiation grading service: an employee grades a submitted evidence
row; 'substantiated' advances the product attested -> substantiated and refreshes
the served PDP. flagged/rejected update the row but never advance."""

import asyncio

import db.brand_attestation_evidence as evidence_db
import services.agent_pdp_view_assembler as asm
import services.claim_state as cs
import services.substantiation_grading_service as grading


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
