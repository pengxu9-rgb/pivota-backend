"""Stage-1 of catalog coverage: turn an audit's discovered competitive landscape
into Path-C enrichment candidates.

The catalog-enrichment agent (Path C — services/catalog_enrichment_agent/) already
takes *candidate metadata* `{brand, product_name, category_path, attribute_summary,
expected_url_domains}`, has Gemini RESOLVE the canonical PDP URL, DROP anything that
doesn't resolve to a live PDP, and ingest the rest as depositable canonical anchors
(pdp_scope=multi_merchant_canonical, conf >= 0.9). The one missing piece was Stage 1:
producing candidates automatically instead of hand-curating JSONL.

A merchant audit already discovers the competitive landscape: `competitors_named`
accrues on the authority hosts (a mix of brand names — "MERIT", "Kosas" — and
brand+product strings — "Olly Collagen", "Youtheory Collagen Liquid"). Those map
DIRECTLY to Path-C candidates; the validator is the quality gate (brand-only /
unresolvable strings simply get dropped, so the transform can be deliberately lossy).

This module is PURE + read-only: it extracts + dedupes candidates. It does NOT call
Gemini and does NOT write the catalog — the caller feeds the output through the
existing `run_catalog_enrichment.py validate + ingest --apply` (the reviewed gate),
or the orchestration runner.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _iter_competitor_names(brand_report: Dict[str, Any]) -> List[str]:
    """All distinct competitor strings the audit surfaced (order-preserving)."""
    seen: Set[str] = set()
    out: List[str] = []
    if not isinstance(brand_report, dict):
        return out
    authority = brand_report.get("authority_map")
    if not isinstance(authority, dict):
        return out
    for sku in authority.get("skus") or []:
        if not isinstance(sku, dict):
            continue
        for host in sku.get("authority_hosts") or []:
            if not isinstance(host, dict):
                continue
            for name in host.get("competitors_named") or []:
                key = _norm(name)
                clean = str(name or "").strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                out.append(clean)
    return out


def audit_to_candidates(
    brand_report: Dict[str, Any],
    *,
    category_path: Optional[str] = None,
    expected_url_domains: Optional[Sequence[str]] = None,
    max_candidates: int = 300,
) -> List[Dict[str, Any]]:
    """Build Path-C candidate records from an audit's competitor landscape.

    `category_path` is the merchant's category (load-bearing for recall) — pass the
    audited merchant's representative category. `expected_url_domains` is an optional
    hint (e.g. the brand's own domain); empty lets the validator search broadly.

    Each candidate carries `source='audit_competitor_discovery'` so the index can
    trace provenance. The competitor string is used as `product_name`; the Gemini
    validator infers the brand and resolves the PDP (or drops it).
    """
    domains = [d for d in (expected_url_domains or []) if d]
    out: List[Dict[str, Any]] = []
    for name in _iter_competitor_names(brand_report):
        out.append(
            {
                "brand": name,
                "product_name": name,
                "category_path": category_path or "",
                "attribute_summary": "",
                "expected_url_domains": list(domains),
                "source": "audit_competitor_discovery",
            }
        )
        if len(out) >= max_candidates:
            break
    return out


async def filter_already_indexed(
    candidates: Sequence[Dict[str, Any]],
    *,
    db: Any,
) -> Tuple[List[Dict[str, Any]], int]:
    """Drop candidates whose brand/title already exists in the commerce index
    (external_seed), so we don't re-enrich what we already carry. Best-effort:
    on any DB error, returns all candidates unfiltered (the validator + ingest
    upserts are idempotent anyway). Returns (new_candidates, skipped_count)."""
    if not candidates:
        return [], 0
    try:
        rows = await db.fetch_all(
            "SELECT brand, title FROM catalog_products WHERE merchant_id = 'external_seed'"
        )
    except Exception:  # noqa: BLE001 — dedup is best-effort
        return list(candidates), 0
    indexed: Set[str] = set()
    for r in rows or []:
        for field in ("brand", "title"):
            n = _norm(r[field]) if field in r else ""
            if n:
                indexed.add(n)
    new: List[Dict[str, Any]] = []
    skipped = 0
    for c in candidates:
        if _norm(c.get("product_name")) in indexed or _norm(c.get("brand")) in indexed:
            skipped += 1
            continue
        new.append(c)
    return new, skipped
