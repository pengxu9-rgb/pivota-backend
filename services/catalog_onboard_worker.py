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

import hashlib
import json
import logging
import re
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlsplit

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

def normalize_curated_brand_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the effective crawl contract before enqueue AND before execution.

    Existing queued rows predate these controls, so the execution boundary must
    validate too. The Path-C seed writer currently owns the US serving partition;
    accepting a different market here would claim a capability it does not have.
    Currency is independent of that partition and is never inferred from it.
    """
    if not isinstance(payload, dict):
        raise ValueError("curated brand payload must be an object")
    raw_domain = str(payload.get("domain") or "").strip()
    parsed = urlsplit(raw_domain if "://" in raw_domain else "https://" + raw_domain)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.port
            or parsed.scheme not in {"https", "http"} or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        raise ValueError("domain must name one storefront host, without a path or credentials")
    domain = parsed.hostname.lower().rstrip(".")
    if "." not in domain or any(c.isspace() for c in domain):
        raise ValueError("domain must be a valid storefront host")
    market = str(payload.get("market") or "US").strip().upper()
    if market != "US":
        raise ValueError("curated onboarding supports market US only; use a market-aware ingest lane")
    if (payload.get("only_vendors") is not None or payload.get("retailer_name")) and not payload.get("source_role"):
        raise ValueError("vendor-filtered or retailer-named onboarding requires explicit source_role (retailer or brand_official)")
    role = payload.get("source_role", "brand_official")
    if role not in {"brand_official", "retailer"}:
        raise ValueError("source_role must be brand_official or retailer")
    if payload.get("retailer_name") and role != "retailer":
        raise ValueError("retailer_name is only valid with source_role=retailer")
    vendors = payload.get("only_vendors")
    if vendors is not None:
        if not isinstance(vendors, list) or not vendors or any(
            not isinstance(v, str) or not v.strip() for v in vendors
        ):
            raise ValueError("only_vendors must be a nonempty list of nonblank vendor names")
        vendors = sorted({" ".join(v.split()).casefold() for v in vendors})
    if role == "retailer" and vendors is None:
        raise ValueError("retailer onboarding requires explicit nonempty only_vendors maker selection")
    currency = payload.get("require_currency")
    if currency is not None:
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Za-z]{3}", currency.strip()):
            raise ValueError("require_currency must be a three-letter currency code")
        currency = currency.strip().upper()
    normalized = {
        "domain": domain,
        "brand": " ".join(str(payload.get("brand") or "").split()) or None,
        "category_path": str(payload.get("category_path") or "").strip().strip("/"),
        "market": market,
        "source_role": role,
        "retailer_name": " ".join(str(payload.get("retailer_name") or "").split()) or None,
        "only_vendors": vendors,
        "require_currency": currency,
    }
    if role == "retailer" and not normalized["retailer_name"]:
        normalized["retailer_name"] = domain
    for name, default in (
        ("emit_real_variants", True), ("base_listings_only", False),
        ("enrich_missing_inci", True), ("enrich_missing_gtin", False),
    ):
        value = payload.get(name, default)
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be a boolean")
        normalized[name] = value
    for name, default in (("max_products", 500), ("max_scan_products", 10000), ("max_pdp_inci_fetches", 300),
                          ("max_pdp_identity_fetches", 100)):
        value = payload.get(name, default)
        if type(value) is not int or value < (0 if name.startswith("max_pdp_") else 1):
            raise ValueError(f"{name} must be a {'nonnegative' if name.startswith('max_pdp_') else 'positive'} integer")
        normalized[name] = value
    return normalized


def curated_brand_work_key(payload: Dict[str, Any], *, source: str = "curated_list") -> str:
    """Idempotency is the requested subset and execution scope, not just a host.

    Versioning leaves legacy pending host-only jobs visible to the cohort audit;
    enqueue never rewrites a processing job or silently merges unlike requests.
    """
    effective = normalize_curated_brand_payload(payload)
    for name in ("brand", "retailer_name"):
        effective[name] = (effective[name] or "").casefold()
    # Preserve pre-recovery v2 keys when this optional observation is disabled.
    # A dormant budget has no effect on the work being requested.
    if not effective["enrich_missing_gtin"]:
        effective.pop("enrich_missing_gtin")
        effective.pop("max_pdp_identity_fetches")
    effective["source"] = source
    encoded = json.dumps(effective, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    return f"curated:v2:{effective['domain']}:{digest}"

async def enqueue_curated_brands(
    brands: Sequence[Dict[str, Any]],
    *,
    priority_rank: Optional[Dict[str, int]] = None,
    source: str = "curated_list",
    db: Any = None,
) -> int:
    """Enqueue validated brand/subset jobs (dedup on effective crawl scope). priority =
    recurrence rank of the brand name. When priority_rank is not supplied, it
    defaults to cross-audit demand so every caller is demand-prioritized."""
    # Validate the whole input before creating any jobs; a bad final row must not
    # leave the caller with a half-enqueued roster and no result.
    jobs = []
    for row in brands or []:
        if not isinstance(row, dict):
            raise ValueError("each curated brand row must be an object")
        if row.get("domain"):
            jobs.append(normalize_curated_brand_payload(row))
    if priority_rank is None:
        priority_rank = await recurrence_rank(db=db)
    n = 0
    for b in jobs:
        pr = (priority_rank or {}).get(_norm(b.get("brand")), 0)
        key = curated_brand_work_key(b, source=source)
        if await q.enqueue(kind="curated_brand", dedup_key=key, payload=b, priority=pr, source=source, db=db):
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
    job = normalize_curated_brand_payload(payload)
    # market is validated above, not sent as a fictional feed capability.
    records = await records_for_brand(**{k: v for k, v in job.items() if k != "market"})
    crawl_report = getattr(records, "crawl_report", None)
    if not isinstance(crawl_report, dict) or crawl_report.get("status") != "complete":
        raise ValueError(f"{job['domain']}: crawl completeness was not proven; no records will be ingested")
    # New unattended rows must not reach ingestion's legacy None -> USD default,
    # even for brand-official jobs without an expected-currency assertion.
    for record in records:
        currency = (record.get("pdp") or {}).get("currency")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError(f"{job['domain']}: storefront currency was not proven; retry before ingest")
        if job["require_currency"] and currency != job["require_currency"]:
            raise ValueError(f"{job['domain']}: record currency does not match required currency")
    if not records:
        if apply:
            raise ValueError(f"{job['domain']}: no products enumerated; onboarding is incomplete")
        return {"records": 0, "applied": None, "note": "no products enumerated", "crawl": crawl_report,
                "primary_ingestion": {"status": "blocked", "reasons": ["no_products"]}}
    from services.catalog_enrichment_agent.primary_ingestion import (
        inspect_primary_plan, require_primary_plan, require_primary_apply,
    )
    plan = ingest_validated_jsonl(records)
    out = {"records": len(records), "plan_pdps": len(plan.get("pdps") or []), "applied": None,
           "crawl": crawl_report, "primary_ingestion": inspect_primary_plan(plan)}
    if apply:
        preflight = require_primary_plan(plan)
    if apply and plan.get("pdps"):
        out["applied"] = await apply_ingest_plan(
            plan, batch_label=f"onboard_queue:curated:{payload.get('domain')}", db=db, primary_readiness=True
        )
        out["primary_ingestion"] = require_primary_apply(preflight, out["applied"])
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
