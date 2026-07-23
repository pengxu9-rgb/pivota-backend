"""PR-2: competitor cohort audit orchestrator.

When a parent audit (merchant or cold-start prospect) names competitor
brands via category_visibility_test, this module:
  1. Resolves each competitor brand → official domain (Gemini grounded)
  2. Runs the cold-start discovery + audit pipeline against each
     competitor domain
  3. Persists results to competitor_audit_runs linked to the parent

Designed to be invoked AFTER the parent audit completes (so we have
its run_id + extracted competitor list). Runs in the background so the
parent audit response isn't blocked on N×30s competitor audits.

Cost guard:
  - Default cohort size = 3 competitors (configurable)
  - Hard cap = 5 (Gemini quota multiplier — at 13 calls per audit,
    5 competitors adds 65 calls per parent audit)
  - Per-merchant semaphore (cap=5) inherited from agent_center_llm_client
  - Per-competitor 60s timeout on the orchestration

Honest behavior:
  - Domain resolution failure → competitor_audit_run row with
    status='failed' + error_message; orchestrator continues to next
  - Discovery failure (robots.txt blocks, 0 products) → same shape
  - Audit failure → same shape
  - Caller can introspect failures via cohort_for_parent_run
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx
from services import vertex_gemini

logger = logging.getLogger(__name__)


_DEFAULT_COHORT_SIZE = 3
_HARD_MAX_COHORT_SIZE = 5

_DOMAIN_RESOLUTION_TIMEOUT_S = 15.0
_PER_COMPETITOR_AUDIT_TIMEOUT_S = 240.0


def _resolve_gemini_api_key() -> Optional[str]:
    for var in ("GEMINI_API_KEY", "PIVOTA_GEMINI_API_KEY"):
        value = os.environ.get(var)
        if value and value.strip():
            return value.strip()
    return None


def _build_domain_prompt(brand: str) -> str:
    return f"""You are a brand-research analyst. What is the official primary website domain for the brand "{brand}"?

OUTPUT FORMAT — strict:
- Reply with a bare JSON object starting with {{ and ending with }}
- Do NOT wrap in markdown fences or prose

Schema:
{{"domain": "example.com", "confidence": "high"}}

Rules:
- domain: bare hostname only (no scheme, no path, no www. prefix)
- confidence: "high" if you've cited this domain in grounded sources for {brand}; "medium" if inferring from category fit; "low" otherwise
- Use null for domain if you can't identify a primary website (e.g. brand-only retailer with no D2C site)"""


async def _resolve_competitor_domain(
    brand: str,
    *,
    api_key: Optional[str] = None,
    timeout_s: float = _DOMAIN_RESOLUTION_TIMEOUT_S,
) -> Optional[str]:
    """Single Gemini grounded call to find a competitor brand's
    official domain. Returns the bare hostname (no scheme) or None
    on any failure / unknown.
    """
    key = api_key if api_key is not None else _resolve_gemini_api_key()
    if not vertex_gemini.credentials_available(key):
        logger.info(
            "competitor_orchestrator: no GEMINI_API_KEY; "
            "cannot resolve domain for %r", brand,
        )
        return None
    body = {
        "contents": [{"role": "user", "parts": [{"text": _build_domain_prompt(brand)}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 256},
        "tools": [{"google_search": {}}],
    }
    url = vertex_gemini.generate_content_url("gemini-2.5-flash")
    headers = await vertex_gemini.auth_headers(key)
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.post(url, headers=headers, json=body)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        logger.warning(
            "competitor_orchestrator: domain HTTP error for %r: %s",
            brand, exc,
        )
        return None
    if r.status_code != 200:
        return None
    try:
        payload = r.json()
    except json.JSONDecodeError:
        return None

    candidates = payload.get("candidates") or []
    if not candidates:
        return None
    parts = (((candidates[0] or {}).get("content") or {}).get("parts") or [])
    text = "\n".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
    # W3: shared tolerant parser (bare/fence/substring), one implementation.
    from services.llm_io import parse_llm_object

    parsed = parse_llm_object(text, label="competitor_audit")
    if parsed is None:
        return None
    domain = parsed.get("domain")
    if not isinstance(domain, str) or not domain.strip():
        return None
    domain = domain.strip().lower()
    # Strip scheme + path defensively (Gemini sometimes returns full URL).
    domain = re.sub(r"^https?://", "", domain)
    domain = domain.split("/", 1)[0]
    if domain.startswith("www."):
        domain = domain[4:]
    if not domain or "." not in domain:
        return None
    return domain


async def _audit_one_competitor(
    *,
    brand: str,
    parent_audit_run_id: str,
    market: str,
    max_runs: int,
    category_override: Optional[str] = None,
) -> Dict[str, Any]:
    """End-to-end: resolve domain → discover products → run audit →
    persist competitor_audit_run row. Returns a summary dict for the
    orchestrator log line. Catches ALL exceptions internally — the
    cohort batch must continue across per-competitor failures."""
    from db.competitor_audit_runs import (
        record_competitor_run_started,
        record_competitor_run_completed,
    )
    from services.bd_cold_start_service import (
        BrandDiscoveryError,
        discover_products_for_audit,
    )
    from services.agent_center_bd_report_service import run_brand_report

    summary: Dict[str, Any] = {
        "competitor_brand": brand,
        "domain": None,
        "status": "skipped",
        "reason": None,
    }

    domain = await _resolve_competitor_domain(brand)
    summary["domain"] = domain
    if not domain:
        # Persist the failure so the cohort dashboard can show "we
        # tried but couldn't find a domain for this competitor."
        run_id = await record_competitor_run_started(
            parent_audit_run_id=parent_audit_run_id,
            competitor_brand=brand,
            competitor_domain=None,
        )
        await record_competitor_run_completed(
            run_id=run_id,
            status="failed",
            error_message="domain_resolution_failed",
        )
        summary["status"] = "failed"
        summary["reason"] = "domain_resolution_failed"
        return summary

    run_id = await record_competitor_run_started(
        parent_audit_run_id=parent_audit_run_id,
        competitor_brand=brand,
        competitor_domain=domain,
    )

    discovery_url = f"https://{domain}/"
    try:
        discovered = await discover_products_for_audit(
            discovery_url,
            max_products=2,  # smaller per competitor; cost guard
            market=market,
            persist=False,  # don't pollute prospect_products with cohort runs
        )
    except BrandDiscoveryError as exc:
        await record_competitor_run_completed(
            run_id=run_id, status="failed",
            error_message=f"discovery_failed: {exc!r}"[:2000],
        )
        summary["status"] = "failed"
        summary["reason"] = f"discovery_failed: {exc!r}"
        return summary
    except Exception as exc:  # noqa: BLE001
        await record_competitor_run_completed(
            run_id=run_id, status="failed",
            error_message=f"discovery_error: {exc!r}"[:2000],
        )
        summary["status"] = "failed"
        summary["reason"] = f"discovery_error: {exc!r}"
        return summary

    products = discovered.get("products") or []
    if not products:
        await record_competitor_run_completed(
            run_id=run_id, status="failed",
            error_message="discovery_zero_products",
        )
        summary["status"] = "failed"
        summary["reason"] = "discovery_zero_products"
        return summary

    # PR-2c: when category_override is set (parent's modal product_type
    # passed down from the cold-start route), force it onto every
    # discovered product so the audit's category_visibility_test asks
    # the SAME queries as the parent. Apples-to-apples cross-brand
    # mention matrix.
    #
    # Side effect: each cohort competitor's individual visibility/
    # attribution scores reflect "do they show up in the PARENT'S
    # category" — not "do they show up in their OWN native category."
    # That's the right shape for cohort comparison; surface in
    # evidence so BD operators understand the framing.
    category_used = None
    if category_override and category_override.strip():
        for p in products:
            p["product_type"] = category_override.strip()
        category_used = category_override.strip()
        logger.info(
            "competitor_orchestrator: forcing category_override=%r on %s products",
            category_used, brand,
        )

    try:
        out = await run_brand_report(
            merchant_name=discovered.get("merchant_name") or brand,
            merchant_domain=discovered.get("merchant_domain") or domain,
            products=products,
            provider="gemini",
            max_runs=max_runs,
            include_category_visibility=True,
            integration_state=None,
        )
    except Exception as exc:  # noqa: BLE001
        await record_competitor_run_completed(
            run_id=run_id, status="failed",
            error_message=f"audit_failed: {exc!r}"[:2000],
        )
        summary["status"] = "failed"
        summary["reason"] = f"audit_failed: {exc!r}"
        return summary

    agg = out.get("aggregate") or {}
    verdict_labels = [
        ((p.get("verdict") or {}).get("label") or "")
        for p in (out.get("per_product") or [])
    ]
    product_keys = [
        p.get("pdp_url") for p in products if p.get("pdp_url")
    ]

    # Stash the override (or None) into the report so the comparison
    # helper can surface "audited under: <category>" framing without
    # a schema change.
    if category_used is not None and isinstance(out, dict):
        out.setdefault("_cohort_meta", {})["category_used_for_audit"] = category_used

    await record_competitor_run_completed(
        run_id=run_id,
        status="succeeded",
        product_keys=product_keys,
        verdict_labels=[v for v in verdict_labels if v],
        visibility_score_avg=agg.get("avg_visibility"),
        attribution_score_avg=agg.get("avg_attribution"),
        category_visibility_score_avg=agg.get("avg_category_visibility"),
        report_jsonb=out,
    )
    summary["status"] = "succeeded"
    summary["run_id"] = run_id
    summary["visibility"] = agg.get("avg_visibility")
    summary["attribution"] = agg.get("avg_attribution")
    summary["category_used"] = category_used
    return summary


async def enqueue_competitor_audits(
    *,
    parent_audit_run_id: str,
    competitor_brands: List[str],
    market: str = "US",
    max_runs: int = 3,
    cohort_size: int = _DEFAULT_COHORT_SIZE,
    category_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Run cohort audits for the top-N competitor brands. Returns a
    summary dict. Designed to be awaited by a background task — the
    parent audit response should NOT block on this.

    `competitor_brands` is the ordered list from
    extract_category_competitors (most-cited first). We slice to
    cohort_size (capped at HARD_MAX_COHORT_SIZE).

    PR-2c: when `category_override` is provided (the parent audit's
    modal product_type), every cohort competitor's products get that
    product_type forced — so each cohort audit asks the SAME category
    queries as the parent. This produces an apples-to-apples
    cross-brand mention matrix in the cohort comparison endpoint.
    Without it, cohort audits run on each competitor's own
    auto-inferred product_type (Nordic Naturals → "fish oil
    supplements", SmartyPants → "Kids" — neither comparable to
    Grüns' "daily gummy vitamins").
    """
    from services.agent_center_llm_client import _get_per_merchant_semaphore

    if not parent_audit_run_id:
        return {"error": "parent_audit_run_id required"}
    if not competitor_brands:
        return {"parent_audit_run_id": parent_audit_run_id, "audited": 0}

    capped_size = max(1, min(int(cohort_size), _HARD_MAX_COHORT_SIZE))
    cohort = competitor_brands[:capped_size]

    # Semaphore inherited from agent_center_llm_client for this
    # synthetic merchant — bounds in-flight Gemini calls regardless of
    # how many competitors fire in parallel.
    sem = await _get_per_merchant_semaphore(f"cohort_{parent_audit_run_id}")

    async def _bounded(brand: str) -> Dict[str, Any]:
        async with sem:
            try:
                return await asyncio.wait_for(
                    _audit_one_competitor(
                        brand=brand,
                        parent_audit_run_id=parent_audit_run_id,
                        market=market,
                        max_runs=max_runs,
                        category_override=category_override,
                    ),
                    timeout=_PER_COMPETITOR_AUDIT_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                return {
                    "competitor_brand": brand,
                    "status": "failed",
                    "reason": "per_competitor_timeout",
                }
            except Exception as exc:  # noqa: BLE001
                return {
                    "competitor_brand": brand,
                    "status": "failed",
                    "reason": f"unexpected: {exc!r}",
                }

    results = await asyncio.gather(*[_bounded(b) for b in cohort])
    succeeded = sum(1 for r in results if r.get("status") == "succeeded")
    failed = sum(1 for r in results if r.get("status") == "failed")
    logger.info(
        "competitor_orchestrator: parent=%s cohort=%d succeeded=%d failed=%d",
        parent_audit_run_id, len(cohort), succeeded, failed,
    )
    return {
        "parent_audit_run_id": parent_audit_run_id,
        "cohort_size": len(cohort),
        "category_override": category_override,
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
    }
