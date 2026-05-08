"""
Phase C — multi-market audit (scaffolding).

The audit recommendation "your visibility is great in the US but
nonexistent in UK / JP" is impossible to surface today because every
audit runs against a single implicit market (en-US). Phase C extends
the probe pipeline to run additional probes per market, then exposes
per-market scores so merchants can see locale-specific gaps.

STATUS: scaffolding only. The actual multi-market dispatch (running
N audits in parallel, one per locale) is gated behind
`settings.phase_c_multi_market_enabled` which defaults to OFF. When
OFF, this module returns an empty markets aggregate — the audit
behaves identically to today.

Why scaffold now: the data model + action surface need to be in
place so portal can consume them when the dispatch wiring lands.
Same pattern as Phase D scaffolding (PR #367 → wire-up #382).

Honesty rules:
  - When the flag is OFF, no per-market data is fabricated. The
    `markets` field is empty, no localization action is emitted.
  - When the flag is ON but a market's audit fails (upstream error),
    that market is omitted from the aggregate — never recorded as
    "score 0/100" which would conflate "not measured" with "actually
    invisible".
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Gap threshold (visibility delta between best market and target market)
# above which the localization action fires. 30 points = "noticeably
# worse in target market". Tunable via settings if we hit calibration
# issues post-launch.
_LOCALIZATION_GAP_THRESHOLD = 30


def is_multi_market_enabled() -> bool:
    """Check the feature flag. Single source of truth — callers
    should not read the setting directly so we can swap to a more
    sophisticated rollout signal (per-merchant flag, A/B) later."""
    from config.settings import settings
    return bool(settings.phase_c_multi_market_enabled)


def get_enabled_markets() -> List[str]:
    """The list of locales to probe when multi-market is enabled.
    When the flag is OFF, returns the single default market so the
    rest of the codebase doesn't have to special-case "no markets"."""
    from config.settings import settings
    if not is_multi_market_enabled():
        return [settings.audit_default_market_locale]
    return list(settings.phase_c_enabled_markets) or [
        settings.audit_default_market_locale
    ]


def empty_markets_aggregate() -> Dict[str, Any]:
    """Default value for merchant_view.receipts.markets when
    multi-market is disabled or not yet computed. Keeps the schema
    stable so portal code doesn't have to null-check at every level."""
    return {
        "enabled": False,
        "default_market": None,
        "per_market": [],
    }


def build_markets_aggregate(
    *,
    default_market: str,
    per_market_results: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build the merchant_view.receipts.markets payload from a list
    of per-market audit results.

    `per_market_results` shape — one entry per market actually probed:
      {
        market_locale: "en-GB",
        visibility_score: 0-100,
        attribution_score: 0-100,
        category_visibility_score: 0-100 | None,
        verdict_label: str,
        probed_at: ISO8601,
      }
    """
    rows = list(per_market_results or [])
    return {
        "enabled": is_multi_market_enabled(),
        "default_market": default_market,
        "per_market": rows,
    }


def build_localization_action(
    *,
    markets_aggregate: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Emit the localization action when one market has materially
    better visibility than another.

    Surfaces ONLY when multi-market is enabled AND we have ≥2
    markets AND the visibility gap between best and worst is above
    threshold. Returns None otherwise — better to omit than to
    fabricate "you should localize" advice when the data doesn't
    actually show a localization gap.
    """
    if not markets_aggregate.get("enabled"):
        return None
    rows = list(markets_aggregate.get("per_market") or [])
    if len(rows) < 2:
        return None
    # Find best + worst by visibility score.
    by_visibility = sorted(
        [
            r for r in rows
            if isinstance(r.get("visibility_score"), int)
        ],
        key=lambda r: r["visibility_score"],
        reverse=True,
    )
    if len(by_visibility) < 2:
        return None
    best = by_visibility[0]
    worst = by_visibility[-1]
    gap = best["visibility_score"] - worst["visibility_score"]
    if gap < _LOCALIZATION_GAP_THRESHOLD:
        return None

    best_market = best["market_locale"]
    worst_market = worst["market_locale"]
    return {
        "severity": "high",
        "lever": "market_localization",
        "title": f"Localize for {worst_market} market",
        "body": (
            f"AI agents surface your products in {best_market} "
            f"(visibility {best['visibility_score']}/100) but not in "
            f"{worst_market} (visibility {worst['visibility_score']}/100) — "
            f"a {gap}-point gap. Closing this typically requires "
            f"market-specific listings (currency, sizing, language, "
            f"local retailers)."
        ),
        "concrete_next_step": (
            f"Audit your {worst_market} product copy: are sizes shown "
            f"in local units (UK / EU / JP)? Is currency converted? "
            f"Is the language localized (not just translated — "
            f"localization includes idiom + tone)? Does your shipping "
            f"page list {worst_market} as a supported destination?"
        ),
        "evidence": {
            "best_market": best_market,
            "best_visibility": best["visibility_score"],
            "worst_market": worst_market,
            "worst_visibility": worst["visibility_score"],
            "gap_points": gap,
        },
    }
