"""P1 — support-assisted (manual) brand-claim verification.

A Pivota EMPLOYEE, having reviewed a brand's ownership evidence offline, approves
a method='manual' claim. The human review IS the verification (no automated
DNS/email proof, no B1 auto-binding); it grants brand_direct directly and records
the approving employee in the proof trail. Employee-gated at the route.
"""

import asyncio

import db.brand_claims as bc
import services.brand_claim_service as svc
import services.claim_state as cs


def _manual_claim(method="manual", status="pending"):
    return {
        "claim_id": "c1",
        "merchant_id": "m1",
        "claim_method": method,
        "brand_domain": "anua.com",
        "verification_status": status,
    }


def _wire(monkeypatch, claim):
    async def _get(cid):
        return claim

    async def _set(mid):
        return True

    async def _mark(cid, *, proof_ref=None):
        _mark.proof = proof_ref
        return True

    async def _promote(mid):
        return 0

    monkeypatch.setattr(bc, "get_brand_claim", _get)
    monkeypatch.setattr(svc, "set_merchant_brand_direct", _set)
    monkeypatch.setattr(bc, "mark_claim_verified", _mark)
    monkeypatch.setattr(cs, "promote_merchant_skus_to_claimed", _promote)
    return _mark


def test_employee_approves_manual_claim(monkeypatch):
    mark = _wire(monkeypatch, _manual_claim())
    res = asyncio.run(
        svc.approve_manual_claim("c1", approved_by="emp42", evidence_ref="ticket#9")
    )
    assert res["status"] == "verified"
    assert res["brand_direct_set"] is True
    assert res["approved_by"] == "emp42"
    # the approving employee + evidence are recorded in the proof trail
    assert mark.proof == "manual:emp42:ticket#9"


def test_non_manual_claim_not_employee_approvable(monkeypatch):
    _wire(monkeypatch, _manual_claim(method="dns"))
    res = asyncio.run(svc.approve_manual_claim("c1", approved_by="emp42"))
    assert res["status"] == "not_manual"  # dns/email self-verify, not employee-approvable


def test_missing_claim_not_found(monkeypatch):
    _wire(monkeypatch, None)
    res = asyncio.run(svc.approve_manual_claim("c1", approved_by="emp42"))
    assert res["status"] == "not_found"


def test_no_approver_is_forbidden():
    # short-circuits before any DB work — a route without an employee id can't grant
    res = asyncio.run(svc.approve_manual_claim("c1", approved_by=""))
    assert res["status"] == "forbidden"


def test_already_verified_is_idempotent(monkeypatch):
    _wire(monkeypatch, _manual_claim(status="verified"))
    res = asyncio.run(svc.approve_manual_claim("c1", approved_by="emp42"))
    assert res["status"] == "verified"
    assert res["brand_direct_set"] is True
