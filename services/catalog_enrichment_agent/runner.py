"""Programmatic Path-C runner: candidates → validate → ingest, in one call.

The `run_catalog_enrichment.py` CLI does this across files (candidates JSONL →
validated JSONL → DB). This is the in-process equivalent so a service/worker (e.g.
the audit→catalog-coverage flow) can run the full pipeline without shelling out:

    candidates ──validate_candidate (Gemini resolves PDP url, drops non-resolving)──►
    validated ──ingest_validated_jsonl (pure plan)──► plan ──apply_ingest_plan (FK-order)──► DB

`apply` defaults to False (validate + build plan only — no catalog writes). Validation
costs ~1 Gemini call per candidate; the caller is responsible for capping/prioritizing.
Source-agnostic: candidates may come from the audit transform or a curated list.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Sequence

from services.catalog_enrichment_agent.apply import apply_ingest_plan
from services.catalog_enrichment_agent.gemini_url_validator import (
    DEFAULT_TIMEOUT_S,
    validate_candidate,
)
from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl

logger = logging.getLogger("catalog_enrichment_agent.runner")


async def _validate_all(
    candidates: Sequence[Dict[str, Any]],
    *,
    concurrency: int,
    timeout_s: float,
) -> List[Dict[str, Any]]:
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(cand: Dict[str, Any]) -> Dict[str, Any]:
        async with sem:
            try:
                return await validate_candidate(cand, timeout_s=timeout_s)
            except Exception as exc:  # noqa: BLE001 — one bad candidate must not abort the batch
                logger.exception("validate failed for %s — %s", cand.get("product_name"), exc)
                _expected = cand.get("expected_url_domains") or []
                return {
                    "pdp": {
                        "brand": cand.get("brand"),
                        "product_name": cand.get("product_name"),
                        "category_path": cand.get("category_path"),
                        "attribute_summary": cand.get("attribute_summary"),
                        # W2: seller of record needs the candidate's own domain.
                        "source_domain": str(_expected[0] if _expected else "").strip().lower().removeprefix("www.") or None,
                    },
                    "offers": [],
                }

    return list(await asyncio.gather(*(_one(c) for c in candidates)))


async def run_candidates(
    candidates: Sequence[Dict[str, Any]],
    *,
    batch_label: str,
    apply: bool = False,
    concurrency: int = 4,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    db: Any = None,
) -> Dict[str, Any]:
    """Validate candidates and (optionally) ingest the resolved ones. Returns a
    summary; with apply=True, `applied` holds the per-table insert counts."""
    if not candidates:
        return {"candidates": 0, "validated_with_offers": 0, "plan_pdps": 0, "applied": None}

    validated = await _validate_all(
        candidates, concurrency=concurrency, timeout_s=timeout_s
    )
    with_offers = sum(1 for v in validated if (v or {}).get("offers"))
    plan = ingest_validated_jsonl(validated)
    summary: Dict[str, Any] = {
        "candidates": len(candidates),
        "validated_with_offers": with_offers,
        "plan_pdps": len(plan.get("pdps") or []),
        "plan_offers": len(plan.get("offers") or []),
        "plan_skipped": plan.get("skipped"),
        "applied": None,
    }
    if apply and plan.get("pdps"):
        summary["applied"] = await apply_ingest_plan(plan, batch_label=batch_label, db=db)
    return summary
