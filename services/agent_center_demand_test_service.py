"""
Agent Center — Demand Test Agent V1 runner.

This is the first concrete agent built on top of `agent_center_service.py`.
A run:

  1. Marks the scan target `running`.
  2. Calls `agent_center_llm_client.probe(...)` (which hits the
     `/internal/agent-center/llm-probe` endpoint on PIVOTA-Agent, or a
     local mock when `PIVOTA_AGENT_INTERNAL_API_KEY` is unset).
  3. Records a usage event on the shared meter (idempotent on
     `idempotency_key`).
  4. Persists each finding as a row in `agent_center_issues`.
  5. Marks the scan target `succeeded` (or `failed` on error / `stub_complete`
     when the upstream returned mock data so it's clear nothing real ran).

This replaces the V1 stub that shipped in #268. Routes import
`run_demand_test`; `run_demand_test_stub` stays as a thin alias so the
existing route imports keep working until the route layer is updated in
the same PR.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from config.settings import settings
from db.database import database
from services import agent_center_llm_client as llm_client
from services import agent_center_service as ac

logger = logging.getLogger(__name__)


# Maximum number of times we'll fall back to a products_cache lookup
# during a single run. Currently 1 (one lookup per run); future work may
# enrich `context.product` with extra fields without changing the call
# site, so the constant exists for clarity.
_PRODUCT_LOOKUP_BUDGET = 1


def _parse_product_entity_id(entity_id: str) -> Optional[Dict[str, str]]:
    """Decode the canonical entity_id format.

    Format: `<merchant_id>|<platform>|<platform_product_id>`. Returns the
    three fields as a dict, or None when the format doesn't match (e.g.
    a free-form entity_id that pre-dates the canonical format)."""
    if not isinstance(entity_id, str):
        return None
    parts = entity_id.split("|")
    if len(parts) != 3 or not all(p.strip() for p in parts):
        return None
    return {
        "merchant_id": parts[0].strip(),
        "platform": parts[1].strip(),
        "platform_product_id": parts[2].strip(),
    }


async def _lookup_product_for_query_generation(
    *, merchant_id: str, platform: str, platform_product_id: str,
) -> Optional[Dict[str, Any]]:
    """Look up products_cache for the (merchant, platform, product_id) and
    return a `{title, vendor, product_type}` dict the PIVOTA-Agent probe
    can use to auto-generate buyer-style queries. Returns None when the
    row is missing or the cached payload doesn't have a usable title.

    Best-effort — failures are logged but don't fail the run. The probe
    will fall back to its V1 behavior (using product_entity_id as the
    sole query) if we return None."""
    try:
        row = await database.fetch_one(
            """
            SELECT product_data
            FROM products_cache
            WHERE merchant_id = :merchant_id
              AND platform = :platform
              AND platform_product_id = :platform_product_id
            ORDER BY cached_at DESC
            LIMIT 1
            """,
            {
                "merchant_id": merchant_id,
                "platform": platform,
                "platform_product_id": platform_product_id,
            },
        )
    except Exception as exc:
        logger.warning(
            "demand_test product lookup failed (%s|%s|%s): %s",
            merchant_id, platform, platform_product_id, exc,
        )
        return None
    if not row:
        return None
    # Reuse the same payload decoder as the sku-match runner — handles
    # bytes/str/dict shapes uniformly.
    from services.agent_center_sku_match_service import _decode_product_data
    data = _decode_product_data(dict(row).get("product_data"))
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    out: Dict[str, Any] = {"title": title.strip()}
    vendor = data.get("vendor")
    if isinstance(vendor, str) and vendor.strip():
        out["vendor"] = vendor.strip()
    product_type = data.get("product_type")
    if isinstance(product_type, str) and product_type.strip():
        out["product_type"] = product_type.strip()
    return out


async def _enrich_context_with_product_info(context: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort: when the operator didn't write any queries and an
    entity_id is present, fill `context.product` from products_cache so
    the PIVOTA-Agent probe can derive buyer-style queries from real
    product attributes (PR 15) instead of using the meaningless
    pipe-delimited entity_id (V1 fallback).

    Returns the (possibly mutated) context dict. Never raises — lookup
    failures degrade gracefully to the V1 path."""
    if not isinstance(context, dict):
        return context
    queries = context.get("queries")
    if isinstance(queries, list) and len(queries) > 0:
        # Operator wrote queries — auto-generation is bypassed in the probe
        # anyway. Skip the lookup to save a SQL roundtrip.
        return context
    if isinstance(context.get("product"), dict) and context["product"].get("title"):
        # Already populated (form sent it directly).
        return context
    entity_id = context.get("product_entity_id")
    parsed = _parse_product_entity_id(entity_id) if entity_id else None
    if not parsed:
        return context
    product = await _lookup_product_for_query_generation(
        merchant_id=parsed["merchant_id"],
        platform=parsed["platform"],
        platform_product_id=parsed["platform_product_id"],
    )
    if product:
        # Mutate a shallow copy so the scan_target's stored context isn't
        # accidentally extended in-memory beyond what we explicitly persist.
        out = dict(context)
        out["product"] = product
        logger.info(
            "demand_test: enriched context.product from products_cache for entity=%s (title=%r)",
            entity_id, product["title"],
        )
        return out
    return context


# Demand-test issue types (lifted from the V1 spec). Aligned with the
# server-side fallback in `services/agent_center_llm_client.py` and with
# `PRIMARY_ISSUE_TYPE_BY_SCAN_MODE` in
# `PIVOTA-Agent/src/internal/agentCenterLlmProbe.js`.
ISSUE_TYPE_BY_SCAN_MODE: Dict[str, str] = {
    "open_product_visibility_test": "ai_visibility_loss",
    "merchant_store_attribution_test": "merchant_store_attribution_gap",
    "pivota_pdp_attribution_test": "pivota_pdp_attribution_gap",
    "search_grounded_product_discovery_test": "ai_visibility_loss",
}


def is_demand_test_scan_mode(scan_mode: str) -> bool:
    return scan_mode in ac.DEMAND_TEST_SCAN_MODES


def _resolve_provider_for_settings() -> str:
    """Pick the LLM provider to ask the upstream for, based on settings.

    `pivota_agent_center_mock_gemini=true` (V1 default) → ask the upstream
    for `mock` provider. Once Gemini prompts are calibrated, flip the
    setting to ship `gemini`.
    """
    return "mock" if settings.pivota_agent_center_mock_gemini else "gemini"


def _final_status_from_probe_result(result: Dict[str, Any]) -> str:
    """Decide whether the scan target ends in `succeeded` or `stub_complete`.

    `succeeded` = a real LLM (gemini) actually answered.
    `stub_complete` = the upstream returned a mock or local-mock result, so
    no real LLM call landed; visible distinction matters when we audit
    coverage of the V1 pilot.
    """
    provider = str(result.get("provider") or "").lower()
    if provider in {"gemini"}:
        return "succeeded"
    return "stub_complete"


async def run_demand_test(scan_target_id: str) -> Dict[str, Any]:
    """Drive a Demand Test run end-to-end.

    Idempotent on the usage event (`demand_test:{scan_target_id}:v1`); a
    replay against a `succeeded` / `stub_complete` row reuses the same
    idempotency key and the existing usage row wins. Issue rows however are
    NOT deduplicated server-side in V1 — replays will produce additional
    issue rows. That's an acceptable V1 trade-off given the agent normally
    runs once per `scan_target_id`.
    """
    target = await ac.get_scan_target(scan_target_id=scan_target_id)
    if target is None:
        raise LookupError(f"scan_target not found: {scan_target_id}")

    # `running` is accepted because the route handler may have already
    # acquired the run-lock (atomic flip to running) before scheduling this
    # background task. Re-runs from succeeded/failed/stub_complete are also
    # accepted — matches the set RUNNABLE_PRIOR_STATUSES + 'running'.
    if target["status"] not in {"queued", "stub_complete", "succeeded", "failed", "running"}:
        raise ValueError(
            f"scan_target {scan_target_id} is in status={target['status']}, "
            "expected one of queued/stub_complete/succeeded/failed/running"
        )

    scan_mode = target["scan_mode"]
    if not is_demand_test_scan_mode(scan_mode):
        raise ValueError(
            f"scan_target {scan_target_id} has scan_mode={scan_mode!r}; "
            "this runner only handles demand-test scan modes"
        )

    merchant_id = target["merchant_id"]
    store_id = target["store_id"]
    context = (target.get("payload") or {}).get("context") or {}
    requested_provider = _resolve_provider_for_settings()

    # 1. Mark running.
    await ac.transition_scan_target(
        scan_target_id=scan_target_id,
        status="running",
        started_at=ac.utcnow(),
    )

    # 2. Best-effort: enrich context.product from products_cache so the
    #    upstream probe can derive buyer-style queries from real attributes
    #    instead of falling back to the meaningless product_entity_id.
    context = await _enrich_context_with_product_info(context)

    # 3. Call the LLM probe. Heartbeat right before so a slow Gemini call
    #    (real probes can take 30-60s) doesn't make the row look stuck.
    await ac.heartbeat_scan_target(scan_target_id=scan_target_id)
    try:
        result = await llm_client.probe(
            scan_mode=scan_mode,
            scan_target_id=scan_target_id,
            merchant_id=merchant_id,
            store_id=store_id,
            context=context,
            provider=requested_provider,
            max_runs=int((target.get("payload") or {}).get("max_runs") or 3),
            # Demand-test deliberately uses stub responses on free-tier
            # preview when PIVOTA_AGENT_INTERNAL_API_KEY is unset — the
            # runner marks results as `stub_complete` (not `succeeded`)
            # so callers can distinguish real Gemini runs from stubs.
            # Merchant audit + BD report MUST NOT pass this flag.
            allow_local_mock=True,
        )
    except llm_client.AgentCenterLlmClientError as exc:
        # Upstream failure — record terminal state + bubble up.
        logger.error(
            "demand_test run failed for scan_target=%s: %s",
            scan_target_id, exc,
        )
        await ac.transition_scan_target(
            scan_target_id=scan_target_id,
            status="failed",
            finished_at=ac.utcnow(),
            payload_patch={"error": {"message": str(exc), "kind": "llm_probe"}},
        )
        raise

    # 3. Record usage event (first-write-wins idempotent).
    usage_event = await ac.record_usage_event(
        idempotency_key=f"demand_test:{scan_target_id}:v1",
        merchant_id=merchant_id,
        store_id=store_id,
        scan_target_id=scan_target_id,
        agent_type="demand_test",
        workflow_type=_workflow_type_for_scan_mode(scan_mode),
        event_type="demand_test_credit",
        provider=str(result.get("provider") or "unknown"),
        scan_mode=scan_mode,
        billing_mode="preview_only",
        billing_status="not_invoiced",
        quantity=int(result.get("runs_count") or 1),
        payload={
            "scores": result.get("scores") or {},
            "usage": result.get("usage") or {},
        },
    )

    # 4. Persist findings as issues.
    created_issue_ids = []
    for finding in (result.get("findings") or []):
        if not isinstance(finding, dict):
            continue
        issue_type = str(finding.get("issue_type") or "").strip()
        if not issue_type:
            continue
        severity = str(finding.get("severity") or "medium").lower()
        if severity not in ac.ISSUE_SEVERITIES:
            severity = "medium"
        evidence = finding.get("evidence")
        issue_payload: Dict[str, Any] = {
            "scan_mode": scan_mode,
            "provider": result.get("provider"),
            "evidence": evidence if isinstance(evidence, dict) else {"raw": evidence},
        }
        try:
            issue = await ac.create_issue(
                merchant_id=merchant_id,
                store_id=store_id,
                scan_target_id=scan_target_id,
                issue_type=issue_type,
                severity=severity,
                payload=issue_payload,
            )
            created_issue_ids.append(issue["id"])
        except ValueError as exc:
            # Bad finding shape — log and continue rather than aborting the
            # whole run. Other findings can still be useful.
            logger.warning(
                "demand_test: skipping malformed finding for scan_target=%s: %s",
                scan_target_id, exc,
            )

    # 5. Mark final status with a payload patch summarising the run.
    final_status = _final_status_from_probe_result(result)
    final = await ac.transition_scan_target(
        scan_target_id=scan_target_id,
        status=final_status,
        finished_at=ac.utcnow(),
        payload_patch={
            "run": {
                "provider": result.get("provider"),
                "runs_count": result.get("runs_count"),
                "scores": result.get("scores"),
                "usage_event_id": usage_event.get("id"),
                "issue_ids": created_issue_ids,
            }
        },
    )
    logger.info(
        "demand_test run complete: scan_target=%s scan_mode=%s status=%s issues=%d",
        scan_target_id, scan_mode, final_status, len(created_issue_ids),
    )
    return final


# Backward-compat alias. The route layer can keep importing
# `run_demand_test_stub` until it switches to the new name; either way the
# behaviour is the real runner.
run_demand_test_stub = run_demand_test


def _workflow_type_for_scan_mode(scan_mode: str) -> str:
    if scan_mode == "open_product_visibility_test":
        return "open_product_visibility"
    if scan_mode == "merchant_store_attribution_test":
        return "merchant_store_attribution"
    if scan_mode == "pivota_pdp_attribution_test":
        return "pivota_pdp_attribution"
    if scan_mode == "search_grounded_product_discovery_test":
        return "search_grounded_product_discovery"
    return "demand_test"
