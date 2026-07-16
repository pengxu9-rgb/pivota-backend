"""Brand-official-first MINT lane.

When a retailer SKU has NO canonical match, the wrong move is to mint a canonical
from the *retailer's* copy — that enshrines a supplier's marketing text as our
product record and (per the StyleKorean pilot) duplicates products we could have
reconciled. The right move (ADR-001) is to ingest the brand's OWN storefront as
the authentic, US-market canonical anchor, then attach the retailer offer to it.

This module:
  1. fetches the brand's official Shopify catalog as validated records
     (`services.curated_brand_feed` — carries brand-direct title/price/image and
     the variant **barcode/GTIN**, the strongest identity basis);
  2. predicts, in memory and WITHOUT writing, which of the retailer's MINT SKUs
     resolve to a brand-official product (same `retailer_match_key`); and
  3. on apply, ingests those canonicals through the sanctioned Path-C executor
     (`ingest_validated_jsonl` → `apply_ingest_plan`).

Only Shopify-hosted brand domains are enumerable this way (non-Shopify →
`records_for_brand` returns []). Retailer SKUs with no brand-official match are
returned as RESIDUE for review, never minted from retailer copy here.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Mapping, Optional, Tuple

from services.catalog_enrichment_agent.apply import apply_ingest_plan
from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl
from services.curated_brand_feed import records_for_brand
from services.pdp_matcher.retailer_match import build_match_index, retailer_match_key


def _official_index_rows(official_records: List[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Adapt {pdp:{brand,product_name}} → the {brand,title} shape build_match_index
    expects, carrying the source record for downstream use."""
    rows: List[Dict[str, Any]] = []
    for rec in official_records:
        pdp = rec.get("pdp") or {}
        rows.append({
            "brand": pdp.get("brand"),
            "title": pdp.get("product_name"),
            "record": rec,
        })
    return rows


def predict_official_matches(
    mint_items: List[Mapping[str, Any]],
    official_records: List[Mapping[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Pure, in-memory: split retailer MINT SKUs into those that resolve to a
    brand-official product vs. residue.

    mint_items: dicts with 'brand' and 'title' (the CLI plan entries).
    Returns (resolved, residue). Each resolved entry is
    {'item': <mint_item>, 'official': <official record>}."""
    index = build_match_index(_official_index_rows(official_records))
    resolved: List[Dict[str, Any]] = []
    residue: List[Dict[str, Any]] = []
    for item in mint_items:
        hit = index.get(retailer_match_key(item.get("brand"), item.get("title")))
        if hit is not None:
            resolved.append({"item": dict(item), "official": hit.get("record")})
        else:
            residue.append(dict(item))
    return resolved, residue


def filter_net_new(
    official_records: List[Mapping[str, Any]],
    our_rows: List[Mapping[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split brand-official records into (net_new, already_have) vs our catalog,
    by retailer_match_key. ONLY net_new is safe to ingest: re-ingesting a product
    we already have would derive a fresh product_key from the storefront's
    (drifted) title and create a DUPLICATE canonical — ingestion.derive_product_key
    has no title-normalization, unlike the match layer. already_have is returned
    for reporting (candidates for a GTIN-refresh path, not a re-INSERT)."""
    our_index = build_match_index(
        [{"brand": r.get("brand"), "title": r.get("title"), "pdp_scope": r.get("pdp_scope")} for r in our_rows]
    )
    net_new: List[Dict[str, Any]] = []
    already: List[Dict[str, Any]] = []
    for rec in official_records:
        pdp = rec.get("pdp") or {}
        key = retailer_match_key(pdp.get("brand"), pdp.get("product_name"))
        (already if key in our_index else net_new).append(dict(rec))
    return net_new, already


async def fetch_official_records(
    *,
    domain: str,
    brand: Optional[str],
    category_path: str,
    max_products: int = 500,
) -> List[Dict[str, Any]]:
    """The brand's official storefront as validated records (Shopify only)."""
    return await records_for_brand(
        domain=domain, category_path=category_path, brand=brand, max_products=max_products
    )


def dominant_brand(items: List[Mapping[str, Any]]) -> Optional[str]:
    """Most common brand string across items (single-brand crawls → that brand)."""
    names = [str(i.get("brand")).strip() for i in items if i.get("brand")]
    if not names:
        return None
    return Counter(names).most_common(1)[0][0]


async def ingest_brand_official(
    *,
    official_records: List[Mapping[str, Any]],
    brand: Optional[str],
    domain: str,
    apply: bool = False,
    db: Any = None,
) -> Dict[str, Any]:
    """Build the canonical-chain plan from brand-official records and (if apply)
    execute it through the sanctioned Path-C writer. Returns a summary."""
    plan = ingest_validated_jsonl(official_records, source_jsonl=f"brand_official:{domain}")
    summary: Dict[str, Any] = {
        "domain": domain,
        "brand": brand,
        "official_records": len(official_records),
        "plan_pdps": len(plan["pdps"]),
        "plan_offers": len(plan["offers"]),
        "skipped": plan["skipped"],
        "applied": None,
    }
    if apply:
        summary["applied"] = await apply_ingest_plan(
            plan, batch_label=f"brand_official:{brand or domain}", db=db
        )
    return summary
