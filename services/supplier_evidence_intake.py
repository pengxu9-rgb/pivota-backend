"""Supplier evidence intake — the merchant-facing writer for the canonical record.

Realizes ADR-001 #2 (supplier raw-input intake) + the data-contract Layer-3 claims:
a merchant/supplier supplies VERIFIABLE EVIDENCE (an INCI list today; brand-official
URL crawl + lab/cert later), and Pivota verifies → substantiates → screens → grades
it into provenance-backed, claim-safe `ProductClaim`s on the canonical record — never
free-text the merchant authored (ADR-001 Option C, rejected).

This is almost pure composition of the existing pipeline:
  1. ingest_canonical_inci(..., "supplier_input")  — write INCI by source precedence
     (a supplier input never downgrades a brand-official record).
  2. enrich_and_persist_product(...)               — INCI → verified actives →
     ingredient-substantiated, drug-screened claims → beauty_product_profiles
     .evidence_profile (fill-only-when-empty) → recompute serving eligibility.
  3. refresh_agent_pdp_view_for_content_key(...)   — project the highest-precedence
     evidence onto the canonical content_key serving view the agent reads (migration
     152 / PR #875), so the graded claims become agent-readable.

The shared-record guardrails are inherited: source precedence (step 1) and
fill-only-when-empty evidence writes (step 2) mean a reseller's input can neither
downgrade nor overwrite a brand-official record on a shared content_key.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from db.database import database
from services.beauty_enrichment_persist import enrich_and_persist_product
from services.canonical_inci_intake import ingest_canonical_inci
from services.agent_pdp_view_assembler import refresh_agent_pdp_view_for_content_key

logger = logging.getLogger(__name__)

# The provenance label for merchant/supplier-supplied raw inputs (rank 2 in
# canonical_inci_intake — above reseller listings, below brand-official).
SUPPLIER_SOURCE = "supplier_input"

REFRESH_SOURCE = "supplier_evidence_intake"


async def ingest_supplier_evidence(
    product_key: str,
    *,
    raw_inci: Optional[str] = None,
    brand_url: Optional[str] = None,
    db: Any = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Verify + grade a supplier's evidence into the canonical record and serve it.

    Accepts either an INCI list (pasted) OR a product-page URL we crawl for the
    INCI (the easier front door — the operator pastes a link). Returns a
    step-by-step summary; never raises on a missing/unresolvable product
    (status="not_found"). Idempotent and ownership-safe (the underlying pipeline
    is fill-only + precedence-gated).
    """
    read_db = db or database
    out: Dict[str, Any] = {
        "product_key": product_key,
        "status": "ok",
        "steps": {},
        "content_key": None,
        "served": False,
    }

    # Brand-URL front door: crawl the page the supplier points at for its INCI
    # (Shopify product-JSON preferred, then rendered text). Best-effort — a fetch
    # miss just means we fall back to whatever INCI was pasted.
    if (not raw_inci or not str(raw_inci).strip()) and brand_url and str(brand_url).strip():
        try:
            from services.canonical_inci_resolver import http_fetch, resolve_inci_from_url

            resolved = await resolve_inci_from_url(str(brand_url).strip(), fetch=http_fetch)
        except Exception as exc:  # noqa: BLE001
            resolved = None
            logger.warning("supplier_evidence_intake: URL crawl failed for %s: %s",
                           brand_url, str(exc)[:200])
        if resolved and getattr(resolved, "raw_inci", None):
            raw_inci = resolved.raw_inci
            out["steps"]["crawl"] = "ok"
            out["crawl_source_url"] = resolved.source_url
        else:
            out["steps"]["crawl"] = "no_inci_found"

    if not raw_inci or not str(raw_inci).strip():
        # No verifiable evidence supplied (or the crawl found no INCI). Nothing to
        # grade. (Lab/cert/community intake land in a later phase.)
        out["status"] = "no_evidence"
        return out

    inci_res = await ingest_canonical_inci(
        product_key, raw_inci, SUPPLIER_SOURCE, db=read_db, dry_run=dry_run
    )
    inci_status = inci_res.get("status")
    out["steps"]["inci"] = inci_status
    out["content_key"] = inci_res.get("content_key")
    if inci_status == "not_found":
        out["status"] = "not_found"
        return out
    if inci_status == "rejected_not_inci":
        out["status"] = "rejected_not_inci"
        return out

    # Derive verified actives → ingredient-substantiated, drug-screened claims →
    # evidence_profile (+ required_disclaimers) → recompute serving eligibility.
    enrich_res = await enrich_and_persist_product(product_key, db=read_db, dry_run=dry_run)
    out["steps"]["enrich"] = enrich_res.get("status")
    out["content_key"] = out["content_key"] or enrich_res.get("content_key")
    out["category_kind"] = enrich_res.get("category_kind")
    out["wrote_evidence"] = bool((enrich_res.get("written") or {}).get("evidence_claims"))
    out["substantiated_claims"] = list(
        (enrich_res.get("derived") or {}).get("substantiated_claims") or []
    )

    # Project the graded claims onto the canonical agent serving view (content_key).
    # Best-effort: a refresh failure never invalidates the persisted evidence.
    if out["content_key"] and not dry_run:
        try:
            out["served"] = await refresh_agent_pdp_view_for_content_key(
                out["content_key"], refresh_source=REFRESH_SOURCE, db=read_db
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "supplier_evidence_intake: agent_pdp_view refresh failed for "
                "content_key=%s: %s",
                out["content_key"], str(exc)[:200],
            )

    return out
