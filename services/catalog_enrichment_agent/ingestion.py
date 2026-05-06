"""Stage 3 ingestion — read validated.jsonl produced by the Stage 2 codex
routine and INSERT into catalog_products + external_product_seeds.

Idempotency contract:
- A PDP is identified by (brand_normalized, canonical_product_name).
  If a catalog_products row with the matching key already exists, skip
  the PDP insert and append any new offers to the existing
  attached_product_key.
- An external_product_seeds row is identified by canonical_url. If a
  matching seed exists for the same PDP, skip; else INSERT with
  attached_product_key set.
- All seeds inserted by this pipeline use:
    merchant_id = "external_seed"
    tool        = "catalog_enrichment_agent_v1"
    status      = "active"

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

logger = logging.getLogger("catalog_enrichment_agent.ingestion")

AGENT_VERSION = "catalog_enrichment_agent_v1"
SYNTHETIC_MERCHANT_ID = "external_seed"
SYNTHETIC_PLATFORM = "external_seed"
DEFAULT_CATEGORY_CONFIDENCE = 0.7
DEFAULT_CATEGORY_LABEL_SOURCE = "enrichment_agent_v1"
DEFAULT_TRUTH_TIER = "primary"
DEFAULT_READINESS_TIER = "referral_only"
DEFAULT_CATALOG_TRACK = "external_referral"

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
    payload = {
        "brand": str(pdp.get("brand") or "").strip(),
        "product_name": str(pdp.get("product_name") or "").strip(),
        "category_path": str(pdp.get("category_path") or "").strip(),
        "attribute_summary": str(pdp.get("attribute_summary") or "").strip(),
        "agent_version": AGENT_VERSION,
    }
    if not payload["brand"] or not payload["product_name"]:
        return {}
    return payload


def _build_pdp_insert(
    *,
    pdp_payload: Dict[str, Any],
    offers: List[Dict[str, Any]],
    source_jsonl: Optional[str],
) -> Dict[str, Any]:
    """Construct the catalog_products row dict that the runner will
    INSERT. The product_key is deterministic so re-runs UPSERT cleanly."""
    product_key = derive_product_key(pdp_payload["brand"], pdp_payload["product_name"])
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
        "merchant_id": SYNTHETIC_MERCHANT_ID,
        "platform": SYNTHETIC_PLATFORM,
        "source_product_id": canonical_product_name(pdp_payload["brand"], pdp_payload["product_name"]),
        "catalog_track": DEFAULT_CATALOG_TRACK,
        "truth_tier": DEFAULT_TRUTH_TIER,
        "readiness_tier": DEFAULT_READINESS_TIER,
        "source_system": AGENT_VERSION,
        "title": pdp_payload["product_name"],
        "description": pdp_payload.get("attribute_summary") or None,
        "brand": pdp_payload["brand"],
        "product_type": pdp_payload.get("category_path", "").split("/")[-1] or None,
        "category": pdp_payload.get("category_path", "").split("/")[-1] or None,
        "category_path": pdp_payload.get("category_path") or None,
        "category_confidence": DEFAULT_CATEGORY_CONFIDENCE,
        "category_label_source": DEFAULT_CATEGORY_LABEL_SOURCE,
        "canonical_url": canonical_url or None,
        "image_url": image_url or None,
        "product_payload": json.dumps({"enrichment_meta": enrichment_meta}),
    }


def _build_seed_inserts(
    *,
    product_key: str,
    pdp_payload: Dict[str, Any],
    offers: List[Dict[str, Any]],
    market: str = "US",
) -> List[Dict[str, Any]]:
    """Produce one external_product_seeds row per validated offer."""
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
        rows.append({
            "id": seed_id,
            "external_product_id": f"{merchant_slug}:{seed_id.split(':')[-1]}",
            "market": market,
            "tool": AGENT_VERSION,
            "title": pdp_payload["product_name"],
            "image_url": offer.get("image_url") or None,
            "price_amount": offer.get("price"),
            "price_currency": "USD",
            "destination_url": destination_url,
            "canonical_url": canonical_url or None,
            "domain": _domain_of(canonical_url or destination_url),
            "attached_product_key": product_key,
            "status": "active",
            "seed_data": json.dumps({
                "brand": pdp_payload["brand"],
                "product_name": pdp_payload["product_name"],
                "category_path": pdp_payload.get("category_path"),
                "merchant_inferred": offer.get("merchant_inferred"),
                "in_stock": offer.get("in_stock"),
                "validated_at": offer.get("validated_at"),
                "agent_version": AGENT_VERSION,
            }),
        })
    return rows


def derive_seed_id(product_key: str, destination_url: str) -> str:
    """Stable seed id from (product_key, destination_url) so re-runs
    UPSERT cleanly."""
    digest = hashlib.sha1(f"{product_key}|{destination_url}".encode("utf-8")).hexdigest()[:16]
    return f"seed:{AGENT_VERSION}:{digest}"


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
    INSERT (PDP row + offer rows). Returns None for malformed records.

    The caller (runner) is responsible for actually executing the INSERT
    statements against the DB."""
    pdp_payload = _build_pdp_payload(record)
    if not pdp_payload:
        return None
    offers = _validated_offers(record)
    if not offers:
        # Record without any validated offer is not actionable —
        # we don't create empty PDPs.
        return None
    pdp_row = _build_pdp_insert(pdp_payload=pdp_payload, offers=offers, source_jsonl=source_jsonl)
    seed_rows = _build_seed_inserts(
        product_key=pdp_row["product_key"],
        pdp_payload=pdp_payload,
        offers=offers,
    )
    return {"pdp": pdp_row, "seeds": seed_rows}


def ingest_validated_jsonl(
    rows: Iterable[Dict[str, Any]],
    *,
    source_jsonl: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
    """Drive ingest_validated_record across an iterable of records and
    return (pdp_rows, seed_rows, skipped_count). Pure — no DB calls."""
    pdp_rows: List[Dict[str, Any]] = []
    seed_rows: List[Dict[str, Any]] = []
    skipped = 0
    for record in rows:
        result = ingest_validated_record(record, source_jsonl=source_jsonl)
        if result is None:
            skipped += 1
            continue
        pdp_rows.append(result["pdp"])
        seed_rows.extend(result["seeds"])
    # De-dupe PDPs by product_key (re-runs across files can stack).
    by_key: Dict[str, Dict[str, Any]] = {}
    for row in pdp_rows:
        by_key.setdefault(row["product_key"], row)
    return list(by_key.values()), seed_rows, skipped
