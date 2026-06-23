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


def make_email_code() -> str:
    """A short, human-enterable email verification code (6 digits). Sent to a
    brand-domain alias; entering it proves control of that mailbox."""
    return f"{secrets.randbelow(10 ** 6):06d}"


def email_target_valid(email: Optional[str], brand_domain: Optional[str]) -> bool:
    """The verification email MUST be a well-formed address AT the brand_domain —
    receiving a code there proves control of a brand-domain mailbox (the email
    analogue of DNS-TXT control). Case-insensitive EXACT domain match (no
    subdomains — avoids attacker-controlled `x.brand_domain` look-alikes)."""
    e = (email or "").strip().lower()
    d = (brand_domain or "").strip().lower()
    if not e or not d or e.count("@") != 1:
        return False
    local, _, host = e.partition("@")
    return bool(local) and host == d


def brand_claim_email_enabled() -> bool:
    """Flag: accept the email claim-verification method. Default OFF — ships dark,
    flipped per-env once the email infra is confirmed for this surface."""
    import os

    return os.getenv("ENABLE_BRAND_CLAIM_EMAIL_VERIFY", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


async def send_brand_claim_email(
    to_email: str, code: str, brand_domain: Optional[str]
) -> bool:
    """Best-effort: email the verification code to a brand-domain alias. Never
    raises — a send failure just leaves the pending claim (the brand re-starts).
    Runs the sync SES sender off the event loop."""
    import asyncio
    import os

    from utils.email_sender import send_email

    subject = "Verify your Pivota brand claim"
    text = (
        f"Your Pivota brand verification code for {brand_domain or 'your brand'} "
        f"is: {code}\n\nEnter it in Pivota to verify your brand. If you didn't "
        "request this, you can ignore this email."
    )
    try:
        result = await asyncio.to_thread(
            send_email,
            to_email=to_email,
            subject=subject,
            text_body=text,
            from_email=(os.getenv("FROM_EMAIL") or "noreply@pivota.ai").strip(),
            tags={"type": "brand_claim_verify"},
        )
        return bool(getattr(result, "ok", False))
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning(
            "send_brand_claim_email failed for %s: %s", to_email, str(exc)[:200]
        )
        return False


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


# Public / shared-platform suffixes under which two different registrations are
# NOT the same organization. A host that IS one of these — or that has too few
# labels to be a registrable domain (a bare TLD like "com") — must never anchor
# a "same registrable org" suffix match: otherwise a junk/short host in the
# merchant's OWN catalog (source_domain/canonical_url) or onboarding
# (store_url/website) — e.g. a bare "myshopify.com" or "com" — would widen the
# bind to every unrelated org that merely shares that suffix.
#
# The repo has no Public Suffix List dependency (cf. canonical_source_discovery's
# curated _TLD_LABELS/_GENERIC_DOMAINS), so this is a conservative, additive
# subset of the PSL, not an exhaustive one. The has-a-dot rule in
# _is_registrable_base is the always-on structural backstop for bare TLDs; this
# set extends it to the multi-label suffixes that rule can't catch on its own.
_PUBLIC_SUFFIXES = frozenset({
    # shared storefront platforms: one tenant (shop.myshopify.com) is a
    # different org from another tenant of the same platform. Kept in step with
    # the platform tokens canonical_source_discovery._GENERIC_DOMAINS already
    # treats as never-a-brand.
    "myshopify.com", "shopify.com", "wixsite.com", "bigcartel.com",
    "squarespace.com",
    # common multi-label registry suffixes (a registrable domain sits one label
    # deeper than these). Covers Pivota's active markets (KR/JP/UK/AU/...).
    "co.uk", "org.uk", "me.uk", "ac.uk", "gov.uk",
    "co.kr", "or.kr", "ne.kr", "go.kr",
    "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp",
    "com.au", "net.au", "org.au",
    "com.br", "com.cn", "com.hk", "com.mx", "com.my", "com.ph",
    "com.sg", "com.tr", "com.tw",
    "co.id", "co.il", "co.in", "co.nz", "co.th", "co.za",
})


def _is_registrable_base(host: str) -> bool:
    """True iff `host` is specific enough to anchor a 'same registrable org'
    suffix match: it has at least two labels AND is not itself a public/shared
    suffix (a bare TLD like 'com', a registry suffix like 'co.uk', or a
    storefront platform like 'myshopify.com'). Suffix-matching against a public
    suffix would bind unrelated orgs that merely share it, so we refuse to."""
    if not host or "." not in host:
        return False  # single label / bare TLD — never a registrable domain
    return host not in _PUBLIC_SUFFIXES


def host_matches_known(domain: Optional[str], known_hosts: Iterable[str]) -> bool:
    """Pure: does the claimed domain match a known merchant host? Exact match, or
    same registrable org (sub.brand.com <-> brand.com) — but a subdomain match is
    honored ONLY when the host acting as the registrable base is a real
    registrable domain, never a public/platform suffix. That guard keeps a
    junk/short host in the merchant's own catalog/onboarding data (e.g. a bare
    'myshopify.com' or 'com') from widening the bind to unrelated orgs."""
    d = normalize_host(domain)
    if not d:
        return False
    for kh in known_hosts:
        k = normalize_host(kh)
        if not k:
            continue
        if d == k:
            return True  # exact host — the strongest, unconditional bind
        # d is a subdomain of k  -> k is the registrable base
        if d.endswith("." + k) and _is_registrable_base(k):
            return True
        # k is a subdomain of d  -> d is the registrable base
        if k.endswith("." + d) and _is_registrable_base(d):
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
    verification_email: Optional[str] = None,
) -> Dict[str, Any]:
    """Begin a claim: issue a challenge + record a pending claim row. B3: reuse an
    existing pending claim for this (merchant, domain) rather than spamming a new
    row (and colliding with the partial-unique index).

    For the email method the challenge is a short code emailed to a brand-domain
    alias — it is NEVER echoed in the response (only the recipient mailbox sees
    it); DNS still returns its TXT token, which the brand must publish."""
    if brand_domain:
        existing = await bc.get_pending_brand_claim(merchant_id, brand_domain)
        if existing and existing.get("challenge_token"):
            ex_method = existing.get("claim_method") or method
            existing_token = existing["challenge_token"]
            email_sent = None
            if ex_method == "email" and verification_email:
                # re-send the SAME code to the (re-supplied) alias on reuse.
                email_sent = await send_brand_claim_email(
                    verification_email, existing_token, brand_domain
                )
            return _claim_response(
                claim_id=existing["claim_id"],
                method=ex_method,
                token=existing_token,
                brand_domain=brand_domain,
                verification_email=verification_email,
                email_sent=email_sent,
                reused=True,
            )

    token = make_email_code() if method == "email" else make_challenge_token()
    claim_id = await bc.insert_brand_claim(
        merchant_id=merchant_id,
        claim_method=method,
        brand_domain=brand_domain,
        content_key=content_key,
        challenge_token=token,
        created_by_user_id=user_id,
    )
    email_sent = None
    if method == "email" and verification_email:
        email_sent = await send_brand_claim_email(verification_email, token, brand_domain)
    return _claim_response(
        claim_id=claim_id,
        method=method,
        token=token,
        brand_domain=brand_domain,
        verification_email=verification_email,
        email_sent=email_sent,
        reused=False,
    )


def _claim_response(
    *,
    claim_id: Any,
    method: str,
    token: str,
    brand_domain: Optional[str],
    verification_email: Optional[str],
    email_sent: Optional[bool],
    reused: bool,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "claim_id": claim_id,
        "method": method,
        # SECURITY: never echo the emailed code — only the mailbox holder may see
        # it. DNS returns its TXT token (the brand must publish it to prove control).
        "challenge_token": None if method == "email" else token,
        "instructions": _claim_instructions(
            method, brand_domain, token, verification_email
        ),
        "reused": reused,
    }
    if method == "email":
        out["email_sent"] = email_sent
    return out


def _claim_instructions(
    method: str,
    brand_domain: Optional[str],
    token: str,
    verification_email: Optional[str] = None,
) -> str:
    if method == "dns":
        return f"Add a DNS TXT record to {brand_domain or 'your brand domain'} with value: {token}"
    if method == "email":
        target = verification_email or f"an email at {brand_domain or 'your brand domain'}"
        return (
            f"We emailed a 6-digit verification code to {target}. "
            "Enter it to verify your brand."
        )
    if method == "manual":
        return (
            "Contact Pivota support with evidence of brand ownership (e.g. brand "
            "registry, trademark, or authorized-representative proof). Our team "
            "will review and verify your claim."
        )
    return f"Verification token: {token}"


async def approve_manual_claim(
    claim_id: str, *, approved_by: str, evidence_ref: Optional[str] = None
) -> Dict[str, Any]:
    """Support-assisted verification: a Pivota EMPLOYEE, having reviewed a brand's
    ownership evidence offline, approves a method='manual' claim. The human review
    IS the verification — so this does NOT run the automated DNS/email proof or the
    B1 auto-binding; it directly grants brand_direct + promotes the brand's SKUs.
    Employee-gated at the route (approved_by is recorded in the proof). Only manual
    claims; idempotent; best-effort writes."""
    if not approved_by:
        return {"status": "forbidden"}
    claim = await bc.get_brand_claim(claim_id)
    if not claim:
        return {"status": "not_found"}
    if claim.get("verification_status") == bc.STATUS_VERIFIED:
        return {"status": "verified", "brand_direct_set": True}
    if claim.get("claim_method") != "manual":
        # dns / email self-verify; only the manual method is employee-approvable.
        return {"status": "not_manual", "method": claim.get("claim_method")}

    ok = await set_merchant_brand_direct(claim["merchant_id"])
    proof = f"manual:{approved_by}"
    if evidence_ref:
        proof = f"{proof}:{evidence_ref}"
    await bc.mark_claim_verified(claim_id, proof_ref=proof)
    # Same lifecycle promotion as DNS/email: unclaimed -> claimed. Best-effort.
    from services.claim_state import promote_merchant_skus_to_claimed

    await promote_merchant_skus_to_claimed(claim["merchant_id"])
    return {"status": "verified", "brand_direct_set": ok, "approved_by": approved_by}


async def verify_brand_claim(
    claim_id: str,
    *,
    submitted_code: Optional[str] = None,
    txt_resolver: Optional[Callable[[str], List[str]]] = None,
    owned_domain_check: Optional[Callable[[str, str], Any]] = None,
) -> Dict[str, Any]:
    """Verify a pending claim. DNS resolves the brand_domain's TXT records and
    checks the challenge token; EMAIL matches `submitted_code` against the code we
    sent to a brand-domain alias. Either proves DOMAIN CONTROL — but brand_direct
    means "verified as the brand," so we only flip the merchant when the verified
    domain is also BOUND to the merchant's known brand identity (B1). A
    domain-verified but unbound claim is NOT auto-granted — it returns
    'domain_verified_unbound' for review. Returns {status, brand_direct_set}."""
    claim = await bc.get_brand_claim(claim_id)
    if not claim:
        return {"status": "not_found"}
    if claim.get("verification_status") == bc.STATUS_VERIFIED:
        return {"status": "verified", "brand_direct_set": True}

    method = claim.get("claim_method")
    token = claim.get("challenge_token") or ""
    domain = claim.get("brand_domain")

    if method == "dns":
        resolver = txt_resolver or _default_txt_resolver
        try:
            records = resolver(domain) if domain else []
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "verify_brand_claim: TXT resolve failed for %s: %s",
                domain, str(exc)[:200],
            )
            records = []
        if not dns_txt_proves_claim(token, records):
            return {"status": "pending", "brand_direct_set": False, "reason": "txt_not_found"}
        proof_ref = f"dns:{domain}"
    elif method == "email":
        submitted = (submitted_code or "").strip()
        # Constant-time compare; a missing/blank stored token can never match.
        if not token or not submitted or not secrets.compare_digest(submitted, token):
            return {"status": "pending", "brand_direct_set": False, "reason": "code_mismatch"}
        proof_ref = f"email:{domain}"
    else:
        # amazon / shopify / manual: not handled here (manual = employee approval).
        return {"status": "unsupported_method", "method": method}

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
    await bc.mark_claim_verified(claim_id, proof_ref=proof_ref)
    # P1: promote the verified brand's audit-seeded SKUs unclaimed -> claimed
    # (the lifecycle backbone for the syndicate-after-claim gate). Best-effort.
    from services.claim_state import promote_merchant_skus_to_claimed

    await promote_merchant_skus_to_claimed(claim["merchant_id"])
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
