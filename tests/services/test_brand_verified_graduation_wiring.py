"""ADR-008 SLICE 2 — verify-to-serve wiring into verify_brand_claim (pure).

Proves the flag-gated, best-effort hook fires (or doesn't) at the right place:
  * flag ON  -> verify_brand_claim graduates the merchant's brand-authored catalog,
  * flag OFF -> NO graduation (verify behaves exactly as today),
  * a graduation error NEVER breaks verify_brand_claim (best-effort).

The raw-SQL / DB boundary is faked exactly as tests/integration/
test_storeless_brand_journey.py does (locally the DB is SQLite while the writes
are postgres-specific).
"""

from __future__ import annotations

import asyncio

import db.brand_claims as bc
import db.database
import services.brand_claim_service as svc
from services.brand_claim_service import verify_brand_claim

_TOKEN = "pivota-verify=v2s-token"


def _pending_dns_claim(merchant_id, brand_domain):
    async def _get(claim_id):
        return {
            "claim_id": claim_id,
            "merchant_id": merchant_id,
            "claim_method": "dns",
            "brand_domain": brand_domain,
            "challenge_token": _TOKEN,
            "verification_status": "pending",
        }

    return _get


def _wire_verified_path(monkeypatch, merchant_id, domain):
    """Patch everything verify_brand_claim needs so it reaches the 'verified'
    branch (the place the graduation hook fires). Returns nothing; caller adds
    the graduation patch/assertions."""
    monkeypatch.setattr(bc, "get_brand_claim", _pending_dns_claim(merchant_id, domain))
    monkeypatch.setattr(svc, "_default_txt_resolver", lambda d: [_TOKEN])

    async def _owns(_mid, _domain):
        return True

    monkeypatch.setattr(svc, "merchant_owns_domain", _owns)

    async def _set_brand_direct(_mid):
        return True

    monkeypatch.setattr(svc, "set_merchant_brand_direct", _set_brand_direct)

    async def _mark(cid, **kw):
        return True

    monkeypatch.setattr(bc, "mark_claim_verified", _mark)

    # promote_merchant_skus_to_claimed runs raw SQL; stub it.
    import services.claim_state as cs

    async def _promote(_mid):
        return 0

    monkeypatch.setattr(cs, "promote_merchant_skus_to_claimed", _promote)


def test_verify_flag_on_graduates_brand_catalog(monkeypatch):
    monkeypatch.setenv("ENABLE_STORELESS_BRAND_CATALOG", "1")
    _wire_verified_path(monkeypatch, "m_brand", "brand.com")

    called = {}
    import services.brand_verified_graduation as grad

    async def _fake_graduate(merchant_id):
        called["merchant_id"] = merchant_id
        return {"products": 1}

    monkeypatch.setattr(grad, "graduate_brand_authored_products", _fake_graduate)

    result = asyncio.run(verify_brand_claim("claim_1"))
    assert result["status"] == "verified"
    assert called.get("merchant_id") == "m_brand"


def test_verify_flag_off_does_not_graduate(monkeypatch):
    monkeypatch.delenv("ENABLE_STORELESS_BRAND_CATALOG", raising=False)
    _wire_verified_path(monkeypatch, "m_brand", "brand.com")

    called = {"hit": False}
    import services.brand_verified_graduation as grad

    async def _fake_graduate(merchant_id):
        called["hit"] = True
        return {}

    monkeypatch.setattr(grad, "graduate_brand_authored_products", _fake_graduate)

    result = asyncio.run(verify_brand_claim("claim_1"))
    assert result["status"] == "verified"  # verify still succeeds
    assert called["hit"] is False  # but no graduation happened


def test_verify_succeeds_even_if_graduation_errors(monkeypatch):
    # Best-effort: a graduation failure must NOT break claim verification.
    monkeypatch.setenv("ENABLE_STORELESS_BRAND_CATALOG", "1")
    _wire_verified_path(monkeypatch, "m_brand", "brand.com")

    import services.brand_verified_graduation as grad

    async def _boom(merchant_id):
        raise RuntimeError("graduation blew up")

    monkeypatch.setattr(grad, "graduate_brand_authored_products", _boom)

    result = asyncio.run(verify_brand_claim("claim_1"))
    assert result["status"] == "verified"
    assert result["brand_direct_set"] is True
