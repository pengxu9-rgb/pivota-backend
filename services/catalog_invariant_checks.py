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
# compiles :func:`pdp_renderable_expression`, so a fix to that predicate (P3's
# minted lane) moves this count and the sitemap in one step, instead of leaving
# the alarm calibrated to a gap that no longer exists.
#
# ⚠️ SINCE 2026-07-26 THIS CHECK AND THE FEED ASK DIFFERENT QUESTIONS, ON
# PURPOSE. An earlier version of this comment claimed they "cannot disagree about
# what renderable means". They now do, and the difference is deliberate: the
# feed's `renderable` column compiles `pdp_will_render_expression` — the content
# route AND get_pdp_v2's serving-eligibility gate — while this invariant stays on
# the content-route half alone, because its threshold is a MEASURED baseline (1,
# on prod) for the narrower question "the trust policy let this row reach 'public'
# while the gateway has no resolvable content route". Folding the serving gate in
# would redefine the alarm and decalibrate the threshold in one move, with no
# re-measurement. Pinned by
# test_public_not_renderable_invariant_stays_on_the_route_only_predicate.
#
# CONSEQUENCE, stated so it is not mistaken for coverage: nothing here alarms on a
# row that is trust-`public` and fails the SERVING gate. With INDEX_ELIGIBLE_READ
# on in prod, catalog_trust_policy can still promote an
# `index_eligible AND NOT serving_eligible` row to 'public', so that class stops
# being ADVERTISED (the feed drops it) without becoming MONITORED. A serving-gate
# invariant is a separate check and needs its own measured baseline.
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
# PUBLIC_NOT_RENDERABLE, which tracks the residual set of rows the trust policy
# lets reach 'public' while the gateway has no resolvable PDP content route.
#
# The default is the MEASURED BASELINE, never an aspiration. A threshold below
# the true count leaves the check permanently red, and a permanently-red check
# cannot signal a NEW regression — it is indistinguishable from the known gap.
# Raising it needs a reason; lowering it is mandatory the moment the true count
# drops, or the alarm goes deaf by exactly the size of the fix.
#
# 2026-07-25, P3: the baseline was 1,376 — 1,375 Path-C minted rows whose seeds
# attach by attached_product_key (so the gateway's external_product_id lookup
# never found them) plus 1 audit-minted url_audit row. PIVOTA-Agent P3 taught
# get_pdp_v2 to resolve that second key and services/pdp_renderability now says
# so, which re-measures the count at exactly 1 on prod: the url_audit row, and
# nothing else. Corpus-wide the predicate change flipped 2,051 rows to
# renderable and ZERO rows away from it.
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
            "route for the row — the URL answers with an HTTP 500 or a generic "
            "noindex shell carrying no product JSON-LD, never a real PDP "
            "(c1.v0.5 gap)"
        ),
        "env": "CATALOG_INVARIANT_RENDERABLE_THRESHOLD",
        # Measured baseline 2026-07-25 AFTER P3, NOT an aspiration — see the
        # note above _CHECKS on why an under-set threshold destroys the signal,
        # and why leaving it at the pre-P3 1,376 would have been just as bad in
        # the other direction (1,375 rows of head-room for a silent regression).
        # The 1 is the single url_audit row: audit-minted, no seed, no sync
        # adapter, measured HTTP 500. It goes to 0 when that row is either
        # given a route or dropped out of 'public'.
        "default_threshold": 1,
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
