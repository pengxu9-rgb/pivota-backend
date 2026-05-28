"""PR-5: funnel event recorder.

Wraps `db.funnel_events.record_funnel_event` with channel + stage
inference. Existing event hooks (`log_api_call_event`,
`log_order_event` in db/products.py) call into this recorder so the
funnel layer populates automatically — no per-call instrumentation
required at every API endpoint.

Inference strategy (heuristic, intentionally conservative):

  source_channel:
    - endpoint starts with /agent/  → "ai_agent"  (Pivota agent API)
    - endpoint starts with /api/agent-center/ → "ai_agent"
    - utm_source matches "tiktok|instagram|...social..." → "social_own"
      (assumes merchant's own posts; KOL attribution needs a
      separate signal we don't have yet)
    - utm_source matches "google|bing|seo" → "seo_organic"
    - utm_source matches "amazon|walmart|sephora|target|ulta" → "retail"
    - referrer host matches a known editorial/press host (cited host
      registry from PR-E earlier) → "editorial"
    - utm_source / referrer present but no match → "direct"
    - everything else → "unknown"

  stage:
    - event_type = "search" / "list" → "impression" (multiple products surfaced)
    - event_type = "product_detail" / "view" → "pdp_view"
    - event_type = "click" / "ctr" → "click"
    - event_type = "cart_add" / "wishlist_add" → "add_to_cart"
    - event_type = "order_created" / "purchase" / "checkout" → "conversion"
    - everything else → "impression" (most permissive default)

Honest framing: the inference is heuristic, not deterministic. Real
cross-channel attribution (which channel ACTUALLY drove this buyer)
needs richer tracking we don't have. For Phase 1 APM the heuristic
is enough to surface "ai_agent visibility is up but conversions
aren't" — the question that motivated this work.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Channel inference patterns.
_AI_AGENT_PATH_PREFIXES = (
    "/agent/",
    "/api/agent-center/",
    "/api/agent/",
)

_UTM_SOURCE_RULES: List[tuple] = [
    # (regex, source_channel)
    (re.compile(r"\b(tiktok|instagram|youtube|x\.com|twitter|linkedin|facebook|pinterest)\b", re.I), "social_own"),
    (re.compile(r"\b(google|bing|seo|organic|search)\b", re.I), "seo_organic"),
    (re.compile(r"\b(amazon|walmart|sephora|target|ulta|nordstrom|macys|costco|kroger)\b", re.I), "retail"),
]

# Editorial host fragments — small starter set. Same data as the
# cited_host_classifier registry could merge in here for a richer
# match. Kept inline for simplicity.
_EDITORIAL_HOST_FRAGMENTS = (
    "nymag.com", "strategist", "forbes.com", "vetted",
    "businessinsider.com", "vogue.com", "marieclaire", "cosmopolitan",
    "popsugar", "buzzfeed", "the-strategist", "cnn.com", "bbc",
    "techcrunch", "engadget", "nytimes", "washingtonpost", "bloomberg",
    "reuters",
)

# Stage inference from event_type strings.
_STAGE_RULES: List[tuple] = [
    (re.compile(r"^(search|list|catalog_query|browse)", re.I), "impression"),
    (re.compile(r"^(product_detail|pdp|view|product_view)", re.I), "pdp_view"),
    (re.compile(r"^(click|ctr|tap)", re.I), "click"),
    (re.compile(r"^(cart|add_to_cart|wishlist)", re.I), "add_to_cart"),
    (re.compile(r"^(order|purchase|checkout|payment_succeeded|conversion)", re.I), "conversion"),
    (re.compile(r"^(profile|brand_visit)", re.I), "profile_visit"),
]


def infer_source_channel(
    *,
    endpoint: Optional[str] = None,
    utm_source: Optional[str] = None,
    referrer: Optional[str] = None,
) -> str:
    """Best-effort source_channel inference. See module docstring."""
    if endpoint:
        ep = endpoint.lower()
        if any(ep.startswith(p) for p in _AI_AGENT_PATH_PREFIXES):
            return "ai_agent"

    if utm_source and isinstance(utm_source, str):
        for pat, channel in _UTM_SOURCE_RULES:
            if pat.search(utm_source):
                return channel

    if referrer and isinstance(referrer, str):
        ref_lower = referrer.lower()
        if any(frag in ref_lower for frag in _EDITORIAL_HOST_FRAGMENTS):
            return "editorial"

    if utm_source or referrer:
        return "direct"
    return "unknown"


def infer_stage(event_type: Optional[str]) -> str:
    """Best-effort stage inference from a free-form event_type string.
    Defaults to 'impression' (most permissive)."""
    if not event_type or not isinstance(event_type, str):
        return "impression"
    for pat, stage in _STAGE_RULES:
        if pat.search(event_type):
            return stage
    return "impression"


async def record_from_api_call_event(
    *,
    merchant_id: Optional[str],
    event_type: str,
    endpoint: Optional[str] = None,
    request_params: Optional[Dict[str, Any]] = None,
    product_ids: Optional[List[str]] = None,
) -> List[str]:
    """Hook called from db.products.log_api_call_event after that
    function writes its existing row. Derives funnel signal +
    persists one event per surfaced product (or one brand-level
    event when no products in the request).

    Best-effort: any failure is swallowed so the existing event
    writer's success isn't undone by funnel persistence issues.
    """
    if not merchant_id:
        return []
    event_ids: List[str] = []
    try:
        from db.funnel_events import record_funnel_event

        # Pull utm + referer from request_params if the caller stashed
        # them. Existing log_api_call_event signatures don't always
        # pass these — graceful when missing.
        params = request_params or {}
        utm_source = (
            params.get("utm_source")
            or params.get("source")
            or None
        )
        referrer = (
            params.get("referer")
            or params.get("referrer")
            or None
        )

        channel = infer_source_channel(
            endpoint=endpoint, utm_source=utm_source, referrer=referrer,
        )
        stage = infer_stage(event_type)
        attribution = {
            "endpoint": endpoint,
            "event_type": event_type,
            "utm_source": utm_source,
            "referrer": referrer,
        }

        if product_ids:
            # One event per surfaced product (impressions for a
            # multi-product list count each impression).
            for pid in product_ids[:50]:  # safety cap
                if not pid:
                    continue
                event_id = await record_funnel_event(
                    merchant_id=merchant_id,
                    source_channel=channel,
                    stage=stage,
                    product_key=pid,
                    attribution=attribution,
                )
                if event_id:
                    event_ids.append(event_id)
        else:
            # Brand-level event (no specific product) — surface as
            # one row with product_key=None.
            event_id = await record_funnel_event(
                merchant_id=merchant_id,
                source_channel=channel,
                stage=stage,
                product_key=None,
                attribution=attribution,
            )
            if event_id:
                event_ids.append(event_id)
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "funnel_recorder.record_from_api_call_event failed: %s",
            str(exc)[:200],
        )
    return event_ids


async def record_from_order_event(
    *,
    merchant_id: Optional[str],
    event_type: str,
    order_id: Optional[str] = None,
    product_ids: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Hook called from db.products.log_order_event. Order events are
    almost always conversion-stage; channel is inferred from order
    metadata when present (utm_source / source).

    Best-effort: any failure swallowed."""
    if not merchant_id:
        return []
    event_ids: List[str] = []
    try:
        from db.funnel_events import record_funnel_event
        meta = metadata or {}
        utm_source = (
            meta.get("utm_source")
            or meta.get("source")
            or meta.get("source_channel")
            or None
        )
        referrer = meta.get("referrer") or None
        channel = infer_source_channel(
            endpoint=None, utm_source=utm_source, referrer=referrer,
        )
        stage = infer_stage(event_type)
        attribution = {
            "event_type": event_type,
            "order_id": order_id,
            "utm_source": utm_source,
            "referrer": referrer,
        }
        if product_ids:
            for pid in product_ids[:50]:
                if not pid:
                    continue
                event_id = await record_funnel_event(
                    merchant_id=merchant_id,
                    source_channel=channel,
                    stage=stage,
                    product_key=pid,
                    attribution=attribution,
                )
                if event_id:
                    event_ids.append(event_id)
        else:
            event_id = await record_funnel_event(
                merchant_id=merchant_id,
                source_channel=channel,
                stage=stage,
                product_key=None,
                attribution=attribution,
            )
            if event_id:
                event_ids.append(event_id)
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "funnel_recorder.record_from_order_event failed: %s",
            str(exc)[:200],
        )
    return event_ids
