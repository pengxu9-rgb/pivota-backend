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

import json
import logging
import re
import secrets
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from db import brand_claims as bc
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
# Brand-identity binding (B1) + hostname validation (B4)
# DNS control proves "I control this domain" — NOT "I am this brand". We only
# flip brand_direct when the verified domain is also one Pivota already
# associates with the merchant (its catalog source/canonical hosts or onboarding
# store_url). Otherwise the claim is recorded but left for review.
# ---------------------------------------------------------------------------
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$"
)


def normalize_host(value: Optional[str]) -> str:
    """Lowercase registrable host from a domain or URL: strip scheme, userinfo,
    port, path, and a leading 'www.'."""
    if not value or not isinstance(value, str):
        return ""
    v = value.strip().lower()
    v = urlparse(v).netloc if "://" in v else v.split("/")[0]
    v = v.split("@")[-1].split(":")[0]
    return v[4:] if v.startswith("www.") else v


def is_valid_public_hostname(value: Optional[str]) -> bool:
    """B4: a well-formed public hostname (labels + a TLD). Rejects internal
    names, IPs, and malformed input before we ever resolve TXT on it."""
    host = normalize_host(value)
    return bool(host) and len(host) <= 253 and bool(_HOSTNAME_RE.match(host))


def host_matches_known(domain: Optional[str], known_hosts: Iterable[str]) -> bool:
    """Pure: does the claimed domain match a known merchant host? Exact match or
    same registrable org (sub.brand.com <-> brand.com)."""
    d = normalize_host(domain)
    if not d:
        return False
    for kh in known_hosts:
        k = normalize_host(kh)
        if k and (d == k or d.endswith("." + k) or k.endswith("." + d)):
            return True
    return False


async def merchant_owned_domains(merchant_id: str) -> set:
    """Best-effort set of hosts Pivota already associates with this merchant:
    onboarding store_url/website + catalog product source/canonical hosts. Does
    NOT include pivota_canonical_url (that's Pivota's host, not the brand's)."""
    hosts: set = set()
    if not merchant_id:
        return hosts
    try:
        from db.merchant_onboarding import get_merchant_onboarding

        ob = await get_merchant_onboarding(merchant_id) or {}
        for key in ("store_url", "website"):
            h = normalize_host(ob.get(key))
            if h:
                hosts.add(h)
    except Exception as exc:  # noqa: BLE001
        logger.warning("merchant_owned_domains: onboarding load failed: %s", str(exc)[:200])
    try:
        rows = await database.fetch_all(
            """
            SELECT DISTINCT source_domain, canonical_url
              FROM catalog_products
             WHERE merchant_id = :merchant_id
             LIMIT 500
            """,
            {"merchant_id": merchant_id},
        )
        for r in rows or []:
            for key in ("source_domain", "canonical_url"):
                h = normalize_host(r[key])
                if h:
                    hosts.add(h)
    except Exception as exc:  # noqa: BLE001
        logger.warning("merchant_owned_domains: catalog load failed: %s", str(exc)[:200])
    return hosts


async def merchant_owns_domain(merchant_id: str, domain: str) -> bool:
    """True iff `domain` is bound to the merchant's known brand identity."""
    return host_matches_known(domain, await merchant_owned_domains(merchant_id))


# ---------------------------------------------------------------------------
# THE missing primitive: write the verified brand relationship
# ---------------------------------------------------------------------------
async def set_merchant_brand_direct(merchant_id: str) -> bool:
    """Set metadata_json.brand_relationship='brand_direct' on catalog_merchants
    via an ATOMIC server-side JSONB merge (|| concat) — no read-modify-write, so
    a concurrent writer to metadata_json can't be clobbered (B2). Best-effort.

    CAST(:patch AS JSONB), NOT :patch::jsonb — the `::` cast-after-param form
    breaks SQLAlchemy text() binding (guarded by the repo's meta-invariant test).
    """
    if not merchant_id:
        return False
    patch = json.dumps(brand_direct_metadata(None))  # {"brand_relationship": "brand_direct"}
    try:
        await database.execute(
            """
            UPDATE catalog_merchants
               SET metadata_json = COALESCE(metadata_json, CAST('{}' AS JSONB))
                                   || CAST(:patch AS JSONB)
             WHERE merchant_id = :merchant_id
            """,
            {"patch": patch, "merchant_id": merchant_id},
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
    """Begin a claim: issue a challenge token + record a pending claim row. B3:
    reuse an existing pending claim for this (merchant, domain) rather than
    spamming a new row (and colliding with the partial-unique index)."""
    if brand_domain:
        existing = await bc.get_pending_brand_claim(merchant_id, brand_domain)
        if existing and existing.get("challenge_token"):
            ex_method = existing.get("claim_method") or method
            return {
                "claim_id": existing["claim_id"],
                "method": ex_method,
                "challenge_token": existing["challenge_token"],
                "instructions": _claim_instructions(
                    ex_method, brand_domain, existing["challenge_token"]
                ),
                "reused": True,
            }

    token = make_challenge_token()
    claim_id = await bc.insert_brand_claim(
        merchant_id=merchant_id,
        claim_method=method,
        brand_domain=brand_domain,
        content_key=content_key,
        challenge_token=token,
        created_by_user_id=user_id,
    )
    return {
        "claim_id": claim_id,
        "method": method,
        "challenge_token": token,
        "instructions": _claim_instructions(method, brand_domain, token),
        "reused": False,
    }


def _claim_instructions(method: str, brand_domain: Optional[str], token: str) -> str:
    if method == "dns":
        return f"Add a DNS TXT record to {brand_domain or 'your brand domain'} with value: {token}"
    return f"Verification token: {token}"


async def verify_brand_claim(
    claim_id: str,
    *,
    txt_resolver: Optional[Callable[[str], List[str]]] = None,
    owned_domain_check: Optional[Callable[[str, str], Any]] = None,
) -> Dict[str, Any]:
    """Verify a pending claim. For DNS we resolve the brand_domain's TXT records
    and check the challenge token. A match proves DOMAIN CONTROL — but
    brand_direct means "verified as the brand," so we only flip the merchant when
    the verified domain is also BOUND to the merchant's known brand identity
    (B1). A domain-verified but unbound claim is NOT auto-granted — it returns
    'domain_verified_unbound' for review. Returns {status, brand_direct_set}."""
    claim = await bc.get_brand_claim(claim_id)
    if not claim:
        return {"status": "not_found"}
    if claim.get("verification_status") == bc.STATUS_VERIFIED:
        return {"status": "verified", "brand_direct_set": True}

    method = claim.get("claim_method")
    if method != "dns":
        # email / amazon / shopify / manual: scaffolded for the next slice.
        return {"status": "unsupported_method", "method": method}

    token = claim.get("challenge_token") or ""
    domain = claim.get("brand_domain")
    resolver = txt_resolver or _default_txt_resolver
    try:
        records = resolver(domain) if domain else []
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "verify_brand_claim: TXT resolve failed for %s: %s", domain, str(exc)[:200]
        )
        records = []
    if not dns_txt_proves_claim(token, records):
        return {"status": "pending", "brand_direct_set": False, "reason": "txt_not_found"}

    # Domain control proven. B1: brand_direct requires brand-identity binding —
    # the verified domain must be one Pivota already associates with this merchant.
    check = owned_domain_check or merchant_owns_domain
    try:
        bound = bool(await check(claim["merchant_id"], domain)) if domain else False
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "verify_brand_claim: ownership check failed for %s: %s", domain, str(exc)[:200]
        )
        bound = False
    if not bound:
        # Record the proof, but DO NOT grant brand_direct on an unbound domain.
        return {
            "status": "domain_verified_unbound",
            "brand_direct_set": False,
            "reason": "brand_domain is not associated with this merchant; needs review before brand_direct",
        }

    ok = await set_merchant_brand_direct(claim["merchant_id"])
    await bc.mark_claim_verified(claim_id, proof_ref=f"dns:{domain}")
    return {"status": "verified", "brand_direct_set": ok}


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
