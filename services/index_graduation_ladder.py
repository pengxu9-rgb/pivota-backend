"""Convergence Phase 1.5 — ONE graduation ladder for the commerce-index tiers.

The audit door (services.audit_index_intake) and the external-seed mirror door
stamp an OBSERVED, unclaimed record with the honest tier triple
``external_referral / observed / referral_only`` and NEVER re-assert it on
conflict, so a row's tiers can only move via a graduation ladder. Until now no
code advanced them: every observed row stayed ``referral_only`` forever, even
after it proved (by passing the index eligibility gate) that it is servable.

This module is that ladder — the SINGLE transition writer for
``catalog_products.readiness_tier`` on the OBSERVED / ``external_referral``
track. It advances a row UP the readiness ladder as
``index_pipeline_state_service`` (the authoritative eligibility oracle) reports
the row has cleared a higher serving floor. It NEVER downgrades, NEVER touches
first-party rows, and NEVER invents new eligibility criteria — the ladder in
``index_pipeline_state_service.py`` stays authoritative (Phase-1 plan constraint).

Semantics on the OBSERVED track (``catalog_track='external_referral'`` +
``truth_tier='observed'``) — readiness_tier reflects the IPS serving FLOOR the
row has cleared, NOT the vertical-enrichment depth axis that first-party sync
uses for the same column:

    referral_only    initial: not yet index-eligible (thin / unresolved seed)
      → knowledge_ready   index_eligible == True  (citable, OFFER-FREE floor:
                          quality+image+description+identity resolved, no price)
      → commerce_ready    serving_eligible == True (index floor PLUS a real
                          priced catalog_offers row → servable as a redirect)

``catalog_track`` (provenance) and ``truth_tier`` (verification) are LEFT
UNTOUCHED here: an audit pass makes an observed row *servable*, it does not make
it *first-party*. Track/truth graduation (observed→primary on a verified claim)
is a separate event owned by the claim/connect path and is intentionally out of
this slice's scope; the writer below is generic enough for that hook to reuse.

Compounds with Phase 1.6 (external offers → persisted ``catalog_offers``): once
an external row carries a priced offer, ``has_price`` flips true in the oracle
and this ladder can advance it to ``commerce_ready``. Mirror-materialized seeds
already carry offers, so this ladder is not a no-op today.

Dark-launch discipline: mutation is gated by ``INDEX_GRADUATION_LADDER_ENABLED``
(default OFF). With it off, callers still run but the writer is a no-op, so
behavior is byte-identical to today. Every function is best-effort + idempotent
and NEVER raises into its caller (nightly job / audit path must not break).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from db.database import database

logger = logging.getLogger(__name__)

# The OBSERVED-track readiness ladder, low → high. Only rows whose current
# readiness_tier is IN this list (and strictly below the target) advance; a row
# already at the top, or carrying a value off this ladder (e.g. a first-party
# 'vertical_ready'), is left untouched by the monotonic guard.
OBSERVED_READINESS_LADDER = ["referral_only", "knowledge_ready", "commerce_ready"]

# The ladder only ever touches the observed / external-referral track. First-
# party rows (internal_merchant / primary) are never eligible for advancement
# here — their readiness is owned by catalog sync.
_OBSERVED_CATALOG_TRACK = "external_referral"
_OBSERVED_TRUTH_TIER = "observed"

GRADUATION_REASON = "index_graduation"


def graduation_ladder_enabled() -> bool:
    """Flag: allow the ladder to WRITE tier transitions. Default OFF.

    Off → detection still runs (callers compute targets, logs emit) but the
    UPDATE is skipped, so the ladder is observable while dark.
    """
    return os.getenv("INDEX_GRADUATION_LADDER_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def target_readiness_tier(
    *, index_eligible: bool, serving_eligible: bool
) -> Optional[str]:
    """Map the IPS oracle's two floors to the highest readiness_tier the row
    has earned on the observed track. Returns None when the row has not cleared
    the citation floor (stays referral_only)."""
    if serving_eligible:
        return "commerce_ready"
    if index_eligible:
        return "knowledge_ready"
    return None


def _tiers_below(target: str) -> list:
    """The ladder values strictly below `target` — the only current tiers a
    monotonic advance to `target` may move. Empty when `target` is the floor."""
    idx = OBSERVED_READINESS_LADDER.index(target)
    return OBSERVED_READINESS_LADDER[:idx]


# The monotonic "never downgrade" guard is expressed as an explicit IN-list of
# the tiers strictly below the target (portable across Postgres/SQLite; no array
# ops). The WHERE pins catalog_track/truth_tier to the observed track so first-
# party rows are excluded regardless. CURRENT_TIMESTAMP works on both backends.
_REQUIRE_PRICE_CLAUSE = """
      AND EXISTS (
            SELECT 1 FROM catalog_offers co
            WHERE co.product_key = catalog_products.product_key
              AND co.list_price > 0
          )
"""


def _build_where(below: list, *, require_row_price: bool) -> tuple:
    """Build the shared WHERE fragment + params for count/update of advanceable
    observed rows. `below` are the current tiers eligible to move to the target.
    When the target needs a real price (commerce_ready), only rows that
    themselves carry a priced catalog_offers row qualify — the same per-product
    has_price signal the oracle uses (never label a price-less row commerce_ready
    just because a sibling row for the content_key is priced)."""
    placeholders = ", ".join(f":b{i}" for i in range(len(below)))
    where = (
        "content_key = :content_key "
        "AND catalog_track = :observed_track "
        "AND truth_tier = :observed_truth "
        f"AND readiness_tier IN ({placeholders})"
    )
    if require_row_price:
        where += _REQUIRE_PRICE_CLAUSE
    params = {
        "content_key": None,  # filled by caller
        "observed_track": _OBSERVED_CATALOG_TRACK,
        "observed_truth": _OBSERVED_TRUTH_TIER,
    }
    for i, tier in enumerate(below):
        params[f"b{i}"] = tier
    return where, params


async def _advance_to(
    content_key: str,
    target: str,
    *,
    require_row_price: bool,
    reason: str,
) -> int:
    """Advance observed rows for a content_key up to `target`. Returns the count
    of rows moved (0 when the flag is off, nothing qualifies, or on error)."""
    if not content_key or target not in OBSERVED_READINESS_LADDER:
        return 0
    below = _tiers_below(target)
    if not below:  # target is the floor; nothing can move up to it
        return 0
    if not graduation_ladder_enabled():
        logger.info({
            "event": "index_graduation_skipped_dark",
            "content_key": content_key,
            "target": target,
            "reason": reason,
        })
        return 0
    where, params = _build_where(below, require_row_price=require_row_price)
    params["content_key"] = content_key
    try:
        # execute()'s return value isn't a reliable rowcount across drivers
        # (see scripts/backfill_audit_seed_tier_labels.py), so count the
        # advanceable rows first, then update.
        row = await database.fetch_one(
            f"SELECT COUNT(*) AS n FROM catalog_products WHERE {where}", params
        )
        advanced = int(dict(row).get("n") or 0) if row else 0
        if not advanced:
            return 0
        await database.execute(
            f"UPDATE catalog_products SET readiness_tier = :target, "
            f"updated_at = CURRENT_TIMESTAMP WHERE {where}",
            {**params, "target": target},
        )
        logger.info({
            "event": "index_graduation_advanced",
            "content_key": content_key,
            "target": target,
            "rows": advanced,
            "reason": reason,
        })
        return advanced
    except Exception as exc:  # noqa: BLE001
        logger.warning({
            "event": "index_graduation_advance_failed",
            "content_key": content_key,
            "target": target,
            "reason": reason,
            "error": str(exc),
        })
        return 0


async def advance_from_state(
    content_key: str,
    state: Dict[str, Any],
    *,
    reason: str = GRADUATION_REASON,
) -> Dict[str, Any]:
    """Advance a content_key using an ALREADY-COMPUTED classified IPS state
    (the nightly job path — avoids a redundant recompute). `state` is the dict
    returned by index_pipeline_state_service._classify_product /
    _select_content_key_state (carries index_eligible / serving_eligible)."""
    target = target_readiness_tier(
        index_eligible=bool(state.get("index_eligible")),
        serving_eligible=bool(state.get("serving_eligible")),
    )
    if target is None:
        return {"content_key": content_key, "target": None, "advanced": 0}
    advanced = await _advance_to(
        content_key,
        target,
        require_row_price=(target == "commerce_ready"),
        reason=reason,
    )
    return {"content_key": content_key, "target": target, "advanced": advanced}


async def graduate_content_key(
    content_key: str,
    *,
    reason: str = GRADUATION_REASON,
) -> Dict[str, Any]:
    """Recompute eligibility for a content_key (persisting IPS) and advance its
    readiness tier. The on-demand / backfill entry point — safe to call after an
    audit pass. Best-effort; never raises."""
    if not content_key:
        return {"content_key": content_key, "target": None, "advanced": 0}
    from services.index_pipeline_state_service import recompute_serving_eligibility

    try:
        # Persist the fresh classification, then read both floors back. recompute
        # returns only serving_eligible; index_eligible lives on the row it wrote.
        await recompute_serving_eligibility(content_key, reason=reason)
        row = await database.fetch_one(
            "SELECT index_eligible, serving_eligible "
            "FROM index_pipeline_state WHERE content_key = :ck",
            {"ck": content_key},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning({
            "event": "index_graduation_recompute_failed",
            "content_key": content_key,
            "reason": reason,
            "error": str(exc),
        })
        return {"content_key": content_key, "target": None, "advanced": 0}

    if not row:
        return {"content_key": content_key, "target": None, "advanced": 0}
    return await advance_from_state(
        content_key,
        {
            "index_eligible": dict(row).get("index_eligible"),
            "serving_eligible": dict(row).get("serving_eligible"),
        },
        reason=reason,
    )
