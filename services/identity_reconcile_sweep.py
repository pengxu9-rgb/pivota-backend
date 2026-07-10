"""ADR-010 D-2 Phase B — the standing catalog-reconciliation sweep.

Weekly tick (wired in services/audit_scheduler.py, prod-worker-gated there,
and DORMANT unless ENABLE_IDENTITY_RECONCILE_SWEEP=1 — same defense-in-depth
as the payment-reconcile tick, because staging shares the prod DB):

  1. gauges     — the D-1 duplication numbers (same-merchant / cross-merchant
                  dup keys), the sweep's scoreboard;
  2. classify   — the step-5 working-set report over live catalog state;
  3. propose    — every strategy plugin, upserted (dedupe on proposal_key);
  4. apply      — auto-approve + apply ONLY the strategies step-5 proved
                  mechanical (AUTO_APPROVE_STRATEGIES); everything else stays
                  'proposed';
  5. review     — non-allowlisted suppress proposals and ambiguous labels
                  become pdp_review_tasks (module 'identity') with
                  deterministic ids (idempotent re-enqueue);
  6. alert      — if any gauge ROSE since the previous sweep, log at ERROR
                  and flag the sweep event (a rise means the ADR-011 intake
                  contract is leaking — it should be impossible);
  7. record     — one identity_resolution_events row (action='sweep') with
                  the full run summary; the next sweep reads it for step 6.

Every apply inherits the engine's guard set (drift fingerprints, keeper
checks, bidirectional seed linkage, post-checks, reversible run-id
tombstones) — see services/identity_resolution.py.

Manual runs / first-light: scripts/run_identity_reconcile_sweep.py
(propose-only by default).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from scripts.step5_lane2_same_url_dedup import DETAIL_SQL
from scripts.step5_working_set import (
    ORPHAN_MIRRORS_SQL,
    WORKING_ROWS_SQL,
    build_report,
)
from services.identity_resolution import (
    INSERT_EVENT_SQL,
    apply_approved,
    upsert_proposals,
)
from services.identity_resolution_strategies import build_all_proposals

logger = logging.getLogger("identity_reconcile_sweep")

# The ONLY strategies the sweep may apply without a human decision — the two
# step-5 proved mechanical. Widening this list is a reviewed decision, not a
# config change: it is deliberately a code constant with a test pinning it.
AUTO_APPROVE_STRATEGIES: Tuple[str, ...] = ("same_url_dup", "junk_url")

GAUGES_SQL = {
    "same_merchant_dup_keys": """
        SELECT COUNT(*) FROM (
            SELECT content_key FROM catalog_products
            WHERE content_key IS NOT NULL AND suppression_reason IS NULL
            GROUP BY content_key, merchant_id HAVING COUNT(*) > 1
        ) t
    """,
    "cross_merchant_shared_keys": """
        SELECT COUNT(*) FROM (
            SELECT content_key FROM catalog_products
            WHERE content_key IS NOT NULL AND suppression_reason IS NULL
            GROUP BY content_key HAVING COUNT(DISTINCT merchant_id) > 1
        ) t
    """,
}

APPROVE_ALLOWLIST_SQL = """
UPDATE identity_resolution_proposals
SET status = 'approved', decided_by = 'sweep_auto_allowlist', decided_at = NOW()
WHERE status = 'proposed' AND strategy = ANY($1::text[])
RETURNING proposal_id
"""

REVIEW_CANDIDATES_SQL = """
SELECT proposal_id, kind, strategy, merchant_id, content_key,
       subject_product_keys, keeper_product_key, confidence, evidence
FROM identity_resolution_proposals
WHERE status = 'proposed'
  AND (kind = 'suppress_dup' OR strategy = 'campaign_clone_ambiguous')
  AND NOT (strategy = ANY($1::text[]))
"""

# Deterministic task id per proposal -> idempotent re-enqueue across sweeps.
ENQUEUE_REVIEW_TASK_SQL = """
INSERT INTO pdp_review_tasks (id, pdp_id, module_key, status, priority,
                              qa_sample, checklist, policy_labels)
VALUES ($1, $2, 'identity', 'needs_review', 'normal', FALSE, $3::jsonb, $4::jsonb)
ON CONFLICT (id) DO NOTHING
RETURNING id
"""

LAST_SWEEP_SQL = """
SELECT detail FROM identity_resolution_events
WHERE action = 'sweep'
ORDER BY created_at DESC
LIMIT 1
"""


def identity_reconcile_sweep_enabled() -> bool:
    return str(os.getenv("ENABLE_IDENTITY_RECONCILE_SWEEP", "")).strip().lower() in (
        "1", "true", "yes", "on",
    )


def detect_gauge_rise(
    previous: Optional[Dict[str, Any]], current: Dict[str, int]
) -> List[str]:
    """Pure: which gauges rose since the last sweep? A rise is an intake-
    contract leak signal — the ADR-011 door flags should make it impossible."""
    if not previous:
        return []
    prev_gauges = previous.get("gauges") or {}
    risen = []
    for name, value in current.items():
        prev = prev_gauges.get(name)
        if isinstance(prev, (int, float)) and value > prev:
            risen.append(f"{name}: {prev} -> {value}")
    return risen


def review_task_row(proposal: Dict[str, Any]) -> Tuple[str, str, str, str]:
    """Pure: deterministic pdp_review_tasks row parts for a proposal."""
    task_id = f"pdptask_ir_{proposal['proposal_id']}"[:96]
    pdp_id = str(proposal.get("keeper_product_key")
                 or (proposal.get("subject_product_keys") or [""])[0])[:96]
    checklist = json.dumps({
        "source": "identity_reconcile_sweep",
        "proposal_id": proposal["proposal_id"],
        "kind": proposal["kind"],
        "strategy": proposal["strategy"],
        "content_key": proposal.get("content_key"),
        "subject_product_keys": list(proposal.get("subject_product_keys") or []),
        "confidence": (float(proposal["confidence"])
                       if proposal.get("confidence") is not None else None),
        "evidence": proposal.get("evidence"),
    })
    labels = json.dumps(["entity_resolution", "identity_reconcile_sweep"])
    return task_id, pdp_id, checklist, labels


async def _connect_with_retry(dsn: str, attempts: int = 6):
    import asyncpg

    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            return await asyncpg.connect(dsn, timeout=30, command_timeout=300)
        except Exception as e:
            last = e
            await asyncio.sleep(2 * (i + 1))
    raise last  # type: ignore[misc]


async def _gauges(conn) -> Dict[str, int]:
    return {name: int(await conn.fetchval(sql)) for name, sql in GAUGES_SQL.items()}


async def _enqueue_review_tasks(conn) -> List[str]:
    rows = await conn.fetch(REVIEW_CANDIDATES_SQL, list(AUTO_APPROVE_STRATEGIES))
    enqueued = []
    for r in rows:
        p = dict(r)
        if isinstance(p.get("evidence"), str):
            p["evidence"] = json.loads(p["evidence"] or "{}")
        task_id, pdp_id, checklist, labels = review_task_row(p)
        created = await conn.fetchrow(
            ENQUEUE_REVIEW_TASK_SQL, task_id, pdp_id, checklist, labels
        )
        if created:
            enqueued.append(task_id)
    return enqueued


async def run_identity_reconcile_sweep_tick(
    *, apply_allowlist: bool = True, force: bool = False
) -> Dict[str, Any]:
    """The sweep. `force=True` bypasses the env flag (manual CLI runs);
    `apply_allowlist=False` proposes + enqueues review but applies nothing."""
    if not force and not identity_reconcile_sweep_enabled():
        return {"skipped": "ENABLE_IDENTITY_RECONCILE_SWEEP is off"}

    conn = await _connect_with_retry(os.environ["DATABASE_URL"])
    try:
        gauges = await _gauges(conn)

        working = [dict(r) for r in await conn.fetch(WORKING_ROWS_SQL)]
        orphans = [dict(r) for r in await conn.fetch(ORPHAN_MIRRORS_SQL)]
        report = build_report(working, orphans)
        lanes_summary = report["summary"]

        keys = sorted({
            r["product_key"]
            for groups in report["lanes"].values()
            for g in groups
            for r in g["rows"]
        })
        detail = [dict(r) for r in await conn.fetch(DETAIL_SQL, keys)] if keys else []
        detail_by_key = {d["product_key"]: d for d in detail}

        per_strategy = build_all_proposals(report, detail_by_key)
        proposed: Dict[str, Any] = {}
        for s, ps in per_strategy.items():
            proposed[s] = await upsert_proposals(conn, ps)

        applied: Dict[str, Any] = {"applied": [], "skipped": []}
        approved: List[str] = []
        if apply_allowlist:
            approved = [
                r["proposal_id"]
                for r in await conn.fetch(
                    APPROVE_ALLOWLIST_SQL, list(AUTO_APPROVE_STRATEGIES)
                )
            ]
            if approved:
                applied = await apply_approved(
                    conn, strategies=AUTO_APPROVE_STRATEGIES
                )

        review_tasks = await _enqueue_review_tasks(conn)

        previous_row = await conn.fetchrow(LAST_SWEEP_SQL)
        previous = None
        if previous_row:
            previous = previous_row["detail"]
            if isinstance(previous, str):
                previous = json.loads(previous or "{}")
        alerts = detect_gauge_rise(previous, gauges)
        if alerts:
            logger.error(
                "identity_reconcile_sweep: DUPLICATION GAUGE ROSE — intake "
                "contract may be leaking: %s", "; ".join(alerts),
            )

        run_id = applied.get("run_id") or "sweep"
        summary = {
            "gauges": gauges,
            "lanes": lanes_summary,
            "proposed": proposed,
            "auto_approved": len(approved),
            "applied": applied.get("applied", []),
            "apply_skipped": applied.get("skipped", []),
            "review_tasks_enqueued": review_tasks,
            "alerts": alerts,
        }
        await conn.execute(
            INSERT_EVENT_SQL, None, "sweep", run_id, json.dumps(summary, default=str)
        )
        logger.info("identity_reconcile_sweep: %s", json.dumps(
            {k: summary[k] for k in ("gauges", "auto_approved", "alerts")}))
        return summary
    finally:
        await conn.close()
