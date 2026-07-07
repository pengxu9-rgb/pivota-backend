"""Observed seller-of-record identity minting at ingestion (packet A9-2).

Every supply-ingestion path must mint (or resolve to) a real per-brand
`catalog_merchants` row at ingestion time. The shared placeholder
`merchant_id='external_seed'` bucket is BANNED for new writes.

Rationale + contract:
  - ADR-009 (Seller-of-Record Identity), decision D2 + Resolved decision #2
    (`docs/adr/ADR-009-seller-of-record-identity.md`): the seller-of-record is a
    `catalog_merchants` row; observed sellers are first-class with
    `status='observed'`; the id is deterministic
    `merch_obs_<hash(brand_identity::etld1)>` — visibly distinct from
    tenant-created `merch_` ids, idempotent (same brand+domain -> same identity
    forever), mintable by any ingestion path without coordination.
  - `docs/IDENTITY_REFERENCE.md` §3 (Trap T3 — the tenant/seller conflation):
    seller subjects live in `catalog_merchants`; tenants in
    `merchant_onboarding`; "claiming attaches, never re-keys." Never introduce
    another shared placeholder merchant for any ingestion path — every supply
    row gets a real per-brand subject (the W5 P3 rule, generalized).

No-fallback discipline (founder directive, ADR-009 Resolved decision #1 amend):
minting NEVER invents an identity from nothing. Empty brand or a non-registrable
domain raises — the caller must supply a real brand + registrable domain or skip
the row loudly. There is deliberately no "unknown seller" bucket to absorb the
gap; a silent bucket is exactly the crutch this packet removes.

Reuse (do not reinvent):
  - brand normalization: `services.catalog_identity.normalize_brand` (the same
    normalizer ADR-008 / `make_content_key` uses for the brand identity).
  - registrable domain (eTLD+1): `services.brand_claim_service.normalize_host`
    + its curated `_PUBLIC_SUFFIXES` set (the repo has no Public Suffix List
    dependency; this is the conservative in-repo scheme used for brand claims).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Optional

from db.database import database
# Reuse the repo's registrable-domain scheme (brand-claim / domain-verification
# path) rather than adding a Public Suffix List dependency. `_PUBLIC_SUFFIXES` is
# module-private there but is the single curated suffix set in the codebase; the
# alternative is duplicating it, which would drift. Cited per IDENTITY_REFERENCE
# §3 discipline (change identity behavior -> cite the source).
from services.brand_claim_service import _PUBLIC_SUFFIXES, normalize_host
from services.catalog_identity import normalize_brand
from services.catalog_sync_service import upsert_catalog_merchant

logger = logging.getLogger(__name__)

# Visibly distinct from tenant-created `merch_` ids (ADR-009 Resolved decision
# #2). 16 hex chars of sha256 over the brand identity.
OBSERVED_ID_PREFIX = "merch_obs_"
OBSERVED_STATUS = "observed"

# The placeholder bucket ADR-009 D2 bans for new writes. Kept here as the single
# named constant every tripwire references.
BANNED_BUCKET_MERCHANT_ID = "external_seed"


def etld1(domain: Optional[str]) -> str:
    """Registrable domain (eTLD+1) of `domain`, or '' when it is not a
    registrable domain (bare TLD, empty, unparseable).

    Reuses `brand_claim_service.normalize_host` (strip scheme/userinfo/port/path
    and a leading www.) + `_PUBLIC_SUFFIXES` (the curated multi-label public
    suffixes: myshopify.com, co.uk, co.kr, ...). A tenant storefront on a shared
    platform (anuko.myshopify.com) is its OWN registrable org, so its eTLD+1 is
    the full 3-label host — exactly the per-brand identity we want.
    """
    host = normalize_host(domain)
    if not host or "." not in host:
        return ""  # single label / bare TLD is never a registrable domain
    labels = host.split(".")
    # Every entry in _PUBLIC_SUFFIXES is a 2-label suffix, so a registrable name
    # under one is the suffix plus one more label (3 labels total).
    if len(labels) >= 3 and ".".join(labels[-2:]) in _PUBLIC_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def make_observed_merchant_id(brand: str, domain: str) -> str:
    """Deterministic observed seller id: `merch_obs_<sha256(nbrand::etld1)[:16]>`.

    Same brand+domain -> same id forever (ADR-009 D2). Raises on empty brand or
    non-registrable domain — never mints from nothing (no fallback identity).
    """
    nbrand = normalize_brand(brand)
    registrable = etld1(domain)
    if not nbrand or not registrable:
        raise ValueError(
            "cannot mint an observed seller identity from empty inputs "
            f"(brand={brand!r} -> {nbrand!r}, domain={domain!r} -> "
            f"etld1={registrable!r}); ADR-009 D2 forbids a fallback identity — "
            "supply a real brand + registrable domain, or skip the row loudly"
        )
    raw = f"{nbrand}::{registrable}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:16]
    return f"{OBSERVED_ID_PREFIX}{digest}"


async def _resolve_claimed_merchant(registrable: str) -> Optional[str]:
    """ADR-008 reconciliation / ADR-009 D2 ("graduation attaches, never
    re-keys"): if a tenant has a VERIFIED `brand_claims` row for this registrable
    domain, the seller-of-record IS that tenant merchant. Return it so we resolve
    to the claimed identity instead of minting a duplicate observed row.

    Best-effort: any DB / missing-table error -> None (fall through to mint). The
    claim's `brand_domain` is compared on its registrable form so www./subdomain
    variants still match.
    """
    if not registrable:
        return None
    try:
        rows = await database.fetch_all(
            """
            SELECT merchant_id, brand_domain
              FROM brand_claims
             WHERE verification_status = 'verified'
               AND brand_domain IS NOT NULL
             ORDER BY verified_at DESC NULLS LAST, created_at DESC
             LIMIT 200
            """
        )
    except Exception as exc:  # noqa: BLE001 — resolution is best-effort
        logger.warning("seller_identity: claimed-merchant lookup failed: %s", str(exc)[:200])
        return None
    for row in rows or []:
        data = dict(row)
        if etld1(data.get("brand_domain")) == registrable:
            mid = str(data.get("merchant_id") or "").strip()
            if mid:
                return mid
    return None


async def ensure_observed_seller(
    *,
    brand: str,
    domain: str,
    source_system: str,
    primary_platform: Optional[str] = None,
) -> str:
    """Resolve-or-mint the per-brand seller-of-record `catalog_merchants.merchant_id`
    for a crawled/observed offer (ADR-009 D2).

    Order (attach beats mint):
      1. If a tenant has VERIFIED-claimed this registrable domain, return THAT
         merchant_id (the identity ladder attaches, never re-keys).
      2. If the observed identity already exists, return it (idempotent — no dup).
      3. Otherwise upsert a fresh `status='observed'` row and return it.

    Raises (via `make_observed_merchant_id`) on empty brand / non-registrable
    domain — no fallback identity.

    NOTE (serving eligibility — verified, not changed by this packet): the public
    canonical-PDP read (`routes/pivota_canonical_routes.py`) INNER-JOINs
    `catalog_merchants` on `indexable IS TRUE AND status='active'`, so
    `status='observed'` rows are deliberately NOT servable as the public citation
    artifact until they graduate to `active` (ADR-009 D2 graduation ladder). The
    agent/search serving surface (`external_seed_servability` / `agent_pdp_view`)
    does not gate on merchant status, so observed supply still surfaces there.
    """
    observed_id = make_observed_merchant_id(brand, domain)  # raises on empty inputs
    registrable = etld1(domain)

    claimed = await _resolve_claimed_merchant(registrable)
    if claimed:
        return claimed

    existing = await database.fetch_one(
        "SELECT merchant_id FROM catalog_merchants WHERE merchant_id = :mid",
        {"mid": observed_id},
    )
    if existing:
        return observed_id

    # Defensive: the merch_obs_ prefix makes this impossible, but the ban is
    # load-bearing — assert it at the write boundary rather than trust the prefix.
    if observed_id == BANNED_BUCKET_MERCHANT_ID:
        raise RuntimeError(
            "ADR-009 D2 violation: refusing to mint the banned 'external_seed' "
            "bucket as a seller-of-record"
        )

    await upsert_catalog_merchant(
        merchant_id=observed_id,
        merchant_name=(brand or "").strip() or None,
        primary_platform=primary_platform,
        source_system=source_system,
        source_ref=registrable,
        status=OBSERVED_STATUS,
        metadata_json={
            "observed": True,
            "minted_by": "seller_identity.ensure_observed_seller",
            "adr": "ADR-009-D2",
            "brand_identity": {"brand": normalize_brand(brand), "etld1": registrable},
        },
    )
    return observed_id
