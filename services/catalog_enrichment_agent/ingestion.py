"""Stage 3 ingestion — read validated.jsonl produced by the Stage 2 codex
routine and INSERT into the canonical four-table chain plus the seed
audit row:
  catalog_merchants  — one synthetic merchant row per validated retailer
  catalog_products   — the PDP (multi_merchant_canonical, see Phase 6)
  catalog_skus       — one synthetic 'canonical' SKU per PDP (no variants)
  catalog_offers     — one row per validated retailer offering this PDP
  external_product_seeds — kept for audit + legacy compatibility; the
                           offer's source-of-truth is now catalog_offers

Phase 7a (this revision): Phase 4 originally only wrote catalog_products
and external_product_seeds. The deployed canonical recall path
(pivot_query_service._fetch_canonical_search_rows) JOINs catalog_skus
and catalog_offers, so without those rows agent-authored PDPs were
invisible to canonical recall and only reachable via the external-seed
fallback (which itself filters out attached seeds via a Node-side gate).
This revision writes the full chain at ingestion time so agent PDPs are
shaped identically to merchant-sync PDPs.

Idempotency contract:
- A PDP is identified by (brand_normalized, canonical_product_name).
- An SKU is identified by (product_key + '::canonical') — one per PDP.
- An offer is identified by a deterministic id derived from
  (product_key, sku_key, destination_url) so re-runs UPSERT cleanly.
- A merchant is identified by 'agent_seed::' + slug(merchant_inferred).
- An external_product_seeds row is identified by (product_key,
  destination_url). Re-runs UPSERT.

Provenance written into catalog_products.product_payload.enrichment_meta:
  {agent_version, source_jsonl, ingested_at, candidate_attribute_summary,
   stage2_validated_at}.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from services.beauty_external_ranking import (
    normalize_external_seed_structured_ingredient_ids,
)
from services.category_kind import resolve_category_kind
from services.catalog_identity import make_content_key
from services.catalog_sync_service import make_pivota_canonical_fields
from services.seller_identity import (
    BANNED_BUCKET_MERCHANT_ID,
    resolve_seed_seller_identity,
)
from services.pdp_lifecycle import compute_lifecycle_stage
from services.pdp_taxonomy import derive_taxonomy_v1
from services.strong_identifier import (
    MPN_CAPTURED_AS_BARCODE,
    NO_STRONG_IDENTIFIER,
    StrongIdentifier,
    extract_strong_identifier,
)
from services.text_normalization.brand_case import proper_case_brand

logger = logging.getLogger("catalog_enrichment_agent.ingestion")

AGENT_VERSION = "catalog_enrichment_agent_v1"
SYNTHETIC_MERCHANT_ID = "external_seed"
SYNTHETIC_PLATFORM = "external_seed"
DEFAULT_CATEGORY_CONFIDENCE = 0.7
DEFAULT_CATEGORY_LABEL_SOURCE = "enrichment_agent_v1"
DEFAULT_TRUTH_TIER = "primary"
DEFAULT_READINESS_TIER = "referral_only"
DEFAULT_CATALOG_TRACK = "external_referral"
DEFAULT_PDP_SCOPE = "multi_merchant_canonical"
DEFAULT_PDP_SCOPE_SOURCE = "enrichment_agent_v1"

# Phase 7a — canonical chain
SKU_SUFFIX = "::canonical"
OFFER_CATALOG_TRACK = "external_referral"
OFFER_TRUTH_TIER = "primary"
OFFER_READINESS_TIER = "referral_only"
OFFER_MODE = "external_referral"
MERCHANT_ID_PREFIX = "agent_seed::"
MERCHANT_PLATFORM = "external_seed"

_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def _normalize_token(value: Optional[str]) -> str:
    if value is None:
        return ""
    return _NORMALIZE_RE.sub(" ", str(value).lower()).strip()


def canonical_product_name(brand: Optional[str], product_name: Optional[str]) -> str:
    """Stable (brand_normalized, product_name_normalized) key. Two PDPs
    with identical brand + product name (modulo punctuation/case) collapse
    to the same key — by design, so the agent can re-run without
    duplicating rows."""
    norm = _normalize_token(f"{brand or ''} {product_name or ''}").replace(" ", "-")
    return norm or "unknown"


def derive_product_key(brand: Optional[str], product_name: Optional[str]) -> str:
    """Stable product_key derived from (brand, product_name). Uses a
    deterministic hash to bound the length to the catalog_products
    VARCHAR(255) limit while preserving readability of the prefix.
    Format: 'ext:<canonical>::<8-char-hash>'."""
    canonical = canonical_product_name(brand, product_name)
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:8]
    # Truncate canonical prefix to keep total length sensible.
    prefix = canonical[:200]
    return f"ext:{prefix}::{digest}"


def _normalize_url(url: Optional[str]) -> str:
    if not url:
        return ""
    return str(url).strip()


def _validated_offers(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_offers = record.get("offers") or []
    offers: List[Dict[str, Any]] = []
    for offer in raw_offers:
        if not isinstance(offer, dict):
            continue
        canonical_url = _normalize_url(offer.get("canonical_url"))
        destination_url = _normalize_url(offer.get("destination_url"))
        if not (canonical_url or destination_url):
            continue
        offers.append({
            "merchant_inferred": str(offer.get("merchant_inferred") or "").strip(),
            "canonical_url": canonical_url,
            "destination_url": destination_url or canonical_url,
            "image_url": _normalize_url(offer.get("image_url")),
            "price": offer.get("price"),
            "in_stock": bool(offer.get("in_stock") or False),
            "validated_at": str(offer.get("validated_at") or "").strip() or None,
        })
    return offers


def _build_pdp_payload(record: Dict[str, Any]) -> Dict[str, Any]:
    pdp = record.get("pdp") or {}
    if not isinstance(pdp, dict):
        return {}
    # Phase O-1 followup: optional tags field in JSONL. Accept either a
    # list of strings or a comma-separated string (some hand-curated
    # JSONLs may use either form). Default to []; keep the
    # "empty list = ingest saw and was empty" semantic from Path A/B.
    raw_tags = pdp.get("tags")
    tags: List[str] = []
    if isinstance(raw_tags, list):
        tags = [str(t).strip() for t in raw_tags if str(t or "").strip()]
    elif isinstance(raw_tags, str):
        tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
    # Codex review 2026-05-12 P0: gtin was dropped here even though
    # _build_pdp_insert reads it for content_key computation. A JSONL
    # record with a real GTIN would produce a no-GTIN content_key,
    # diverging from Path A's GTIN-included key for the same product.
    # Pass through unchanged; downstream callers handle empty values.
    gtin_raw = pdp.get("gtin") or pdp.get("barcode") or ""
    upc_raw = pdp.get("upc") or ""
    ean_raw = pdp.get("ean") or ""
    barcode_raw = pdp.get("barcode") or ""
    mpn_raw = pdp.get("mpn") or ""
    source_domain = str(pdp.get("source_domain") or "").strip() or None
    # Display-cased — upstream candidates routinely arrive lowercase
    # ("fenty beauty"). Identity/dedup keys use lowercase via
    # canonical_product_name() below, so this only fixes display.
    payload = {
        "brand": proper_case_brand(pdp.get("brand")),
        "product_name": str(pdp.get("product_name") or "").strip(),
        "category_path": str(pdp.get("category_path") or "").strip(),
        "attribute_summary": str(pdp.get("attribute_summary") or "").strip(),
        "gtin": str(gtin_raw).strip() if gtin_raw else None,
        "upc": str(upc_raw).strip() if upc_raw else None,
        "ean": str(ean_raw).strip() if ean_raw else None,
        "barcode": str(barcode_raw).strip() if barcode_raw else None,
        "mpn": str(mpn_raw).strip() if mpn_raw else None,
        "strong_identity": {
            "gtin": str(pdp.get("gtin") or "").strip() or None,
            "upc": str(upc_raw).strip() if upc_raw else None,
            "ean": str(ean_raw).strip() if ean_raw else None,
            "barcode": str(barcode_raw).strip() if barcode_raw else None,
            "mpn": str(mpn_raw).strip() if mpn_raw else None,
        },
        "source_domain": source_domain,
        "tags": tags,
        "agent_version": AGENT_VERSION,
        # Optional review signal → catalog_products.rating_value/rating_count.
        # Absent/unparseable stays None (never fabricated); ingest is never
        # blocked on a missing rating.
        "rating_value": _coerce_rating_value(pdp.get("rating_value")),
        "rating_count": _coerce_rating_count(pdp.get("rating_count")),
        # Optional published INCI → beauty_sku_ingredients at apply time, with
        # its provenance tier (brand_official > reseller_listing). Kept out of the
        # catalog_products columns on purpose — INCI lives in the beauty tables.
        "raw_inci": (str(pdp.get("raw_inci")).strip() or None) if pdp.get("raw_inci") else None,
        "inci_source": str(pdp.get("inci_source") or "").strip() or None,
    }
    if not payload["brand"] or not payload["product_name"]:
        return {}
    return payload


def _coerce_rating_value(value: Any) -> Optional[float]:
    """Coerce to a 0–5 star rating; NULL outside that range (defense-in-depth for
    a hand-authored Path-C JSONL that bypasses the JSON-LD parser's range guard —
    a mis-scaled/negative rating is dropped, never stored)."""
    if value is None or value == "":
        return None
    try:
        rating = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return rating if 0 <= rating <= 5 else None


def _coerce_rating_count(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        count = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    return count if count >= 0 else None


def _build_pdp_insert(
    *,
    pdp_payload: Dict[str, Any],
    offers: List[Dict[str, Any]],
    source_jsonl: Optional[str],
    seller: Dict[str, str],
) -> Dict[str, Any]:
    """Construct the catalog_products row dict that the runner will
    INSERT. The product_key is deterministic so re-runs UPSERT cleanly."""
    product_key = derive_product_key(pdp_payload["brand"], pdp_payload["product_name"])
    source_product_id = canonical_product_name(
        pdp_payload["brand"],
        pdp_payload["product_name"],
    )
    pivota_fields = make_pivota_canonical_fields(
        SYNTHETIC_MERCHANT_ID,
        SYNTHETIC_PLATFORM,
        source_product_id,
    )
    canonical_url = ""
    image_url = ""
    if offers:
        # Prefer the first validated offer's canonical_url + image_url
        # for catalog_products' canonical_url field. Future passes can
        # promote a different offer.
        canonical_url = offers[0].get("canonical_url") or offers[0].get("destination_url") or ""
        image_url = offers[0].get("image_url") or ""
    enrichment_meta = {
        "agent_version": AGENT_VERSION,
        "source_jsonl": source_jsonl or None,
        "candidate_attribute_summary": pdp_payload.get("attribute_summary") or None,
        "offer_count": len(offers),
    }
    return {
        "product_key": product_key,
        # W2: seller of record (retailer domains key on etld1 alone;
        # brand-direct on (brand, etld1)). product_key and the pivota_* sig
        # fields keep their historical derivation inputs above — sigs are
        # write-once (T5) and keys are opaque plumbing (D4.2); only the
        # ownership column changes.
        "merchant_id": seller["merchant_id"],
        "platform": SYNTHETIC_PLATFORM,
        "source_product_id": source_product_id,
        "pivota_signature_id": pivota_fields["pivota_signature_id"],
        "pivota_canonical_url": pivota_fields["pivota_canonical_url"],
        "pivota_signature_minted_at": pivota_fields["pivota_signature_minted_at"],
        "catalog_track": DEFAULT_CATALOG_TRACK,
        "truth_tier": DEFAULT_TRUTH_TIER,
        "readiness_tier": DEFAULT_READINESS_TIER,
        "source_system": AGENT_VERSION,
        "source_domain": pdp_payload.get("source_domain") or None,
        "title": pdp_payload["product_name"],
        "description": pdp_payload.get("attribute_summary") or None,
        "brand": pdp_payload["brand"],
        "product_type": pdp_payload.get("category_path", "").split("/")[-1] or None,
        "category": pdp_payload.get("category_path", "").split("/")[-1] or None,
        "category_path": pdp_payload.get("category_path") or None,
        # Durable category_kind (skincare/haircare/supplement) — drives
        # claim-safety, required disclaimers, and the serving gate. Resolved from
        # the same authoritative category_path the row is stamped with; None
        # ("unknown") when the path isn't a contract kind (makeup, tools, etc.),
        # never text-guessed for skincare/haircare. Previously omitted, so every
        # Path-C mint landed category_kind NULL.
        "category_kind": resolve_category_kind(
            category_path=pdp_payload.get("category_path"),
            product_type=pdp_payload.get("category_path", "").split("/")[-1] or None,
            title=pdp_payload["product_name"],
            tags=pdp_payload.get("tags"),
        ),
        "category_confidence": DEFAULT_CATEGORY_CONFIDENCE,
        "category_label_source": DEFAULT_CATEGORY_LABEL_SOURCE,
        "canonical_url": canonical_url or None,
        "image_url": image_url or None,
        "product_payload": json.dumps({"enrichment_meta": enrichment_meta}),
        # Phase O-1 followup: persist optional curator-supplied tags from
        # JSONL (always a list, possibly empty). Same semantic as Path A
        # and Path B: `[]` means "ingest saw the candidate and the JSONL
        # didn't list any tags", NULL means "row predates the column".
        # Stringified here because run_catalog_enrichment.py wraps the
        # bind value with CAST(:tags AS jsonb).
        "tags": json.dumps(list(pdp_payload.get("tags") or []), ensure_ascii=False),
        # Phase O-2: derived taxonomy v1. Catalog enrichment agent
        # candidates rarely have explicit price (validator finds offer
        # prices, not candidate prices), so price_tier may be NULL —
        # that's expected. Title + attribute_summary + tags drive the
        # extractors. Stringified for CAST(:... AS jsonb) in the
        # runner. mig 076.
        **_taxonomy_v1_payload(pdp_payload, offers),
        # Phase O-4: lifecycle stage. Path C ingests under
        # source_system='catalog_enrichment_agent_v1' which is one of
        # the published-readiness triggers, so a curated row with
        # category_path + at-least-one taxonomy signal lands as
        # 'published' directly. mig 077.
        "pdp_lifecycle_stage": _lifecycle_stage_for_agent_pdp(pdp_payload, offers, canonical_url, image_url),
        "pdp_scope": DEFAULT_PDP_SCOPE,
        "pdp_scope_source": DEFAULT_PDP_SCOPE_SOURCE,
        # Stage 1 (mig 083): content-derived identity. Path C is the
        # path most likely to share a content_key with Path A/B rows
        # (same physical product enriched by the agent + scraped by
        # the mirror + sold via a connected merchant). Agent payloads
        # typically don't carry GTIN at the candidate stage — gtin
        # arrives later via offers. Stage 2 auto-grouper will pick up
        # cross-path matches via brand+title alone.
        "content_key": make_content_key(
            pdp_payload.get("brand"),
            pdp_payload.get("product_name"),
            pdp_payload.get("gtin") or None,
        ),
        # Review signal columns (mig 186). Additive + nullable — a product with no
        # reviews lands NULL, and these never touch offer pricing. Consumed by the
        # decision-intelligence lane as rating_value / rating_count.
        "rating_value": pdp_payload.get("rating_value"),
        "rating_count": pdp_payload.get("rating_count"),
    }


def _lifecycle_stage_for_agent_pdp(
    pdp_payload: Dict[str, Any],
    offers: List[Dict[str, Any]],
    canonical_url: str,
    image_url: str,
) -> str:
    """Compute the lifecycle stage for a Path C agent-ingested row.
    Reuses the same compute_lifecycle_stage gates as Path A/B —
    inputs reshape to the row dict the gate expects."""
    # The taxonomy v1 derivation runs again here to get list values
    # (the main return path serializes to JSON strings for the runner;
    # the gate needs Python lists).
    price = None
    if offers:
        raw_price = offers[0].get("price")
        try:
            price = float(raw_price) if raw_price is not None else None
        except (TypeError, ValueError):
            price = None
    taxonomy = derive_taxonomy_v1(
        price=price,
        title=pdp_payload.get("product_name"),
        description=pdp_payload.get("attribute_summary"),
        tags=list(pdp_payload.get("tags") or []),
    )
    return compute_lifecycle_stage(
        {
            "title": pdp_payload.get("product_name"),
            "description": pdp_payload.get("attribute_summary"),
            "image_url": image_url or None,
            "category_path": pdp_payload.get("category_path") or None,
            "tags": list(pdp_payload.get("tags") or []),
            "demographic": taxonomy.get("demographic"),
            "use_case_tags": taxonomy.get("use_case_tags"),
            "lifestyle_tags": taxonomy.get("lifestyle_tags"),
            "pdp_scope": DEFAULT_PDP_SCOPE,
            "source_system": AGENT_VERSION,
        }
    )


def _taxonomy_v1_payload(pdp_payload: Dict[str, Any], offers: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute the 4 taxonomy v1 columns from a catalog enrichment agent
    record. Price defaults to the first offer's price when present.
    Returns the dict shape Postgres expects for jsonb columns wrapped
    via CAST(:... AS jsonb): scalars stay scalars, lists become JSON
    string literals."""
    price = None
    if offers:
        raw_price = offers[0].get("price")
        try:
            price = float(raw_price) if raw_price is not None else None
        except (TypeError, ValueError):
            price = None
    taxonomy = derive_taxonomy_v1(
        price=price,
        title=pdp_payload.get("product_name"),
        description=pdp_payload.get("attribute_summary"),
        tags=list(pdp_payload.get("tags") or []),
    )
    return {
        "price_tier": taxonomy["price_tier"],
        "use_case_tags": json.dumps(taxonomy["use_case_tags"], ensure_ascii=False),
        "lifestyle_tags": json.dumps(taxonomy["lifestyle_tags"], ensure_ascii=False),
        "demographic": taxonomy["demographic"],
    }


def _build_seed_inserts(
    *,
    product_key: str,
    pdp_payload: Dict[str, Any],
    offers: List[Dict[str, Any]],
    market: str = "US",
) -> List[Dict[str, Any]]:
    """Produce one external_product_seeds row per validated offer.

    Phase 7c: each row carries a single synthetic variant inside
    seed_data and a column-level `availability` value. Without this,
    services.external_referral_readiness.evaluate_external_referral_seed
    audits the row, finds zero variants, returns status='blocked', and
    routes/agent_api.py:_build_external_seed_product silently drops the
    seed (no metric reason recorded). This is what made probe v9 return
    0/9 lipstick queries even though Option A's Node-side patch landed
    — the wrong code path entirely. The correct hot path is
    pivota-backend's external_seed loader, and it gates on the audit.
    """
    rows: List[Dict[str, Any]] = []
    seen_urls: set = set()
    for offer in offers:
        canonical_url = offer.get("canonical_url") or ""
        destination_url = offer.get("destination_url") or canonical_url
        if not destination_url or destination_url in seen_urls:
            continue
        seen_urls.add(destination_url)
        seed_id = derive_seed_id(product_key, destination_url)
        merchant_slug = _normalize_token(offer.get("merchant_inferred") or "merchant").replace(" ", "-") or "merchant"
        external_product_id = f"{merchant_slug}:{seed_id.split(':')[-1]}"
        in_stock = bool(offer.get("in_stock") or False)
        availability = "in_stock" if in_stock else "out_of_stock"
        price = offer.get("price")
        # One synthetic variant — the audit only requires a non-empty
        # variant list with currency matching the row's price_currency.
        # We don't have real merchant variant data; this represents the
        # canonical purchasable identity and matches the same shape used
        # by Phase 7a's catalog_skus + catalog_offers chain.
        synthetic_variant = {
            "variant_id": f"{external_product_id}::canonical",
            "id": f"{external_product_id}::canonical",
            "sku": external_product_id,
            "title": pdp_payload["product_name"],
            "currency": "USD",
            "price_amount": price,
            "price": price,
            "availability": availability,
            "in_stock": in_stock,
        }
        rows.append({
            "id": seed_id,
            "external_product_id": external_product_id,
            "market": market,
            "tool": AGENT_VERSION,
            "title": pdp_payload["product_name"],
            "image_url": offer.get("image_url") or None,
            "price_amount": price,
            "price_currency": "USD",
            "destination_url": destination_url,
            "canonical_url": canonical_url or None,
            "domain": _domain_of(canonical_url or destination_url),
            "attached_product_key": product_key,
            "status": "active",
            "availability": availability,
            "seed_data": json.dumps({
                "brand": pdp_payload["brand"],
                "product_name": pdp_payload["product_name"],
                "title": pdp_payload["product_name"],
                "category_path": pdp_payload.get("category_path"),
                "merchant_inferred": offer.get("merchant_inferred"),
                "in_stock": in_stock,
                "availability": availability,
                "validated_at": offer.get("validated_at"),
                "agent_version": AGENT_VERSION,
                "variants": [synthetic_variant],
                "image_urls": [offer.get("image_url")] if offer.get("image_url") else [],
            }),
        })
    return rows


def derive_seed_id(product_key: str, destination_url: str) -> str:
    """Stable seed id from (product_key, destination_url) so re-runs
    UPSERT cleanly."""
    digest = hashlib.sha1(f"{product_key}|{destination_url}".encode("utf-8")).hexdigest()[:16]
    return f"seed:{AGENT_VERSION}:{digest}"


def derive_sku_key(product_key: str) -> str:
    """One synthetic SKU per PDP. Agent-authored PDPs have no real
    variants — the SKU represents the PDP's canonical purchasable
    identity. The suffix is fixed so re-runs UPSERT."""
    return f"{product_key}{SKU_SUFFIX}"


def derive_offer_id(product_key: str, sku_key: str, destination_url: str) -> str:
    """One offer per (PDP, retailer URL). The triple keys idempotency:
    re-running the agent updates price/availability without churn."""
    digest = hashlib.sha1(
        f"{product_key}|{sku_key}|{destination_url}".encode("utf-8")
    ).hexdigest()[:16]
    return f"offer:{AGENT_VERSION}:{digest}"


def derive_merchant_id(merchant_inferred: Optional[str], domain: Optional[str]) -> str:
    """Stable merchant_id derived from the validated retailer name (or
    domain as fallback). Format: 'agent_seed::<slug>'. Two offers from
    the same retailer collapse to the same merchant_id, which is what
    Phase 6's seller_count rule needs to compute pdp_scope correctly."""
    raw = merchant_inferred or domain or "unknown"
    slug = _normalize_token(raw).replace(" ", "-") or "unknown"
    return f"{MERCHANT_ID_PREFIX}{slug[:80]}"


def _build_sku_insert(
    *,
    product_key: str,
    pdp_payload: Dict[str, Any],
    canonical_url: Optional[str],
    image_url: Optional[str],
    seller: Dict[str, str],
) -> Dict[str, Any]:
    """One row per PDP. The SKU mirrors the PDP's identity (no variant
    axes), which lets canonical recall JOIN through catalog_skus to
    catalog_offers without our agent rows looking different from
    merchant-sync rows."""
    sku_key = derive_sku_key(product_key)
    strong_identifier = extract_strong_identifier(pdp_payload.get("strong_identity"))
    # Infer ingredient ids from the PDP name + summary (the enrichment pdp_payload carries no structured
    # ingredient field). Previously hardcoded [] which dropped ingredient evidence at migration, so
    # ingredient-constrained beauty search returned nothing. (#1659)
    _ingredient_text = " ".join(
        [
            str(pdp_payload.get("product_name") or ""),
            str(pdp_payload.get("attribute_summary") or ""),
        ]
    ).strip()
    sku_ingredient_ids = normalize_external_seed_structured_ingredient_ids(
        {"title": _ingredient_text}, {}
    )
    sku_payload = {"agent_version": AGENT_VERSION}
    if strong_identifier is not None:
        sku_payload["strong_identifier_kind"] = strong_identifier.kind
    return {
        "sku_key": sku_key,
        "product_key": product_key,
        # W2: the row rides under its seller of record. The sku_key /
        # source_variant_id derivations are untouched storage tokens.
        "merchant_id": seller["merchant_id"],
        "platform": SYNTHETIC_PLATFORM,
        "source_product_id": canonical_product_name(
            pdp_payload["brand"], pdp_payload["product_name"]
        ),
        # Must be unique within (merchant_id, platform) per
        # idx_catalog_skus_source_identity. All agent SKUs share
        # merchant_id='external_seed' + platform='external_seed', so
        # source_variant_id has to vary per PDP. Using product_key
        # makes it deterministic across re-runs.
        "source_variant_id": product_key,
        "source_domain": pdp_payload.get("source_domain") or None,
        "sku": None,
        "barcode": strong_identifier.value if strong_identifier else None,
        "title": pdp_payload["product_name"],
        "currency": "USD",
        "image_url": image_url or None,
        "visible_attributes": json.dumps({}),
        "visible_option_labels": json.dumps([]),
        "ingredient_ids": json.dumps(sku_ingredient_ids),
        "sku_payload": json.dumps(sku_payload),
        "readiness_tier": OFFER_READINESS_TIER,
    }


def _identifier_audit_reasons(identifier: Optional[StrongIdentifier]) -> Dict[str, int]:
    if identifier is None:
        return {NO_STRONG_IDENTIFIER: 1}
    if identifier.kind == "mpn":
        return {MPN_CAPTURED_AS_BARCODE: 1}
    return {}


def _build_merchant_upserts(
    offers: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Distinct catalog_merchants rows for the validated retailers.
    De-duped by merchant_id since two offers from the same retailer
    (e.g. two MAC products from maccosmetics.com) share one merchant
    row."""
    seen: Dict[str, Dict[str, Any]] = {}
    for offer in offers:
        merchant_inferred = str(offer.get("merchant_inferred") or "").strip() or None
        domain = _domain_of(offer.get("canonical_url") or offer.get("destination_url"))
        merchant_id = derive_merchant_id(merchant_inferred, domain)
        if merchant_id in seen:
            continue
        seen[merchant_id] = {
            "merchant_id": merchant_id,
            "merchant_name": merchant_inferred or domain or "Unknown retailer",
            "primary_platform": MERCHANT_PLATFORM,
            "status": "active",
            "source_system": AGENT_VERSION,
            "source_ref": domain or None,
            "metadata_json": json.dumps({
                "agent_version": AGENT_VERSION,
                "domain": domain,
            }),
        }
    return list(seen.values())


def _build_offer_inserts(
    *,
    product_key: str,
    sku_key: str,
    offers: List[Dict[str, Any]],
    source_domain: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """One catalog_offers row per validated offer. merchant_id resolves
    to the per-retailer synthetic id (see derive_merchant_id), which is
    what makes Phase 6 seller_count meaningful for the canonical
    classification."""
    rows: List[Dict[str, Any]] = []
    seen_offer_ids: set = set()
    for offer in offers:
        canonical_url = offer.get("canonical_url") or ""
        destination_url = offer.get("destination_url") or canonical_url
        if not destination_url:
            continue
        offer_id = derive_offer_id(product_key, sku_key, destination_url)
        if offer_id in seen_offer_ids:
            continue
        seen_offer_ids.add(offer_id)
        merchant_inferred = str(offer.get("merchant_inferred") or "").strip() or None
        domain = _domain_of(canonical_url or destination_url)
        merchant_id = derive_merchant_id(merchant_inferred, domain)
        availability = "in_stock" if offer.get("in_stock") else "unknown"
        price = offer.get("price")
        try:
            price_value = float(price) if price is not None else None
        except (TypeError, ValueError):
            price_value = None
        rows.append({
            "offer_id": offer_id,
            "sku_key": sku_key,
            "product_key": product_key,
            "merchant_id": merchant_id,
            "catalog_track": OFFER_CATALOG_TRACK,
            "truth_tier": OFFER_TRUTH_TIER,
            "readiness_tier": OFFER_READINESS_TIER,
            "offer_mode": OFFER_MODE,
            "channel": "default",
            "availability": availability,
            "inventory_quantity": 999 if offer.get("in_stock") else 0,
            "currency": "USD",
            "list_price": price_value,
            "merchant_effective_price": price_value,
            "estimated_best_price": price_value,
            "price_confidence": 0.7 if price_value is not None else None,
            "source_system": AGENT_VERSION,
            "source_ref": destination_url,
            "source_domain": source_domain,
            "offer_payload": json.dumps({
                "agent_version": AGENT_VERSION,
                "destination_url": destination_url,
                "canonical_url": canonical_url or None,
                "merchant_inferred": merchant_inferred,
                "domain": domain,
                "validated_at": offer.get("validated_at"),
            }),
        })
    return rows


def _domain_of(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    raw = str(url).strip()
    if not raw:
        return None
    try:
        from urllib.parse import urlparse

        parsed = urlparse(raw)
        host = (parsed.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        return host or None
    except Exception:
        return None


def ingest_validated_record(record: Dict[str, Any], *, source_jsonl: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Pure function: take one validated record and return the rows to
    INSERT/UPSERT for the canonical chain (merchants, products, skus,
    offers) plus the audit seed rows. Returns None for malformed
    records.

    The caller (runner) is responsible for actually executing the
    INSERT/UPSERT statements against the DB in FK order:
      1. catalog_merchants (FK target for offers)
      2. catalog_products  (FK target for skus + offers)
      3. catalog_skus      (FK target for offers)
      4. catalog_offers
      5. external_product_seeds (audit; no FK enforcement on the offer
         side, attached_product_key references catalog_products)
    """
    pdp_payload = _build_pdp_payload(record)
    if not pdp_payload:
        return None
    offers = _validated_offers(record)
    if not offers:
        # Record without any validated offer is not actionable —
        # we don't create empty PDPs.
        return None
    # W2 (ADR-011 R5 closure): resolve the seller of record BEFORE building any
    # row. Retailer domains key on etld1 alone; brand-direct keys on
    # (brand, etld1). Unresolvable -> the record is BLOCKED (skipped loudly and
    # retried on a later run) — never landed in the 'external_seed' bucket,
    # which is a retiring legacy population, not a landing zone (founder rule).
    # The seller derives from the PDP's OWN source_domain, never from
    # "whichever offer happens to be first" — that would make the identity
    # order-dependent, a fallback producing wrong-but-plausible sellers. A PDP
    # that cannot name where it came from cannot name its seller: blocked.
    try:
        seller = resolve_seed_seller_identity(
            brand=pdp_payload.get("brand"), domain=pdp_payload.get("source_domain")
        )
    except ValueError as exc:
        return {"skipped_reason": f"seller_of_record_unresolved: {exc}"}
    pdp_row = _build_pdp_insert(
        pdp_payload=pdp_payload, offers=offers, source_jsonl=source_jsonl, seller=seller
    )
    sku_row = _build_sku_insert(
        product_key=pdp_row["product_key"],
        pdp_payload=pdp_payload,
        canonical_url=pdp_row.get("canonical_url"),
        image_url=pdp_row.get("image_url"),
        seller=seller,
    )
    merchant_rows = _build_merchant_upserts(offers)
    # The observed seller-of-record row rides in the same merchants list so the
    # FK order (merchants -> products) already holds. `_ensure_only` tells apply
    # to insert-if-missing with claimed-attach — NEVER the clobbering upsert,
    # whose `status = EXCLUDED.status` would downgrade a graduated merchant.
    merchant_rows = merchant_rows + [{
        "merchant_id": seller["merchant_id"],
        "merchant_name": seller["merchant_name"],
        "primary_platform": MERCHANT_PLATFORM,
        "status": "observed",
        "source_system": AGENT_VERSION,
        "source_ref": seller["registrable"],
        "metadata_json": json.dumps({
            "observed": True,
            "minted_by": "catalog_enrichment_agent.ingestion (W2)",
            "adr": "ADR-009-D2",
            "seller_identity": {"kind": seller["kind"], "etld1": seller["registrable"]},
        }),
        "_ensure_only": True,
    }]
    offer_rows = _build_offer_inserts(
        product_key=pdp_row["product_key"],
        sku_key=sku_row["sku_key"],
        offers=offers,
        source_domain=pdp_row.get("source_domain"),
    )
    seed_rows = _build_seed_inserts(
        product_key=pdp_row["product_key"],
        pdp_payload=pdp_payload,
        offers=offers,
    )
    inci_row = _build_inci_row(
        product_key=pdp_row["product_key"],
        sku_key=sku_row["sku_key"],
        pdp_payload=pdp_payload,
    )
    return {
        "pdp": pdp_row,
        "sku": sku_row,
        "merchants": merchant_rows,
        "offers": offer_rows,
        "seeds": seed_rows,
        "inci": inci_row,
        "audit_reasons": _identifier_audit_reasons(
            extract_strong_identifier(pdp_payload.get("strong_identity"))
        ),
    }


def _build_inci_row(
    *,
    product_key: str,
    sku_key: str,
    pdp_payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """One optional beauty_sku_ingredients write intent per PDP that carried a
    published INCI list. sku_key is the canonical SKU (product_key || '::canonical').
    None when the record exposed no INCI — ingest is never blocked on it. Source
    precedence (brand_official > reseller_listing) is enforced by the apply-time
    writer (canonical_inci_intake.may_write), not here."""
    raw_inci = pdp_payload.get("raw_inci")
    if not raw_inci:
        return None
    return {
        "product_key": product_key,
        "sku_key": sku_key,
        "raw_inci": str(raw_inci),
        "source": pdp_payload.get("inci_source") or "reseller_listing",
    }


def ingest_validated_jsonl(
    rows: Iterable[Dict[str, Any]],
    *,
    source_jsonl: Optional[str] = None,
) -> Dict[str, Any]:
    """Drive ingest_validated_record across an iterable of records and
    return all five row collections plus skipped_count. Pure — no DB
    calls.

    Returns a dict with keys: pdps, skus, merchants, offers, seeds,
    skipped. Lists are de-duped by their natural primary key so re-runs
    across files don't stack duplicate row dicts.
    """
    pdp_rows: List[Dict[str, Any]] = []
    sku_rows: List[Dict[str, Any]] = []
    merchant_rows: List[Dict[str, Any]] = []
    offer_rows: List[Dict[str, Any]] = []
    seed_rows: List[Dict[str, Any]] = []
    inci_rows: List[Dict[str, Any]] = []
    audit_reasons: Dict[str, int] = {}
    skipped = 0
    skipped_reasons: Dict[str, int] = {}
    for record in rows:
        result = ingest_validated_record(record, source_jsonl=source_jsonl)
        if result is None:
            skipped += 1
            continue
        if result.get("skipped_reason"):
            skipped += 1
            reason = str(result["skipped_reason"]).split(":")[0]
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
            continue
        pdp_rows.append(result["pdp"])
        sku_rows.append(result["sku"])
        merchant_rows.extend(result["merchants"])
        offer_rows.extend(result["offers"])
        seed_rows.extend(result["seeds"])
        if result.get("inci"):
            inci_rows.append(result["inci"])
        for reason, count in (result.get("audit_reasons") or {}).items():
            audit_reasons[reason] = audit_reasons.get(reason, 0) + int(count or 0)

    def _dedupe(rows_in: List[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
        by_key: Dict[str, Dict[str, Any]] = {}
        for row in rows_in:
            k = row.get(key)
            if k is None:
                continue
            by_key.setdefault(str(k), row)
        return list(by_key.values())

    return {
        "pdps": _dedupe(pdp_rows, "product_key"),
        "skus": _dedupe(sku_rows, "sku_key"),
        "merchants": _dedupe(merchant_rows, "merchant_id"),
        "offers": _dedupe(offer_rows, "offer_id"),
        "seeds": _dedupe(seed_rows, "id"),
        "incis": _dedupe(inci_rows, "sku_key"),
        "skipped": skipped,
        "skipped_reasons": skipped_reasons,
        "audit_reasons": audit_reasons,
    }
