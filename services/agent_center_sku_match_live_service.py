"""
Agent Center — SKU Match Agent V1.5 (live drift detection).

Where V1 (`agent_center_sku_match_service`) only inspects what we already
have cached, V1.5 fetches the merchant's live source-of-truth via their
platform API (Shopify, Wix, etc. — see `adapters.product_adapters`) and
diffs it against `products_cache` to surface real drift.

Drift findings:

  - live_price_drift          — price differs between live and cache
  - live_title_drift          — title differs (rename / typo fix upstream)
  - live_inventory_drift      — in_stock disagrees (live in stock, cache out
                                of stock or vice versa)
  - live_image_drift          — primary image URL differs
  - live_sku_missing_in_cache — live product the cache has no row for
  - live_product_unpublished  — cached product no longer exists upstream
                                (or the live fetch couldn't find its
                                platform_product_id)

V1.5 supports Shopify only. Other platforms raise `LiveAdapterUnsupportedError`
which the runner translates to a `failed` scan_target with a descriptive
payload — the route + UI can guide the merchant to upgrade their connector.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Tuple

from db.database import database
from services import agent_center_service as ac

logger = logging.getLogger(__name__)


SUPPORTED_LIVE_PLATFORMS: tuple = ("shopify", "wix", "woocommerce", "bigcommerce")

# Anything below this is considered "same price" — guards against float
# noise in upstream platforms that store prices as strings then parse them.
_PRICE_EPSILON = 0.01


class LiveAdapterUnsupportedError(Exception):
    """Raised when the merchant's platform doesn't have a live adapter yet."""


# ---------------------------------------------------------------------------
# Live-fetch step. Indirection so tests can monkeypatch a fake fetcher
# without spinning up a real Shopify mock server.
# ---------------------------------------------------------------------------


async def _resolve_live_credentials(*, merchant_id: str, platform: str) -> Dict[str, Any]:
    """Resolve the merchant's connected-store credentials for a given
    platform. Shopify reuses the existing OAuth-aware helper (handles
    token refresh); other platforms read from `merchant_stores` and
    parse the JSON credentials blob stored in `api_key`.

    Adding a new platform = adding a branch here. The returned dict is
    passed through to `adapters.product_adapters.fetch_merchant_products`
    which knows the per-platform field names."""
    if platform == "shopify":
        from services.shopify_products_sync import _get_shopify_store_credentials
        return await _get_shopify_store_credentials(merchant_id)
    if platform in {"wix", "woocommerce", "bigcommerce"}:
        return await _resolve_generic_store_credentials(
            merchant_id=merchant_id, platform=platform,
        )
    raise LiveAdapterUnsupportedError(
        f"live SKU match does not yet support platform {platform!r}; "
        f"supported: {list(SUPPORTED_LIVE_PLATFORMS)}"
    )


async def _resolve_generic_store_credentials(
    *, merchant_id: str, platform: str,
) -> Dict[str, Any]:
    """Read a connected merchant_stores row and unpack its api_key JSON
    blob. Returns the credentials dict the adapter dispatcher expects.

    Different platforms store different fields inside the blob:
      wix          → {"site_id": "...", "api_key": "..."}
      woocommerce  → {"store_url": "...", "consumer_key": "...", "consumer_secret": "..."}
      bigcommerce  → {"store_hash": "...", "access_token": "...", "client_id": "..."}

    We pass everything through; missing fields surface as 4xx upstream
    when the platform's API rejects the request."""
    from services.merchant_store_service import parse_api_credentials

    row = await database.fetch_one(
        """
        SELECT store_id, domain, api_key, status
        FROM merchant_stores
        WHERE merchant_id = :merchant_id
          AND platform = :platform
          AND status IN ('active', 'connected')
        ORDER BY connected_at DESC NULLS LAST
        LIMIT 1
        """,
        {"merchant_id": merchant_id, "platform": platform},
    )
    if not row:
        raise ValueError(
            f"no connected {platform!r} store for merchant {merchant_id!r}"
        )
    data = dict(row)
    blob = parse_api_credentials(data.get("api_key") or "")
    creds: Dict[str, Any] = dict(blob) if blob else {}
    # Backfill domain → store_url for woocommerce convenience (the merchants-
    # portal sometimes records it on the merchant_stores.domain column instead
    # of the JSON blob).
    if platform == "woocommerce" and not creds.get("store_url") and data.get("domain"):
        creds["store_url"] = data["domain"]
    if platform == "wix" and not creds.get("site_id") and data.get("store_id"):
        creds["site_id"] = data["store_id"]
    return creds


async def _fetch_live_products(
    *, merchant_id: str, platform: str, limit: int,
) -> List[Dict[str, Any]]:
    """Fetch a sample of live products from the merchant's platform.
    Returns dicts (StandardProduct.dict()) — easier to compare against
    products_cache rows than mixing dicts + pydantic models."""
    from adapters.product_adapters import fetch_merchant_products

    credentials = await _resolve_live_credentials(
        merchant_id=merchant_id, platform=platform,
    )
    products, _next, error = await fetch_merchant_products(
        merchant_id=merchant_id,
        platform=platform,
        credentials=credentials,
        limit=limit,
    )
    if error:
        # Re-raise so the runner can mark the scan failed with a useful
        # error payload. Auth / rate-limit errors are already raised as
        # specific exceptions inside the adapter.
        raise RuntimeError(f"live fetch error: {error}")
    return [p.dict() if hasattr(p, "dict") else dict(p) for p in (products or [])]


# ---------------------------------------------------------------------------
# Cached side: pull existing products_cache rows, indexed by entity key.
# ---------------------------------------------------------------------------


async def _fetch_cached_products(
    *, merchant_id: str, platform: str, limit: int,
) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        SELECT id, merchant_id, platform, platform_product_id, product_data, cached_at
        FROM products_cache
        WHERE merchant_id = :merchant_id
          AND platform = :platform
        ORDER BY cached_at DESC
        LIMIT :limit
        """,
        {"merchant_id": merchant_id, "platform": platform, "limit": int(limit)},
    )
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        d = dict(row)
        # Lazy-import to avoid circulars; reuse the same decoder V1 uses.
        from services.agent_center_sku_match_service import _decode_product_data
        d["product_data_decoded"] = _decode_product_data(d.get("product_data"))
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Diff: pure function, easy to unit-test.
# ---------------------------------------------------------------------------


def _entity_key(*, merchant_id: str, platform: str, platform_product_id: Any) -> str:
    return f"{merchant_id}|{platform}|{platform_product_id}"


def _norm_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _approx_equal_price(a: Any, b: Any) -> bool:
    try:
        af = float(a) if a is not None else None
        bf = float(b) if b is not None else None
    except (TypeError, ValueError):
        return False
    if af is None or bf is None:
        return af is None and bf is None
    return math.isclose(af, bf, abs_tol=_PRICE_EPSILON)


def diff_live_vs_cached(
    *,
    live: List[Dict[str, Any]],
    cached: List[Dict[str, Any]],
    merchant_id: str,
    platform: str,
) -> List[Dict[str, Any]]:
    """Return a list of finding dicts (one per drift) — pure function.

    Each finding: {issue_type, severity, product_entity_id, evidence}.
    """
    findings: List[Dict[str, Any]] = []

    cached_by_id: Dict[str, Dict[str, Any]] = {}
    for row in cached:
        ppid = row.get("platform_product_id")
        if ppid is None:
            continue
        cached_by_id[str(ppid)] = row

    live_by_id: Dict[str, Dict[str, Any]] = {}
    for prod in live:
        # StandardProduct uses `id` for the platform product id.
        ppid = prod.get("id") or prod.get("product_id")
        if ppid is None:
            continue
        live_by_id[str(ppid)] = prod

    # 1. Live product not in cache → discovery gap (high severity: anything
    #    we surface in Pivota will miss this product entirely until we
    #    refresh the cache).
    for ppid, live_p in live_by_id.items():
        if ppid in cached_by_id:
            continue
        findings.append({
            "issue_type": "live_sku_missing_in_cache",
            "severity": "high",
            "product_entity_id": _entity_key(
                merchant_id=merchant_id, platform=platform, platform_product_id=ppid,
            ),
            "evidence": {
                "platform_product_id": ppid,
                "live_title": _norm_str(live_p.get("title")),
                "live_price": live_p.get("price"),
            },
        })

    # 2. Cached product not in live → unpublished or deleted upstream.
    #    Medium severity: not actively wrong but the cache is lying.
    for ppid, cached_p in cached_by_id.items():
        if ppid in live_by_id:
            continue
        cached_data = cached_p.get("product_data_decoded") or {}
        findings.append({
            "issue_type": "live_product_unpublished",
            "severity": "medium",
            "product_entity_id": _entity_key(
                merchant_id=merchant_id, platform=platform, platform_product_id=ppid,
            ),
            "evidence": {
                "platform_product_id": ppid,
                "cached_title": _norm_str(cached_data.get("title")),
                "cached_at": str(cached_p.get("cached_at")) if cached_p.get("cached_at") else None,
            },
        })

    # 3. Field-level drift on products that exist in both.
    for ppid in (live_by_id.keys() & cached_by_id.keys()):
        live_p = live_by_id[ppid]
        cached_data = cached_by_id[ppid].get("product_data_decoded") or {}
        entity_id = _entity_key(
            merchant_id=merchant_id, platform=platform, platform_product_id=ppid,
        )

        # Price drift — critical: agents quote outdated prices, customers
        # see one price in checkout vs another at PDP.
        if not _approx_equal_price(live_p.get("price"), cached_data.get("price")):
            findings.append({
                "issue_type": "live_price_drift",
                "severity": "critical",
                "product_entity_id": entity_id,
                "evidence": {
                    "live_price": live_p.get("price"),
                    "cached_price": cached_data.get("price"),
                    "currency": _norm_str(live_p.get("currency"))
                                 or _norm_str(cached_data.get("currency")),
                },
            })

        # Title drift — low severity (often just a copy edit upstream).
        live_title = _norm_str(live_p.get("title"))
        cached_title = _norm_str(cached_data.get("title"))
        if live_title and cached_title and live_title != cached_title:
            findings.append({
                "issue_type": "live_title_drift",
                "severity": "low",
                "product_entity_id": entity_id,
                "evidence": {"live_title": live_title, "cached_title": cached_title},
            })

        # Image drift — medium severity (UI quality).
        live_image = _norm_str(live_p.get("image_url"))
        cached_image = _norm_str(cached_data.get("image_url"))
        if live_image and cached_image and live_image != cached_image:
            findings.append({
                "issue_type": "live_image_drift",
                "severity": "medium",
                "product_entity_id": entity_id,
                "evidence": {"live_image": live_image, "cached_image": cached_image},
            })

        # Inventory drift — high severity (out-of-stock that we sell as
        # in-stock leads to refunds).
        live_in_stock = live_p.get("in_stock")
        cached_in_stock = cached_data.get("in_stock")
        if (
            isinstance(live_in_stock, bool)
            and isinstance(cached_in_stock, bool)
            and live_in_stock != cached_in_stock
        ):
            findings.append({
                "issue_type": "live_inventory_drift",
                "severity": "high",
                "product_entity_id": entity_id,
                "evidence": {
                    "live_in_stock": live_in_stock,
                    "cached_in_stock": cached_in_stock,
                    "live_inventory_quantity": live_p.get("inventory_quantity"),
                    "cached_inventory_quantity": cached_data.get("inventory_quantity"),
                },
            })

    return findings


# ---------------------------------------------------------------------------
# Runner: drives a sku_match scan_target through running → succeeded.
# ---------------------------------------------------------------------------


async def run_sku_match_live(scan_target_id: str) -> Dict[str, Any]:
    """Live-comparison runner. Pulled from `run_sku_match` when
    `payload.options.mode == 'live'` is set on the scan_target."""
    target = await ac.get_scan_target(scan_target_id=scan_target_id)
    if target is None:
        raise LookupError(f"scan_target not found: {scan_target_id}")

    merchant_id = target["merchant_id"]
    store_id = target["store_id"]
    payload = target.get("payload") or {}
    options = (payload.get("options") if isinstance(payload.get("options"), dict) else {}) or {}
    platform = (options.get("platform") if isinstance(options.get("platform"), str) else "shopify").lower()
    limit = max(1, min(int(options.get("limit") or 50), 500))

    if platform not in SUPPORTED_LIVE_PLATFORMS:
        await ac.transition_scan_target(
            scan_target_id=scan_target_id,
            status="failed",
            finished_at=ac.utcnow(),
            payload_patch={
                "error": {
                    "kind": "unsupported_platform",
                    "platform": platform,
                    "supported": list(SUPPORTED_LIVE_PLATFORMS),
                    "message": (
                        f"live SKU match V1.5 supports only {SUPPORTED_LIVE_PLATFORMS}; "
                        f"{platform!r} is on the roadmap"
                    ),
                },
            },
        )
        raise LiveAdapterUnsupportedError(
            f"platform {platform!r} not supported for live SKU match"
        )

    # Mark running (no-op if route already acquired the lock).
    if target["status"] != "running":
        await ac.transition_scan_target(
            scan_target_id=scan_target_id, status="running", started_at=ac.utcnow(),
        )

    # Heartbeat right before the multi-page platform fetch — this is the
    # slowest section (Shopify pagination over hundreds of products can
    # take 10-30s) and the one most likely to make a healthy run look
    # stuck if the stale threshold gets tightened later.
    await ac.heartbeat_scan_target(scan_target_id=scan_target_id)
    try:
        live_products, cached_products = await _fetch_both(
            merchant_id=merchant_id, platform=platform, limit=limit,
        )
    except LiveAdapterUnsupportedError:
        # already transitioned above — re-raise.
        raise
    except Exception as exc:
        logger.error(
            "sku_match_live: fetch failed merchant=%s scan_target=%s: %s",
            merchant_id, scan_target_id, exc, exc_info=True,
        )
        await ac.transition_scan_target(
            scan_target_id=scan_target_id,
            status="failed",
            finished_at=ac.utcnow(),
            payload_patch={"error": {"kind": "live_fetch", "message": str(exc)}},
        )
        raise

    # After the slow fetch — heartbeat again before the in-memory diff so
    # the runner's progress is visible even on smaller catalogs.
    await ac.heartbeat_scan_target(scan_target_id=scan_target_id)
    findings = diff_live_vs_cached(
        live=live_products,
        cached=cached_products,
        merchant_id=merchant_id,
        platform=platform,
    )

    # Idempotent usage event — distinct key from the V1 internal runner so
    # the same scan_target isn't double-counted if a merchant runs both
    # modes back-to-back. (We currently only run one mode per scan_target,
    # but make the key unambiguous anyway.)
    usage_event = await ac.record_usage_event(
        idempotency_key=f"sku_match_live:{scan_target_id}:v1",
        merchant_id=merchant_id,
        store_id=store_id,
        scan_target_id=scan_target_id,
        agent_type="sku_match",
        workflow_type="sku_match_live_drift",
        event_type="sku_match_live_credit",
        provider="merchant_platform",
        scan_mode="sku_match",
        billing_mode="preview_only",
        billing_status="not_invoiced",
        quantity=len(live_products),
        payload={
            "live_count": len(live_products),
            "cached_count": len(cached_products),
            "findings_count": len(findings),
            "platform": platform,
        },
    )

    created_issue_ids: List[str] = []
    for f in findings:
        try:
            issue = await ac.create_issue(
                merchant_id=merchant_id,
                store_id=store_id,
                scan_target_id=scan_target_id,
                issue_type=f["issue_type"],
                severity=f["severity"],
                product_entity_id=f.get("product_entity_id"),
                payload={"evidence": f.get("evidence", {})},
            )
            created_issue_ids.append(issue["id"])
        except ValueError as exc:
            logger.warning(
                "sku_match_live: skipping malformed finding scan_target=%s: %s",
                scan_target_id, exc,
            )

    final = await ac.transition_scan_target(
        scan_target_id=scan_target_id,
        status="succeeded",
        finished_at=ac.utcnow(),
        payload_patch={
            "run": {
                "mode": "live",
                "platform": platform,
                "live_count": len(live_products),
                "cached_count": len(cached_products),
                "findings_count": len(findings),
                "issue_ids": created_issue_ids,
                "usage_event_id": usage_event.get("id"),
                "limit": limit,
            },
        },
    )
    logger.info(
        "sku_match_live run complete: scan_target=%s live=%d cached=%d findings=%d",
        scan_target_id, len(live_products), len(cached_products), len(findings),
    )
    return final


async def _fetch_both(
    *, merchant_id: str, platform: str, limit: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Tiny indirection so tests can monkeypatch this single seam to
    return canned (live, cached) tuples."""
    live = await _fetch_live_products(
        merchant_id=merchant_id, platform=platform, limit=limit,
    )
    cached = await _fetch_cached_products(
        merchant_id=merchant_id, platform=platform, limit=limit,
    )
    return live, cached
