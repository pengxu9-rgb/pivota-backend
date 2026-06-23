"""E2E (composition) — the STORE-LESS brand journey:

    audit a product URL  ->  the SKU is seeded into the index (source_domain
    recorded)  ->  brand claims that domain  ->  DNS verify BINDS (because the
    audit-seed established the domain as the merchant's)  ->  brand_direct is
    written  ->  the serving reader surfaces offer_type=brand_direct.

This proves the pieces COMPOSE — in particular the self-reinforcing integration
that a store-less brand has no storefront, so auditing its products is precisely
what establishes the domain identity the claim-verify later binds against.

Scope: the cross-module orchestration + the real binding chain
(merchant_owned_domains -> merchant_owns_domain -> host_matches_known) run for
real; only the raw-SQL boundary (database.fetch_all / the brand_claims
accessors) is faked, since locally the DB is SQLite while the writes are
postgres-specific. Each function's SQL is unit-tested + reviewed separately.
"""

import asyncio

import db.brand_claims as bc
import db.database
import services.brand_claim_service as svc
from services.audit_index_intake import audit_product_to_index_fields
from services.brand_claim_service import BRAND_DIRECT, verify_brand_claim
from services.offer_classification import OFFER_TYPE_BRAND_DIRECT
from services.pivot_query_service import _resolve_offer_type

_TOKEN = "pivota-verify=storeless-token"


def _fake_catalog_fetch_all(source_domain):
    """Stand in for `SELECT source_domain, canonical_url FROM catalog_products
    WHERE merchant_id=...` — i.e. what the audit-seed wrote."""

    async def _fetch_all(query, values=None):
        return [{"source_domain": source_domain, "canonical_url": None}]

    return _fetch_all


def _pending_claim(merchant_id, brand_domain):
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


def test_storeless_journey_audit_seed_enables_claim_and_brand_direct(monkeypatch):
    MERCHANT = "m_storeless_anua"

    # 1. The store-less brand audits a product URL. The seed's canonical fields
    #    are what land in catalog_products — including source_domain.
    audit_product = {
        "title": "Heartleaf 77% Soothing Toner",
        "vendor": "Anua",
        "pdp_url": "https://www.anua.com/products/heartleaf-toner",
    }
    seed = audit_product_to_index_fields(MERCHANT, audit_product)
    assert seed["source_domain"] == "anua.com"  # what the seed records

    # 2. Because that SKU is now in the index, merchant_owned_domains (reading
    #    catalog_products.source_domain) knows anua.com belongs to this merchant.
    #    Run the REAL binding chain against a faked catalog read; no onboarding row
    #    (store-less merchant).
    async def _no_onboarding(_mid):
        return {}

    monkeypatch.setattr(
        "db.merchant_onboarding.get_merchant_onboarding", _no_onboarding
    )
    monkeypatch.setattr(
        db.database.database, "fetch_all", _fake_catalog_fetch_all(seed["source_domain"])
    )

    # 3. The brand claims that domain; DNS TXT proves control.
    monkeypatch.setattr(bc, "get_brand_claim", _pending_claim(MERCHANT, "anua.com"))
    monkeypatch.setattr(svc, "_default_txt_resolver", lambda d: [_TOKEN])
    flipped = {}

    async def _set_brand_direct(mid):
        flipped["merchant"] = mid
        return True

    monkeypatch.setattr(svc, "set_merchant_brand_direct", _set_brand_direct)

    async def _mark(cid, **kw):
        return True

    monkeypatch.setattr(bc, "mark_claim_verified", _mark)

    # 4. Verify -> the REAL merchant_owns_domain binds anua.com (from the seed) ->
    #    brand_direct is written.
    result = asyncio.run(verify_brand_claim("claim_1"))
    assert result["status"] == "verified"
    assert result["brand_direct_set"] is True
    assert flipped["merchant"] == MERCHANT  # brand_direct written for THIS merchant

    # 5. The serving reader surfaces brand_direct for that merchant's internal
    #    offer (flag on).
    offer_type = _resolve_offer_type(
        None, "internal_merchant", BRAND_DIRECT, brand_direct_enabled=True
    )
    assert offer_type == OFFER_TYPE_BRAND_DIRECT


def test_storeless_journey_unaudited_domain_does_not_bind(monkeypatch):
    # NEGATIVE: a domain the brand controls but never AUDITED (not in the index)
    # is domain-verified but NOT bound -> no brand_direct. Proves the audit-seed
    # is the thing that enables the bind.
    MERCHANT = "m1"

    async def _no_onboarding(_mid):
        return {}

    monkeypatch.setattr(
        "db.merchant_onboarding.get_merchant_onboarding", _no_onboarding
    )
    # The index only knows the AUDITED domain (anua.com)...
    monkeypatch.setattr(
        db.database.database, "fetch_all", _fake_catalog_fetch_all("anua.com")
    )
    # ...but the brand tries to claim an unrelated domain it happens to control.
    monkeypatch.setattr(
        bc, "get_brand_claim", _pending_claim(MERCHANT, "some-other-domain.com")
    )
    monkeypatch.setattr(svc, "_default_txt_resolver", lambda d: [_TOKEN])
    called = {"flip": False}

    async def _set_brand_direct(mid):
        called["flip"] = True
        return True

    monkeypatch.setattr(svc, "set_merchant_brand_direct", _set_brand_direct)

    result = asyncio.run(verify_brand_claim("claim_x"))
    assert result["status"] == "domain_verified_unbound"
    assert result["brand_direct_set"] is False
    assert called["flip"] is False  # brand_direct NOT written
