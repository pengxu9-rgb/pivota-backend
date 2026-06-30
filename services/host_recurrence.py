"""Cross-merchant cited-host recurrence — the BD Channel Graph demand signal.

`citation_observations` (migration 157) already stores one normalized row per
(audit_run, content_key, provider, query, cited_host): who got cited, on which
channel, by which model, for which query, first-party vs competitor. That makes a
cross-merchant *channel* rollup a pure read — unlike niche_query_recurrence
(migration 153), which needed its own write+read table because the niche queries
weren't otherwise persisted. So this module mirrors niche_recurrence's *style*
(best-effort, degrade to empty on any DB miss, never raise into a caller) but adds
NO write path and NO new table: it aggregates the existing observations at read
time.

The headline demand signal is **distinct_merchants** — a host cited across 40
brands is a partner-once-unlock-40 channel; a host seen for one brand is noise.
Each ranked host is enriched via `cited_host_classifier.classify_host` so the BD
operator sees type/categories/tier/AI-grounding weight and the reusable
`pitch_recipient` contact (already merchant-facing via pitch drafts), not a bare
hostname.

IMPORTANT (data completeness): rows are persisted ONLY for *depositable* products
(see services.catalog_identity.resolve_deposit_content_key). So every count here is
"over depositable audits", not total coverage. Callers that surface these numbers
externally must carry that caveat — never quote "N merchants cite host X" to a
channel as if it were the whole base.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from db.database import database
from services.cited_host_classifier import classify_host

logger = logging.getLogger(__name__)


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    return value if value is None else str(value)


def _enrich(host: str, observed_host_type: Optional[str]) -> Dict[str, Any]:
    """BD-actionable metadata for a host from the curated registry. Best-effort:
    classify_host never raises and degrades unknown hosts to 'unclassified'."""
    try:
        c = classify_host(host)
    except Exception as exc:  # noqa: BLE001 — enrichment must never break the rollup
        logger.debug("classify_host failed for %r: %s", host, str(exc)[:160])
        c = {"type": "unclassified"}
    host_type = c.get("type") or "unclassified"
    return {
        "type": host_type,
        "subtype": c.get("subtype"),
        "categories": c.get("categories") or [],
        "coverage_note": c.get("coverage_note"),
        "outreach_hint": c.get("outreach_hint"),
        "tier": c.get("tier"),
        "editorial_cadence": c.get("editorial_cadence"),
        "ai_grounding_weight": c.get("ai_grounding_weight"),
        "expected_outreach_cycle_weeks": c.get("expected_outreach_cycle_weeks"),
        # pitch_recipient: {email, submission_url, note} — present for the
        # editorial head only; already merchant-facing via pitch drafts.
        "contact": c.get("pitch_recipient"),
        "is_classified": host_type != "unclassified",
        # The host_type the audit pipeline actually observed (may differ from the
        # registry's curated type — surface both so the operator can spot drift).
        "observed_host_type": observed_host_type,
    }


async def channel_recurrence(
    *,
    limit: int = 100,
    min_merchants: int = 1,
    host_type: Optional[str] = None,
    provider: Optional[str] = None,
    since_days: Optional[int] = None,
    exclude_first_party: bool = False,
    db: Any = None,
) -> List[Dict[str, Any]]:
    """Rank cited hosts by cross-merchant demand over citation_observations.

    Returns one dict per host (already enriched), ordered by distinct_merchants
    desc then total_observations desc. Empty list on any DB miss (table absent,
    no rows) — best-effort, never raises.

    Filters (all optional): host_type (observed), provider, since_days (lookback
    window), exclude_first_party (drop rows where the host is the merchant's own
    domain), min_merchants (HAVING floor to cut single-brand noise).
    """
    read_db = db or database
    where = ["cited_host IS NOT NULL", "cited_host <> ''"]
    params: Dict[str, Any] = {
        "limit": max(1, int(limit or 1)),
        "min_merchants": max(1, int(min_merchants or 1)),
    }
    if host_type:
        where.append("host_type = :host_type")
        params["host_type"] = str(host_type)
    if provider:
        where.append("provider = :provider")
        params["provider"] = str(provider)
    if since_days:
        where.append("observed_at >= NOW() - make_interval(days => :since_days)")
        params["since_days"] = int(since_days)
    if exclude_first_party:
        where.append("first_party IS NOT TRUE")

    sql = f"""
        SELECT
            cited_host,
            COUNT(DISTINCT merchant_id)                        AS distinct_merchants,
            COUNT(DISTINCT content_key)                        AS distinct_products,
            COUNT(*)                                           AS total_observations,
            ARRAY_AGG(DISTINCT provider)                       AS providers,
            COUNT(DISTINCT provider)                           AS provider_count,
            MODE() WITHIN GROUP (ORDER BY host_type)           AS observed_host_type,
            COUNT(*) FILTER (WHERE first_party IS TRUE)        AS first_party_observations,
            COUNT(*) FILTER (WHERE is_competitor IS TRUE)      AS competitor_observations,
            MIN(observed_at)                                   AS first_observed_at,
            MAX(observed_at)                                   AS last_observed_at
        FROM citation_observations
        WHERE {' AND '.join(where)}
        GROUP BY cited_host
        HAVING COUNT(DISTINCT merchant_id) >= :min_merchants
        ORDER BY distinct_merchants DESC, total_observations DESC
        LIMIT :limit
    """
    try:
        rows = await read_db.fetch_all(sql, params)
    except Exception as exc:  # noqa: BLE001
        logger.debug("host_recurrence read failed: %s", str(exc)[:200])
        return []

    out: List[Dict[str, Any]] = []
    for r in rows or []:
        host = r["cited_host"]
        observed_type = r["observed_host_type"]
        rec: Dict[str, Any] = {
            "host": host,
            "distinct_merchants": int(r["distinct_merchants"] or 0),
            "distinct_products": int(r["distinct_products"] or 0),
            "total_observations": int(r["total_observations"] or 0),
            "providers": list(r["providers"] or []),
            "provider_count": int(r["provider_count"] or 0),
            "first_party_observations": int(r["first_party_observations"] or 0),
            "competitor_observations": int(r["competitor_observations"] or 0),
            "first_observed_at": _iso(r["first_observed_at"]),
            "last_observed_at": _iso(r["last_observed_at"]),
        }
        rec.update(_enrich(host, observed_type))
        out.append(rec)
    return out


async def channel_detail(
    host: str,
    *,
    merchant_limit: int = 200,
    query_limit: int = 50,
    db: Any = None,
) -> Dict[str, Any]:
    """Drill-down for one cited host: per-provider coverage, per-merchant
    breakdown (best-effort merchant_name via catalog_merchants), and sample
    queries. Returns {host, found: bool, ...}; found=False when the host has no
    observations. Best-effort — never raises."""
    read_db = db or database
    norm = str(host or "").strip()
    base = {"host": norm, "found": False}
    if not norm:
        return base

    # Per-provider coverage.
    try:
        provider_rows = await read_db.fetch_all(
            """
            SELECT provider,
                   COUNT(DISTINCT merchant_id) AS distinct_merchants,
                   COUNT(*)                    AS observations
            FROM citation_observations
            WHERE cited_host = :host
            GROUP BY provider
            ORDER BY distinct_merchants DESC, observations DESC
            """,
            {"host": norm},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("channel_detail provider read failed for %r: %s", norm, str(exc)[:160])
        return base
    if not provider_rows:
        return base

    # Per-merchant breakdown — LEFT JOIN catalog_merchants for a display name
    # (best-effort: null name when id-spaces differ → caller falls back to id).
    try:
        merchant_rows = await read_db.fetch_all(
            """
            SELECT co.merchant_id,
                   cm.merchant_name,
                   COUNT(*)                      AS observations,
                   ARRAY_AGG(DISTINCT co.provider) AS providers,
                   BOOL_OR(co.first_party IS TRUE)  AS any_first_party,
                   BOOL_OR(co.is_competitor IS TRUE) AS any_competitor,
                   MAX(co.observed_at)           AS last_observed_at,
                   MIN(co.evidence_url)          AS sample_evidence_url
            FROM citation_observations co
            LEFT JOIN catalog_merchants cm ON cm.merchant_id = co.merchant_id
            WHERE co.cited_host = :host
            GROUP BY co.merchant_id, cm.merchant_name
            ORDER BY observations DESC
            LIMIT :limit
            """,
            {"host": norm, "limit": max(1, int(merchant_limit or 1))},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("channel_detail merchant read failed for %r: %s", norm, str(exc)[:160])
        merchant_rows = []

    # Distinct sample queries this host was cited for.
    try:
        query_rows = await read_db.fetch_all(
            """
            SELECT query, COUNT(*) AS observations
            FROM citation_observations
            WHERE cited_host = :host AND query IS NOT NULL AND query <> ''
            GROUP BY query
            ORDER BY observations DESC
            LIMIT :limit
            """,
            {"host": norm, "limit": max(1, int(query_limit or 1))},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("channel_detail query read failed for %r: %s", norm, str(exc)[:160])
        query_rows = []

    # Accurate distinct-merchant count (merchant_rows is capped at merchant_limit,
    # so len() would under-report vs the list view's uncapped COUNT(DISTINCT)).
    distinct_merchants = len(merchant_rows)
    try:
        mc = await read_db.fetch_one(
            "SELECT COUNT(DISTINCT merchant_id) AS c FROM citation_observations "
            "WHERE cited_host = :host",
            {"host": norm},
        )
        if mc and mc["c"] is not None:
            distinct_merchants = int(mc["c"])
    except Exception as exc:  # noqa: BLE001
        logger.debug("channel_detail count read failed for %r: %s", norm, str(exc)[:160])

    out: Dict[str, Any] = {
        "host": norm,
        "found": True,
        "distinct_merchants": distinct_merchants,
        "total_observations": sum(int(p["observations"] or 0) for p in provider_rows),
        "providers": [
            {
                "provider": p["provider"],
                "distinct_merchants": int(p["distinct_merchants"] or 0),
                "observations": int(p["observations"] or 0),
            }
            for p in provider_rows
        ],
        "merchants": [
            {
                "merchant_id": m["merchant_id"],
                "merchant_name": m["merchant_name"],
                "observations": int(m["observations"] or 0),
                "providers": list(m["providers"] or []),
                "first_party": bool(m["any_first_party"]),
                "competitor": bool(m["any_competitor"]),
                "last_observed_at": _iso(m["last_observed_at"]),
                "sample_evidence_url": m["sample_evidence_url"],
            }
            for m in merchant_rows
        ],
        "sample_queries": [
            {"query": q["query"], "observations": int(q["observations"] or 0)}
            for q in query_rows
        ],
    }
    out.update(_enrich(norm, None))
    return out
