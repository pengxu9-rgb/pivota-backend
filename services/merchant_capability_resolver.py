"""P-T2.1 — merchant capability resolver (Tier-2 protocol routing gate).

`resolve_merchant_capability(merchant_id)` answers the one question the decision
layer needs before it can offer an in-chat protocol checkout:

    { "platform": ..., "psp": ..., "protocols": [...] }

- **platform** comes from a connected store (`get_primary_store` /
  `merchant_onboarding.mcp_platform`) when the merchant is integrated, else from
  cheap hostname fingerprinting so a *crawled* (un-integrated) merchant still
  resolves a platform — or an honest ``"unknown"`` when we genuinely cannot tell.
- **psp** is the merchant's live-charge-ready PSP provider (or None), reusing the
  same `evaluate_psp_readiness` gate the commerce-readiness state uses.
- **protocols[]** is computed from the truthful per-platform matrix in
  `services.platform_capabilities`, gated on a live PSP for charge-capable
  protocols. Empty means "redirect floor only" — never a dead end.

This phase does NOT persist protocols[]; it resolves on the fly (a companion
column on merchant_commerce_readiness_state is a later optimization). It also
does NOT route or charge — that's P-T2.2 (kill-switch) and P-T2.3 (ACP lane).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from db.database import database
from db.merchant_onboarding import get_merchant_onboarding
from services.merchant_psp_config_service import evaluate_psp_readiness
from services.merchant_store_service import get_merchant_active_stores, get_primary_store
from services.platform_capabilities import get_platform_protocols_for_store

logger = logging.getLogger("merchant_capability_resolver")

PLATFORM_SOURCE_CONNECTED = "connected"
PLATFORM_SOURCE_FINGERPRINT = "domain_fingerprint"
PLATFORM_SOURCE_UNKNOWN = "unknown"

# Hostname → platform fingerprints. Deliberately limited to signals that are
# *unambiguous* from the host alone; anything softer stays "unknown" (honest gap)
# rather than guessing. WooCommerce/custom self-host on arbitrary domains and are
# intentionally NOT fingerprintable here — an HTML/DOM fingerprint pass is a later
# extension point, not a hostname heuristic.
_HOST_SUFFIX_PLATFORM = {
    ".myshopify.com": "shopify",
    ".wixsite.com": "wix",
    ".wixstudio.com": "wix",
    ".editorx.io": "wix",
    ".mybigcommerce.com": "bigcommerce",
}


def _normalize_host(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    host = urlparse(raw).hostname or ""
    if host.startswith("www."):
        host = host[4:]
    return host


def fingerprint_platform_from_domain(domain: Any) -> Optional[str]:
    """Best-effort platform from a bare domain/URL, hostname-only.

    Returns a platform slug for an unambiguous host suffix (e.g. a
    ``*.myshopify.com`` store), else None — never a guess.
    """
    host = _normalize_host(domain)
    if not host:
        return None
    for suffix, platform in _HOST_SUFFIX_PLATFORM.items():
        # suffix match on a dotted boundary, and also match the bare apex
        # (e.g. "myshopify.com" itself is not a store, so only true subdomains).
        if host.endswith(suffix):
            return platform
    return None


async def _resolve_live_psp_provider(merchant_id: str) -> Optional[str]:
    """The merchant's live-charge-ready PSP provider, or None.

    Mirrors merchant_commerce_readiness_service: active merchant_psps →
    evaluate_psp_readiness → first live_charge_ready provider. Best-effort: a
    query error resolves to None (no PSP), never raises into the caller.
    """
    try:
        rows = await database.fetch_all(
            """
            SELECT provider, status, api_key, account_id, provider_config,
                   environment, validation_status, validation_error
            FROM merchant_psps
            WHERE merchant_id = :merchant_id
              AND status = 'active'
            ORDER BY connected_at DESC NULLS LAST, psp_id ASC
            """,
            {"merchant_id": merchant_id},
        )
    except Exception as exc:  # noqa: BLE001 — resolver must not break on PSP read
        logger.warning(
            "capability_resolver: active PSP lookup failed merchant_id=%s: %s",
            merchant_id, str(exc)[:200],
        )
        return None
    for row in rows or []:
        record = dict(row)
        readiness = evaluate_psp_readiness(
            record.get("provider") or "",
            status=record.get("status"),
            api_key=record.get("api_key"),
            account_id=record.get("account_id"),
            provider_config=record.get("provider_config"),
            environment=record.get("environment"),
            validation_status=record.get("validation_status"),
            validation_error=record.get("validation_error"),
        )
        if readiness.get("live_charge_ready"):
            return readiness.get("provider") or (record.get("provider") or None)
    return None


async def _select_target_store(
    merchant_id: str,
    *,
    store_id: Optional[str],
    platform_override: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Pick the connected store to resolve capability against.

    With no selector → the primary store (existing behavior). With a `store_id` or
    `platform` selector → the merchant's matching *active* store (store_id wins;
    else first store on that platform). A selector that matches no active store
    returns None so the caller resolves to the redirect floor — the override only
    ever narrows to a store the merchant genuinely has, never fabricates one.
    """
    sid = str(store_id or "").strip()
    plat = str(platform_override or "").strip().lower()
    if not sid and not plat:
        return await get_primary_store(merchant_id)
    try:
        stores = await get_merchant_active_stores(merchant_id)
    except Exception as exc:  # noqa: BLE001 — never break routing on a store read
        logger.warning(
            "capability_resolver: active-store lookup failed merchant_id=%s: %s",
            merchant_id, str(exc)[:200],
        )
        return None
    if sid:
        for s in stores or []:
            if str((s or {}).get("store_id") or "").strip() == sid:
                return s
        return None  # named a store_id this merchant doesn't have → honest miss
    for s in stores or []:
        if str((s or {}).get("platform") or "").strip().lower() == plat:
            return s
    return None  # named a platform this merchant has no active store on


async def resolve_merchant_capability(
    merchant_id: str,
    *,
    store_id: Optional[str] = None,
    platform_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve `{platform, psp, protocols[]}` for Tier-2 routing.

    Always returns a dict (never raises); ``platform`` is a slug or ``"unknown"``,
    ``psp`` is a provider slug or None, ``protocols`` is a possibly-empty list.
    ``platform_source`` records how the platform was resolved (connected store,
    domain fingerprint, or unknown) so callers/telemetry can distinguish a
    high-confidence connected platform from a fingerprinted crawl merchant.

    `store_id` / `platform_override` optionally select which connected store to
    resolve against (else the primary store). A selector that matches no active
    store resolves to ``"unknown"`` (redirect floor) — it never falls back to a
    *different* store than asked for, and never fabricates a platform.
    """
    merchant_id = str(merchant_id or "").strip()
    selector_requested = bool(
        str(store_id or "").strip() or str(platform_override or "").strip()
    )
    if not merchant_id:
        return {
            "merchant_id": merchant_id,
            "platform": PLATFORM_SOURCE_UNKNOWN,
            "platform_source": PLATFORM_SOURCE_UNKNOWN,
            "psp": None,
            "has_live_psp": False,
            "protocols": [],
        }

    store = await _select_target_store(
        merchant_id, store_id=store_id, platform_override=platform_override
    )
    if selector_requested and store is None:
        # Override named a store/platform this merchant has no active store on →
        # honest unknown. Do NOT fingerprint or fall back to primary: that would
        # silently check out against a different store than requested.
        return {
            "merchant_id": merchant_id,
            "platform": PLATFORM_SOURCE_UNKNOWN,
            "platform_source": PLATFORM_SOURCE_UNKNOWN,
            "psp": None,
            "has_live_psp": False,
            "protocols": [],
            "store_selector": {
                "store_id": str(store_id or "").strip() or None,
                "platform": str(platform_override or "").strip().lower() or None,
                "matched": False,
            },
        }
    merchant = await get_merchant_onboarding(merchant_id)

    platform = str(
        (store or {}).get("platform") or (merchant or {}).get("mcp_platform") or ""
    ).strip().lower() or None
    platform_source = PLATFORM_SOURCE_CONNECTED if platform else None

    if not platform:
        # Crawl / un-integrated merchant: try to fingerprint a platform from any
        # domain we know about, else honest "unknown".
        domain = (
            (store or {}).get("domain")
            or (merchant or {}).get("mcp_shop_domain")
            or (merchant or {}).get("store_url")
            or (merchant or {}).get("website")
        )
        fingerprinted = fingerprint_platform_from_domain(domain)
        if fingerprinted:
            platform, platform_source = fingerprinted, PLATFORM_SOURCE_FINGERPRINT
        else:
            platform_source = PLATFORM_SOURCE_UNKNOWN

    live_psp = await _resolve_live_psp_provider(merchant_id)
    has_live_psp = bool(live_psp)
    # Store-aware: Wix ACP lights up per-store (order-writeback readiness), so
    # pass the resolved store. `store` is None for fingerprinted/crawl merchants
    # → no per-store grant (honest). Shopify et al. are unaffected.
    protocols: List[str] = get_platform_protocols_for_store(
        platform, store, has_live_psp=has_live_psp
    )

    result: Dict[str, Any] = {
        "merchant_id": merchant_id,
        "platform": platform or PLATFORM_SOURCE_UNKNOWN,
        "platform_source": platform_source or PLATFORM_SOURCE_UNKNOWN,
        "psp": live_psp,
        "has_live_psp": has_live_psp,
        "protocols": protocols,
    }
    if selector_requested:
        result["store_selector"] = {
            "store_id": str(store_id or "").strip() or None,
            "platform": str(platform_override or "").strip().lower() or None,
            "matched": True,
            "resolved_store_id": str((store or {}).get("store_id") or "").strip() or None,
        }
    return result
