"""ADR-012 Phase 0b — internal-consistency invariants for the serving surface.

Each invariant is a Postgres count of rows where the SERVED state contradicts
upstream truth. These are direct correctness checks, deliberately independent
of the completeness-style quality score (which has never caught this class:
stale-served quarantined stores, shell PDPs, public rows with no offer).

Shared by the on-demand endpoint (routes/__catalog_health.py) and the daily
sweep job (jobs/catalog_invariant_sweep_job.py). Read-only; no writes.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from sqlalchemy import String, and_, column, func, not_, select, table

from db.catalog import catalog_products
from services.pdp_renderability import compile_pg, pdp_renderable_expression

logger = logging.getLogger(__name__)

_SAMPLE_LIMIT = 5


# --- public_not_renderable, built from the shared renderability predicate ----
#
# This check used to hand-write "no approved + live_read_enabled
# pdp_identity_listing row exists" — the same belief the sitemap feed held, and
# the same belief 29 live PDP fetches disproved on 2026-07-25 (rows with NO
# identity listing at all render full 200s; rows with one render 500s). It now
# compiles :func:`pdp_renderable_expression`, so the invariant and the feed
# cannot disagree about what "renderable" means.
_crt = table(
    "catalog_row_trust",
    column("subject_type", String),
    column("subject_key", String),
    column("serving_decision", String),
)
_cp = catalog_products.alias("cp")

_NOT_RENDERABLE_PUBLIC_WHERE = and_(
    _crt.c.subject_type == "product",
    _crt.c.serving_decision == "public",
    not_(pdp_renderable_expression(_cp)),
)
_NOT_RENDERABLE_PUBLIC_FROM = _crt.join(
    _cp, _cp.c.product_key == _crt.c.subject_key
)

_PUBLIC_NOT_RENDERABLE_COUNT_SQL = compile_pg(
    select(func.count().label("c"))
    .select_from(_NOT_RENDERABLE_PUBLIC_FROM)
    .where(_NOT_RENDERABLE_PUBLIC_WHERE)
)
_PUBLIC_NOT_RENDERABLE_SAMPLE_SQL = compile_pg(
    select(_crt.c.subject_key)
    .select_from(_NOT_RENDERABLE_PUBLIC_FROM)
    .where(_NOT_RENDERABLE_PUBLIC_WHERE)
    .limit(_SAMPLE_LIMIT)
)

# Each check: (name, threshold_env, default_threshold, description, count SQL,
# sample SQL). Violation = count > threshold. Thresholds default to 0 except
# PUBLIC_NOT_RENDERABLE: the trust policy still lets rows reach 'public' whose
# PDP has no resolvable content route (measured 2026-07-25: 1,375 Path-C minted
# rows whose seeds attach by attached_product_key, so the gateway's
# external_product_id lookup never finds them, plus 1 audit-minted url_audit
# row) — threshold is env-tunable so the sweep tracks the number without paging
# until CATALOG_TRUST_RENDERABLE_GATE is flipped on or the P3 identity path for
# catalog_enrichment_agent_v1 ships.
_CHECKS: List[Dict[str, Any]] = [
    {
        "name": "public_but_suppressed",
        "description": "trust says public but catalog row is tombstoned",
        "env": "CATALOG_INVARIANT_SUPPRESSED_THRESHOLD",
        "default_threshold": 0,
        "count_sql": """
            SELECT count(*) AS c
            FROM catalog_row_trust crt
            JOIN catalog_products cp ON cp.product_key = crt.subject_key
            WHERE crt.subject_type = 'product'
              AND crt.serving_decision = 'public'
              AND cp.suppression_reason IS NOT NULL
        """,
        "sample_sql": """
            SELECT crt.subject_key
            FROM catalog_row_trust crt
            JOIN catalog_products cp ON cp.product_key = crt.subject_key
            WHERE crt.subject_type = 'product'
              AND crt.serving_decision = 'public'
              AND cp.suppression_reason IS NOT NULL
            LIMIT 5
        """,
    },
    {
        "name": "public_not_live",
        "description": "trust says public but catalog sync_status is not 'live'",
        "env": "CATALOG_INVARIANT_NOT_LIVE_THRESHOLD",
        "default_threshold": 0,
        "count_sql": """
            SELECT count(*) AS c
            FROM catalog_row_trust crt
            JOIN catalog_products cp ON cp.product_key = crt.subject_key
            WHERE crt.subject_type = 'product'
              AND crt.serving_decision = 'public'
              AND cp.sync_status IS DISTINCT FROM 'live'
        """,
        "sample_sql": """
            SELECT crt.subject_key
            FROM catalog_row_trust crt
            JOIN catalog_products cp ON cp.product_key = crt.subject_key
            WHERE crt.subject_type = 'product'
              AND crt.serving_decision = 'public'
              AND cp.sync_status IS DISTINCT FROM 'live'
            LIMIT 5
        """,
    },
    {
        "name": "public_without_priced_offer",
        "description": "trust says public but no catalog_offers row with list_price > 0",
        "env": "CATALOG_INVARIANT_NO_OFFER_THRESHOLD",
        "default_threshold": 0,
        "count_sql": """
            SELECT count(*) AS c
            FROM catalog_row_trust crt
            WHERE crt.subject_type = 'product'
              AND crt.serving_decision = 'public'
              AND NOT EXISTS (
                  SELECT 1 FROM catalog_offers co
                  WHERE co.product_key = crt.subject_key
                    AND co.list_price > 0
              )
        """,
        "sample_sql": """
            SELECT crt.subject_key
            FROM catalog_row_trust crt
            WHERE crt.subject_type = 'product'
              AND crt.serving_decision = 'public'
              AND NOT EXISTS (
                  SELECT 1 FROM catalog_offers co
                  WHERE co.product_key = crt.subject_key
                    AND co.list_price > 0
              )
            LIMIT 5
        """,
    },
    {
        "name": "public_not_renderable",
        "description": (
            "trust says public but the gateway has no resolvable PDP content "
            "route for the row (measured: hard HTTP 500, not a shell) — "
            "c1.v0.5 gap"
        ),
        "env": "CATALOG_INVARIANT_RENDERABLE_THRESHOLD",
        "default_threshold": 500,
        "count_sql": _PUBLIC_NOT_RENDERABLE_COUNT_SQL,
        "sample_sql": _PUBLIC_NOT_RENDERABLE_SAMPLE_SQL,
    },
    {
        "name": "orphan_trust_rows",
        "description": "trust rows whose catalog_products row no longer exists",
        "env": "CATALOG_INVARIANT_ORPHAN_THRESHOLD",
        "default_threshold": 25,
        "count_sql": """
            SELECT count(*) AS c
            FROM catalog_row_trust crt
            WHERE crt.subject_type = 'product'
              AND NOT EXISTS (
                  SELECT 1 FROM catalog_products cp
                  WHERE cp.product_key = crt.subject_key
              )
        """,
        "sample_sql": """
            SELECT crt.subject_key
            FROM catalog_row_trust crt
            WHERE crt.subject_type = 'product'
              AND NOT EXISTS (
                  SELECT 1 FROM catalog_products cp
                  WHERE cp.product_key = crt.subject_key
              )
            LIMIT 5
        """,
    },
    {
        "name": "missing_trust_rows",
        "description": "catalog rows with no trust row (fail-closed invisible)",
        "env": "CATALOG_TRUST_DRIFT_ALERT_THRESHOLD",
        "default_threshold": 50,
        "count_sql": """
            SELECT count(*) AS c
            FROM catalog_products cp
            LEFT JOIN catalog_row_trust crt
                ON crt.subject_type = 'product'
               AND crt.subject_key  = cp.product_key
            WHERE crt.subject_key IS NULL
        """,
        "sample_sql": """
            SELECT cp.product_key AS subject_key
            FROM catalog_products cp
            LEFT JOIN catalog_row_trust crt
                ON crt.subject_type = 'product'
               AND crt.subject_key  = cp.product_key
            WHERE crt.subject_key IS NULL
            LIMIT 5
        """,
    },
]


def _threshold(check: Dict[str, Any]) -> int:
    raw = os.getenv(check["env"])
    if raw is None:
        return int(check["default_threshold"])
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        logger.warning(
            "catalog_invariants: invalid %s=%r; using %d",
            check["env"], raw, check["default_threshold"],
        )
        return int(check["default_threshold"])


async def run_catalog_invariant_checks(db: Any) -> Dict[str, Any]:
    """Run every invariant; return counts, thresholds, and sample keys for
    violated checks. Never raises for a single failing check — a check that
    errors is reported as {"error": ...} so the rest still run."""
    results: List[Dict[str, Any]] = []
    violated = 0
    for check in _CHECKS:
        entry: Dict[str, Any] = {
            "name": check["name"],
            "description": check["description"],
        }
        try:
            row = await db.fetch_one(check["count_sql"])
            count = int((row["c"] if row is not None else 0) or 0)
            threshold = _threshold(check)
            entry["count"] = count
            entry["threshold"] = threshold
            entry["violated"] = count > threshold
            if entry["violated"]:
                violated += 1
                samples = await db.fetch_all(check["sample_sql"])
                entry["sample_keys"] = [r["subject_key"] for r in samples]
        except Exception as exc:  # noqa: BLE001 — one bad check must not sink the sweep
            logger.exception("catalog_invariants: check %s failed", check["name"])
            entry["error"] = str(exc)
        results.append(entry)
    return {"violated_count": violated, "checks": results}
