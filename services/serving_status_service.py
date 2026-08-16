"""Merchant-facing serving-visibility status.

Surfaces, per SKU, whether a merchant's product is discoverable by AI shopping
agents (index_pipeline_state.serving_eligible) and — when it is not — a
PLAIN-ENGLISH, action-oriented reason. The internal blocker_code and internal
metric names (e.g. content_quality_score) never reach merchant copy: the audit
that leaked "add the missing product quality score to your page" is exactly the
credibility-killer this map exists to prevent.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from db.database import database


# Internal blocker_code → merchant-facing reason. Each reason is an action the
# merchant can actually take on their own product page — never an internal
# metric name, score, or pipeline term.
BLOCKER_CODE_TO_REASON: Dict[str, str] = {
    "not_live": "This product isn't live in your store yet. Publish it, then re-sync.",
    "merchant_store_inactive": "Your store is no longer connected, so its products aren't listed for shopping agents. Reconnect your store to resume serving.",
    "non_core_product": "This looks like a sample, gift, or add-on, so it isn't listed as a standalone product for shopping agents.",
    "no_seed": "We're still preparing this product for AI shopping agents. Re-sync if it doesn't appear shortly.",
    "no_extraction": "We couldn't read this product's details. Make sure it has a clear title and description, then re-sync.",
    "low_quality": "This product page is too thin for shopping agents to recommend confidently. Add a fuller description, a clear image, and key details.",
    # Distinct from low_quality on purpose: we have not scored this page yet, so
    # telling the merchant their content is thin would be a false accusation.
    "not_scored": "We're still scoring this product for AI shopping agents. No action needed — this usually resolves on its own shortly.",
    "no_image": "This product has no image. Add a product photo so shopping agents can show it.",
    "no_price": "This product has no price. Add a price so shopping agents can quote and check it out.",
    "short_description": "This product's description is too short. Add a few sentences describing what it is and who it's for.",
    "entity_unresolved": "We're still matching this product to a catalog identity. This usually resolves on its own shortly.",
    "seed_audit_fail": "We found unresolved issues with this product's listing data. Review the product details and re-sync.",
    "extractor_regression": "We're temporarily unable to read products from this store reliably. No action needed — we're on it.",
    # Agent-decision-grade gates (PR4).
    "no_us_offer": "This product has no US-market offer. Add US pricing and availability so shopping agents can recommend it to US shoppers.",
    "no_provenance_claim": "This product's benefit claims aren't backed by a cited source yet. Add sourced claims so shopping agents can justify recommending it.",
    "missing_disclaimers": "This product is missing a required disclaimer (for example, the FDA supplement disclaimer). Add it so shopping agents can list it.",
    "unreviewed_evidence": "This product's claims and evidence are still being reviewed. This usually resolves on its own shortly.",
    "missing_category_attributes": "This product is missing key category details shoppers expect. Add the relevant attributes for its category, then re-sync.",
}

# A null index_pipeline_state row (never classified yet) is "pending", not blocked.
PENDING_REASON = "This product is still being processed for AI shopping agents. Check back shortly."


def reason_for(serving_eligible: Optional[bool], blocker_code: Optional[str]) -> Optional[str]:
    """Plain-English reason a SKU is not agent-visible, or None when it is."""
    if serving_eligible:
        return None
    if serving_eligible is None:
        return PENDING_REASON
    code = (blocker_code or "").strip()
    if not code or code == "none":
        # Not eligible but no specific blocker recorded — generic pending.
        return PENDING_REASON
    return BLOCKER_CODE_TO_REASON.get(code, PENDING_REASON)


def _status_row(
    *,
    sku_key: Optional[str],
    product_key: Optional[str],
    title: Optional[str],
    serving_eligible: Optional[bool],
    blocker_code: Optional[str],
) -> Dict[str, Any]:
    return {
        "sku_key": sku_key,
        "product_key": product_key,
        "title": title,
        "agent_visible": bool(serving_eligible),
        "serving_eligible": serving_eligible,
        "blocker_code": (blocker_code or None) if not serving_eligible else None,
        "blocker_reason": reason_for(serving_eligible, blocker_code),
    }


SUMMARY_SQL = """
SELECT
    COUNT(*)                                                          AS total,
    COUNT(*) FILTER (WHERE ips.serving_eligible IS TRUE)             AS eligible,
    COUNT(*) FILTER (WHERE ips.serving_eligible IS FALSE)           AS blocked,
    COUNT(*) FILTER (WHERE ips.serving_eligible IS NULL)            AS pending
FROM catalog_products cp
LEFT JOIN index_pipeline_state ips ON ips.content_key = cp.content_key
WHERE cp.merchant_id = :merchant_id
"""

# Blocked + pending first (serving_eligible FALSE then NULL then TRUE), so the
# merchant sees what needs fixing at the top.
LIST_SQL = """
SELECT
    cp.product_key,
    cp.platform,
    cp.source_product_id,
    cp.title,
    ips.serving_eligible,
    ips.blocker_code
FROM catalog_products cp
LEFT JOIN index_pipeline_state ips ON ips.content_key = cp.content_key
WHERE cp.merchant_id = :merchant_id
ORDER BY (ips.serving_eligible IS TRUE), (ips.serving_eligible IS NULL) DESC, cp.title
LIMIT :limit
"""

BY_PRODUCT_KEYS_SQL = """
SELECT
    cp.product_key,
    cp.platform,
    cp.source_product_id,
    cp.title,
    ips.serving_eligible,
    ips.blocker_code
FROM catalog_products cp
LEFT JOIN index_pipeline_state ips ON ips.content_key = cp.content_key
WHERE cp.merchant_id = :merchant_id
  AND cp.product_key = ANY(:product_keys)
"""


async def get_serving_summary(merchant_id: str, *, db: Any = None) -> Dict[str, int]:
    read_db = db or database
    row = await read_db.fetch_one(SUMMARY_SQL, {"merchant_id": merchant_id})
    r = dict(row) if row else {}
    return {
        "total": int(r.get("total") or 0),
        "eligible": int(r.get("eligible") or 0),
        "blocked": int(r.get("blocked") or 0),
        "pending": int(r.get("pending") or 0),
    }


async def list_serving_status(
    merchant_id: str, *, limit: int = 200, db: Any = None
) -> List[Dict[str, Any]]:
    read_db = db or database
    rows = await read_db.fetch_all(
        LIST_SQL, {"merchant_id": merchant_id, "limit": int(limit)}
    )
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        r = dict(row)
        out.append(_status_row(
            sku_key=f"{r.get('platform')}:{r.get('source_product_id')}",
            product_key=r.get("product_key"),
            title=r.get("title"),
            serving_eligible=r.get("serving_eligible"),
            blocker_code=r.get("blocker_code"),
        ))
    return out


async def serving_status_for_product_keys(
    merchant_id: str, product_keys: List[str], *, db: Any = None
) -> List[Dict[str, Any]]:
    read_db = db or database
    if not product_keys:
        return []
    rows = await read_db.fetch_all(
        BY_PRODUCT_KEYS_SQL, {"merchant_id": merchant_id, "product_keys": product_keys}
    )
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        r = dict(row)
        out.append(_status_row(
            sku_key=f"{r.get('platform')}:{r.get('source_product_id')}",
            product_key=r.get("product_key"),
            title=r.get("title"),
            serving_eligible=r.get("serving_eligible"),
            blocker_code=r.get("blocker_code"),
        ))
    return out


RESOLVE_COMPOSITES_SQL = """
SELECT product_key, platform, source_product_id
FROM catalog_products
WHERE merchant_id = :merchant_id
  AND (platform, source_product_id) IN (
      SELECT unnest(CAST(:platforms AS text[])), unnest(CAST(:source_ids AS text[]))
  )
"""


async def serving_status_for_sku_keys(
    merchant_id: str, sku_keys: List[str], *, db: Any = None
) -> List[Dict[str, Any]]:
    """Per-SKU serving status for merchant-supplied refs.

    Accepts `platform:source_product_id` composites (single ':') or bare
    `product_key`s, mirroring _resolve_refs_to_product_keys but NON-raising:
    a ref the merchant doesn't own is returned with serving_eligible=None and
    a "not found" reason rather than 404-ing the whole request.
    """
    read_db = db or database
    cleaned = [s.strip() for s in (sku_keys or []) if s and s.strip()]
    if not cleaned:
        return []

    composites = [s for s in cleaned if ":" in s and "::" not in s]
    bare = [s for s in cleaned if s not in composites]

    sku_to_pk: Dict[str, str] = {}
    if composites:
        pairs = [tuple(p.strip() for p in s.split(":", 1)) for s in composites]
        rows = await read_db.fetch_all(
            RESOLVE_COMPOSITES_SQL,
            {
                "merchant_id": merchant_id,
                "platforms": [p for p, _ in pairs],
                "source_ids": [s for _, s in pairs],
            },
        )
        pair_to_key = {
            (str(dict(r).get("platform") or ""), str(dict(r).get("source_product_id") or "")): str(dict(r).get("product_key") or "")
            for r in rows or []
        }
        for ref, pair in zip(composites, pairs):
            key = pair_to_key.get(pair)
            if key:
                sku_to_pk[ref] = key
    for ref in bare:
        sku_to_pk[ref] = ref  # treat as product_key; ownership enforced by the merchant_id filter below

    pk_to_status = {
        s["product_key"]: s
        for s in await serving_status_for_product_keys(
            merchant_id, list(sku_to_pk.values()), db=read_db
        )
    }

    out: List[Dict[str, Any]] = []
    for ref in cleaned:
        pk = sku_to_pk.get(ref)
        status = pk_to_status.get(pk) if pk else None
        if status:
            out.append({**status, "sku_key": ref})
        else:
            out.append(_status_row(
                sku_key=ref, product_key=None, title=None,
                serving_eligible=None, blocker_code=None,
            ) | {"blocker_reason": "We couldn't find this product in your synced catalog."})
    return out
