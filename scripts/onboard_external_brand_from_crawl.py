"""Onboard a crawled (external-seed) brand's products into the live catalog so
they land BOTH decision-grade and SERVABLE, in one pass.

--no-serving mode: onboard DECISION-GRADE ONLY. Writes identity + mirror +
category/offer + INCI (so the rows are audit- and deposit-eligible once the
identity graph credits trust — the deposit gate reads catalog_row_trust,
independent of serving) but SKIPS the serving half (_resolve_pdp_scope +
make_external_seed_servable). The rows then trip all four serving blockers
(entity_unresolved + no_seed + low_quality + no_image) and never enter
search/PLP. Use to measure/deposit a brand held out of serving (the electronics
pilot's Mojawa onboard).

Complements scripts/ingest_crawled_inci.py (#855), which adds INCI to products
that already exist by product_key. This runner is the other entry point: it
CREATES the external-seed catalog product from crawl output and walks it all the
way to serving_eligible.

Per product it does:
  1. upsert an external_product_seeds row
  2. run the external_seed -> catalog_products mirror
  3. set category_kind + mark the brand-direct offer first-party
  4. INCI + enrichment via the SHARED crawled-INCI ingest
     (services.crawled_inci_ingest) -- one upsert-raw_inci + enrich_and_persist
     implementation for both crawl tools, so they don't drift
  5. make_external_seed_servable: attached_product_key back-link + quality
     snapshot + agent_pdp_view refresh + recompute eligibility

Before onboarding, the cohort is deduplicated (dedupe_cohort): duplicate Shopify
listings of ONE real product — the "(Copy)"/"(Copy_T1)"/"(Convert_a)"-titled and
`-N`/`-copy`-handled clones a merchant's admin accumulates — collapse to a single
canonical (clean title, clean handle, oldest listing). Only the canonical is
seeded; each skipped clone's already-active seed is deactivated and its
catalog_products mirror suppressed (two-mirror gotcha), so a re-crawl of a
polluted store self-heals instead of re-creating N serving rows. See
docs/HANDOFF_crawl_side_dedup.md.

Idempotent throughout. Input: a JSON array on --file or stdin; each item:
  {external_product_id, brand, title, category_kind, product_type,
   destination_url, image_url, price_amount, description, raw_inci,
   offer_type?("brand_direct"|"retailer", default brand_direct),
   market?(default "US"), price_currency?(default "USD")}

market/price_currency carry the offer's real locale (e.g. a Korean D2C brand
sells in KRW) instead of being forced to US/USD -- baking a foreign price as USD
would corrupt the shared index. The mirror already propagates seed.market and
seed.price_currency to catalog_offers, so threading them here is sufficient.

Usage:
  python -m scripts.onboard_external_brand_from_crawl --file cohort.json --apply
  python -m scripts.onboard_external_brand_from_crawl --file cohort.json --apply --no-serving
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database
from scripts.mirror_external_seeds_to_catalog_products import _apply as mirror_apply
from services.brand_claim_service import normalize_host
from services.catalog_sync_service import make_catalog_product_key
from services.crawled_inci_ingest import ingest_crawled_inci_items
from services.external_seed_servability import (
    build_servable_quality_payload,
    make_external_seed_servable,
)
from services.offer_seller_identity import derive_offer_seller_identity
from services.pdp_scope_classifier import (
    ScopeSignals,
    classify,
    own_merchant_seller_term_sql,
)
# ADR-009 D2/D3 (docs/adr/ADR-009-seller-of-record-identity.md; IDENTITY_REFERENCE
# §3 Trap T3, §4): a crawl-onboarded brand mints its own per-brand observed
# seller-of-record instead of landing under the banned 'external_seed' bucket,
# and the seed row records that seller (seller_ref/seed_kind) so T2 attribution
# keys conversions by seller.
from services.seller_identity import derive_seed_seller, ensure_observed_seller

TOOL = "external_brand_crawl"
# catalog_products.platform channel for external-seed supply (matches the mirror).
PLATFORM = "external_seed"


def _seed_id(epid: str) -> str:
    return f"{TOOL}::{epid}"


# ---------------------------------------------------------------------------
# Crawl-side dedup of duplicate Shopify listings.
#
# A merchant who uses Shopify's "Duplicate product" (or hand-relists) ends up
# with several distinct product IDs for ONE real product — the copies carry
# junk titles like "... (Copy)", "(Copy_T1)", "(Convert_a)" and handles like
# `foo-serum-2` / `foo-serum-copy`. Each distinct title mints a distinct
# content_key on the mirror, so the serving-side near-dup collapse (keyed on
# content_key) can't merge them and every copy leaks into search/PLP as its own
# serving row (see docs/HANDOFF_crawl_side_dedup.md; the Jumiso "20% NIACINAMIDE
# High Potency Dark Spot Serum" cluster was 21 active seeds → 1).
#
# We collapse them at the source: group a cohort by (normalized brand, base
# title) and seed only ONE canonical per group — preferring a clean (non-copy)
# title, then a clean (unsuffixed) handle, then the oldest listing (smallest
# Shopify id = the original). Grouping is deliberately by brand+title only (NOT
# handle) so genuinely-distinct products are never merged; the handle is used
# only to pick the winner inside an already-established duplicate group.
# ---------------------------------------------------------------------------

# An opening "(Copy" / "(Convert" parenthetical anywhere in the title,
# case-insensitive. Deliberately does NOT require a closing paren — prod has a
# malformed leaker titled "...Dark Spot Serum (Copy_e" (no close). Mirrors the
# heuristic in scripts/cleanup_niacinamide_test_variants.py.
_JUNK_TITLE_RE = re.compile(r"\(\s*(?:copy|convert)", re.IGNORECASE)
# Everything from that parenthetical to end of string — stripped to derive the
# shared base title that copies collapse onto.
_JUNK_TITLE_SUFFIX_RE = re.compile(r"\s*\(\s*(?:copy|convert).*$", re.IGNORECASE)
# Trailing handle suffixes Shopify/relists append to a duplicate:
# `-copy`, `-copy_a`, `-copy-2`, `-convert_a`, or a bare `-2` / `-3`.
_HANDLE_DUP_SUFFIX_RE = re.compile(r"-(?:copy|convert)(?:[_-]?[a-z0-9]+)*$", re.IGNORECASE)
_HANDLE_NUM_SUFFIX_RE = re.compile(r"-\d+$")
_TRAILING_DIGITS_RE = re.compile(r"(\d+)$")

# Mirror suppression reason for a duplicate listing whose canonical sibling is
# seeded instead. Follows the established tombstone pattern (migrations 139/146):
# deactivating a seed does NOT clean its catalog_products mirror, so the mirror
# row must be suppressed explicitly.
DUP_SUPPRESSION_REASON = "external_brand_crawl_dup_listing"


def _norm_ws(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _norm_brand(brand: Any) -> str:
    return _norm_ws(brand).lower()


def _is_junk_title(title: Any) -> bool:
    return bool(_JUNK_TITLE_RE.search(str(title or "")))


def _base_title(title: Any) -> str:
    """Title with the '(copy…'/'(convert…' parenthetical (and everything after)
    stripped, lowercased and whitespace-collapsed. This is the key duplicate
    listings of one product share."""
    return _norm_ws(_JUNK_TITLE_SUFFIX_RE.sub("", str(title or ""))).lower()


def _handle_from_url(url: Any) -> str:
    """Last path segment of a Shopify destination_url (…/products/<handle>),
    lowercased, query/fragment removed. Empty string if unavailable."""
    raw = str(url or "").split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if not raw:
        return ""
    return raw.rsplit("/", 1)[-1].strip().lower()


def _base_handle(handle: str) -> str:
    """Strip trailing duplicate suffixes (`-copy…`, `-convert…`, bare `-N`) off a
    handle, iteratively, so `foo-serum-copy-2` and `foo-serum-2` both reduce to
    `foo-serum`. Used only for ranking within a duplicate group, so mild
    over-stripping is harmless."""
    prev = None
    cur = handle
    while cur and cur != prev:
        prev = cur
        cur = _HANDLE_DUP_SUFFIX_RE.sub("", cur)
        cur = _HANDLE_NUM_SUFFIX_RE.sub("", cur)
    return cur


def _numeric_id(epid: str) -> int:
    """Trailing digit run of the external_product_id (the Shopify product id).
    Smaller = created earlier = the original listing. Non-numeric ids sort last
    but remain stable via the epid tiebreak."""
    m = _TRAILING_DIGITS_RE.search(epid or "")
    return int(m.group(1)) if m else 2**63 - 1


def _group_key(p: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """(normalized brand, base title). None when there's no usable title — such
    rows are never grouped (each kept as-is) so we never collapse on emptiness."""
    base = _base_title(p.get("title"))
    if not base:
        return None
    return (_norm_brand(p.get("brand")), base)


def _canonical_rank(p: Dict[str, Any]) -> Tuple[int, int, int, str]:
    """Sort key inside a duplicate group; smallest wins. Prefer clean title, then
    clean handle, then the oldest (smallest Shopify id), then a stable id tiebreak."""
    epid = str(p.get("external_product_id") or "")
    handle = _handle_from_url(p.get("destination_url"))
    handle_clean = 0 if (handle and _base_handle(handle) == handle) else 1
    title_clean = 0 if not _is_junk_title(p.get("title")) else 1
    return (title_clean, handle_clean, _numeric_id(epid), epid)


def dedupe_cohort(
    cohort: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Tuple[Dict[str, Any], List[Dict[str, Any]]]]]:
    """Collapse duplicate listings of one product to a single canonical.

    Returns (kept, dropped, decisions):
      - kept: one canonical per product plus every ungrouped/singleton row,
        in original cohort order.
      - dropped: the duplicate listings to skip (and suppress if already seeded).
      - decisions: [(canonical, [dropped…])] for grouped products, for logging.

    Pure — no DB, no I/O — so it's directly unit-testable.
    """
    groups: Dict[Tuple[str, str], List[int]] = {}
    order: List[Tuple[str, str]] = []
    for idx, p in enumerate(cohort):
        key = _group_key(p)
        if key is None:
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(idx)

    dropped_idx: Dict[int, int] = {}  # dropped cohort index -> canonical index
    decisions: List[Tuple[Dict[str, Any], List[Dict[str, Any]]]] = []
    for key in order:
        members = groups[key]
        if len(members) < 2:
            continue
        ranked = sorted(members, key=lambda i: _canonical_rank(cohort[i]))
        canonical_i = ranked[0]
        drops = ranked[1:]
        for i in drops:
            dropped_idx[i] = canonical_i
        decisions.append((cohort[canonical_i], [cohort[i] for i in drops]))

    kept = [p for i, p in enumerate(cohort) if i not in dropped_idx]
    dropped = [p for i, p in enumerate(cohort) if i in dropped_idx]
    return kept, dropped, decisions


def _seed_domain(p: Dict[str, Any]) -> str:
    """Registrable host of the crawled brand site (its destination_url). This is
    the seller identity's domain half; it is also written to the seed's `domain`
    column so the mirror resolves the SAME observed seller for this product."""
    return normalize_host(p.get("destination_url"))


async def _resolve_seller_and_key(p: Dict[str, Any]) -> tuple[str, str]:
    """Resolve-or-mint this product's observed seller-of-record and its
    per-brand product_key. Deterministic on (brand, registrable-domain), so the
    key computed here MATCHES what the mirror writes for the same seed
    (ensure_observed_seller is idempotent). Raises on empty brand/domain — no
    fallback identity (ADR-009 D2)."""
    merchant_id = await ensure_observed_seller(
        brand=p.get("brand") or "",
        domain=_seed_domain(p),
        source_system=TOOL,
        primary_platform=PLATFORM,
    )
    return merchant_id, make_catalog_product_key(merchant_id, PLATFORM, p["external_product_id"])


def build_default_seed_variant(p: Dict[str, Any]) -> Dict[str, Any]:
    """Minimal sellable variant from the crawl row's own price/availability.

    The external-referral runtime gate (services/external_referral_readiness.py)
    treats `zero_variants` as a BLOCKER, so a seed without a variants array is
    dropped from every agent-search response at build time even when recalled.
    Crawled D2C PDPs are single-offer pages: the row-level price/currency/
    availability IS the sellable unit, so author it as one explicit default
    variant instead of leaving the seed permanently blocked. (Live incident
    2026-07-11: all 2,151 external_brand_crawl seeds carried no variants ->
    the whole cohort was invisible to find_products_multi.)
    """
    epid = str(p.get("external_product_id") or "").strip()
    variant_id = f"{epid}-default" if epid else "seed-variant-default"
    return {
        "id": variant_id,
        "variant_id": variant_id,
        "sku": variant_id,
        "title": p.get("title") or "Default",
        "price_amount": p.get("price_amount"),
        "price": p.get("price_amount"),
        "currency": (p.get("price_currency") or "USD").strip() or "USD",
        "availability": "in_stock",
        "image_url": p.get("image_url"),
        "source": "crawl_default_variant_v1",
    }


async def _upsert_seed(
    p: Dict[str, Any],
    *,
    seller_ref: Optional[str],
    seed_kind: Optional[str],
) -> None:
    from datetime import datetime, timezone

    seed_data = {
        "snapshot": {
            "title": p["title"], "description": p.get("description"), "brand": p.get("brand"),
            "product_type": p.get("product_type"), "category": p.get("category_kind"),
            # Sellable unit + extraction time. variants: see
            # build_default_seed_variant (zero_variants runtime blocker).
            # extracted_at: the stale_snapshot gate (7d) falls back to
            # updated_at when absent, which any metadata write silently
            # extends — stamp the real extraction event instead so freshness
            # is auditable. Crawl cohorts carry it as extracted_at when the
            # crawler recorded one; onboarding time is the honest floor
            # otherwise (this runner ingests fresh crawl output).
            "variants": [build_default_seed_variant(p)],
            "extracted_at": (
                str(p.get("extracted_at") or "").strip()
                or datetime.now(timezone.utc).isoformat()
            ),
        },
        "pdp_description_raw": p.get("description"),
        "pdp_ingredients_raw": p.get("raw_inci"),
        "inci_list": p.get("raw_inci"),
    }
    # ADR-009 D3: stamp the derived seller-of-record. A crawl-onboarded brand's
    # destination IS its own store, so this resolves to seed_kind='self',
    # seller_ref=<the observed seller>. NULL only if the brand/domain were
    # unmintable (derive_seed_seller logged loudly) — never assumed 'self'.
    await database.execute(
        """
        INSERT INTO external_product_seeds
          (id, external_product_id, market, tool, domain, destination_url, title, image_url,
           price_amount, price_currency, availability, seed_data, status, seller_ref, seed_kind)
        VALUES
          (:id, :epid, :market, :tool, :domain, :url, :title, :img,
           :price, :currency, 'in_stock', CAST(:data AS jsonb), 'active', :seller_ref, :seed_kind)
        ON CONFLICT (id) DO UPDATE SET
          title=EXCLUDED.title, image_url=EXCLUDED.image_url, price_amount=EXCLUDED.price_amount,
          price_currency=EXCLUDED.price_currency, market=EXCLUDED.market,
          domain=EXCLUDED.domain,
          seller_ref=COALESCE(EXCLUDED.seller_ref, external_product_seeds.seller_ref),
          seed_kind=COALESCE(EXCLUDED.seed_kind, external_product_seeds.seed_kind),
          -- MERGE, don't REPLACE (#1516): a re-run must not wipe enrichment keys
          -- written to seed_data AFTER onboarding (e.g. curated electronics_meta).
          -- `||` is a shallow, per-top-level-key merge: EXCLUDED wins for every
          -- top-level key the cohort actually carries (snapshot, pdp_description_raw,
          -- inci_list, …) — those are fully replaced per key, which is intended —
          -- while stored-only enrichment keys the cohort never writes survive.
          seed_data=COALESCE(external_product_seeds.seed_data, '{}'::jsonb) || EXCLUDED.seed_data,
          status='active', updated_at=NOW()
        """,
        {"id": _seed_id(p["external_product_id"]), "epid": p["external_product_id"], "tool": TOOL,
         "domain": _seed_domain(p) or None,
         "url": p.get("destination_url"), "title": p["title"], "img": p.get("image_url"),
         "price": p.get("price_amount"), "data": json.dumps(seed_data, ensure_ascii=False),
         "market": (p.get("market") or "US").strip() or "US",
         "currency": (p.get("price_currency") or "USD").strip() or "USD",
         "seller_ref": seller_ref, "seed_kind": seed_kind},
    )


async def _set_category_and_offer(
    p: Dict[str, Any], product_key: str, self_merchant_id: str
) -> None:
    pk = product_key
    if p.get("category_kind"):
        await database.execute(
            "UPDATE catalog_products SET category_kind=:ck, updated_at=NOW() WHERE product_key=:pk",
            {"ck": p["category_kind"], "pk": pk},
        )
    # Seller-identity derivation (Fix Plan C). Path B onboards a brand's OWN
    # storefront, so the common case is brand_direct — but derive from the domain
    # so a KNOWN-retailer host is never mislabelled brand_direct. An explicit
    # caller-provided offer_type still wins (manual override); otherwise we use the
    # domain/brand verdict and only fall back to brand_direct when the domain gives
    # no signal (the storefront we just crawled is the brand's by construction).
    explicit_offer_type = (p.get("offer_type") or "").strip()
    if explicit_offer_type:
        offer_type = explicit_offer_type
        is_first_party = offer_type == "brand_direct"
    else:
        verdict = derive_offer_seller_identity(
            domain=_seed_domain(p),
            canonical_url=p.get("destination_url"),
            official_domain=p.get("official_domain"),
            brand=p.get("brand"),
        )
        offer_type = verdict["offer_type"] or "brand_direct"
        is_first_party = offer_type == "brand_direct"
    # Respect the product's real market (the mirror already set it from the seed);
    # don't force 'US' -- a KRW Korean offer must not be relabeled US-market.
    market = (p.get("market") or "US").strip() or "US"
    # Scope to THIS seller's own offer (product_key + observed merchant), NOT every
    # offer on the product_key. A canonical product carried by multiple sellers has
    # sibling offers (a retailer offer via attach_retailer_offer, a connected-
    # merchant offer) sharing this product_key under different merchant_ids; an
    # unqualified `WHERE product_key` would stamp the crawled brand's brand_direct/
    # first-party onto those third-party offers too. The very next step
    # (_resolve_pdp_scope) counts those other sellers, so multi-seller products are
    # a real, expected case here.
    await database.execute(
        "UPDATE catalog_offers SET is_first_party=:fp, offer_type=:ot, market=:market, updated_at=NOW() "
        "WHERE product_key=:pk AND merchant_id=:mid",
        {"fp": is_first_party, "ot": offer_type, "market": market, "pk": pk, "mid": self_merchant_id},
    )


async def _resolve_pdp_scope(p: Dict[str, Any], product_key: str, self_merchant_id: str) -> None:
    """Promote the mirrored row off the 'unverified' scope default so it clears
    the serving-eligibility `entity_unresolved` gate (which requires a resolved
    pdp_scope OR a product_group_id). Without this, a crawl-onboarded brand mirrors
    + scores fine but never serves. Single-seller brand-direct crawl → seller_count
    is 1 → classify() returns 'merchant_owned'; a product already carried by another
    seller correctly classifies 'multi_merchant_canonical'.

    seller_count counts distinct OTHER sellers of the product. Under ADR-009 D2
    the product's own seller is now a per-brand observed merchant (not the shared
    'external_seed' bucket), so the self-exclusion is on `self_merchant_id` — this
    preserves the exact prior semantics (count of OTHER sellers) without changing
    any serving-eligibility rule.
    """
    pk = product_key
    row = await database.fetch_one(
        """
        SELECT {own_merchant_term}
          + COALESCE((SELECT COUNT(DISTINCT co.merchant_id) FROM catalog_offers co
                      WHERE co.product_key = :pk AND co.merchant_id IS NOT NULL
                        AND co.merchant_id <> :self_merchant), 0)
          + COALESCE((SELECT COUNT(DISTINCT eps.domain) FROM external_product_seeds eps
                      WHERE eps.attached_product_key = :pk AND eps.status = 'active'
                        AND eps.domain IS NOT NULL), 0) AS seller_count
        """.format(own_merchant_term=own_merchant_seller_term_sql("CAST(:self_merchant AS text)")),
        {"pk": pk, "self_merchant": self_merchant_id},
    )
    seller_count = int((dict(row) if row else {}).get("seller_count") or 1)
    scope = classify(ScopeSignals(category_label_source=None, seller_count=seller_count))
    await database.execute(
        "UPDATE catalog_products SET pdp_scope=:s, pdp_scope_source=:src, pdp_scope_set_at=NOW() "
        "WHERE product_key=:pk AND pdp_scope='unverified'",
        {"s": scope, "src": TOOL, "pk": pk},
    )


async def _suppress_dropped_listings(dropped: List[Dict[str, Any]]) -> int:
    """Self-heal already-polluted stores: for each duplicate listing we're now
    skipping, deactivate any active seed row and suppress its catalog_products
    mirror. Two-mirror gotcha — deactivating a seed does NOT tombstone its
    mirror (the stale-catalog sweep excludes external_seed), so the mirror row
    is suppressed explicitly via source_ref = seed id (globally unique; no
    merchant literal needed). Both writes are idempotency-guarded, so a re-crawl
    is a no-op once collapsed. No hard-deletes (reversible)."""
    suppressed = 0
    for p in dropped:
        sid = _seed_id(p["external_product_id"])
        await database.execute(
            "UPDATE external_product_seeds SET status='inactive', updated_at=NOW() "
            "WHERE id=:id AND status='active'",
            {"id": sid},
        )
        await database.execute(
            "UPDATE catalog_products SET suppression_reason=:reason, "
            "suppressed_at=COALESCE(suppressed_at, NOW()), updated_at=NOW() "
            "WHERE source_ref=:id AND suppression_reason IS NULL",
            {"id": sid, "reason": DUP_SUPPRESSION_REASON},
        )
        suppressed += 1
    return suppressed


async def _onboard(
    cohort: List[Dict[str, Any]],
    dropped: List[Dict[str, Any]],
    *,
    serve: bool = True,
) -> None:
    # Collapse duplicate Shopify listings BEFORE anything else: deactivate the
    # skipped listings' seeds + suppress their mirror rows (must precede
    # mirror_apply so the mirror doesn't recreate them — the mirror only inserts
    # rows for ACTIVE seeds). `cohort` is already the deduped (canonical) set.
    suppressed = await _suppress_dropped_listings(dropped)
    print(f"dedup: {len(cohort)} canonical, {len(dropped)} duplicate listing(s) suppressed={suppressed}")
    # ADR-009 D2/D3: resolve-or-mint each product's per-brand observed seller
    # BEFORE upserting the seed (so seller_ref/seed_kind land on the row) AND
    # before the mirror runs. The mirror derives the SAME identity (deterministic
    # on brand + registrable domain), so the product_key stashed here matches the
    # row the mirror writes — every downstream update targets the right PDP.
    keys: Dict[str, tuple[str, str]] = {}
    for p in cohort:
        keys[p["external_product_id"]] = await _resolve_seller_and_key(p)
    for p in cohort:
        merchant_id, _pk = keys[p["external_product_id"]]
        # A crawl-onboarded brand's destination is its own store → derive resolves
        # to ('self', <observed seller>). Using the shared derivation keeps a
        # single write-time rule and the honest NULL path (never assume 'self').
        seller_ref, seed_kind = await derive_seed_seller(
            anchor_merchant_id=merchant_id,
            brand=p.get("brand") or "",
            destination_domain=p.get("destination_url"),
            source_system=TOOL,
            primary_platform=PLATFORM,
        )
        await _upsert_seed(p, seller_ref=seller_ref, seed_kind=seed_kind)
    # mirror_apply ITSELF runs make_external_seed_servable (quality snapshot +
    # agent_pdp_view + serving-eligibility recompute) when its own env flag
    # EXTERNAL_SEED_MIRROR_MAKE_SERVABLE is on (prod has it on) — so skipping the
    # script's later servable loop is NOT enough to keep a --no-serving onboard
    # decision-grade only; the mirror would still mint serving artifacts, and the
    # rows would only be held out by serving_eligible=False (the low_quality gate,
    # since no quality snapshot is written). Under --no-serving we disable that
    # mirror pass just for THIS mirror_apply call so no serving artifacts are
    # created at all — the invariant then holds by construction (no agent_pdp_view
    # / no quality snapshot / no ips row). Save+restore the flag so a later
    # _onboard(serve=True) in the same process (a future batch driver) is not
    # silently held out of serving. The unconditional attached_product_key
    # back-link the mirror also does is serving-neutral (just links seed↔product).
    _prev_make_servable = os.environ.get("EXTERNAL_SEED_MIRROR_MAKE_SERVABLE")
    if not serve:
        os.environ["EXTERNAL_SEED_MIRROR_MAKE_SERVABLE"] = "0"
    try:
        inserted = await mirror_apply(max(50, len(cohort) * 3))
    finally:
        if not serve:
            if _prev_make_servable is None:
                os.environ.pop("EXTERNAL_SEED_MIRROR_MAKE_SERVABLE", None)
            else:
                os.environ["EXTERNAL_SEED_MIRROR_MAKE_SERVABLE"] = _prev_make_servable
    print(f"mirror inserted_catalog_products={inserted}")
    for p in cohort:
        merchant_id, product_key = keys[p["external_product_id"]]
        await _set_category_and_offer(p, product_key, merchant_id)
        # Resolve identity scope BEFORE make_external_seed_servable recomputes
        # eligibility, so the row can actually reach serving_eligible. SKIPPED
        # under --no-serving: leaving pdp_scope=NULL holds the row at the
        # `entity_unresolved` serving blocker — a backstop behind the operative
        # `low_quality` gate (see the early return below).
        if serve:
            await _resolve_pdp_scope(p, product_key, merchant_id)
    # INCI + enrichment via the shared crawled-INCI ingest (scripts.ingest_crawled_inci,
    # #855) -- one upsert-raw_inci + enrich_and_persist path for both crawl tools.
    # category_kind is set above first (enrichment reads it).
    inci_items = [
        {
            "product_key": keys[p["external_product_id"]][1],
            "sku_key": keys[p["external_product_id"]][1] + "::canonical",
            "raw_inci": p.get("raw_inci") or "",
        }
        for p in cohort
    ]
    report = await ingest_crawled_inci_items(inci_items, dry_run=False, db=database)
    print(f"inci ingest: written={report.get('inci_written')} actives={report.get('actives_filled')} skipped={report.get('skipped')}")
    if not serve:
        # Cap the held-out rows' lifecycle so they can NEVER enter cross-merchant
        # recall. The recall lane services/pivot_query_service.py
        # `_fetch_canonical_search_rows` gates on pdp_lifecycle_stage IN
        # ('validated','published') OR IS NULL — it trusts lifecycle, NOT
        # serving_eligible. But the mirror computed pdp_lifecycle_stage from CONTENT
        # (compute_lifecycle_stage): a row with a title+image+description PLUS a
        # category_path and any taxonomy tag lands at 'validated' independent of the
        # serving artifacts we deliberately skip below. So a --no-serving row that is
        # content-rich would leak into recall even though it's held out of serving.
        # (Live: hoverair_x1_combo did exactly this once the drone category
        # classifier gave held-out rows a category_path + a description-derived tag →
        # 'validated' → recall, while its 'candidate' siblings stayed hidden.)
        # Cap to 'candidate' (the row is content-complete; we only need it BELOW the
        # recall lane's validated/published threshold — 'draft' would understate it).
        # Idempotent + per-row (ON CONFLICT in the mirror is DO NOTHING, so it never
        # rewrites an existing row's stage; this cap is the explicit correction).
        for p in cohort:
            _merchant_id, product_key = keys[p["external_product_id"]]
            await database.execute(
                "UPDATE catalog_products SET pdp_lifecycle_stage='candidate', updated_at=NOW() "
                "WHERE product_key=:pk AND pdp_lifecycle_stage IN ('validated','published')",
                {"pk": product_key},
            )
        # Decision-grade-only onboard (--no-serving). Everything above is written:
        # the per-brand observed seller identity, the external_product_seeds rows,
        # the catalog_products + catalog_skus mirror (with content_key), the
        # category/offer, and INCI/enrichment. Those make the rows AUDIT- and
        # DEPOSIT-eligible once the identity graph credits identity_confidence
        # (deposit reads catalog_row_trust, independent of serving). What we
        # deliberately DON'T do here is the serving half: no _resolve_pdp_scope
        # (above) and no make_external_seed_servable (via the mirror or the loop
        # below), so no product_quality_snapshot and no agent_pdp_view row are
        # produced. (The mirror's attached_product_key backlink IS unconditional,
        # so `no_seed` is cleared — but the serving-eligibility ladder is
        # first-fail-wins and `low_quality` fires first, since no quality snapshot
        # exists; services.index_pipeline_state_service.) The row therefore stays
        # serving_eligible=False and never becomes servable — robustly, because
        # low_quality depends directly on the skipped make_servable pass, so
        # serving stays off even if pdp_scope were later resolved. Use this for a
        # merchant we want measured/deposited but held out of search/PLP (e.g. the
        # electronics pilot's Mojawa onboard).
        print(
            f"no-serving: {len(cohort)} row(s) onboarded decision-grade only "
            f"(serving artifacts intentionally skipped)"
        )
        return
    for p in cohort:
        _merchant_id, product_key = keys[p["external_product_id"]]
        summary = await make_external_seed_servable(
            product_key=product_key,
            seed_id=_seed_id(p["external_product_id"]),
            source_product_id=p["external_product_id"],
            quality_payload=build_servable_quality_payload(
                title=p["title"], description=p.get("description"), price=p.get("price_amount"),
                image_url=p.get("image_url"), brand=p.get("brand"), product_type=p.get("product_type"),
                # category_kind is always set (never null) → lets brand+category
                # score; raw_inci feeds the source-backed attributes component.
                category=p.get("category_kind"), raw_inci=p.get("raw_inci"),
            ),
            reason=TOOL,
        )
        print(f"  {p['external_product_id']}: {summary}")


def _load(args: argparse.Namespace) -> List[Dict[str, Any]]:
    raw = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    data = json.loads(raw)
    if not isinstance(data, list):
        raise SystemExit("input must be a JSON array of products")
    return data


async def _drive(args: argparse.Namespace) -> None:
    cohort = _load(args)
    # extracted_at honesty contract (review finding on the variants fix):
    # snapshot.extracted_at is what the 7d stale_snapshot serving gate audits,
    # so re-running a STALE cohort file must not mint fresh-looking extraction
    # events. Cohort items may carry their own extracted_at; --extracted-at
    # stamps a caller-asserted crawl time onto items that lack one; otherwise
    # _upsert_seed falls back to run time — only honest for freshly-crawled
    # input, hence the loud warning.
    run_extracted_at = str(getattr(args, "extracted_at", "") or "").strip()
    missing_ts = [p for p in cohort if not str(p.get("extracted_at") or "").strip()]
    if run_extracted_at:
        for p in missing_ts:
            p["extracted_at"] = run_extracted_at
    elif missing_ts:
        print(
            f"WARNING: {len(missing_ts)}/{len(cohort)} cohort items carry no extracted_at; "
            "their snapshot.extracted_at will be stamped with RUN TIME. Only proceed if "
            "this file is fresh crawl output — for older files pass --extracted-at "
            "<ISO timestamp of the actual crawl>.",
            file=sys.stderr,
        )
    kept, dropped, decisions = dedupe_cohort(cohort)
    serve = not args.no_serving
    print(
        f"{'APPLY' if args.apply else 'DRY'} :: {len(cohort)} products "
        f"({len(kept)} canonical, {len(dropped)} duplicate listing(s) collapsed) "
        f"[posture: {'serving' if serve else 'decision-grade only (no serving)'}]"
    )
    for canonical, drops in decisions:
        print(
            f"  collapse {len(drops) + 1} listings of "
            f"{canonical.get('brand')!r} / {_base_title(canonical.get('title'))!r} "
            f"-> keep {canonical.get('external_product_id')}"
        )
        for d in drops:
            print(f"      drop {d.get('external_product_id')} :: {d.get('title')}")
    if not args.apply:
        for p in kept:
            print(
                f"  would onboard {p.get('external_product_id')} "
                f"({p.get('category_kind')}) "
                f"{p.get('price_amount')} {p.get('price_currency') or 'USD'} "
                f"market={p.get('market') or 'US'} "
                f"{'[decision-grade only]' if not serve else '[+serving]'} "
                f":: {p.get('title')}"
            )
        for p in dropped:
            print(
                f"  would SKIP dup {p.get('external_product_id')} "
                f"(deactivate seed + suppress mirror if present) :: {p.get('title')}"
            )
        return
    await database.connect()
    try:
        await _onboard(kept, dropped, serve=serve)
    finally:
        await database.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", help="JSON array file (default: stdin)")
    parser.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    parser.add_argument(
        "--extracted-at",
        dest="extracted_at",
        default="",
        help=(
            "ISO timestamp of the crawl that produced this cohort file; stamped as "
            "snapshot.extracted_at on items that lack their own. Without it, items "
            "missing extracted_at get RUN time (only honest for fresh crawl output)."
        ),
    )
    parser.add_argument(
        "--no-serving",
        action="store_true",
        help=(
            "decision-grade-only onboard: write identity + mirror + category/offer "
            "+ INCI (so the rows are audit/deposit-eligible) but SKIP the serving "
            "half (_resolve_pdp_scope + make_external_seed_servable), so the rows "
            "never become servable. Use to measure/deposit a brand while holding it "
            "out of search/PLP (e.g. the electronics pilot's Mojawa onboard)."
        ),
    )
    asyncio.run(_drive(parser.parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
