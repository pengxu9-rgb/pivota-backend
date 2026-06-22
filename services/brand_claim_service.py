"""P1 — brand claim WRITE PATH (the one missing primitive).

classify_offer_type (services/offer_classification.py) only returns
'brand_direct' for an internal merchant whose
catalog_merchants.metadata_json.brand_relationship == 'brand_direct' — a
VERIFIED value it "never assumes." No service ever wrote it. This module is
that writer: a brand claims its merchant/domain, verifies ownership (DNS TXT
first), and on success we set brand_relationship='brand_direct'.

Storefront-agnostic: claiming is domain/registry-based, and catalog_merchants is
the storefront-optional registry — so a store-less brand (e.g. Anua/Anuko) can
enter the index as a brand-attested canonical product.

Pure helpers (brand_direct_metadata / dns_txt_proves_claim / make_challenge_token)
are unit-tested with no I/O; the DB-bound functions are best-effort.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any, Callable, Dict, Iterable, List, Optional

from db import brand_claims as bc
from db.catalog import catalog_merchants
from db.database import database

logger = logging.getLogger(__name__)

BRAND_DIRECT = "brand_direct"
_TXT_PREFIX = "pivota-verify="


# ---------------------------------------------------------------------------
# Pure helpers (no I/O — unit-tested)
# ---------------------------------------------------------------------------
def brand_direct_metadata(existing: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge brand_relationship='brand_direct' into a metadata_json dict without
    clobbering other keys. This value is the ONLY thing classify_offer_type
    trusts to surface a brand-direct offer."""
    md = dict(existing or {})
    md["brand_relationship"] = BRAND_DIRECT
    return md


def make_challenge_token() -> str:
    """A DNS/email verification challenge: 'pivota-verify=<random>'."""
    return _TXT_PREFIX + secrets.token_urlsafe(24)


def dns_txt_proves_claim(expected_token: str, txt_records: Iterable[str]) -> bool:
    """True iff the expected 'pivota-verify=<token>' value appears verbatim among
    the domain's TXT records. Exact-match — a partial/prefix match doesn't prove
    control of the record's value."""
    if not expected_token:
        return False
    return any((r or "").strip() == expected_token for r in (txt_records or []))


# ---------------------------------------------------------------------------
# THE missing primitive: write the verified brand relationship
# ---------------------------------------------------------------------------
async def set_merchant_brand_direct(merchant_id: str) -> bool:
    """Write metadata_json.brand_relationship='brand_direct' on catalog_merchants,
    preserving existing metadata. Best-effort. This is the chokepoint that turns
    a verified claim into decision-layer brand authority (read by
    classify_offer_type)."""
    if not merchant_id:
        return False
    try:
        row = await database.fetch_one(
            catalog_merchants.select().where(
                catalog_merchants.c.merchant_id == merchant_id
            )
        )
        if not row:
            logger.warning("set_merchant_brand_direct: merchant %s not found", merchant_id)
            return False
        existing = row["metadata_json"] if isinstance(row["metadata_json"], dict) else {}
        await database.execute(
            catalog_merchants.update()
            .where(catalog_merchants.c.merchant_id == merchant_id)
            .values(metadata_json=brand_direct_metadata(existing))
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "set_merchant_brand_direct failed for %s: %s", merchant_id, str(exc)[:200]
        )
        return False


# ---------------------------------------------------------------------------
# Claim lifecycle
# ---------------------------------------------------------------------------
async def start_brand_claim(
    *,
    merchant_id: str,
    brand_domain: Optional[str] = None,
    method: str = "dns",
    content_key: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Begin a claim: issue a challenge token + record a pending claim row."""
    token = make_challenge_token()
    claim_id = await bc.insert_brand_claim(
        merchant_id=merchant_id,
        claim_method=method,
        brand_domain=brand_domain,
        content_key=content_key,
        challenge_token=token,
        created_by_user_id=user_id,
    )
    instructions = (
        f"Add a DNS TXT record to {brand_domain or 'your brand domain'} "
        f"with value: {token}"
        if method == "dns"
        else f"Verification token: {token}"
    )
    return {
        "claim_id": claim_id,
        "method": method,
        "challenge_token": token,
        "instructions": instructions,
    }


async def verify_brand_claim(
    claim_id: str,
    *,
    txt_resolver: Optional[Callable[[str], List[str]]] = None,
) -> Dict[str, Any]:
    """Verify a pending claim. For DNS: resolve the brand_domain's TXT records and
    check the challenge token; on success, write brand_relationship='brand_direct'
    and mark the claim verified. Returns {status, brand_direct_set}."""
    claim = await bc.get_brand_claim(claim_id)
    if not claim:
        return {"status": "not_found"}
    if claim.get("verification_status") == bc.STATUS_VERIFIED:
        return {"status": "verified", "brand_direct_set": True}

    method = claim.get("claim_method")
    token = claim.get("challenge_token") or ""

    if method == "dns":
        domain = claim.get("brand_domain")
        resolver = txt_resolver or _default_txt_resolver
        try:
            records = resolver(domain) if domain else []
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "verify_brand_claim: TXT resolve failed for %s: %s",
                domain, str(exc)[:200],
            )
            records = []
        if dns_txt_proves_claim(token, records):
            ok = await set_merchant_brand_direct(claim["merchant_id"])
            await bc.mark_claim_verified(claim_id, proof_ref=f"dns:{domain}")
            return {"status": "verified", "brand_direct_set": ok}
        return {"status": "pending", "brand_direct_set": False, "reason": "txt_not_found"}

    # email / amazon / shopify / manual: scaffolded for the next slice.
    return {"status": "unsupported_method", "method": method}


def _default_txt_resolver(domain: str) -> List[str]:
    """Best-effort TXT lookup via dnspython if installed; [] otherwise (callers
    inject a resolver in tests, and the route can require dnspython in prod)."""
    try:
        import dns.resolver  # type: ignore

        out: List[str] = []
        for rec in dns.resolver.resolve(domain, "TXT"):
            strings = getattr(rec, "strings", None)
            if strings:
                out.append(b"".join(strings).decode("utf-8", "ignore"))
            else:
                out.append(str(rec).strip('"'))
        return out
    except Exception:  # noqa: BLE001 — missing lib / NXDOMAIN / timeout
        return []
