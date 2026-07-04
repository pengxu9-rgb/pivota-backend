"""Controlled-signal scorecard.

Measures the data we control — coverage, freshness, and agent-exposure — per
decision signal, so we pick the biggest gap deliberately instead of perfecting
one signal. "Measure the data, don't just emit it." This is the rollout
dashboard and the seed of the future merchant-facing "index health" view.

The universe is the set of serving-eligible content_keys (the SKUs that can
actually reach agents). Each signal reports how many of those carry it, how
fresh it is, and whether it's exposed to agents today.

Every signal is computed DEFENSIVELY: some signal tables (relationship graph,
product-intel KB) are written by the PIVOTA-Agent gateway and co-reside in the
same Postgres but are not defined in this repo's migrations, so a query that
can't run degrades that one signal to {available: false, error} rather than
failing the whole scorecard.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from db.database import database

logger = logging.getLogger(__name__)


# Static exposure registry — what an agent actually sees today (from the
# 2026-06-12 agent-surface scan). Coverage numbers are computed live; this
# captures the EXPOSURE axis, which is config/code state, not a DB fact.
#   live     — in the agent MCP payload now
#   gated    — built + wired but behind a default-off flag (data-starved)
#   internal — computed, used internally, never sent to agents
#   none     — not exposed (chat-only, or not yet built into the agent surface)
EXPOSURE: Dict[str, Dict[str, str]] = {
    "catalog_base": {"state": "live", "note": "search_catalog / get_product (ranking noise stripped)"},
    "quality_meta": {"state": "internal", "note": "serving gate only; scores not sent to agents"},
    "offers": {"state": "gated", "note": "get_offers built; flag AURORA_BFF_RELATIONSHIP_GRAPH_AGENT_ENABLED off"},
    "trust": {"state": "live", "note": "get_seller_trust + seller_trust on agent_pdp_view offers (outcome-derived, sample-gated; W8)"},
    "alternatives": {"state": "gated", "note": "get_alternatives built; same flag off; data thin"},
    "intel": {"state": "none", "note": "why/fit/evidence is chat-only; not on the agent PDP"},
    "outcomes": {"state": "internal", "note": "aggregated_outcomes computed + refreshed daily; surfaced via seller_trust (offers) + merchant brand rollup"},
}


async def _scalar(db: Any, sql: str, params: Optional[Dict[str, Any]] = None) -> Optional[int]:
    row = await db.fetch_one(sql, params or {})
    if row is None:
        return None
    val = list(dict(row).values())[0]
    return int(val) if val is not None else 0


def _pct(covered: Optional[int], denom: Optional[int]) -> Optional[float]:
    if not denom or covered is None:
        return None
    return round(100.0 * covered / denom, 1)


def _signal(
    key: str,
    label: str,
    *,
    covered: Optional[int] = None,
    denominator: Optional[int] = None,
    freshness_median_age_days: Optional[float] = None,
    extra: Optional[Dict[str, Any]] = None,
    available: bool = True,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    exposure = EXPOSURE.get(key, {"state": "unknown", "note": ""})
    out: Dict[str, Any] = {
        "signal": key,
        "label": label,
        "available": available,
        "covered": covered,
        "denominator": denominator,
        "coverage_pct": _pct(covered, denominator),
        "freshness_median_age_days": freshness_median_age_days,
        "exposed_to_agent": exposure["state"],
        "exposure_note": exposure["note"],
    }
    if extra:
        out.update(extra)
    if error:
        out["error"] = error
    return out


# --- universe -----------------------------------------------------------------

async def _serving_eligible_count(db: Any) -> int:
    return await _scalar(
        db, "SELECT count(*) FROM index_pipeline_state WHERE serving_eligible IS TRUE"
    ) or 0


# --- per-signal coverage ------------------------------------------------------

async def _catalog_base(db: Any, eligible: int) -> Dict[str, Any]:
    total = await _scalar(db, "SELECT count(*) FROM index_pipeline_state") or 0
    return _signal("catalog_base", "Serving-eligible products",
                   covered=eligible, denominator=total)


async def _quality_meta(db: Any, eligible: int) -> Dict[str, Any]:
    covered = await _scalar(
        db,
        "SELECT count(*) FROM index_pipeline_state "
        "WHERE serving_eligible IS TRUE AND content_quality_score IS NOT NULL",
    )
    age = await _scalar(
        db,
        "SELECT (percentile_cont(0.5) WITHIN GROUP "
        "(ORDER BY EXTRACT(EPOCH FROM (now() - last_consolidated_at))))::bigint / 86400 "
        "FROM index_pipeline_state WHERE serving_eligible IS TRUE",
    )
    return _signal("quality_meta", "Quality / freshness metadata",
                   covered=covered, denominator=eligible,
                   freshness_median_age_days=float(age) if age is not None else None)


async def _offers(db: Any, eligible: int) -> Dict[str, Any]:
    covered = await _scalar(
        db,
        "SELECT count(DISTINCT ips.content_key) "
        "FROM index_pipeline_state ips "
        "JOIN catalog_products cp ON cp.content_key = ips.content_key "
        "JOIN catalog_offers co ON co.product_key = cp.product_key "
        "WHERE ips.serving_eligible IS TRUE "
        "AND co.suppressed_at IS NULL AND co.list_price > 0",
    )
    return _signal("offers", "Offers / buy-box", covered=covered, denominator=eligible)


async def _trust(db: Any, eligible: int) -> Dict[str, Any]:
    covered = await _scalar(
        db,
        "SELECT count(DISTINCT ips.content_key) "
        "FROM index_pipeline_state ips "
        "JOIN catalog_row_trust t ON t.content_key = ips.content_key "
        "WHERE ips.serving_eligible IS TRUE AND t.serving_decision = 'public'",
    )
    return _signal("trust", "Merchant / seller trust", covered=covered, denominator=eligible)


async def _alternatives(db: Any, eligible: int) -> Dict[str, Any]:
    # Agent-side served view. Keyed on ext_* anchors, which don't join cleanly to
    # content_key, so we report served-edge breadth, not content_key coverage.
    anchors = await _scalar(
        db, "SELECT count(DISTINCT anchor_ref) FROM product_relationship_edges"
    )
    edges = await _scalar(db, "SELECT count(*) FROM product_relationship_edges")
    return _signal(
        "alternatives", "Alternatives (relationship graph)",
        covered=anchors, denominator=eligible,
        extra={
            "served_edges": edges,
            "coverage_caveat": "anchors are ext_* keyed; not a clean content_key join — breadth, not true coverage",
        },
    )


async def _intel(db: Any, eligible: int) -> Dict[str, Any]:
    # Agent-side KB, keyed on kb_key (not content_key). Report raw breadth.
    entries = await _scalar(db, "SELECT count(*) FROM aurora_product_intel_kb")
    age = await _scalar(
        db,
        "SELECT (percentile_cont(0.5) WITHIN GROUP "
        "(ORDER BY EXTRACT(EPOCH FROM (now() - updated_at))))::bigint / 86400 FROM aurora_product_intel_kb",
    )
    return _signal(
        "intel", "Why / fit / evidence (intel KB)",
        covered=entries, denominator=eligible,
        freshness_median_age_days=float(age) if age is not None else None,
        extra={"coverage_caveat": "kb_key keyed; entry count vs eligible is indicative, not a join"},
    )


async def _outcomes(db: Any, eligible: int) -> Dict[str, Any]:
    links = await _scalar(db, "SELECT count(*) FROM agent_decision_funnel_links")
    decisions = await _scalar(
        db,
        "SELECT count(DISTINCT decision_id) FROM agent_decision_funnel_links "
        "WHERE decision_id IS NOT NULL",
    )
    age = await _scalar(
        db,
        "SELECT (percentile_cont(0.5) WITHIN GROUP "
        "(ORDER BY EXTRACT(EPOCH FROM (now() - created_at))))::bigint / 86400 FROM agent_decision_funnel_links",
    )
    return _signal(
        "outcomes", "Outcomes (rail_transacted joins)",
        covered=links, denominator=None,
        freshness_median_age_days=float(age) if age is not None else None,
        extra={"distinct_decisions_linked": decisions},
    )


_SIGNALS: List[Callable[[Any, int], Awaitable[Dict[str, Any]]]] = [
    _catalog_base, _quality_meta, _offers, _trust, _alternatives, _intel, _outcomes,
]


async def compute_scorecard(*, db: Any = None) -> Dict[str, Any]:
    read_db = db or database
    eligible = await _serving_eligible_count(read_db)
    signals: List[Dict[str, Any]] = []
    for fn in _SIGNALS:
        key = fn.__name__.lstrip("_")
        try:
            signals.append(await fn(read_db, eligible))
        except Exception as exc:  # noqa: BLE001 — one signal's failure must not sink the rest
            logger.warning("scorecard signal %s failed: %s", key, exc)
            signals.append(_signal(key, key, available=False, error=str(exc)[:200]))
    return {"serving_eligible_universe": eligible, "signals": signals}
