"""Shared assembler and source-row readers for agent_pdp_view rows (Stage 3a).

The agent_pdp_view denormalized table (mig 085, Stage 3a-i) is fed from
two surfaces:

  * The one-shot backfill (Stage 3a-ii — scripts/backfill_agent_pdp_view.py)
  * The writer hook (Stage 3a-iii — services/seed_data_writer.py), which
    refreshes affected rows on every seed_data commit.

Both call the same pure-Python assembler defined here. Keeping assembly
side-effect-free (no DB writes inside assemble_row) means the writer
hook can dry-run / preview the next row state without touching prod,
and the backfill stays trivially testable. The read helpers here share
the per-content_key SELECT shape across the backfill and writer hook;
callers still own batching and persistence.
"""

from __future__ import annotations

import json
import logging
import os
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from db.database import database
from services.catalog_identity import normalize_gtin
from services.claim_safety import ensure_category_disclaimers
from services.source_quarantine import (
    CATALOG_PRODUCT_DOMAIN_SQL,
    build_quarantine_anti_join_sql,
)
from services.title_normalization import normalize_display_title

logger = logging.getLogger(__name__)

# Top-N offers stored per row. Schema docstring (mig 085) says <=5;
# matches the AggregateOffer behavior on the frontend.
OFFER_TOP_N = 5

# Cap variants at 50 — matches the schema comment. Tom Ford foundation
# has 40 shades, our prod p99.
VARIANT_CAP = 50

PDP_URL_PREFIX = "https://agent.pivota.cc/products/"

# Refresh-source tag the backfill writes when seeding rows from
# scratch. The Stage 3a-iii writer hook sets its own source ("writer_commit"
# or similar) so the audit trail distinguishes one-shot fills from
# incremental refreshes.
BACKFILL_REFRESH_SOURCE = "backfill_3a_ii"

# Enforce the source-quarantine overlay (mig 134) at the canonical-pick / serve
# layer. catalog_source_quarantine is OPT-IN — readers must anti-join it, and
# this serving assembler historically did NOT, so quarantines were silently
# shadow here. fetch_products_for_key is the single loader feeding pick_canonical
# (both assemble_row and the enrichment overlay fetch, via
# refresh_agent_pdp_view_for_content_key — the only APV rebuild path, shared by
# on-demand refresh AND catalog_sync). Anti-joining here keeps a quarantined
# source (e.g. a duplicate App Store review account sharing a real merchant's
# store) from winning the canonical pick and shadowing the real merchant's
# enriched PDP. Pure SQL with hardcoded column refs — no user input.
_SOURCE_QUARANTINE_ANTI_JOIN = build_quarantine_anti_join_sql(
    # The FULL domain chain, not cp.source_domain alone — see #1643 and the
    # comment on CATALOG_PRODUCT_DOMAIN_SQL. 4,007 of 14,124 prod rows have a
    # NULL source_domain; on those this predicate was blind to the quarantine
    # while the trust layer was not.
    row_domain_expr=CATALOG_PRODUCT_DOMAIN_SQL,
    row_merchant_expr="cp.merchant_id",
    row_platform_expr="cp.platform",
    row_source_system_expr="cp.source_system",
    row_source_ref_expr="cp.source_ref",
)


# ---------------------------------------------------------------------
# DB fetch helpers
# ---------------------------------------------------------------------

async def fetch_products_for_key(content_key: str, *, db: Any = None) -> List[Dict[str, Any]]:
    read_db = db or database
    rows = await read_db.fetch_all(
        f"""
        SELECT
          cp.product_key,
          cp.merchant_id,
          cp.platform,
          cp.source_product_id,
          cp.title,
          cp.description,
          cp.brand,
          cp.product_type,
          cp.category,
          cp.category_kind,
          cp.image_url,
          cp.product_payload,
          cp.tags,
          cp.price_tier,
          cp.use_case_tags,
          cp.lifestyle_tags,
          cp.demographic,
          cp.pdp_lifecycle_stage,
          cp.pivota_signature_id,
          cp.canonical_url,
          cp.sync_status,
          cp.created_at,
          cp.material,
          cp.material_source,
          cp.material_confidence,
          cp.care,
          cp.care_source,
          cp.care_confidence,
          cp.size_guide,
          cp.size_guide_source,
          cp.size_guide_confidence,
          cp.rating_value,
          cp.rating_count,
          pgm.product_group_id,
          pgm.is_primary AS group_is_primary
        FROM catalog_products cp
        LEFT JOIN product_group_members pgm
          ON pgm.merchant_id = cp.merchant_id
         AND pgm.platform = cp.platform
         AND pgm.platform_product_id = cp.source_product_id
        WHERE cp.content_key = :ck
        {_SOURCE_QUARANTINE_ANTI_JOIN}
        """,
        {"ck": content_key},
    )
    return [dict(r) for r in rows or []]


async def fetch_skus_for_keys(product_keys: List[str], *, db: Any = None) -> List[Dict[str, Any]]:
    if not product_keys:
        return []
    read_db = db or database
    rows = await read_db.fetch_all(
        """
        SELECT
          sku_key, product_key, merchant_id, source_variant_id, source_product_id,
          sku, barcode, title, currency, image_url, visible_attributes,
          visible_option_labels
        FROM catalog_skus
        WHERE product_key = ANY(:keys)
        """,
        {"keys": product_keys},
    )
    return [dict(r) for r in rows or []]


async def fetch_offers_for_keys(product_keys: List[str], *, db: Any = None) -> List[Dict[str, Any]]:
    if not product_keys:
        return []
    read_db = db or database
    rows = await read_db.fetch_all(
        """
        SELECT
          o.offer_id, o.sku_key, o.product_key, o.merchant_id,
          o.availability, o.currency, o.list_price,
          o.merchant_effective_price, o.estimated_best_price,
          o.market, o.offer_type, o.is_first_party,
          m.merchant_name
        FROM catalog_offers o
        LEFT JOIN catalog_merchants m ON m.merchant_id = o.merchant_id
        WHERE o.product_key = ANY(:keys)
          AND o.suppressed_at IS NULL
        """,
        {"keys": product_keys},
    )
    return [dict(r) for r in rows or []]


async def fetch_external_seed_for_keys(
    product_keys: List[str],
    *,
    db: Any = None,
) -> Optional[Dict[str, Any]]:
    """First active external_product_seed attached to any product in a
    content_key cluster. The backfill uses this — it has no specific
    seed in hand, just a group of product_keys, and any one row works
    as a content fallback.

    The writer hook should NOT use this; it has a specific seed in
    hand (the one whose seed_data just changed). See
    fetch_external_seed_by_id.
    """
    if not product_keys:
        return None
    read_db = db or database
    row = await read_db.fetch_one(
        """
        SELECT id, attached_product_key, title, image_url, seed_data,
               canonical_url, destination_url
        FROM external_product_seeds
        WHERE attached_product_key = ANY(:keys)
          AND status = 'active'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"keys": product_keys},
    )
    return dict(row) if row else None


async def fetch_external_seed_by_id(
    seed_id: str,
    *,
    db: Any = None,
) -> Optional[Dict[str, Any]]:
    """Fetch the external seed by its specific id. Used by the writer
    hook — the triggering seed_id is in scope, and we want the
    description fallback to come from the seed whose seed_data just
    changed, not "newest active in the group" (which can be a
    different, stale row).
    """
    read_db = db or database
    row = await read_db.fetch_one(
        """
        SELECT id, attached_product_key, title, image_url, seed_data,
               canonical_url, destination_url
        FROM external_product_seeds
        WHERE id = :seed_id
        """,
        {"seed_id": seed_id},
    )
    return dict(row) if row else None


async def fetch_evidence_for_keys(
    product_keys: List[str],
    *,
    db: Any = None,
) -> Dict[str, Any]:
    """Highest-precedence agent-decision-grade evidence for a content_key cluster.

    evidence_profile lives per product_key (beauty_product_profiles), but the
    canonical record is shared across the merchant rows that resolve to one
    content_key. Per ADR-001 source precedence (brand-official > supplier >
    reseller) the canonical evidence is the brand-official one — which for the
    crawled wedge catalog is the external-seed row: the legacy 'external_seed'
    bucket OR (ADR-009) a per-brand observed seller (merch_obs_…). So prefer
    external-seed-origin evidence, then the most-recently-authored, among the
    rows that actually carry evidence.

    Returns {} when no row in the cluster has authored evidence yet (the agent
    view column stays NULL — the merchant simply isn't decision-grade yet).
    """
    if not product_keys:
        return {}
    read_db = db or database
    row = await read_db.fetch_one(
        """
        SELECT evidence_profile, required_disclaimers
        FROM beauty_product_profiles
        WHERE product_key = ANY(:keys)
          AND evidence_profile IS NOT NULL
        ORDER BY (merchant_id = 'external_seed' OR merchant_id LIKE 'merch_obs_%') DESC, updated_at DESC
        LIMIT 1
        """,
        {"keys": product_keys},
    )

    # Phase 2a: UNION the cross-vertical merchant evidence store with the beauty
    # (ingredient-mechanism) evidence so non-beauty merchant claims land on
    # agent_pdp_view.evidence_profile too. Beauty passed first → it wins dedupe +
    # review_state precedence. Best-effort; a missing store yields {}.
    from db.product_evidence import (
        fetch_product_evidence_for_keys,
        merge_disclaimer_lists,
        merge_evidence_profiles,
    )

    beauty_profile = row["evidence_profile"] if row else None
    beauty_disclaimers = row["required_disclaimers"] if row else None
    general = await fetch_product_evidence_for_keys(product_keys, db=read_db)

    merged_profile = merge_evidence_profiles(beauty_profile, general.get("evidence_profile"))
    merged_disclaimers = merge_disclaimer_lists(beauty_disclaimers, general.get("required_disclaimers"))

    if not merged_profile and not merged_disclaimers:
        return {}
    return {
        "evidence_profile": merged_profile,
        "required_disclaimers": merged_disclaimers,
    }


def coalesce_first(*values: Any) -> Any:
    """First non-empty value (string trim aware)."""
    for v in values:
        if v is None:
            continue
        if isinstance(v, str):
            stripped = v.strip()
            if stripped:
                return stripped
        else:
            return v
    return None


def pick_canonical(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Deterministic canonical-row pick.

    Tiebreak ladder:
      0. platform != 'url_audit'  (a real row always beats an observed audit seed)
      1. product_group_members.is_primary = true  (multi-seller canonical)
      2. catalog_products.pivota_signature_id is set  (indexed surface)
      3. lowest product_key ASC  (stable hash-derived ordering)

    Tier 0 is a safety guardrail: a `url_audit` seed's content_key is brand+title
    (non-unique, GTIN-less), so it can COLLIDE with a real/claimed product sharing
    that content_key. agent_pdp_view is keyed by content_key, so whichever row
    wins here supplies the served title/description/image. An audit seed must
    NEVER win that over a non-audit row — else a colliding seed would overwrite a
    claimed product's served PDP. Audit seeds therefore sort strictly last; when a
    content_key has ONLY audit seeds (the common case — a competitor/arbitrary URL
    the merchant audited), it isn't served anyway (no index_pipeline_state row).
    """
    def key(r: Dict[str, Any]) -> Tuple[int, int, int, str]:
        audit_rank = 1 if r.get("platform") == "url_audit" else 0
        primary_rank = 0 if r.get("group_is_primary") else 1
        sig_rank = 0 if r.get("pivota_signature_id") else 1
        return (audit_rank, primary_rank, sig_rank, r.get("product_key") or "")

    return sorted(rows, key=key)[0]


def read_image_urls(payload: Any) -> List[str]:
    """Pull a deduped image-URL list out of product_payload JSONB. Real
    upstream shapes vary (Shopify nests under .images[].src, Wix has
    .media, our external seeds drop a flat array). Try the common ones.
    """
    if not isinstance(payload, dict):
        return []
    found: List[str] = []

    def _add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            url = value.strip()
            if url not in found:
                found.append(url)

    for v in payload.get("image_urls") or []:
        _add(v)
    for v in payload.get("images") or []:
        if isinstance(v, str):
            _add(v)
        elif isinstance(v, dict):
            _add(v.get("src") or v.get("url"))
    if not found:
        _add(payload.get("image_url"))
    return found


def normalize_offer(offer: Dict[str, Any], primary_merchant_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Project a catalog_offers row + merchants join down to the
    offer-shape we store in agent_pdp_view.offers JSONB. Returns None if
    the offer has no usable price.
    """
    price = coalesce_first(
        offer.get("merchant_effective_price"),
        offer.get("estimated_best_price"),
        offer.get("list_price"),
    )
    if price is None:
        return None
    try:
        price_decimal = Decimal(price)
    except Exception:
        return None
    if price_decimal <= 0:
        return None
    return {
        "merchant_id": offer.get("merchant_id"),
        "merchant_name": offer.get("merchant_name"),
        "price": float(price_decimal),
        "currency": offer.get("currency"),
        "availability": offer.get("availability"),
        "url": None,  # caller injects via merchant_url_by_id
        "is_primary": offer.get("merchant_id") == primary_merchant_id,
        # Buyability facts (market-agnostic; the serve layer decides buyable-here
        # against the request market). A brand-direct KRW offer and a US retailer
        # offer must be distinguishable so agents don't treat a cross-border
        # listing as a same-market purchase.
        "market": offer.get("market"),
        "offer_type": offer.get("offer_type"),
        "is_first_party": bool(offer.get("is_first_party")),
    }


def aggregate_offers(
    offers: List[Dict[str, Any]],
    primary_merchant_id: Optional[str],
    merchant_url_by_id: Dict[str, Optional[str]],
    seller_trust_by_id: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[
    Optional[str],
    Optional[Decimal],
    Optional[Decimal],
    int,
    List[Dict[str, Any]],
]:
    """Compute price aggregates + top-N offers. Stable ordering:
    primary merchant first, then price ASC, then merchant_id ASC.

    `seller_trust_by_id` (W8): the outcome-derived per-merchant trust envelope,
    injected onto each offer like `url` is. Absent/None → offers carry no trust
    key (the honest empty state — no transacted outcomes, no claim).
    """
    trust_by_id = seller_trust_by_id or {}
    normalized: List[Dict[str, Any]] = []
    for o in offers:
        n = normalize_offer(o, primary_merchant_id)
        if not n:
            continue
        merchant_id = n.get("merchant_id") or ""
        n["url"] = merchant_url_by_id.get(merchant_id)
        trust = trust_by_id.get(merchant_id)
        if trust:
            n["seller_trust"] = trust
        normalized.append(n)

    if not normalized:
        return None, None, None, 0, []

    # Modal currency: dominant one across the group. Mixed-currency
    # groups (rare but real — .com vs .ca) surface the dominant code at
    # the row level; each offer keeps its own currency intact.
    currency_counts: Dict[str, int] = {}
    for n in normalized:
        c = n.get("currency") or ""
        if c:
            currency_counts[c] = currency_counts.get(c, 0) + 1
    currency = (
        max(currency_counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
        if currency_counts
        else None
    )

    prices_in_currency = [
        Decimal(str(n["price"])) for n in normalized
        if currency is None or n.get("currency") == currency
    ]
    price_min = min(prices_in_currency) if prices_in_currency else None
    price_max = max(prices_in_currency) if prices_in_currency else None

    def sort_key(o: Dict[str, Any]) -> Tuple[int, float, str]:
        return (
            0 if o.get("is_primary") else 1,
            float(o.get("price") or 0.0),
            o.get("merchant_id") or "",
        )

    top = sorted(normalized, key=sort_key)[:OFFER_TOP_N]
    return currency, price_min, price_max, len(normalized), top


def aggregate_variants(skus: List[Dict[str, Any]], canonical_source_product_id: Optional[str]) -> Tuple[List[Dict[str, Any]], int]:
    """Build variants[]. Drop the "singleton placeholder" SKU where
    source_variant_id == source_product_id (Path A sync inserts one such
    row per product to keep SKU joins valid even when upstream has no
    variants — not a real variant).
    """
    variants: List[Dict[str, Any]] = []
    for s in skus:
        svid = s.get("source_variant_id")
        spid = s.get("source_product_id")
        if svid and spid and str(svid) == str(spid):
            continue
        variants.append({
            "variant_id": svid,
            "sku": s.get("sku"),
            "title": s.get("title"),
            "options": s.get("visible_option_labels") or s.get("visible_attributes") or {},
            "image_url": s.get("image_url"),
            "currency": s.get("currency"),
            "merchant_id": s.get("merchant_id"),
        })

    # Deterministic ordering: SKU then variant_id. Keeps re-runs
    # byte-identical for diff-friendly admin tooling.
    variants.sort(key=lambda v: (v.get("sku") or "", str(v.get("variant_id") or "")))

    count = len(variants)
    return variants[:VARIANT_CAP], count


def pick_gtin13(skus: List[Dict[str, Any]]) -> Optional[str]:
    """Pick the canonical 14-char GTIN for the content_key group.

    Two failure modes to avoid:
      1. agent_pdp_view.gtin13 is VARCHAR(14); normalize_gtin passes
         15+ digit malformed inputs through unchanged. Skip those —
         they aren't valid GTIN-14.
      2. SKUs in the same content_key group can carry different
         barcodes (data noise, or genuine cross-merchant disagreement).
         Pick the modal value so we deterministically converge on the
         GTIN that appears most often; tiebreak by lex order so re-runs
         are byte-identical.
    """
    counts: Dict[str, int] = {}
    for s in skus:
        bar = s.get("barcode")
        if not bar:
            continue
        canon = normalize_gtin(str(bar))
        if not canon or len(canon) != 14:
            continue
        counts[canon] = counts.get(canon, 0) + 1
    if not counts:
        return None
    # Sort by count DESC, then GTIN ASC for stable tiebreak.
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def evidence_safe_product_keys(
    products: List[Dict[str, Any]], skus: List[Dict[str, Any]]
) -> List[str]:
    """Cluster members SAFE to draw agent-decision-grade evidence from.

    A no-GTIN content_key is DELIBERATELY non-unique — it collapses to brand+title
    (services/catalog_identity.py) — so a content_key cluster can collide two
    DISTINCT physical products. fetch_evidence_for_keys picks ONE member's evidence
    cluster-wide (ORDER BY external-seed-origin DESC, updated_at DESC LIMIT 1) with no
    record of which member; assemble_row then bakes it onto pick_canonical's row,
    which may be a DIFFERENT product — i.e. Brand A's substantiated claim served on
    Brand B's listing. Restrict the evidence fetch to the canonical member UNLESS
    the cluster is provably ONE product:
      - GTIN-resolved (a real barcode folded the cluster — the merge is intended);
      - a single member (nothing was merged);
      - all members share one normalized brand + title (legit multi-merchant DTC).
    Returns the product_key list to pass to fetch_evidence_for_keys.
    """
    pkeys = [p["product_key"] for p in products if p.get("product_key")]
    if len(set(pkeys)) <= 1:
        return pkeys
    if pick_gtin13(skus):
        return pkeys
    from services.catalog_identity import normalize_brand, normalize_title

    brands = {normalize_brand(p.get("brand") or "") for p in products}
    titles = {normalize_title(p.get("title") or "") for p in products}
    if len(brands) == 1 and "" not in brands and len(titles) == 1:
        return pkeys
    # Collision: only the canonical member's evidence may be attributed to the
    # served row. Scoping the fetch (rather than suppressing post-hoc) also keeps
    # the canonical product's own required_disclaimers — the floor-safety field
    # rides the same query — without ever pulling a colliding member's claims.
    canonical = pick_canonical(products)
    canon_pk = canonical.get("product_key")
    return [canon_pk] if canon_pk else []


# The list-typed members of taxonomy_tags. Their source columns are JSONB, and
# depending on driver codec a read can hand them back as JSON *strings* rather
# than decoded lists. Embedding that string and re-serializing the outer dict is
# how prod ended up serving `"tags": "[\"serum\"]"` — a JSON string inside
# JSON, which every agent-side consumer mis-parses (measured 2026-08-10:
# 33/26/26 string-typed values across a 39-row sample of the live view).
_TAXONOMY_LIST_KEYS = ("tags", "use_case_tags", "lifestyle_tags")


def _parse_jsonish(value: Any) -> Any:
    """Decode a JSON-encoded-string container; pass everything else through.

    Only strings that LOOK like JSON containers are attempted, and a failed
    parse returns the original value — a plain scalar tag is never destroyed
    and nothing is ever invented.
    """
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in ("[", "{"):
            try:
                return json.loads(stripped)
            except (ValueError, TypeError):
                return value
    return value


def normalize_taxonomy_tags(value: Any) -> Any:
    """Repair double-encoded taxonomy_tags for consumers.

    Read-side twin of the write-side coercion in build_taxonomy_tags — and
    effectively PERMANENT, not transitional: the reconciler only re-assembles
    rows whose truth timestamps moved, so a historical row with no source
    change keeps the string-encoded shape forever unless
    scripts/backfill_agent_pdp_view.py is run once. Every projection must
    therefore normalize on the way out, indefinitely. Handles the
    whole-dict-as-string case too. Never raises.
    """
    value = _parse_jsonish(value)
    if not isinstance(value, dict):
        return value
    out = dict(value)
    for key in _TAXONOMY_LIST_KEYS:
        if key in out:
            out[key] = _parse_jsonish(out[key])
    return out


def build_taxonomy_tags(canonical: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    fields = {
        "price_tier": canonical.get("price_tier"),
        # Coerced at WRITE time so freshly assembled rows store real arrays —
        # see _TAXONOMY_LIST_KEYS for how the string shape got in.
        "use_case_tags": _parse_jsonish(canonical.get("use_case_tags")),
        "lifestyle_tags": _parse_jsonish(canonical.get("lifestyle_tags")),
        "demographic": canonical.get("demographic"),
        "product_type": canonical.get("product_type"),
        "category": canonical.get("category"),
        "tags": _parse_jsonish(canonical.get("tags")),
    }
    tags = {k: v for k, v in fields.items() if v not in (None, "", [], {})}
    return tags or None


def build_breadcrumb(canonical: Dict[str, Any], pdp_url: Optional[str]) -> Optional[List[Dict[str, Any]]]:
    title = coalesce_first(canonical.get("title"))
    if not title:
        return None
    crumbs: List[Dict[str, Any]] = [
        {"position": 1, "name": "Home", "item": "https://agent.pivota.cc/"},
    ]
    category = coalesce_first(canonical.get("category"), canonical.get("product_type"))
    if category:
        crumbs.append({"position": len(crumbs) + 1, "name": category, "item": None})
    brand = coalesce_first(canonical.get("brand"))
    if brand:
        crumbs.append({"position": len(crumbs) + 1, "name": brand, "item": None})
    crumbs.append({"position": len(crumbs) + 1, "name": title, "item": pdp_url})
    return crumbs


# Source-priority ordering used by coalesce_fashion_fields. Lower index = higher
# priority. Merchant_payload always wins (it's the merchant's authoritative
# platform); merchant_authored beats LLM; LLM beats external_seed; unknown /
# legacy sources fall through to the lowest priority. Order matters — do not
# rearrange without updating the corresponding source-precedence rule in
# services/fashion_field_authoring.py (write-side guard).
_FASHION_SOURCE_PRIORITY = (
    "merchant_payload",
    "merchant_authored",
    "llm_extraction_v1",
    "external_seed",
)


def _source_rank(src: Optional[str]) -> int:
    """Lower is better. Unknown / None falls to the worst rank."""
    if not src:
        return len(_FASHION_SOURCE_PRIORITY) + 1
    try:
        return _FASHION_SOURCE_PRIORITY.index(src)
    except ValueError:
        return len(_FASHION_SOURCE_PRIORITY)


def _looks_like_text_value(v: Any) -> bool:
    """material / care must be a non-blank string. Codex review noted
    that an empty dict, list, or whitespace-only string from a corrupted
    upstream row was passing the truthiness check and could land in a
    TEXT column or surface to merchant-facing prose. Defend explicitly."""
    return isinstance(v, str) and bool(v.strip())


def _looks_like_size_guide_value(v: Any) -> bool:
    """size_guide accepts strings (wrapped as {raw: ...}) or dicts
    (structured charts). Lists, numbers, bools are rejected."""
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, dict):
        return bool(v)
    return False


def _value_is_valid_for_field(field: str, value: Any) -> bool:
    if field == "size_guide":
        return _looks_like_size_guide_value(value)
    return _looks_like_text_value(value)


def _candidates_for(
    products: List[Dict[str, Any]],
    external_seed: Optional[Dict[str, Any]],
    field: str,
) -> List[Dict[str, Any]]:
    """Build the candidate list for one fashion field across all members
    of a product_group plus the matched external_product_seed.

    Each candidate is `{value, source, confidence, product_key,
    is_primary}` with provenance preserved so the caller can record
    which source won, and so equal-rank ties break deterministically
    against the product_group primary member.

    Type-gating per field is enforced here — a corrupted upstream row
    with `material: []` or `care: {}` is dropped before it reaches the
    coalesce sort or a merchant-facing surface.
    """
    out: List[Dict[str, Any]] = []
    src_col = f"{field}_source"
    conf_col = f"{field}_confidence"
    for p in products:
        v = p.get(field)
        if not _value_is_valid_for_field(field, v):
            continue
        out.append({
            "value": v,
            "source": p.get(src_col),
            "confidence": p.get(conf_col),
            "product_key": p.get("product_key") or "",
            "is_primary": bool(p.get("group_is_primary")),
        })
    if external_seed is not None:
        seed_data = external_seed.get("seed_data") or {}
        if isinstance(seed_data, str):
            try:
                seed_data = json.loads(seed_data)
            except Exception:
                seed_data = {}
        if isinstance(seed_data, dict):
            seed_val = seed_data.get(field)
            if _value_is_valid_for_field(field, seed_val):
                # size_guide from a seed is the same plain-string shape
                # the LLM extractor and the merchant-authored path use —
                # wrap into {raw: ...} so downstream JSONB writers see a
                # consistent dict. Was previously emitted unwrapped,
                # producing inconsistent gateway shapes.
                if field == "size_guide" and isinstance(seed_val, str):
                    seed_val = {"raw": seed_val.strip()}
                out.append({
                    "value": seed_val,
                    "source": "external_seed",
                    # Seed-derived confidence isn't structured in seed_data
                    # today; treat as 1.0 to keep ranking deterministic but
                    # park it at the lowest source priority above.
                    "confidence": 1.0,
                    "product_key": "",
                    "is_primary": False,
                })
    return out


def coalesce_fashion_fields(
    products: List[Dict[str, Any]],
    external_seed: Optional[Dict[str, Any]],
) -> Dict[str, Optional[Any]]:
    """Pick the winning material / care / size_guide across the product_group.

    For each field, sort candidates by (source priority, confidence DESC,
    is_primary DESC, product_key ASC) and take the head. Each field is
    coalesced independently — the winning row for material may differ
    from the winning row for size_guide.

    Deterministic tie-breaking: equal source + confidence is resolved by
    preferring the product_group primary member, then by alphabetic
    product_key. Codex review flagged that unordered ties could flap
    between merchants on every refresh; this pins them.

    Returns a dict with `<field>`, `<field>_source`, `<field>_confidence`
    for all three fields (None when no candidate exists).
    """
    coalesced: Dict[str, Optional[Any]] = {}
    for field in ("material", "care", "size_guide"):
        candidates = _candidates_for(products, external_seed, field)
        if not candidates:
            coalesced[field] = None
            coalesced[f"{field}_source"] = None
            coalesced[f"{field}_confidence"] = None
            continue
        candidates.sort(key=lambda c: (
            _source_rank(c["source"]),
            -(c["confidence"] if isinstance(c.get("confidence"), (int, float)) else 0.0),
            # is_primary True (0) wins over False (1)
            0 if c.get("is_primary") else 1,
            c.get("product_key") or "",
        ))
        winner = candidates[0]
        coalesced[field] = winner["value"]
        coalesced[f"{field}_source"] = winner["source"]
        coalesced[f"{field}_confidence"] = winner["confidence"]
    return coalesced


def assemble_row(
    *,
    content_key: str,
    products: List[Dict[str, Any]],
    skus: List[Dict[str, Any]],
    offers: List[Dict[str, Any]],
    external_seed: Optional[Dict[str, Any]],
    evidence: Optional[Dict[str, Any]] = None,
    refresh_source: str = BACKFILL_REFRESH_SOURCE,
    enrichment: Optional[Dict[str, Any]] = None,
    seller_trust_by_id: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Produce the agent_pdp_view row payload from raw source rows.
    Returns None when the data is too thin to be useful (no title).
    Side-effect-free: caller persists.

    `enrichment` is the Pivota product_enrichment overlay row for the canonical
    product (E2 — the enrichment publish bridge). When it carries a
    description_markdown, that curated/generated copy takes precedence over the
    raw storefront description so it reaches BOTH the served PDP and the
    serving-eligibility gate (which reads agent_pdp_view.description). Optional;
    callers that don't pass it (the maintenance scripts) keep prior behavior.
    """
    canonical = pick_canonical(products)
    # E2 publish bridge: a brand-attested title_override outranks the raw
    # storefront title (same precedence the description bridge below applies),
    # so the brand's own title reaches the served PDP. coalesce_first is
    # strip-aware, so an absent/empty override falls through.
    title = coalesce_first(
        (enrichment or {}).get("title_override"),
        canonical.get("title"),
    )
    if not title:
        return None
    # Clean storefront noise (bundle tags, legal-entity seller prefix) before
    # this title reaches the agent surface; keeps size/pack info intact.
    title = normalize_display_title(title)

    seed_data = (external_seed or {}).get("seed_data") or {}
    if isinstance(seed_data, str):
        try:
            seed_data = json.loads(seed_data)
        except Exception:
            seed_data = {}

    # E2 publish bridge: the Pivota enrichment overlay's description_markdown
    # outranks the raw storefront description (it's the compliance-checked,
    # citable copy generated for the canonical PDP). None/empty falls through —
    # coalesce_first is strip-aware, so an absent overlay can't blank the field.
    description = coalesce_first(
        (enrichment or {}).get("description_markdown"),
        canonical.get("description"),
        seed_data.get("description"),
        seed_data.get("short_description"),
    )

    image_url = coalesce_first(
        canonical.get("image_url"),
        (external_seed or {}).get("image_url"),
        seed_data.get("image_url"),
    )

    image_urls = read_image_urls(canonical.get("product_payload"))
    if image_url and image_url not in image_urls:
        image_urls.insert(0, image_url)
    seed_images = seed_data.get("image_urls") if isinstance(seed_data, dict) else None
    if isinstance(seed_images, list):
        for u in seed_images:
            if isinstance(u, str) and u.strip() and u.strip() not in image_urls:
                image_urls.append(u.strip())

    product_group_id = coalesce_first(
        canonical.get("product_group_id"),
        *[p.get("product_group_id") for p in products],
    )

    primary_merchant_id = None
    for p in products:
        if p.get("group_is_primary"):
            primary_merchant_id = p.get("merchant_id")
            break
    if not primary_merchant_id:
        primary_merchant_id = canonical.get("merchant_id")

    merchant_url_by_id: Dict[str, Optional[str]] = {}
    for p in products:
        mid = p.get("merchant_id")
        if mid and mid not in merchant_url_by_id:
            merchant_url_by_id[mid] = p.get("canonical_url")

    currency, price_min, price_max, offer_count, top_offers = aggregate_offers(
        offers, primary_merchant_id, merchant_url_by_id, seller_trust_by_id
    )

    variants_capped, variants_count = aggregate_variants(
        skus, canonical.get("source_product_id")
    )
    gtin13 = pick_gtin13(skus)

    sig = canonical.get("pivota_signature_id")
    pdp_url = f"{PDP_URL_PREFIX}{sig}" if sig else None
    breadcrumb = build_breadcrumb(canonical, pdp_url)
    taxonomy_tags = build_taxonomy_tags(canonical)
    category_path = coalesce_first(canonical.get("category"), canonical.get("product_type"))

    fashion = coalesce_fashion_fields(products, external_seed)

    return {
        "content_key": content_key,
        "pivota_signature_id": sig,
        "product_group_id": product_group_id,
        "brand": canonical.get("brand"),
        "title": title,
        "description": (description[:5000] if isinstance(description, str) else None),
        # Brand-attested rich content (E2 bridge) — key selling points + how-to-use.
        "bullet_points": (enrichment or {}).get("bullet_points"),
        "usage_scenarios": (enrichment or {}).get("usage_scenarios"),
        # Agent-decision-grade evidence (provenance-backed claims + required
        # disclaimers) — highest-precedence across the content_key cluster.
        # Floor-safety: a category-mandatory disclaimer (the FDA/DSHEA supplement
        # statement) rides along even when the merchant authored none, so the
        # direct PDP route serves it at parity with the search path.
        "evidence_profile": (evidence or {}).get("evidence_profile"),
        "required_disclaimers": ensure_category_disclaimers(
            (evidence or {}).get("required_disclaimers"),
            canonical.get("category_kind"),
        ),
        "image_url": image_url,
        "image_urls": image_urls or None,
        "currency": currency,
        "price_min": price_min,
        "price_max": price_max,
        "offer_count": offer_count,
        "offers": top_offers or None,
        "variants": variants_capped or None,
        "variants_count": variants_count,
        "gtin13": gtin13,
        "category_path": category_path,
        "taxonomy_tags": taxonomy_tags,
        "breadcrumb": breadcrumb,
        "pdp_lifecycle_stage": canonical.get("pdp_lifecycle_stage"),
        "sync_status": canonical.get("sync_status"),
        "primary_merchant_id": primary_merchant_id,
        "material": fashion["material"],
        "material_source": fashion["material_source"],
        "material_confidence": fashion["material_confidence"],
        "care": fashion["care"],
        "care_source": fashion["care_source"],
        "care_confidence": fashion["care_confidence"],
        "size_guide": fashion["size_guide"],
        "size_guide_source": fashion["size_guide_source"],
        "size_guide_confidence": fashion["size_guide_confidence"],
        # Review signal surfaced verbatim from the canonical (mig 186) — the
        # decision-intelligence lane consumes rating_value / rating_count. Null
        # when the canonical has no captured rating.
        "rating_value": canonical.get("rating_value"),
        "rating_count": canonical.get("rating_count"),
        "refresh_source": refresh_source,
    }


UPSERT_SQL = """
    INSERT INTO agent_pdp_view (
      content_key, pivota_signature_id, product_group_id,
      brand, title, description, bullet_points, usage_scenarios,
      evidence_profile, required_disclaimers,
      image_url, image_urls,
      currency, price_min, price_max, offer_count, offers,
      variants, variants_count, gtin13,
      category_path, taxonomy_tags, breadcrumb,
      pdp_lifecycle_stage, sync_status, primary_merchant_id,
      material, material_source, material_confidence,
      care, care_source, care_confidence,
      size_guide, size_guide_source, size_guide_confidence,
      rating_value, rating_count,
      refreshed_at, refresh_source, refreshed_by_proposal_id
    ) VALUES (
      :content_key, :pivota_signature_id, :product_group_id,
      :brand, :title, :description,
      CAST(:bullet_points AS jsonb), CAST(:usage_scenarios AS jsonb),
      CAST(:evidence_profile AS jsonb), CAST(:required_disclaimers AS jsonb),
      :image_url, CAST(:image_urls AS jsonb),
      :currency, :price_min, :price_max, :offer_count, CAST(:offers AS jsonb),
      CAST(:variants AS jsonb), :variants_count, :gtin13,
      :category_path, CAST(:taxonomy_tags AS jsonb), CAST(:breadcrumb AS jsonb),
      :pdp_lifecycle_stage, :sync_status, :primary_merchant_id,
      :material, :material_source, :material_confidence,
      :care, :care_source, :care_confidence,
      CAST(:size_guide AS jsonb), :size_guide_source, :size_guide_confidence,
      :rating_value, :rating_count,
      NOW(), :refresh_source, :refreshed_by_proposal_id
    )
    ON CONFLICT (content_key) DO UPDATE SET
      pivota_signature_id = EXCLUDED.pivota_signature_id,
      product_group_id = EXCLUDED.product_group_id,
      brand = EXCLUDED.brand,
      -- Overlay-derived columns. When the overlay READ failed (as opposed to
      -- succeeding and finding nothing) the assembled row carries no values for
      -- these, so assigning from EXCLUDED would erase what is already published.
      -- Keep the current value in that case; a genuine removal still propagates,
      -- because a successful fetch that found nothing leaves the flag false.
      -- The CASTs are required: an untyped bind compared inside CASE is one of
      -- the IndeterminateDatatype shapes the prepare gate rejects.
      title = CASE WHEN CAST(:preserve_enrichment AS boolean)
                   THEN agent_pdp_view.title ELSE EXCLUDED.title END,
      description = CASE WHEN CAST(:preserve_enrichment AS boolean)
                   THEN agent_pdp_view.description ELSE EXCLUDED.description END,
      bullet_points = CASE WHEN CAST(:preserve_enrichment AS boolean)
                   THEN agent_pdp_view.bullet_points ELSE EXCLUDED.bullet_points END,
      usage_scenarios = CASE WHEN CAST(:preserve_enrichment AS boolean)
                   THEN agent_pdp_view.usage_scenarios ELSE EXCLUDED.usage_scenarios END,
      evidence_profile = CASE WHEN CAST(:preserve_evidence AS boolean)
                   THEN agent_pdp_view.evidence_profile ELSE EXCLUDED.evidence_profile END,
      required_disclaimers = CASE WHEN CAST(:preserve_evidence AS boolean)
                   THEN agent_pdp_view.required_disclaimers
                   ELSE EXCLUDED.required_disclaimers END,
      image_url = EXCLUDED.image_url,
      image_urls = EXCLUDED.image_urls,
      currency = EXCLUDED.currency,
      price_min = EXCLUDED.price_min,
      price_max = EXCLUDED.price_max,
      offer_count = EXCLUDED.offer_count,
      offers = EXCLUDED.offers,
      variants = EXCLUDED.variants,
      variants_count = EXCLUDED.variants_count,
      gtin13 = EXCLUDED.gtin13,
      category_path = EXCLUDED.category_path,
      taxonomy_tags = EXCLUDED.taxonomy_tags,
      breadcrumb = EXCLUDED.breadcrumb,
      pdp_lifecycle_stage = EXCLUDED.pdp_lifecycle_stage,
      sync_status = EXCLUDED.sync_status,
      primary_merchant_id = EXCLUDED.primary_merchant_id,
      material = EXCLUDED.material,
      material_source = EXCLUDED.material_source,
      material_confidence = EXCLUDED.material_confidence,
      care = EXCLUDED.care,
      care_source = EXCLUDED.care_source,
      care_confidence = EXCLUDED.care_confidence,
      size_guide = EXCLUDED.size_guide,
      size_guide_source = EXCLUDED.size_guide_source,
      size_guide_confidence = EXCLUDED.size_guide_confidence,
      rating_value = EXCLUDED.rating_value,
      rating_count = EXCLUDED.rating_count,
      refreshed_at = NOW(),
      refresh_source = EXCLUDED.refresh_source,
      refreshed_by_proposal_id = EXCLUDED.refreshed_by_proposal_id
"""


def to_jsonb(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, default=str)


def row_to_upsert_params(row: Dict[str, Any]) -> Dict[str, Any]:
    """Project assemble_row() output to the bind-parameter dict shape
    UPSERT_SQL expects (JSONB columns → JSON-encoded strings)."""
    params = dict(row)
    params["image_urls"] = to_jsonb(row.get("image_urls"))
    params["offers"] = to_jsonb(row.get("offers"))
    params["variants"] = to_jsonb(row.get("variants"))
    params["taxonomy_tags"] = to_jsonb(row.get("taxonomy_tags"))
    params["breadcrumb"] = to_jsonb(row.get("breadcrumb"))
    params["evidence_profile"] = to_jsonb(row.get("evidence_profile"))
    params["required_disclaimers"] = to_jsonb(row.get("required_disclaimers"))
    params["bullet_points"] = to_jsonb(row.get("bullet_points"))
    params["usage_scenarios"] = to_jsonb(row.get("usage_scenarios"))
    # size_guide on catalog_products is already a JSONB value (stored as
    # dict by the LLM extractor / authoring path). coalesce_fashion_fields
    # passes it through verbatim. Re-encode for the SQLAlchemy bind. Other
    # fashion fields are plain text + float and pass through unchanged.
    params["size_guide"] = to_jsonb(row.get("size_guide"))
    params["refreshed_by_proposal_id"] = row.get("refreshed_by_proposal_id")
    # Default FALSE, so every existing caller that builds a row without going
    # through build_agent_pdp_view_row keeps the previous overwrite semantics
    # rather than silently preserving stale overlays.
    params["preserve_enrichment"] = bool(row.get("preserve_enrichment"))
    params["preserve_evidence"] = bool(row.get("preserve_evidence"))
    return params


class _FetchFailed:
    """Sentinel: the overlay fetch ITSELF failed, as opposed to succeeding and
    finding nothing.

    Without this distinction both outcomes arrive as None, `assemble_row` builds
    a row with the overlay columns empty, and UPSERT_SQL — which assigns every
    column from EXCLUDED — writes those empties over a row that was serving real
    curated copy. A transient `get_enrichment` hiccup during one reconciler pass
    therefore strips the overlay permanently: the write stamps refreshed_at=NOW(),
    and the reconciler only re-selects keys whose truth timestamps moved PAST
    refreshed_at, so the repaired-by-the-next-sweep assumption does not hold. The
    row silently stops serving its enrichment until unrelated catalog truth
    changes.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<overlay-fetch-failed>"


FETCH_FAILED = _FetchFailed()


async def _fetch_enrichment_for_canonical(
    products: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Fetch the product_enrichment overlay for the served PDP. Prefers a
    BRAND-ATTESTED overlay anywhere in the content_key cluster (the verified
    brand's own copy is authoritative for the entity, even if attest keyed it to
    a non-canonical member), else the canonical product's overlay. Keyed by the
    catalog identity triple (merchant_id, platform, source_product_id), geo
    'default'. Best-effort: any failure degrades to no overlay rather than
    breaking the serve-cache rebuild — agent_pdp_view is a cache, never truth."""
    if not products:
        return None
    from db.product_enrichment import get_enrichment

    canonical = pick_canonical(products)
    canonical_key = (
        canonical.get("merchant_id"),
        canonical.get("platform"),
        canonical.get("source_product_id"),
    )
    # Finding B: walk the content_key cluster — a brand-attested overlay wins
    # (only the verified brand can create one, so it's safe + authoritative for
    # the entity, even if attest keyed it to a non-canonical seed beside a synced
    # product that wins pick_canonical), else fall back to the canonical's overlay.
    canonical_overlay: Optional[Dict[str, Any]] = None
    any_fetch_failed = False
    for product in products:
        ident = (
            product.get("merchant_id"),
            product.get("platform"),
            product.get("source_product_id"),
        )
        if not all(ident):
            continue
        try:
            overlay = await get_enrichment(str(ident[0]), str(ident[1]), str(ident[2]))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "agent_pdp_view enrichment overlay fetch failed (best-effort): %s",
                str(exc)[:200],
            )
            any_fetch_failed = True
            continue
        if not overlay:
            continue
        if overlay.get("updated_by_employee_id") == "brand_attestation":
            return overlay  # authoritative brand copy — wins immediately
        if ident == canonical_key:
            canonical_overlay = overlay
    if canonical_overlay is None and any_fetch_failed:
        # We have no overlay AND at least one read errored, so "there is no
        # overlay" is not a conclusion we are entitled to draw. Say so, and let
        # the caller preserve whatever is already published.
        return FETCH_FAILED
    return canonical_overlay


async def refresh_agent_pdp_view_for_content_key(
    content_key: str,
    *,
    refresh_source: str,
    db: Any = None,
) -> bool:
    """Rebuild + upsert the denormalized agent_pdp_view row for a content_key.

    Canonical fetch→assemble→upsert orchestration (previously inlined in the
    backfill script, the seed writer, and fashion authoring). Pure of any
    serving-eligibility recompute — the caller owns that, so a caller that
    already recomputes right after (e.g. catalog_sync) does not double-fire.

    Returns True when a row was upserted, False when there was nothing to
    build (no catalog rows / too thin a row to be useful — no title). Raises
    on DB errors so callers can decide isolation; the catalog_sync caller
    wraps this best-effort so a stale PDP cache never breaks ingest.
    """
    read_db = db or database
    row = await build_agent_pdp_view_row(
        content_key, refresh_source=refresh_source, db=read_db
    )
    if row is None:
        return False
    await read_db.execute(UPSERT_SQL, row_to_upsert_params(row))
    return True


async def build_agent_pdp_view_row(
    content_key: str,
    *,
    refresh_source: str,
    db: Any = None,
) -> Optional[Dict[str, Any]]:
    """Fetch every source row for a content_key and assemble the view row.

    The read half of refresh_agent_pdp_view_for_content_key, split out so a
    caller can PREVIEW the next row state without writing it. Side-effect-free.
    Returns None when there is nothing to build (no catalog rows, or too thin a
    row to be useful — no title).

    Assembling through this function rather than calling assemble_row directly
    is what keeps the evidence / enrichment / seller-trust overlays attached. A
    caller that skips it gets a row with those fields None, and UPSERT_SQL sets
    every column from EXCLUDED — so writing such a row DELETES the enrichment,
    evidence_profile and required_disclaimers already serving on it. That is not
    hypothetical: scripts/backfill_agent_pdp_view.py did exactly this.
    """
    read_db = db or database
    products = await fetch_products_for_key(content_key, db=read_db)
    if not products:
        return None
    product_keys = [p["product_key"] for p in products if p.get("product_key")]
    skus = await fetch_skus_for_keys(product_keys, db=read_db)
    offers = await fetch_offers_for_keys(product_keys, db=read_db)
    external_seed = await fetch_external_seed_for_keys(product_keys, db=read_db)
    # Identity-confidence gate: a no-GTIN content_key is deliberately non-unique
    # (brand+title), so a cluster can collide DISTINCT products. Scope the evidence
    # fetch to members safe to attribute to the served row, so we never bake
    # Brand A's substantiated claim onto Brand B (see evidence_safe_product_keys).
    evidence_keys = evidence_safe_product_keys(products, skus)
    try:
        evidence = await fetch_evidence_for_keys(evidence_keys, db=read_db)
    except Exception as exc:  # noqa: BLE001 — see _FetchFailed
        logger.warning(
            "agent_pdp_view evidence fetch failed; preserving published evidence: %s",
            str(exc)[:200],
        )
        evidence = FETCH_FAILED
    # E2 publish bridge: overlay the Pivota enrichment (generated/curated
    # description_markdown) so it reaches the served PDP AND the
    # serving-eligibility gate. Best-effort; None keeps the raw description.
    enrichment = await _fetch_enrichment_for_canonical(products)
    # W8: attach the outcome-derived seller-trust signal to each offer's merchant.
    # Best-effort — a trust-store hiccup must never block the served PDP, and a
    # merchant with no transacted outcomes simply carries no trust key.
    seller_trust_by_id: Dict[str, Dict[str, Any]] = {}
    try:
        from services.outcome_aggregation_service import seller_trust_bulk

        seller_trust_by_id = await seller_trust_bulk(
            [o.get("merchant_id") for o in offers if o.get("merchant_id")]
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "agent_pdp_view seller_trust fetch failed (best-effort): %s",
            str(exc)[:200],
        )

    # Resolve the tri-state. A failed fetch contributes NO values and instead
    # tells the write to keep whatever is already published for those columns;
    # a successful fetch that found nothing writes empties as before, so a
    # genuine removal (an employee deleting enrichment) still propagates.
    preserve_enrichment = enrichment is FETCH_FAILED
    preserve_evidence = evidence is FETCH_FAILED
    row = assemble_row(
        content_key=content_key,
        products=products,
        skus=skus,
        offers=offers,
        external_seed=external_seed,
        evidence=None if preserve_evidence else evidence,
        refresh_source=refresh_source,
        enrichment=None if preserve_enrichment else enrichment,
        seller_trust_by_id=seller_trust_by_id,
    )
    if row is not None:
        row["preserve_enrichment"] = preserve_enrichment
        row["preserve_evidence"] = preserve_evidence
    return row


# ---------------------------------------------------------------------
# Orphan reaper — agent_pdp_view rows whose content_key vanished
# ---------------------------------------------------------------------
#
# agent_pdp_view is keyed by content_key and kept fresh with UPSERT-on-content_key.
# But content_key = make_content_key(brand, title, gtin) is derived from MUTABLE
# fields: when a merchant re-syncs a product whose title or barcode changed, the
# catalog_products row is re-keyed to the NEW content_key in place — and nothing
# ever deletes the OLD content_key's agent_pdp_view row. It becomes an orphan:
# unreachable by content_key, yet it keeps its pivota_signature_id. Because sig is
# merchant-scoped and now belongs to the row under the NEW content_key, the unique
# sig index blocks the live row from materializing, AND a /products/sig_* lookup
# can resolve to the DEAD orphan instead of the live product. Reaping orphans fixes
# both: it clears the latent mis-serve and frees the sig for the live row.
#
# "Orphaned" is defined conservatively as: an agent_pdp_view row whose content_key
# has NO catalog_products row referencing it. That NOT-EXISTS guard means a
# content_key still shared by another merchant/path (the legitimate multi-seller
# case) is never reaped.

_DELETE_ORPHAN_IF_UNREFERENCED_SQL = """
    DELETE FROM agent_pdp_view av
    WHERE av.content_key = :content_key
      AND NOT EXISTS (
        SELECT 1 FROM catalog_products cp WHERE cp.content_key = :content_key
      )
"""


async def delete_agent_pdp_view_if_orphaned(content_key: str, *, db: Any = None) -> bool:
    """Delete the agent_pdp_view row for `content_key` IFF it is orphaned — no
    catalog_products row references it any more. No-op when the content_key is still
    live (shared by another merchant/path) or has no view row. Safe to call on every
    re-key; the DELETE re-checks NOT EXISTS so it can never remove a live row.

    Returns True when a row was deleted, False otherwise.
    """
    if not content_key:
        return False
    read_db = db or database
    was_orphan = await read_db.fetch_val(
        """
        SELECT EXISTS (SELECT 1 FROM agent_pdp_view WHERE content_key = :ck)
           AND NOT EXISTS (SELECT 1 FROM catalog_products WHERE content_key = :ck)
        """,
        {"ck": content_key},
    )
    if not was_orphan:
        return False
    await read_db.execute(_DELETE_ORPHAN_IF_UNREFERENCED_SQL, {"content_key": content_key})
    logger.info(
        {"event": "agent_pdp_view_orphan_reaped", "content_key": content_key}
    )
    return True


async def reap_orphaned_agent_pdp_view_rows(
    *,
    db: Any = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Sweep + delete ALL orphaned agent_pdp_view rows (content_key absent from
    catalog_products). This is the catch-all for every re-key site — not just
    catalog_sync — and the belt-and-suspenders backstop for the inline reap.

    Deletes regardless of whether the orphan still carries an evidence_profile: an
    orphan is unreachable by content_key and its evidence cannot be regenerated for
    a content_key nothing resolves to (the live product under the new content_key
    keeps its own evidence). `with_evidence` is surfaced for visibility since a
    non-zero value is unusual and worth noticing.

    Returns {"orphans", "with_evidence", "deleted", "sample"}. When dry_run, reports
    without deleting.
    """
    read_db = db or database
    limit_clause = " LIMIT :limit" if limit and int(limit) > 0 else ""
    params = {"limit": int(limit)} if limit and int(limit) > 0 else {}
    rows = await read_db.fetch_all(
        """
        SELECT av.content_key, av.pivota_signature_id, av.title,
               av.refresh_source, (av.evidence_profile IS NOT NULL) AS has_evidence
        FROM agent_pdp_view av
        WHERE NOT EXISTS (
            SELECT 1 FROM catalog_products cp WHERE cp.content_key = av.content_key
        )
        ORDER BY av.refreshed_at ASC
        """ + limit_clause,
        params,
    )
    orphans = [dict(r) for r in rows]
    with_evidence = sum(1 for r in orphans if r.get("has_evidence"))
    deleted = 0
    if not dry_run:
        # Per-row guarded delete (re-checks NOT EXISTS). Orphan counts are small and
        # this reuses the single-row invariant, so no fragile array binding.
        for r in orphans:
            await read_db.execute(
                _DELETE_ORPHAN_IF_UNREFERENCED_SQL, {"content_key": r["content_key"]}
            )
            deleted += 1
    if orphans:
        logger.info({
            "event": "agent_pdp_view_orphan_sweep",
            "orphans": len(orphans),
            "with_evidence": with_evidence,
            "deleted": deleted,
            "dry_run": dry_run,
        })
    return {
        "orphans": len(orphans),
        "with_evidence": with_evidence,
        "deleted": deleted,
        "sample": orphans[:10],
    }


def _propagate_enrichment_on_write_enabled() -> bool:
    """Canary flag for write-triggered enrichment propagation (B①).

    Default OFF. When off, an enrichment write does NOT immediately rebuild the
    served agent_pdp_view — the overlay still reaches the view on the next
    unrelated re-assembly (catalog sync, graduation, etc.), i.e. today's
    behavior. When on, an enrichment write propagates straight to the served PDP.
    """
    return (os.getenv("SERVE_PDP_ENRICHMENT_ON_WRITE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def refresh_agent_pdp_view_for_enrichment_write(
    merchant_id: Optional[str],
    platform: Optional[str],
    platform_product_id: Optional[str],
    *,
    db: Any = None,
) -> bool:
    """Best-effort: rebuild the served agent_pdp_view after an enrichment WRITE.

    The E2 publish bridge (assemble_row) already overlays product_enrichment into
    agent_pdp_view at assembly time — but the only writers that re-assembled on
    write were the merchant add/edit routes. The automated enrichment pipeline +
    the employee path wrote enrichment WITHOUT re-assembling, so generated/curated
    copy didn't reach the served PDP until some unrelated re-assembly. This closes
    that gap: resolve the source product's content_key and refresh.

    Flag-gated (SERVE_PDP_ENRICHMENT_ON_WRITE, default OFF) so it's a canary, and
    fully best-effort — it never raises into the enrichment writer.

    Returns True iff a view row was rebuilt.
    """
    if not _propagate_enrichment_on_write_enabled():
        return False
    if not (merchant_id and platform and platform_product_id):
        return False
    read_db = db or database
    try:
        # The enrichment domain calls this identifier `platform_product_id`
        # (it is the param name on upsert_enrichment / get_enrichment); on
        # catalog_products the SAME value is stored as `source_product_id`.
        # The column below MUST use the catalog spelling — see
        # _fetch_enrichment_for_canonical, which builds the identical triple.
        # Shipped as `platform_product_id`, which is not a column on this
        # table, so every call raised UndefinedColumn into the best-effort
        # except below and this bridge silently never propagated anything.
        row = await read_db.fetch_one(
            "SELECT content_key FROM catalog_products "
            "WHERE merchant_id = :merchant_id "
            "  AND platform = :platform "
            "  AND source_product_id = :platform_product_id "
            "  AND content_key IS NOT NULL "
            "LIMIT 1",
            {
                "merchant_id": str(merchant_id),
                "platform": str(platform),
                "platform_product_id": str(platform_product_id),
            },
        )
        if not row:
            return False
        content_key = str(dict(row).get("content_key") or "")
        if not content_key:
            return False
        return await refresh_agent_pdp_view_for_content_key(
            content_key,
            refresh_source="enrichment_write",
            db=read_db,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort; never break the writer
        logger.warning(
            "enrichment-write PDP refresh failed (best-effort) for %s/%s/%s: %s",
            merchant_id,
            platform,
            platform_product_id,
            str(exc)[:200],
        )
        return False
