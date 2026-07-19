"""Catalog-onboard queue worker — the unattended catalog-coverage growth layer.

Enqueue work from the feeds (curated brand lists; audit competitor-discovery,
recurrence-prioritized) and drain it on a schedule: each item runs the existing
onboarding path (curated_brand → curated_brand_feed; audit_candidate → Path-C
runner) and ingests via the shared executor. No human runs a CLI per brand.

Reuse-only: this orchestrates services already shipped (curated_brand_feed,
catalog_enrichment_agent.runner/ingestion/apply) over the queue (db.catalog_onboard_queue).
Best-effort per item — one failure (with retry budget) never aborts the tick.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Sequence

import db.catalog_onboard_queue as q
from services.catalog_enrichment_agent.apply import apply_ingest_plan
from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl
from services.catalog_enrichment_agent.runner import run_candidates
from services.competitor_recurrence import recurrence_rank
from services.curated_brand_feed import records_for_brand

logger = logging.getLogger(__name__)


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _as_dict(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except Exception:  # noqa: BLE001
            return {}
    return {}


# ---- enqueue (sources) -------------------------------------------------------

async def enqueue_curated_brands(
    brands: Sequence[Dict[str, Any]],
    *,
    priority_rank: Optional[Dict[str, int]] = None,
    source: str = "curated_list",
    db: Any = None,
) -> int:
    """Enqueue {domain, category_path, brand?} rows (dedup on domain). priority =
    recurrence rank of the brand name. When priority_rank is not supplied, it
    defaults to cross-audit demand so every caller is demand-prioritized."""
    if priority_rank is None:
        priority_rank = await recurrence_rank(db=db)
    n = 0
    for b in brands or []:
        domain = str(b.get("domain") or "").strip().lower()
        if not domain:
            continue
        pr = (priority_rank or {}).get(_norm(b.get("brand")), 0)
        if await q.enqueue(kind="curated_brand", dedup_key=domain, payload=b, priority=pr, source=source, db=db):
            n += 1
    return n


async def enqueue_audit_candidates(
    candidates: Sequence[Dict[str, Any]],
    *,
    priority_rank: Optional[Dict[str, int]] = None,
    source: str = "audit",
    db: Any = None,
) -> int:
    """Enqueue Path-C candidates (dedup on normalized product_name). priority =
    cross-audit recurrence rank; defaults to live demand when not supplied so every
    caller is demand-prioritized."""
    if priority_rank is None:
        priority_rank = await recurrence_rank(db=db)
    n = 0
    for c in candidates or []:
        key = _norm(c.get("product_name"))
        if not key:
            continue
        pr = (priority_rank or {}).get(key, 0)
        if await q.enqueue(kind="audit_candidate", dedup_key=key, payload=c, priority=pr, source=source, db=db):
            n += 1
    return n


# ---- drain (worker) ----------------------------------------------------------

async def _process_curated_brand(payload: Dict[str, Any], *, apply: bool, db: Any) -> Dict[str, Any]:
    records = await records_for_brand(
        domain=payload.get("domain"),
        category_path=payload.get("category_path") or "",
        brand=payload.get("brand"),
        # NEW brand-official mints recover INCI from the PDP metafield/accordion
        # when body_html has none (the cohort keeps INCI out of /products.json).
        enrich_missing_inci=True,
    )
    if not records:
        return {"records": 0, "applied": None, "note": "no products enumerated"}
    plan = ingest_validated_jsonl(records)
    out = {"records": len(records), "plan_pdps": len(plan.get("pdps") or []), "applied": None}
    if apply and plan.get("pdps"):
        out["applied"] = await apply_ingest_plan(
            plan, batch_label=f"onboard_queue:curated:{payload.get('domain')}", db=db
        )
    return out


async def _process_audit_candidate(payload: Dict[str, Any], *, apply: bool, db: Any) -> Dict[str, Any]:
    return await run_candidates(
        [payload], batch_label="onboard_queue:audit_candidate", apply=apply, db=db
    )


async def process_queue(
    *,
    limit: int = 10,
    apply: bool = False,
    db: Any = None,
) -> Dict[str, Any]:
    """Claim + process up to `limit` items. apply=False = enumerate/validate only
    (no catalog writes). Returns a tick summary."""
    items = await q.claim_batch(limit, db=db)
    summary = {"claimed": len(items), "done": 0, "failed": 0, "skipped": 0}
    for item in items:
        item_id = item["id"]
        payload = _as_dict(item.get("payload"))
        try:
            if item["kind"] == "curated_brand":
                result = await _process_curated_brand(payload, apply=apply, db=db)
            elif item["kind"] == "audit_candidate":
                result = await _process_audit_candidate(payload, apply=apply, db=db)
            else:
                await q.mark_skipped(item_id, reason=f"unknown kind {item['kind']}", db=db)
                summary["skipped"] += 1
                continue
            await q.mark_done(item_id, result=result, db=db)
            summary["done"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("onboard_queue item %s (%s) failed: %s", item_id, item.get("kind"), exc)
            await q.mark_failed(
                item_id,
                error=str(exc),
                attempts=int(item.get("attempts") or 0),
                max_attempts=int(item.get("max_attempts") or 3),
                db=db,
            )
            summary["failed"] += 1
    return summary
