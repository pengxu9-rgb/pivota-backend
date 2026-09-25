"""Stage 1 daily shadow-mode monitor.

Runs every Stage 1 promotion-gate check (§A.1–§A.6 + §6 future-date dry run)
against production. Produces a one-page report — pass/fail per check, expected vs
actual, and an exit code that reflects whether the day is clean.

Usage (operator). Production is Cloud Run (pivota-prod/us-west1); the Railway
public Postgres proxy this used to reach is the ROLLBACK's database, so a day
graded there is clean for traffic it never saw. Run it inside production, where
the job's exit code carries the clean/dirty verdict out:
    scripts/ops/run_oneoff_job.sh scripts/stage1_daily_monitor.py

Or pipe to a daily log file:
    python3 scripts/stage1_daily_monitor.py >> /tmp/stage1-day-$(date -u +%Y%m%d).log

Exit codes:
    0 — all checks clean (counts as one of the 3 consecutive clean days)
    1 — one or more checks failed
    2 — script error (DB unreachable, missing env var, etc.)

The script intentionally avoids importing service modules — it issues raw
SQL via psycopg/databases-equivalent. That way ops can run it from a
separate environment without the FastAPI app's import graph.

References:
- docs/monetization/deploy/STAGE_1_SHADOW_MODE_ROLLOUT.md §A.1-A.6, §6
- docs/monetization/CODE_REVIEW_FINDINGS_v1.3.md
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    sys.stderr.write(
        "ERROR: psycopg2 not installed. Install with: pip install psycopg2-binary\n"
    )
    sys.exit(2)


# Filter clock-harness artifacts out of monitoring — these are test pollution
# that has been cleaned manually but the harness root cause (see Stage 1
# trail log) may re-pollute. Keep this filter until the harness is locked
# out of production.
_CLOCK_FILTER_SQL = "merchant_id NOT LIKE 'clock_%'"


@dataclass
class CheckResult:
    name: str
    description: str
    passed: bool
    detail: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)


def _connect():
    url = os.environ.get("DATABASE_PUBLIC_URL") or os.environ.get("DATABASE_URL")
    if not url:
        sys.stderr.write(
            "ERROR: neither DATABASE_PUBLIC_URL nor DATABASE_URL is set.\n"
            "    In production, run this inside Cloud Run - it mounts the secret:\n"
            "      scripts/ops/run_oneoff_job.sh scripts/stage1_daily_monitor.py\n"
            "    Locally, set DATABASE_URL to a database you already have a URL for.\n"
        )
        sys.exit(2)
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


def _check_a1_agent_orders_vs_edges(cur) -> CheckResult:
    cur.execute("""
        WITH agent_orders AS (
          SELECT order_id, subtotal, discount_total, created_at
          FROM orders
          WHERE payment_status = 'paid'
            AND agent_id IS NOT NULL
            AND created_at >= CURRENT_DATE - INTERVAL '1 day'
            AND created_at <  CURRENT_DATE
        ),
        stamped_edges AS (
          SELECT cae.order_id, cae.gross_attributed_gmv_cents
          FROM commerce_attribution_edges cae
          JOIN agent_orders o ON o.order_id = cae.order_id
        )
        SELECT (SELECT COUNT(*) FROM agent_orders)                                     AS agent_orders_yesterday,
               (SELECT COUNT(*) FROM stamped_edges)                                    AS edges_yesterday,
               (SELECT COUNT(*) FROM stamped_edges WHERE gross_attributed_gmv_cents IS NOT NULL) AS edges_with_value;
    """)
    row = cur.fetchone()
    aoy = row["agent_orders_yesterday"]
    ey = row["edges_yesterday"]
    ewv = row["edges_with_value"]
    passed = aoy == ey == ewv
    return CheckResult(
        name="A.1",
        description="Daily agent-order count == stamped-edge count == edges_with_value",
        passed=passed,
        detail=f"agent_orders_yesterday={aoy}, edges_yesterday={ey}, edges_with_value={ewv}",
        rows=[dict(row)],
    )


def _check_a2_gmv_math(cur) -> CheckResult:
    cur.execute("""
        SELECT COUNT(*) AS mismatched_rows
        FROM commerce_attribution_edges cae
        JOIN orders o ON o.order_id = cae.order_id
        WHERE cae.created_at >= CURRENT_DATE - INTERVAL '1 day'
          AND cae.created_at <  CURRENT_DATE
          AND cae.gross_attributed_gmv_cents IS NOT NULL
          AND cae.gross_attributed_gmv_cents !=
              ROUND((COALESCE(o.subtotal,0) - COALESCE(o.discount_total,0)) * 100)::BIGINT;
    """)
    row = cur.fetchone()
    passed = row["mismatched_rows"] == 0
    return CheckResult(
        name="A.2",
        description="GMV math: gross = (subtotal - discount) * 100, no tax/shipping",
        passed=passed,
        detail=f"mismatched_rows={row['mismatched_rows']}",
    )


def _check_a3_rollup_reconciliation(cur) -> CheckResult:
    cur.execute(f"""
        SELECT gad.date, gad.merchant_id,
               gad.gross_attributed_gmv_cents AS rollup_gross,
               cae_sum.gross AS edge_sum_gross,
               gad.gross_attributed_gmv_cents - cae_sum.gross AS drift
        FROM gmv_attribution_daily gad
        JOIN LATERAL (
          SELECT COALESCE(SUM(gross_attributed_gmv_cents),0) AS gross
          FROM commerce_attribution_edges cae
          WHERE (cae.created_at AT TIME ZONE 'UTC')::date = gad.date
            AND cae.merchant_id = gad.merchant_id
            -- Match the gated rollup (#1481): inferred edges are excluded there, so
            -- excluding them here too keeps the reconciliation from flagging their
            -- (never-billed) GMV as drift.
            AND (cae.metadata->>'inferred')::boolean IS NOT TRUE
        ) cae_sum ON TRUE
        WHERE gad.date >= CURRENT_DATE - INTERVAL '7 days'
          AND {_CLOCK_FILTER_SQL.replace("merchant_id", "gad.merchant_id")}
          AND gad.gross_attributed_gmv_cents != cae_sum.gross
        ORDER BY gad.date DESC, gad.merchant_id;
    """)
    rows = cur.fetchall()
    passed = len(rows) == 0
    return CheckResult(
        name="A.3",
        description="gmv_attribution_daily rollups reconcile to raw edges",
        passed=passed,
        detail=f"drift_rows_last_7d={len(rows)}",
        rows=[dict(r) for r in rows],
    )


def _check_a6_dup_detection(cur) -> CheckResult:
    cur.execute("""
        SELECT order_id, merchant_id, agent_id, channel_partner_id,
               COUNT(*) AS dup_count, ARRAY_AGG(edge_id) AS edges
        FROM commerce_attribution_edges
        WHERE DATE(created_at) = CURRENT_DATE - INTERVAL '1 day'
        GROUP BY order_id, merchant_id, agent_id, channel_partner_id
        HAVING COUNT(*) > 1;
    """)
    rows = cur.fetchall()
    passed = len(rows) == 0
    return CheckResult(
        name="A.6",
        description="True duplicate edges (same order_id + attribution dimensions)",
        passed=passed,
        detail=f"dup_rows_yesterday={len(rows)}",
        rows=[dict(r) for r in rows],
    )


def _check_future_date_dryrun(cur) -> CheckResult:
    """§6 promotion checklist: aggregate_daily for a future date returns 0 rows."""
    cur.execute("""
        WITH rollup AS (
          SELECT
              (e.created_at AT TIME ZONE 'UTC')::date AS date,
              e.merchant_id,
              SUM(e.gross_attributed_gmv_cents) AS gross_sum
          FROM commerce_attribution_edges e
          WHERE (e.created_at AT TIME ZONE 'UTC')::date = CURRENT_DATE + INTERVAL '30 days'
            AND (CAST(NULL AS TEXT) IS NULL OR e.merchant_id = CAST(NULL AS TEXT))
            AND e.gross_attributed_gmv_cents IS NOT NULL
            AND (e.metadata->>'inferred')::boolean IS NOT TRUE  -- mirror the gated rollup (#1481)
          GROUP BY (e.created_at AT TIME ZONE 'UTC')::date, e.merchant_id
        )
        SELECT COUNT(*) AS rollup_count FROM rollup;
    """)
    row = cur.fetchone()
    passed = row["rollup_count"] == 0
    return CheckResult(
        name="§6.future-date",
        description="aggregate_daily for +30 days returns 0 rows (merchant_id=NULL cron path)",
        passed=passed,
        detail=f"rollup_count={row['rollup_count']}",
    )


def _check_silent_reject_volume(cur) -> CheckResult:
    """Stage 1 health metric: stamped edges + silent reject rate over last 24h.
    Surfaces whether the direct-checkout cohort (PR #594 counter) is growing —
    informs codex finding #8 disposition.
    """
    cur.execute("""
        SELECT
          (SELECT COUNT(*) FROM commerce_attribution_edges
            WHERE created_at >= NOW() - INTERVAL '24 hours') AS edges_24h,
          (SELECT COUNT(*) FROM commerce_attribution_edges
            WHERE created_at >= NOW() - INTERVAL '24 hours'
              AND gross_attributed_gmv_cents IS NOT NULL) AS stamped_24h;
    """)
    row = cur.fetchone()
    # Informational only — not a gate. Reports the number; passes always.
    return CheckResult(
        name="info.edges",
        description="Edges + stamped count over last 24h (informational)",
        passed=True,
        detail=f"edges_24h={row['edges_24h']}, stamped_24h={row['stamped_24h']}",
    )


def _check_promotion_gate_progress(cur) -> CheckResult:
    """Stage 1 §6 promotion gate: >= 5 stamped attribution edges accumulated
    from live agent traffic. Counts edges from real merchants only — excludes
    the clock-harness pollution that's already cleaned + filtered."""
    cur.execute(f"""
        SELECT COUNT(*) AS stamped_edges_total
        FROM commerce_attribution_edges cae
        WHERE gross_attributed_gmv_cents IS NOT NULL
          AND {_CLOCK_FILTER_SQL.replace("merchant_id", "cae.merchant_id")};
    """)
    row = cur.fetchone()
    count = row["stamped_edges_total"]
    passed = count >= 5
    return CheckResult(
        name="§6.edges-threshold",
        description="Stage 1 → Stage 2 gate: >= 5 stamped edges from live agent traffic",
        passed=passed,
        detail=f"stamped_edges_total={count} (need >= 5)",
    )


def _render_report(checks: list[CheckResult]) -> int:
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"Stage 1 daily shadow-mode monitor — {now_iso}")
    print("=" * 78)
    fail_count = 0
    info_count = 0
    for check in checks:
        is_info = check.name.startswith("info.")
        if is_info:
            symbol = "i"
            info_count += 1
        elif check.passed:
            symbol = "✓"
        else:
            symbol = "✗"
            fail_count += 1
        print(f"  [{symbol}] {check.name:20} {check.description}")
        print(f"      {check.detail}")
        if not check.passed and check.rows:
            print(f"      first failing rows:")
            for row in check.rows[:3]:
                print(f"        {json.dumps(row, default=str)}")
    print("=" * 78)
    gate_count = sum(1 for c in checks if not c.name.startswith("info."))
    pass_count = gate_count - fail_count
    print(f"Summary: {pass_count}/{gate_count} gate checks passed, {info_count} info")
    if fail_count == 0:
        print("Day status: CLEAN (counts toward 3-consecutive-day promotion gate)")
        return 0
    print(f"Day status: FAIL ({fail_count} gate check{'s' if fail_count != 1 else ''} failed)")
    return 1


def main() -> int:
    try:
        conn = _connect()
    except psycopg2.Error as exc:
        sys.stderr.write(f"ERROR: DB connect failed: {exc}\n")
        return 2

    try:
        with conn.cursor() as cur:
            checks = [
                _check_a1_agent_orders_vs_edges(cur),
                _check_a2_gmv_math(cur),
                _check_a3_rollup_reconciliation(cur),
                _check_a6_dup_detection(cur),
                _check_future_date_dryrun(cur),
                _check_silent_reject_volume(cur),
                _check_promotion_gate_progress(cur),
            ]
        return _render_report(checks)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
