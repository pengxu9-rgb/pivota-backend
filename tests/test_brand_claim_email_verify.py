"""P1 — email-based brand-claim verification (alongside DNS-TXT).

A 6-digit code is emailed to a brand-domain alias; entering it proves control of
a brand-domain mailbox (the email analogue of DNS-TXT control). The code is the
proof and is NEVER echoed in the API response. brand_direct still requires the
B1 domain-identity binding, exactly like DNS.
"""

import asyncio

import db.brand_claims as bc
import services.brand_claim_service as svc
import services.claim_state as cs


# ---- pure helpers ---------------------------------------------------------
def test_email_target_must_be_mailbox_at_brand_domain():
    assert svc.email_target_valid("admin@anua.com", "anua.com") is True
    assert svc.email_target_valid("admin@ANUA.com", "anua.com") is True  # case-insensitive
    assert svc.email_target_valid("admin@evil.com", "anua.com") is False
    # exact domain only — a subdomain mailbox must NOT pass (look-alike defense)
    assert svc.email_target_valid("admin@mail.anua.com", "anua.com") is False
    assert svc.email_target_valid("notanemail", "anua.com") is False
    assert svc.email_target_valid("a@b@anua.com", "anua.com") is False
    assert svc.email_target_valid("", "anua.com") is False
    assert svc.email_target_valid("admin@anua.com", "") is False


def test_make_email_code_is_six_digits():
    code = svc.make_email_code()
    assert len(code) == 6 and code.isdigit()


# ---- start: the code is emailed, never echoed -----------------------------
def test_start_email_sends_code_and_never_echoes_it(monkeypatch):
    sent = {}

    async def _pending(mid, dom):
        return None

    async def _insert(**kw):
        assert kw["claim_method"] == "email"
        return "c1"

    async def _send(to, code, dom):
        sent.update({"to": to, "code": code, "domain": dom})
        return True

    monkeypatch.setattr(bc, "get_pending_brand_claim", _pending)
    monkeypatch.setattr(bc, "insert_brand_claim", _insert)
    monkeypatch.setattr(svc, "send_brand_claim_email", _send)

    res = asyncio.run(
        svc.start_brand_claim(
            merchant_id="m1",
            brand_domain="anua.com",
            method="email",
            verification_email="admin@anua.com",
        )
    )
    assert res["method"] == "email"
    assert res["challenge_token"] is None  # SECURITY: code never echoed
    assert res["email_sent"] is True
    assert sent["to"] == "admin@anua.com"
    assert len(sent["code"]) == 6 and sent["code"].isdigit()


# ---- verify: code match -> brand_direct (with B1 binding) -----------------
def _email_claim(token="123456"):
    return {
        "claim_id": "c1",
        "merchant_id": "m1",
        "claim_method": "email",
        "brand_domain": "anua.com",
        "challenge_token": token,
        "verification_status": "pending",
    }


def test_email_verify_success_sets_brand_direct(monkeypatch):
    async def _get(cid):
        return _email_claim("123456")

    async def _bound(mid, dom):
        return True

    async def _set(mid):
        return True

    async def _mark(cid, *, proof_ref=None):
        _mark.proof = proof_ref
        return True

    async def _promote(mid):
        return 0

    monkeypatch.setattr(bc, "get_brand_claim", _get)
    monkeypatch.setattr(bc, "mark_claim_verified", _mark)
    monkeypatch.setattr(svc, "set_merchant_brand_direct", _set)
    monkeypatch.setattr(cs, "promote_merchant_skus_to_claimed", _promote)

    res = asyncio.run(
        svc.verify_brand_claim("c1", submitted_code="123456", owned_domain_check=_bound)
    )
    assert res["status"] == "verified"
    assert res["brand_direct_set"] is True
    assert _mark.proof == "email:anua.com"  # email proof recorded (not dns:)


def test_email_verify_wrong_code_stays_pending(monkeypatch):
    async def _get(cid):
        return _email_claim("123456")

    monkeypatch.setattr(bc, "get_brand_claim", _get)
    res = asyncio.run(svc.verify_brand_claim("c1", submitted_code="000000"))
    assert res["status"] == "pending"
    assert res["reason"] == "code_mismatch"


def test_email_verify_proven_but_unbound_is_not_granted(monkeypatch):
    async def _get(cid):
        return _email_claim("123456")

    async def _unbound(mid, dom):
        return False

    monkeypatch.setattr(bc, "get_brand_claim", _get)
    res = asyncio.run(
        svc.verify_brand_claim("c1", submitted_code="123456", owned_domain_check=_unbound)
    )
    # right code proves mailbox control, but B1 binding gates brand_direct
    assert res["status"] == "domain_verified_unbound"
    assert res["brand_direct_set"] is False


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("ENABLE_BRAND_CLAIM_EMAIL_VERIFY", raising=False)
    assert svc.brand_claim_email_enabled() is False
    monkeypatch.setenv("ENABLE_BRAND_CLAIM_EMAIL_VERIFY", "true")
    assert svc.brand_claim_email_enabled() is True
