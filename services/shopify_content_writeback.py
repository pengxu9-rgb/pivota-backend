from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# The app-owned metafield Pivota writes the AI-ready copy into. NEVER body_html.
# The merchant chooses to surface it in their theme; nothing customer-visible
# changes until they do. metafieldsSet is upsert-by-(owner, namespace, key), so
# re-writes are idempotent and never create duplicates or touch native fields.
CONTENT_METAFIELD_NAMESPACE = "pivota"
CONTENT_METAFIELD_KEY = "ai_pdp"
CONTENT_METAFIELD_TYPE = "json"

_METAFIELDS_SET = """
mutation PivotaContentMetafieldSet($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    metafields { id namespace key }
    userErrors { field message code }
  }
}
"""


def _content_hash(payload: Dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_metafield_value(enrichment: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Shape the copy fields the merchant surfaces. Returns None when there's no
    copy to write (nothing to publish)."""
    def _s(v: Any) -> str:
        return v.strip() if isinstance(v, str) else ""

    def _list(v: Any) -> List[str]:
        if not isinstance(v, list):
            return []
        return [x.strip() for x in v if isinstance(x, str) and x.strip()]

    copy: Dict[str, Any] = {
        "title": _s(enrichment.get("title_override")),
        "summary": _s(enrichment.get("summary_short")),
        "description": _s(enrichment.get("description_markdown")),
        "bullets": _list(enrichment.get("bullet_points")),
        "usage": _list(enrichment.get("usage_scenarios")),
    }
    if not (copy["title"] or copy["summary"] or copy["description"] or copy["bullets"]):
        return None
    return copy


def _shopify_product_gid(platform_product_id: str) -> str:
    pid = str(platform_product_id or "").strip()
    if pid.startswith("gid://"):
        return pid
    return f"gid://shopify/Product/{pid}"


def _looks_like_access_denied(text: str) -> bool:
    t = (text or "").lower()
    return "access_denied" in t or "401" in t or "403" in t or "not approved" in t


async def publish_content_to_store(
    *,
    merchant_id: str,
    platform: str,
    platform_product_id: str,
    enrichment: Dict[str, Any],
) -> Dict[str, Any]:
    """Write the AI-ready copy to an app-owned Shopify metafield (pivota/ai_pdp),
    GATED by the per-store content-writeback state machine. This is the first
    write to a merchant's live store: fail-closed everywhere, never writes
    body_html, never raises.

    Returns {status, ...} where status is one of:
      'written' | 'blocked' (+blocker, +gate) | 'needs_write_products'
      | 'no_copy' | 'store_missing' | 'error' (+message).
    """
    from services.content_writeback_readiness import store_content_writeback_context
    from services.merchant_store_service import get_merchant_active_stores
    from services.shopify_access_token_service import resolve_shopify_admin_access_token
    from services.shopify_graphql_client import shopify_admin_graphql

    plat = str(platform or "").strip().lower()

    # 1. Resolve the store (hydrated: api_key_raw, domain, content_writeback_*).
    try:
        stores = await get_merchant_active_stores(merchant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("content_writeback: store load failed for %s: %s", merchant_id, str(exc)[:200])
        return {"status": "error", "message": "store_lookup_failed"}
    store = next(
        (s for s in stores if str(s.get("platform") or "").strip().lower() == plat),
        None,
    )
    if not store:
        return {"status": "store_missing"}

    # 2. Gate: default-OFF, canary must match this product, global kill switch.
    ctx = store_content_writeback_context(store, product_id=platform_product_id, platform=plat)
    if not ctx.get("allowed"):
        return {"status": "blocked", "blocker": ctx.get("blocker"), "gate": ctx}

    # 3. The copy to write (the already-#968-gated enrichment overlay).
    value = _build_metafield_value(enrichment or {})
    if value is None:
        return {"status": "no_copy"}
    value["_provenance"] = {
        "written_by": "pivota",
        "executor": "content_writeback",
        "content_hash": _content_hash({k: v for k, v in value.items() if k != "_provenance"}),
        "written_at": datetime.now(timezone.utc).isoformat(),
    }

    # 4. Resolve the Admin token. App B tokens carry write_products after the
    #    merchant re-consents; an App A / un-re-consented token will be rejected
    #    by Shopify at metafieldsSet (handled below as needs_write_products).
    if plat != "shopify":
        return {"status": "error", "message": f"unsupported_platform:{plat}"}
    token, _meta = await resolve_shopify_admin_access_token(
        shop_domain=store.get("domain"),
        api_key_raw=store.get("api_key_raw"),
        store_id=store.get("store_id"),
    )
    if not token:
        return {"status": "error", "message": "no_admin_token"}

    variables = {
        "metafields": [
            {
                "ownerId": _shopify_product_gid(platform_product_id),
                "namespace": CONTENT_METAFIELD_NAMESPACE,
                "key": CONTENT_METAFIELD_KEY,
                "type": CONTENT_METAFIELD_TYPE,
                "value": json.dumps(value, ensure_ascii=False),
            }
        ]
    }

    # 5. metafieldsSet (upsert; NEVER body_html).
    try:
        data = await shopify_admin_graphql(
            shop_domain=str(store.get("domain") or ""),
            access_token=token,
            query=_METAFIELDS_SET,
            variables=variables,
        )
    except Exception as exc:  # noqa: BLE001 -- scope/HTTP/top-level errors all fail closed
        msg = str(exc)
        if _looks_like_access_denied(msg):
            return {"status": "needs_write_products"}
        logger.warning(
            "content_writeback: metafieldsSet failed %s/%s: %s",
            merchant_id, platform_product_id, msg[:200],
        )
        return {"status": "error", "message": msg[:200]}

    result = (data or {}).get("metafieldsSet") or {}
    user_errors = result.get("userErrors") or []
    if user_errors:
        codes = [str((e or {}).get("code") or "").upper() for e in user_errors if isinstance(e, dict)]
        if "ACCESS_DENIED" in codes:
            return {"status": "needs_write_products"}
        return {"status": "error", "message": str(user_errors)[:300]}

    return {
        "status": "written",
        "metafield": {
            "namespace": CONTENT_METAFIELD_NAMESPACE,
            "key": CONTENT_METAFIELD_KEY,
        },
        "content_hash": value["_provenance"]["content_hash"],
    }
