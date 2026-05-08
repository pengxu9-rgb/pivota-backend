"""
Merchant integration state — lean check answering "is this merchant
fully onboarded with Pivota?".

Phase 0 of the audit execution layer uses this to decide whether the
audit's #1 action should be "Complete Pivota integration" (un-integrated)
or whether to defer to executable per-host actions (Phases A-F).

Distinct from `services.merchant_commerce_readiness_service` which
runs full readiness checks (listings, clicks, edges, orders) — that's
heavy and meant for the merchant ops dashboard. We need a 2-question
yes/no check on the audit hot path.

Two integrations matter:
  - **Store platform** (Shopify, BigCommerce, etc.) — gives Pivota the
    canonical PDP path for indexing + product sync. Without it,
    Pivota canonical PDP can't be minted with real product data.
  - **PSP** (Stripe, Adyen) — enables in-chat checkout. Without it,
    even when AI agents recommend the merchant, the conversion has
    no completion path inside the chat surface.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


async def get_integration_state(merchant_id: str) -> Dict[str, Any]:
    """Returns the merchant's Pivota integration state.

    Output shape:
      {
        "store_platform_integrated": bool,
        "psp_integrated": bool,           # any active PSP, charge-ready
        "fully_integrated": bool,         # both above
        "missing_pieces": List[str],      # ["store_platform"], ["psp"], or both
        "integration_completed_at": datetime | None,
        # Diagnostic fields (UI surfacing optional):
        "store_platform_name": str | None,   # "shopify", "bigcommerce", ...
        "psp_provider": str | None,          # "stripe", "adyen", ...
        "store_connected_at": datetime | None,
      }

    Best-effort: any lookup failure produces a "treat as un-integrated"
    state so the audit surfaces the integration CTA. Better to over-
    surface than to mislead a half-integrated merchant into thinking
    they're done.
    """
    out: Dict[str, Any] = {
        "store_platform_integrated": False,
        "psp_integrated": False,
        "fully_integrated": False,
        "missing_pieces": ["store_platform", "psp"],
        "integration_completed_at": None,
        "store_platform_name": None,
        "psp_provider": None,
        "store_connected_at": None,
    }
    if not merchant_id or not str(merchant_id).strip():
        return out

    # Store-platform check.
    store: Optional[Dict[str, Any]] = None
    try:
        from services.merchant_store_service import get_primary_store
        store = await get_primary_store(merchant_id)
    except Exception:
        store = None
    if store:
        out["store_platform_integrated"] = True
        out["store_platform_name"] = (
            (store.get("platform") or "").strip().lower() or None
        )
        out["store_connected_at"] = _normalize_ts(store.get("connected_at"))

    # PSP check — at least one active row, AND charge-ready per
    # evaluate_psp_readiness (configured + validated).
    live_psp: Optional[Dict[str, Any]] = None
    try:
        from services.merchant_psp_config_service import (
            fetch_active_merchant_psps,
            evaluate_psp_readiness,
        )
        psp_rows = await fetch_active_merchant_psps(merchant_id=merchant_id)
        for row in psp_rows or []:
            readiness = evaluate_psp_readiness(
                row.get("provider") or "",
                status=row.get("status"),
                api_key=row.get("api_key"),
                account_id=row.get("account_id"),
                provider_config=row.get("provider_config"),
                environment=row.get("environment"),
                validation_status=row.get("validation_status"),
                validation_error=row.get("validation_error"),
            )
            if readiness.get("live_charge_ready"):
                live_psp = row
                break
    except Exception:
        live_psp = None
    if live_psp:
        out["psp_integrated"] = True
        out["psp_provider"] = (
            (live_psp.get("provider") or "").strip().lower() or None
        )

    missing: List[str] = []
    if not out["store_platform_integrated"]:
        missing.append("store_platform")
    if not out["psp_integrated"]:
        missing.append("psp")
    out["missing_pieces"] = missing
    out["fully_integrated"] = not missing

    if out["fully_integrated"]:
        # Best estimate of when integration completed: the later of
        # store_connected_at and the PSP's connected_at (if available).
        store_ts = out["store_connected_at"]
        psp_ts = _normalize_ts((live_psp or {}).get("connected_at")) if live_psp else None
        candidates = [t for t in (store_ts, psp_ts) if t is not None]
        out["integration_completed_at"] = max(candidates) if candidates else None

    return out


def _normalize_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            v = value.replace("Z", "+00:00")
            return datetime.fromisoformat(v)
        except ValueError:
            return None
    return None


def build_integration_action(
    state: Dict[str, Any],
    *,
    onboarding_url: str = "/onboarding/pivota",
) -> Optional[Dict[str, Any]]:
    """Build the #1 audit action when integration is incomplete.
    Returns None when fully integrated (other actions take over).

    `lever="pivota_integration"` marks this action as exempt from the
    PITCH_TOKENS test in tests/test_diagnostic_no_pitch.py — the whole
    point of this action is to surface Pivota's value prop where the
    merchant can act on it. All other actions remain pitch-free.
    """
    if state.get("fully_integrated"):
        return None
    missing = list(state.get("missing_pieces") or [])
    if not missing:
        return None

    needs_store = "store_platform" in missing
    needs_psp = "psp" in missing

    title = "Complete Pivota integration"
    if needs_store and needs_psp:
        body = (
            "Connect your store platform and a payment provider to "
            "Pivota. This unlocks two visibility paths: (a) your "
            "products gain Pivota canonical PDP URLs — indexable, "
            "structured surfaces AI agents can cite directly, "
            "alongside your own URL; (b) in-chat checkout — when an "
            "AI agent recommends your product, the buyer can complete "
            "the purchase inside the chat surface (works with any "
            "agent protocol — OpenAI Operator, Claude, ChatGPT, etc.) "
            "instead of dropping out of the funnel."
        )
        cta_label = "Start Pivota onboarding"
    elif needs_store and not needs_psp:
        body = (
            "Your payment provider is connected. Connect your store "
            "platform to enable Pivota canonical PDP indexing — your "
            "products become AI-citable surfaces, complementing your "
            "own URL in grounded answers."
        )
        cta_label = "Connect store platform"
    else:  # needs_psp only
        body = (
            "Your store platform is connected. Connect a payment "
            "provider (Stripe or Adyen) to enable in-chat checkout — "
            "when an AI agent recommends your product, the buyer can "
            "complete the purchase inside the chat surface instead of "
            "dropping out of the funnel."
        )
        cta_label = "Connect payment provider"

    return {
        "severity": "critical",
        "priority_order": 1,  # always first; merged_actions ordering
                              # in _build_merchant_view re-stamps this
                              # alongside other actions but the helper
                              # caller prepends this to the list.
        "lever": "pivota_integration",
        "title": title,
        "body": body,
        "concrete_next_step": cta_label,
        "cta_url": onboarding_url,
        "cta_label": cta_label,
        "evidence": {
            "missing_pieces": missing,
            "store_platform_integrated": bool(state.get("store_platform_integrated")),
            "psp_integrated": bool(state.get("psp_integrated")),
        },
    }
