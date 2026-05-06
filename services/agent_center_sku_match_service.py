"""
Agent Center — SKU Match Agent V1 (internal data-quality runner).

The SKU Match Agent verifies that what Pivota scraped + cached for a merchant
matches the merchant's source-of-truth. **V1** is a tighter scope: it only
performs *internal* data-quality checks against the data we already have in
`products_cache`. Live comparison against the merchant's store-platform API
(via pivota-acp adapters) is V1.5 work — that path needs adapter upgrades to
expose `export_normalized_skus(merchant_id)` per platform.

V1 issue types:

  - sku_missing                 — variant has no SKU
  - price_missing               — price is None / 0 / negative
  - currency_missing            — price.currency is missing or not a 3-letter code
  - image_missing               — no image_url AND images[] is empty
  - inventory_unknown           — inventory_quantity is None AND in_stock is None
  - stale_cache                 — cached_at older than `max_age_days` (default 7)

Each issue references a single product (`product_entity_id` =
`<merchant_id>|<platform>|<platform_product_id>`). Creating one issue per
problem (rather than one issue per product with a list of problems) keeps
triage and resolution-plan routing simple.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

from db.database import database
from services import agent_center_service as ac

logger = logging.getLogger(__name__)


SKU_MATCH_SCAN_MODE = "sku_match"

# Scoping defaults for a single V1 run.
DEFAULT_PRODUCT_LIMIT = 50
HARD_PRODUCT_LIMIT = 500
DEFAULT_STALE_DAYS = 7


def is_sku_match_scan_mode(scan_mode: str) -> bool:
    return scan_mode == SKU_MATCH_SCAN_MODE


def _decode_product_data(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, (bytes, bytearray)):
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw or "{}")
        except Exception:
            return {}
    return {}


def _check_product(product: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return a list of `(issue_type, severity, evidence)` dicts found on
    one product. Pure function — easy to test without DB."""
    findings: List[Dict[str, Any]] = []

    sku = (product.get("sku") or "").strip() if isinstance(product.get("sku"), str) else product.get("sku")
    variants = product.get("variants") or []
    has_top_sku = bool(sku)
    has_variant_sku = any(
        (v or {}).get("sku") and str((v or {}).get("sku")).strip()
        for v in (variants if isinstance(variants, list) else [])
    )
    if not has_top_sku and not has_variant_sku:
        findings.append(
            {
                "issue_type": "sku_missing",
                "severity": "high",
                "evidence": {
                    "top_level_sku": sku,
                    "variants_count": len(variants) if isinstance(variants, list) else 0,
                },
            }
        )

    price = product.get("price")
    try:
        price_num = float(price) if price is not None else None
    except (TypeError, ValueError):
        price_num = None
    if price_num is None or price_num <= 0:
        findings.append(
            {
                "issue_type": "price_missing",
                "severity": "high",
                "evidence": {"price": price},
            }
        )

    currency = product.get("currency")
    if not isinstance(currency, str) or len(currency.strip()) != 3:
        findings.append(
            {
                "issue_type": "currency_missing",
                "severity": "medium",
                "evidence": {"currency": currency},
            }
        )

    image_url = (product.get("image_url") or "").strip() if isinstance(product.get("image_url"), str) else None
    images = product.get("images") or []
    if not image_url and not (isinstance(images, list) and any(isinstance(i, str) and i.strip() for i in images)):
        findings.append(
            {
                "issue_type": "image_missing",
                "severity": "medium",
                "evidence": {"image_url": image_url, "images_count": len(images) if isinstance(images, list) else 0},
            }
        )

    inv = product.get("inventory_quantity")
    in_stock = product.get("in_stock")
    if inv is None and in_stock is None:
        findings.append(
            {
                "issue_type": "inventory_unknown",
                "severity": "medium",
                "evidence": {"inventory_quantity": inv, "in_stock": in_stock},
            }
        )

    return findings


async def _fetch_products_for_merchant(
    *,
    merchant_id: str,
    platform: Optional[str],
    limit: int,
) -> List[Dict[str, Any]]:
    """Pull a sample of cached products for one merchant. Returns the raw
    `products_cache` rows decoded into dicts."""
    clauses = ["merchant_id = :merchant_id"]
    params: Dict[str, Any] = {"merchant_id": merchant_id, "limit": int(limit)}
    if platform:
        clauses.append("platform = :platform")
        params["platform"] = platform
    rows = await database.fetch_all(
        f"""
        SELECT
            id,
            merchant_id,
            platform,
            platform_product_id,
            product_data,
            cached_at,
            expires_at
        FROM products_cache
        WHERE {' AND '.join(clauses)}
        ORDER BY cached_at DESC
        LIMIT :limit
        """,
        params,
    )
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        data = dict(row) if row is not None else {}
        data["product_data_decoded"] = _decode_product_data(data.get("product_data"))
        out.append(data)
    return out


async def run_sku_match(scan_target_id: str) -> Dict[str, Any]:
    """Drive a SKU Match run.

    Idempotent on the usage event (`sku_match:{scan_target_id}:v1`); replaying
    against a `succeeded` row reuses the same usage row but creates fresh
    issue rows (V1 doesn't dedupe issue creation — replays are rare and
    duplicate issues are easy to spot in the UI).
    """
    target = await ac.get_scan_target(scan_target_id=scan_target_id)
    if target is None:
        raise LookupError(f"scan_target not found: {scan_target_id}")

    # `running` is accepted because the route handler may have already
    # acquired the run-lock (atomic flip to running) before scheduling this
    # background task. Direct callers (tests, manual debugging) typically
    # pass `queued` / `stub_complete` / `succeeded` / `failed`.
    if target["status"] not in {"queued", "stub_complete", "succeeded", "failed", "running"}:
        raise ValueError(
            f"scan_target {scan_target_id} is in status={target['status']}, "
            "expected one of queued/stub_complete/succeeded/failed/running"
        )

    if target["scan_mode"] != SKU_MATCH_SCAN_MODE:
        raise ValueError(
            f"scan_target {scan_target_id} has scan_mode={target['scan_mode']!r}; "
            f"this runner only handles {SKU_MATCH_SCAN_MODE!r}"
        )

    # V1.5 dispatch: if the merchant requested a live drift comparison,
    # delegate to the live runner. The internal-data-quality runner stays
    # the default for backward compat (existing scan_targets without an
    # explicit `mode` field continue to work unchanged).
    payload = target.get("payload") or {}
    options = (payload.get("options") if isinstance(payload.get("options"), dict) else {}) or {}
    requested_mode = options.get("mode")
    if isinstance(requested_mode, str) and requested_mode.strip().lower() == "live":
        from services.agent_center_sku_match_live_service import run_sku_match_live
        return await run_sku_match_live(scan_target_id)

    merchant_id = target["merchant_id"]
    store_id = target["store_id"]
    payload = target.get("payload") or {}
    options = (payload.get("options") if isinstance(payload.get("options"), dict) else {}) or {}
    platform = options.get("platform") if isinstance(options.get("platform"), str) else None
    limit = max(1, min(int(options.get("limit") or DEFAULT_PRODUCT_LIMIT), HARD_PRODUCT_LIMIT))
    stale_days = max(1, int(options.get("max_age_days") or DEFAULT_STALE_DAYS))

    # 1. Mark running.
    await ac.transition_scan_target(
        scan_target_id=scan_target_id,
        status="running",
        started_at=ac.utcnow(),
    )

    # 2. Pull cached products + scan each.
    try:
        products = await _fetch_products_for_merchant(
            merchant_id=merchant_id, platform=platform, limit=limit,
        )
    except Exception as exc:
        logger.error(
            "sku_match: products_cache query failed for merchant=%s scan_target=%s: %s",
            merchant_id, scan_target_id, exc, exc_info=True,
        )
        await ac.transition_scan_target(
            scan_target_id=scan_target_id,
            status="failed",
            finished_at=ac.utcnow(),
            payload_patch={"error": {"kind": "products_cache_query", "message": str(exc)}},
        )
        raise

    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)

    findings: List[Dict[str, Any]] = []
    products_by_status = {"checked": 0, "with_findings": 0}
    # Heartbeat cadence: every N products through the loop. Pure-function
    # work per product is microseconds, so a single heartbeat per run is
    # plenty in normal cases; we bump it every 100 to cover hand-tuned
    # max-product runs that walk a large catalog.
    HEARTBEAT_EVERY_N = 100
    for idx, row in enumerate(products):
        if idx > 0 and idx % HEARTBEAT_EVERY_N == 0:
            await ac.heartbeat_scan_target(scan_target_id=scan_target_id)
        product_data = row.get("product_data_decoded") or {}
        product_findings = _check_product(product_data)

        cached_at = row.get("cached_at")
        if isinstance(cached_at, datetime):
            ts = cached_at if cached_at.tzinfo else cached_at.replace(tzinfo=timezone.utc)
            if ts < stale_cutoff:
                product_findings.append(
                    {
                        "issue_type": "stale_cache",
                        "severity": "low",
                        "evidence": {
                            "cached_at": ts.isoformat(),
                            "max_age_days": stale_days,
                        },
                    }
                )

        if product_findings:
            products_by_status["with_findings"] += 1
            entity_id = (
                f"{row.get('merchant_id')}|{row.get('platform')}|{row.get('platform_product_id')}"
            )
            for f in product_findings:
                findings.append({**f, "product_entity_id": entity_id})
        products_by_status["checked"] += 1

    # 3. Record usage event (idempotent).
    usage_event = await ac.record_usage_event(
        idempotency_key=f"sku_match:{scan_target_id}:v1",
        merchant_id=merchant_id,
        store_id=store_id,
        scan_target_id=scan_target_id,
        agent_type="sku_match",
        workflow_type="sku_match_readiness",
        event_type="sku_match_credit",
        provider="internal",
        scan_mode=SKU_MATCH_SCAN_MODE,
        billing_mode="preview_only",
        billing_status="not_invoiced",
        quantity=products_by_status["checked"],
        payload={
            "products_checked": products_by_status["checked"],
            "products_with_findings": products_by_status["with_findings"],
            "limit": limit,
            "platform": platform,
            "max_age_days": stale_days,
        },
    )

    # 4. Persist findings as issues.
    created_issue_ids: List[str] = []
    for finding in findings:
        try:
            issue = await ac.create_issue(
                merchant_id=merchant_id,
                store_id=store_id,
                scan_target_id=scan_target_id,
                issue_type=finding["issue_type"],
                severity=finding.get("severity", "medium"),
                product_entity_id=finding.get("product_entity_id"),
                payload={"evidence": finding.get("evidence", {})},
            )
            created_issue_ids.append(issue["id"])
        except ValueError as exc:
            logger.warning(
                "sku_match: skipping malformed finding for scan_target=%s: %s",
                scan_target_id, exc,
            )

    # 5. Mark succeeded.
    final = await ac.transition_scan_target(
        scan_target_id=scan_target_id,
        status="succeeded",
        finished_at=ac.utcnow(),
        payload_patch={
            "run": {
                "products_checked": products_by_status["checked"],
                "products_with_findings": products_by_status["with_findings"],
                "issue_ids": created_issue_ids,
                "usage_event_id": usage_event.get("id"),
                "limit": limit,
                "platform": platform,
                "max_age_days": stale_days,
            }
        },
    )
    logger.info(
        "sku_match run complete: scan_target=%s checked=%d findings=%d issues=%d",
        scan_target_id,
        products_by_status["checked"],
        products_by_status["with_findings"],
        len(created_issue_ids),
    )
    return final


# Exposed for unit tests.
def check_product_for_findings(product: Mapping[str, Any]) -> List[Dict[str, Any]]:
    return _check_product(dict(product))
