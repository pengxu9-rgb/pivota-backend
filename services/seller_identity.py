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
from typing import Optional, Dict

from db.database import database
# Reuse the repo's registrable-domain scheme (brand-claim / domain-verification
# path) rather than adding a Public Suffix List dependency. `_PUBLIC_SUFFIXES` is
# module-private there but is the single curated suffix set in the codebase; the
# alternative is duplicating it, which would drift. Cited per IDENTITY_REFERENCE
# §3 discipline (change identity behavior -> cite the source).
from services.brand_claim_service import _PUBLIC_SUFFIXES, normalize_host
from services.catalog_identity import normalize_brand
from services.catalog_sync_service import upsert_catalog_merchant
from services.offer_seller_identity import is_known_retailer

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


def is_known_retailer_domain(domain: Optional[str]) -> bool:
    """W2 (ADR-011 R5 closure): is this domain a known RETAILER host? ONE
    definition system-wide — this delegates to the exact matcher the offer
    lane uses (`offer_seller_identity.is_known_retailer`, suffix match), so
    the two lanes cannot classify the same host differently. Known limitation,
    deliberate: a subdomain-only list entry (e.g. beauty.example.com) still
    KEYS on the registrable (etld1) — adding a subdomain to the retailer list
    designates the whole registrable as a retailer for identity purposes."""
    from services.offer_seller_identity import is_known_retailer

    return is_known_retailer(str(domain or "").strip().rstrip("."))


# Second-level labels that are generic registries, not sellers. `etld1` uses a
# CURATED suffix list, so an uncurated ccTLD (brand.com.vn) yields a bare
# public suffix ("com.vn") as its "registrable". Minting or claim-matching on
# that would collapse every site under that registry into one identity — and a
# verified brand claim on ANY .com.vn domain would capture them all.
_GENERIC_SECOND_LEVEL_LABELS = frozenset({
    "com", "co", "net", "org", "edu", "gov", "ac", "or", "ne", "mil", "int", "gob",
})


def _plausible_registrable(registrable: str) -> bool:
    """Reject registrables that are (or look like) bare public suffixes."""
    if not registrable or "." not in registrable:
        return False
    first = registrable.split(".", 1)[0]
    if first in _GENERIC_SECOND_LEVEL_LABELS:
        return False
    return True


def make_observed_retailer_id(domain: str) -> str:
    """Deterministic observed RETAILER seller id, keyed on the domain ALONE
    (W2 / founder-ratified 2026-08-05): one retailer = one merchant, no matter
    how many brands it carries. Per-brand keying on a retailer domain would
    mint hundreds of merchants for one seller — the mirror image of the
    shared-bucket bug ADR-009 fixes. The brand stays a product attribute.

    Id space: `merch_obs_<sha256("retailer::" + etld1)[:16]>` — the namespace
    prefix keeps it disjoint from the brand-direct space by construction.
    Raises on a non-registrable domain — never mints from nothing."""
    registrable = etld1(str(domain or "").strip().rstrip("."))
    if not _plausible_registrable(registrable):
        raise ValueError(
            "cannot mint an observed retailer identity from a non-registrable "
            f"domain ({domain!r} -> {registrable!r}); supply a real domain or "
            "skip the row loudly"
        )
    raw = f"retailer::{registrable}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:16]
    return f"{OBSERVED_ID_PREFIX}{digest}"


def resolve_seed_seller_identity(*, brand: Optional[str], domain: Optional[str]) -> Dict[str, str]:
    """PURE W2 dispatch — the seller of record is the party that sells, and the
    keying follows the domain's type:

      retailer domain (known list)  -> keyed on etld1 alone
      anything else (brand-direct)  -> keyed on (brand, etld1), unchanged ADR-009

    Returns {kind, merchant_id, merchant_name, registrable}. Raises ValueError
    when the identity cannot be derived (unregistrable domain, or empty brand
    on a brand-direct domain) — the caller must SKIP the row loudly and retry
    later; there is no fallback identity and the legacy bucket is never a
    landing zone (founder rule, 2026-08-06).

    DB concerns stay out of this function on purpose: claimed-domain attach
    ("attach beats mint") and the insert-if-missing of the merchant row happen
    at apply time, where a connection exists."""
    registrable = etld1(str(domain or "").strip().rstrip("."))
    if not _plausible_registrable(registrable):
        raise ValueError(
            f"seller-of-record unresolvable: non-registrable domain {domain!r}"
            f" (registrable {registrable!r})"
        )
    if is_known_retailer_domain(domain):
        return {
            "kind": "retailer",
            "merchant_id": make_observed_retailer_id(registrable),
            "merchant_name": registrable,
            "registrable": registrable,
        }
    nbrand = normalize_brand(brand or "")
    if not nbrand:
        raise ValueError(
            "seller-of-record unresolvable: empty brand on a brand-direct "
            f"domain ({domain!r}); not a known retailer, so (brand, etld1) "
            "keying applies and needs a real brand"
        )
    return {
        "kind": "brand_direct",
        "merchant_id": make_observed_merchant_id(brand or "", registrable),
        "merchant_name": (brand or "").strip(),
        "registrable": registrable,
    }


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


# ---------------------------------------------------------------------------
# A9-3 (ADR-009 D3): derive the seller-of-record for a NEW external_product_seeds
# row at write time. `docs/adr/ADR-009-seller-of-record-identity.md` §D3 +
# `docs/IDENTITY_REFERENCE.md` §3 (Trap T3) / §4.
#
# The seed's identity today is the ANCHOR merchant's `attached_product_key` plus a
# raw destination `domain`. A9-3 makes the seller explicit:
#   - SELF  : the destination registrable domain belongs to the anchor merchant
#             (its connected-store domains / mcp_shop_domain / catalog source_ref)
#             → seller_ref = the anchor merchant_id, seed_kind='self'. Anchor ==
#             seller, so attribution was already coincidentally correct.
#   - CROSS : otherwise → seller_ref = ensure_observed_seller(brand, destination)
#             (A9-2), seed_kind='cross'. The anchor is a surface dimension only.
#   - UNRESOLVABLE : no destination to key a seller on, or a CROSS seed with an
#             empty brand / non-registrable domain (ensure_observed_seller raises)
#             → (None, None). No fallback identity is invented (founder no-fallback
#             directive); NULL is the honest pre-A9-4 state and is backfilled by
#             A9-4. NEVER assume 'self'.
# ---------------------------------------------------------------------------


def anchor_merchant_from_product_key(attached_product_key: Optional[str]) -> Optional[str]:
    """Extract the anchor merchant_id embedded in a seed's `attached_product_key`.

    STORAGE format is `prod::{merchant_id}::{platform}::{source_product_id}` (the
    `make_catalog_product_key` double-colon form — IDENTITY_REFERENCE §2). The
    PIPE transport form (`{merchant}|{platform}|{pid}`) is defended against too,
    but per Trap T1 it must never be in the column. Returns None for a synthetic
    / non-catalog key (e.g. the enrichment agent's `pk_<hash>`), which correctly
    yields "no tenant anchor" → the seed is treated as CROSS.
    """
    key = str(attached_product_key or "").strip()
    if not key:
        return None
    if key.startswith("prod::"):
        parts = key.split("::")
        # ['prod', merchant_id, platform, source_product_id...]
        if len(parts) >= 2 and parts[1].strip():
            return parts[1].strip()
        return None
    if key.count("|") >= 2:  # defensive: transport form (should not be persisted)
        head = key.split("|", 1)[0].strip()
        return head or None
    return None


async def _anchor_owns_domain(anchor_merchant_id: str, registrable: str) -> bool:
    """True iff `registrable` (an eTLD+1) belongs to the anchor merchant.

    Checks the three places a merchant's own storefront domain is recorded:
      - `catalog_merchants.source_ref` (the index/serving subject's brand domain,
        which is exactly what A9-2 stamps on an observed seller);
      - `merchant_onboarding.mcp_shop_domain` (the tenant's authenticated store);
      - `merchant_stores` connected-store domains (store_url / shop_domain).
    Each candidate is reduced to its eTLD+1 (`etld1`) before comparison, so a
    www./subdomain/path variant still matches. Best-effort: any DB / missing-table
    error → False (fall through to CROSS — never guess SELF). ADR-009 D3.
    """
    if not anchor_merchant_id or not registrable:
        return False
    # catalog_merchants.source_ref (already a registrable domain for observed rows)
    try:
        row = await database.fetch_one(
            "SELECT source_ref FROM catalog_merchants WHERE merchant_id = :mid",
            {"mid": anchor_merchant_id},
        )
        if row and etld1(dict(row).get("source_ref")) == registrable:
            return True
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("seller_identity: catalog_merchants source_ref lookup failed: %s", str(exc)[:200])
    # merchant_onboarding.mcp_shop_domain (the authenticated tenant storefront)
    try:
        row = await database.fetch_one(
            "SELECT mcp_shop_domain FROM merchant_onboarding WHERE merchant_id = :mid",
            {"mid": anchor_merchant_id},
        )
        if row and etld1(dict(row).get("mcp_shop_domain")) == registrable:
            return True
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("seller_identity: merchant_onboarding lookup failed: %s", str(exc)[:200])
    # merchant_stores connected-store domains (the `domain` column — verified
    # against services/external_referral_readiness._fetch_store_domains_by_merchant).
    try:
        rows = await database.fetch_all(
            "SELECT domain FROM merchant_stores WHERE merchant_id = :mid AND domain IS NOT NULL",
            {"mid": anchor_merchant_id},
        )
        for r in rows or []:
            if etld1(dict(r).get("domain")) == registrable:
                return True
    except Exception as exc:  # noqa: BLE001 — best-effort (table/column may vary)
        logger.warning("seller_identity: merchant_stores lookup failed: %s", str(exc)[:200])
    return False


async def derive_seed_seller(
    *,
    anchor_merchant_id: Optional[str],
    brand: Optional[str],
    destination_domain: Optional[str],
    source_system: str,
    primary_platform: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Derive `(seller_ref, seed_kind)` for a NEW external_product_seeds row.

    Returns:
      - `(anchor_merchant_id, 'self')` when the destination belongs to the anchor
        AND is not a known retailer/marketplace host;
      - `(observed_seller_id, 'cross')` when it belongs to a different seller OR is
        a known-retailer host (a marketplace is never a brand's own store, so it
        must not become a brand-official 'self'/public seed);
      - `(None, None)` when unresolvable (leave the columns NULL — pre-A9-4 state).

    ``destination_domain`` may be a bare host or a full URL; it is reduced to its
    registrable domain here. ``anchor_merchant_id`` is the merchant whose product
    the seed is attached to (see ``anchor_merchant_from_product_key``); pass None
    for a standalone seed with no anchor (→ CROSS).

    No-fallback (ADR-009 D3): a missing destination logs at DEBUG and returns
    NULL (there is simply nothing to key a seller on — not an error, and A9-4
    backfills it). A present-but-unmintable destination on a CROSS seed (empty
    brand / non-registrable domain) logs LOUDLY (warning) and returns NULL — we
    NEVER invent a seller and NEVER assume 'self'.
    """
    registrable = etld1(destination_domain)
    if not registrable:
        # No destination identity to key a seller on. Not an error (many legacy /
        # content-only seed writers carry no destination) — quiet, NULL, A9-4
        # backfills. Assuming 'self' here is exactly the crutch D3 forbids.
        logger.debug(
            "derive_seed_seller: no registrable destination (domain=%r) — leaving "
            "seller_ref NULL (pre-A9-4 legacy state); ADR-009 D3",
            destination_domain,
        )
        return (None, None)

    # A KNOWN-RETAILER / marketplace destination (Amazon, Olive Young, StyleKorean,
    # …) is NEVER a brand's own store — even when an observed seller was minted from
    # that host (which stamps the host as the seller's domain and would otherwise
    # satisfy _anchor_owns_domain → 'self'). A 'self' seed is treated brand-official
    # and served PUBLIC by the trust policy (PUBLIC_PASSTHROUGH); a retailer-sourced
    # seed must be 'cross' so trust requires identity coverage (→ shadow) instead of
    # public-passthrough. (VODANA 2026-07-20: a no-D2C brand crawled from Amazon
    # minted self→public.) So a retailer host skips the self branch and falls
    # through to the CROSS mint below; a brand's OWN domain is unaffected.
    anchor = str(anchor_merchant_id or "").strip() or None
    if (
        anchor
        and not is_known_retailer(registrable)
        and await _anchor_owns_domain(anchor, registrable)
    ):
        return (anchor, "self")

    try:
        seller_ref = await ensure_observed_seller(
            brand=brand or "",
            domain=destination_domain or "",
            source_system=source_system,
            primary_platform=primary_platform,
        )
    except ValueError as exc:
        # We HAD a destination but could not mint a seller (empty brand / non-
        # registrable domain). Loud, NULL, never guessed — ADR-009 D3 no-fallback.
        logger.warning(
            "derive_seed_seller: UNRESOLVABLE cross-seller for brand=%r domain=%r "
            "(%s) — leaving seller_ref NULL (ADR-009 D3 no-fallback; A9-4 backfills)",
            brand,
            destination_domain,
            str(exc)[:200],
        )
        return (None, None)
    return (seller_ref, "cross")
