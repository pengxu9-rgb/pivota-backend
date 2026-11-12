"""How far is agents.total_orders from the orders each agent really created? Read-only census.

Until the create-only fix (#2781, merged 2026-11-10 03:30Z) `log_agent_request` added one to
agents.total_orders for EVERY successful call that carried an order_id, not only a create:

  POST /agent/v1/orders/create                    the only real order (also the only GMV add)
  POST /agent/v1/orders/{id}/confirm-payment      counted, 6 success paths
  GET  /agent/v1/orders/{id}                      counted (the MCP get_order tool lands here)
  GET  /agent/v1/fulfillment/track/{id}           counted

Only the create passed order_amount, so total_gmv was never inflated by a read. It is reported
anyway, because a mismatch there would mean something this note does not know about.

This census sizes the drift. It writes nothing and proposes nothing: a correction of the stored
counters is a separate decision.

SECTIONS (one `ATO {...}` line each; `ATO_ROW {...}` one line per agent, worst drift first)
  window    oldest / newest usage row and how many carry an order_id. If the oldest row is younger
            than an agent, the usage log cannot explain that agent's whole counter.
  agent     per agent with any stored or real order:
              stored_orders / stored_gmv        the agents row today
              orders_rows / orders_rows_real    orders.agent_id = agent, all / not test-stamped
              orders_total_real                 sum(orders.total) of the real ones, MIXED CURRENCY
              log_create / log_confirm / log_read / log_track / log_other
                                                2xx usage rows with an order_id, by endpoint
              log_noncreate_before_fix          the same non-create rows before --fix-live-at:
                                                the inflation the log can account for
              log_noncreate_after_fix           after it. These did NOT count if the fix is live.
              drift                             stored_orders - orders_rows
              unexplained                       stored_orders - (log_create + log_noncreate_before_fix)
  totals    the sums, and how many agents have drift > 0 / < 0.

Reading it: `drift` is the size of the correction. `unexplained` near zero means the usage log
reproduces the stored counter, so the read-counting bug explains the drift. A large `unexplained`
means the log is incomplete for that agent (the write is best-effort, eval traffic writes none) or
another writer exists. A negative drift means orders reached the table without passing the create
route's counter: do not "correct" those upward without reading how they were made.

SAFETY. Session READ ONLY, statement_timeout 25 s, each statement runs once. The usage-log scans
filter on order_id, which has no index, so they are sequential: start it only while the primary's
CPU is quiet and no ingest drain or graph sync is running. A timeout is a result (the table is too
big for one pass), not something to retry in a loop. Fetches nothing from any merchant.

    # before this file is in the deployed image (the program must stay free of the at-sign):
    bash scripts/ops/run_oneoff_job.sh -c "$(cat scripts/ops/agent_total_orders_census.py)"
    # once it is:
    bash scripts/ops/run_oneoff_job.sh scripts/ops/agent_total_orders_census.py
    # with the real go-live time of the fix (UTC, from the revision that first carried #2781):
    bash scripts/ops/run_oneoff_job.sh scripts/ops/agent_total_orders_census.py --fix-live-at 2026-11-10T04:00:00Z
"""

import argparse
import json
import os
import sys

# The merge time of #2781. The deploy landed some minutes later; pass the real one when known.
DEFAULT_FIX_LIVE_AT = "2026-11-10T03:30:33Z"

# Same predicate as services.test_traffic.not_test_traffic_sql, inlined so the census also runs
# with `-c` on an image that predates that module.
NOT_TEST = "COALESCE(CAST(o.metadata->>'test_traffic' AS TEXT), '') NOT IN ('true', '1')"

# A usage row that the old code counted: it carried an order_id and the call succeeded.
COUNTED = "l.order_id IS NOT NULL AND l.order_id <> '' AND l.status_code < 400"
IS_CREATE = "(l.method = 'POST' AND l.endpoint LIKE '%%/orders/create')"
IS_CONFIRM = "(l.endpoint LIKE '%%/confirm-payment')"
IS_TRACK = "(l.endpoint LIKE '%%/fulfillment/track/%%' OR l.endpoint LIKE '%%/orders/%%/track')"
IS_READ = f"(l.method = 'GET' AND l.endpoint LIKE '%%/orders/%%' AND NOT {IS_TRACK})"

WINDOW_SQL = """
SELECT MIN(l.timestamp) AS oldest, MAX(l.timestamp) AS newest, COUNT(*) AS usage_rows,
       COUNT(*) FILTER (WHERE l.order_id IS NOT NULL AND l.order_id <> '') AS rows_with_order_id
FROM agent_usage_logs l"""

AGENT_SQL = f"""
WITH o AS (
    SELECT o.agent_id,
           COUNT(*) AS orders_rows,
           COUNT(*) FILTER (WHERE {NOT_TEST}) AS orders_rows_real,
           COALESCE(SUM(o.total) FILTER (WHERE {NOT_TEST}), 0) AS orders_total_real,
           MIN(o.created_at) AS first_order_at
    FROM orders o
    WHERE o.agent_id IS NOT NULL AND o.agent_id <> ''
    GROUP BY o.agent_id
), u AS (
    SELECT l.agent_id,
           COUNT(*) FILTER (WHERE {IS_CREATE}) AS log_create,
           COUNT(*) FILTER (WHERE {IS_CONFIRM}) AS log_confirm,
           COUNT(*) FILTER (WHERE {IS_READ}) AS log_read,
           COUNT(*) FILTER (WHERE {IS_TRACK}) AS log_track,
           COUNT(*) FILTER (WHERE NOT ({IS_CREATE} OR {IS_CONFIRM} OR {IS_READ} OR {IS_TRACK})) AS log_other,
           COUNT(*) FILTER (WHERE NOT {IS_CREATE} AND l.timestamp < %(fix)s) AS log_noncreate_before_fix,
           COUNT(*) FILTER (WHERE NOT {IS_CREATE} AND l.timestamp >= %(fix)s) AS log_noncreate_after_fix,
           COUNT(DISTINCT l.order_id) AS log_distinct_orders,
           MIN(l.timestamp) AS first_log_at
    FROM agent_usage_logs l
    WHERE {COUNTED}
    GROUP BY l.agent_id
)
SELECT a.agent_id, a.is_active, a.created_at AS agent_created_at,
       COALESCE(a.total_orders, 0) AS stored_orders, COALESCE(a.total_gmv, 0) AS stored_gmv,
       COALESCE(o.orders_rows, 0) AS orders_rows, COALESCE(o.orders_rows_real, 0) AS orders_rows_real,
       COALESCE(o.orders_total_real, 0) AS orders_total_real, o.first_order_at,
       COALESCE(u.log_create, 0) AS log_create, COALESCE(u.log_confirm, 0) AS log_confirm,
       COALESCE(u.log_read, 0) AS log_read, COALESCE(u.log_track, 0) AS log_track,
       COALESCE(u.log_other, 0) AS log_other,
       COALESCE(u.log_noncreate_before_fix, 0) AS log_noncreate_before_fix,
       COALESCE(u.log_noncreate_after_fix, 0) AS log_noncreate_after_fix,
       COALESCE(u.log_distinct_orders, 0) AS log_distinct_orders, u.first_log_at,
       COALESCE(a.total_orders, 0) - COALESCE(o.orders_rows, 0) AS drift,
       COALESCE(a.total_orders, 0) - (COALESCE(u.log_create, 0) + COALESCE(u.log_noncreate_before_fix, 0))
           AS unexplained
FROM agents a
LEFT JOIN o ON o.agent_id = a.agent_id
LEFT JOIN u ON u.agent_id = a.agent_id
WHERE COALESCE(a.total_orders, 0) <> 0 OR COALESCE(o.orders_rows, 0) <> 0 OR COALESCE(u.log_create, 0) <> 0
ORDER BY ABS(COALESCE(a.total_orders, 0) - COALESCE(o.orders_rows, 0)) DESC, a.agent_id
LIMIT 2000"""

SUMMED = (
    "stored_orders", "orders_rows", "orders_rows_real", "log_create", "log_confirm", "log_read",
    "log_track", "log_other", "log_noncreate_before_fix", "log_noncreate_after_fix", "drift", "unexplained",
)


def _emit(tag, payload):
    print(tag + " " + json.dumps(payload, default=str, sort_keys=True), flush=True)


def _rows(cur):
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def run(conn, fix_live_at):
    """Run the census on an open connection. Returns the totals dict (also printed)."""
    cur = conn.cursor()
    cur.execute("SET TRANSACTION READ ONLY")
    cur.execute("SET LOCAL statement_timeout = '25s'")

    cur.execute(WINDOW_SQL)
    _emit("ATO", {"section": "window", "fix_live_at": fix_live_at, **_rows(cur)[0]})

    cur.execute(AGENT_SQL, {"fix": fix_live_at})
    rows = _rows(cur)
    for row in rows:
        _emit("ATO_ROW", row)

    totals = {k: sum(int(r[k] or 0) for r in rows) for k in SUMMED}
    totals.update(
        section="totals",
        agents=len(rows),
        agents_over=sum(1 for r in rows if r["drift"] > 0),
        agents_under=sum(1 for r in rows if r["drift"] < 0),
        agents_exact=sum(1 for r in rows if r["drift"] == 0),
        stored_gmv=str(sum(r["stored_gmv"] or 0 for r in rows)),
        orders_total_real_mixed_currency=str(sum(r["orders_total_real"] or 0 for r in rows)),
        truncated=len(rows) >= 2000,
    )
    _emit("ATO", totals)
    return totals


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fix-live-at", default=DEFAULT_FIX_LIVE_AT,
                        help="UTC time the create-only fix went live (ISO 8601)")
    args = parser.parse_args(argv)

    import psycopg2

    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("ATO_FATAL DATABASE_URL is not set (a one-off job inherits no secrets)", file=sys.stderr)
        return 2
    conn = psycopg2.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        run(conn, args.fix_live_at)
    finally:
        conn.rollback()
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
