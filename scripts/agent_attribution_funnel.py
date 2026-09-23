#!/usr/bin/env python3
"""Per-agent attribution funnel: does an agent's work actually become a credited order?

Read-only. For a time window it reports, per agent:

  referral lane   links issued (surface_click_events rows) -> clicked (click_count > 0)
  partner lane    purchases opened (reap_agentic_purchases) -> completed -> credited
                  (a converted commerce_attribution_edges row carrying that agent)
  money           credited GMV per currency, and refunds recorded against credited edges

and the integrity exceptions that would make the numbers lie:

  completed_without_edge     a completed purchase whose partner-reported edge never landed
                             (split by why: no order id, non-positive amount, or missing)
  partner_edge_without_agent a partner-reported edge that credits nobody
  agent_mismatch             a partner edge whose agent is not the purchase's agent

The first real end-to-end order (ADR-025 proof gate) shows up here as one agent with
opened=1, completed=1, credited=1 and no exceptions.

Runs inside the production image as a one-off job, because the prod DB is VPC-only:

    bash scripts/ops/run_oneoff_job.sh scripts/agent_attribution_funnel.py --days 30

Before this file is deployed, pass its source inline (it is kept free of the runner's
delimiter characters for exactly this):

    bash scripts/ops/run_oneoff_job.sh -c "$(cat scripts/agent_attribution_funnel.py)"

The last line of output is `FUNNEL_JSON {...}` for machines; everything above it is for people.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

MAX_EXCEPTION_ROWS = 20
MAX_AGENTS = 50
NO_AGENT = "(none)"

# Purchases: the ledger's own states (migration 224). Everything not listed as terminal is
# still in flight.
PURCHASE_TERMINAL = ("completed", "failed", "refused", "expired")


def purchase_columns_sql():
    return (
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'reap_agentic_purchases'"
    )


def build_queries(purchase_cols):
    """The read-only queries, adapted to the purchase columns prod actually has.

    `item_source` arrived in migration 229, and numbered migrations are not applied by deploy
    in prod, so a missing column is reported as the default lane rather than failing the report.
    """
    cols = set(purchase_cols or ())
    src = "coalesce(p.item_source, 'reap_variant')" if "item_source" in cols else "'reap_variant'"
    queries = {
        "clicks": (
            "SELECT coalesce(nullif(c.agent_id, ''), :none) AS agent, coalesce(c.surface, '') AS surface, "
            "count(*) AS issued, count(*) FILTER (WHERE c.click_count > 0) AS clicked "
            "FROM surface_click_events c WHERE c.created_at >= :since GROUP BY 1, 2"
        ),
        "edges": (
            "SELECT coalesce(nullif(e.agent_id, ''), :none) AS agent, "
            "coalesce(e.metadata ->> 'agent_source', '') AS agent_source, "
            "(e.metadata -> 'partner_provenance' ->> 'partner_reported') = 'true' AS partner, "
            "coalesce(e.state, '') AS state, coalesce(e.currency, '') AS currency, "
            "count(*) AS n, coalesce(sum(e.gross_attributed_gmv_cents), 0) AS gmv_minor, "
            "count(*) FILTER (WHERE coalesce(e.refund_count, 0) > 0) AS refunded_edges, "
            "coalesce(sum(e.refunded_amount), 0) AS refunded_amount "
            "FROM commerce_attribution_edges e WHERE e.created_at >= :since GROUP BY 1, 2, 3, 4, 5"
        ),
    }
    if not cols:
        return queries
    queries["purchases"] = (
        "SELECT coalesce(nullif(p.agent_id, ''), :none) AS agent, " + src + " AS item_source, "
        "p.state AS state, coalesce(p.currency, '') AS currency, count(*) AS n, "
        "coalesce(sum(p.final_total_minor) FILTER (WHERE p.state = 'completed'), 0) AS completed_minor "
        "FROM reap_agentic_purchases p WHERE p.created_at >= :since GROUP BY 1, 2, 3, 4"
    )
    # A completed purchase and its edge are joined on the purchase id the closure stamps into
    # metadata.partner_provenance -- the only link that cannot collide across merchants.
    edge_for_purchase = (
        "SELECT 1 FROM commerce_attribution_edges e "
        "WHERE e.metadata -> 'partner_provenance' ->> 'purchase_id' = p.id"
    )
    queries["completed_without_edge"] = (
        "SELECT p.id AS purchase_id, coalesce(p.agent_id, '') AS agent, p.merchant_domain AS merchant, "
        "p.reap_order_id AS order_id, p.final_total_minor AS final_minor, p.currency AS currency, "
        "p.last_error_code AS last_error_code, p.terminal_at AS terminal_at, "
        "CASE WHEN coalesce(p.reap_order_id, '') = '' THEN 'no_order_id' "
        "WHEN coalesce(p.final_total_minor, 0) <= 0 THEN 'non_positive_amount' "
        "ELSE 'missing_edge' END AS reason "
        "FROM reap_agentic_purchases p WHERE p.state = 'completed' AND p.created_at >= :since "
        "AND NOT EXISTS (" + edge_for_purchase + ") ORDER BY p.terminal_at DESC NULLS LAST"
    )
    queries["partner_edge_agent_check"] = (
        "SELECT e.edge_id AS edge_id, coalesce(e.agent_id, '') AS edge_agent, "
        "coalesce(p.agent_id, '') AS purchase_agent, "
        "e.metadata -> 'partner_provenance' ->> 'purchase_id' AS purchase_id, e.created_at AS created_at "
        "FROM commerce_attribution_edges e "
        "LEFT JOIN reap_agentic_purchases p ON p.id = e.metadata -> 'partner_provenance' ->> 'purchase_id' "
        "WHERE (e.metadata -> 'partner_provenance' ->> 'partner_reported') = 'true' "
        "AND e.created_at >= :since "
        "AND (coalesce(e.agent_id, '') = '' OR coalesce(e.agent_id, '') <> coalesce(p.agent_id, '')) "
        "ORDER BY e.created_at DESC"
    )
    return queries


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def build_funnel(rows, days, now=None):
    """Fold the query results into one row per agent plus the exception lists. Pure."""
    now = now or datetime.now(timezone.utc)
    agents = defaultdict(lambda: {
        "issued": 0, "clicked": 0,
        "opened": 0, "completed": 0, "in_flight": 0, "failed": 0,
        "credited": 0, "credited_partner": 0,
        "credited_minor": defaultdict(int), "refunded_edges": 0,
        "states": defaultdict(int), "lanes": defaultdict(int),
    })
    for r in rows.get("clicks") or []:
        a = agents[r["agent"]]
        a["issued"] += _int(r["issued"])
        a["clicked"] += _int(r["clicked"])
    for r in rows.get("purchases") or []:
        a = agents[r["agent"]]
        n = _int(r["n"])
        a["opened"] += n
        a["states"][r["state"]] += n
        a["lanes"][r["item_source"]] += n
        if r["state"] == "completed":
            a["completed"] += n
        elif r["state"] in PURCHASE_TERMINAL:
            a["failed"] += n
        else:
            a["in_flight"] += n
    for r in rows.get("edges") or []:
        if r["state"] != "converted":
            continue
        a = agents[r["agent"]]
        n = _int(r["n"])
        a["credited"] += n
        if r.get("partner"):
            a["credited_partner"] += n
        a["credited_minor"][r["currency"] or "?"] += _int(r["gmv_minor"])
        a["refunded_edges"] += _int(r["refunded_edges"])

    ordered = sorted(
        agents.items(),
        key=lambda kv: (kv[0] == NO_AGENT, -kv[1]["credited"], -kv[1]["completed"],
                        -kv[1]["opened"], -kv[1]["issued"], kv[0]),
    )
    all_rows = []
    for name, a in ordered:
        all_rows.append({
            "agent": name,
            "issued": a["issued"], "clicked": a["clicked"],
            "opened": a["opened"], "in_flight": a["in_flight"], "completed": a["completed"],
            "failed": a["failed"],
            "credited": a["credited"], "credited_partner": a["credited_partner"],
            "credited_minor": dict(a["credited_minor"]),
            "refunded_edges": a["refunded_edges"],
            "states": dict(a["states"]), "lanes": dict(a["lanes"]),
        })

    missing = rows.get("completed_without_edge") or []
    reasons = defaultdict(int)
    for r in missing:
        reasons[r["reason"]] += 1
    checks = rows.get("partner_edge_agent_check") or []
    no_agent = [r for r in checks if not r["edge_agent"]]
    mismatch = [r for r in checks if r["edge_agent"] and r["edge_agent"] != r["purchase_agent"]]

    agent_rows = all_rows[:MAX_AGENTS]
    # Totals are over EVERY agent; only the per-agent table is truncated.
    totals = {k: sum(r[k] for r in all_rows) for k in
              ("issued", "clicked", "opened", "completed", "credited", "credited_partner")}
    # "Credited to an agent" means a named agent: an edge with no agent credits nobody, however
    # it was closed, and counting it here would report the exact gap this report exists to find.
    totals["credited_partner_to_agent"] = sum(
        r["credited_partner"] for r in all_rows if r["agent"] != NO_AGENT
    )
    totals["agents_truncated"] = max(0, len(ordered) - MAX_AGENTS)
    return {
        "window_days": days,
        "generated_at": now.isoformat(),
        "purchases_table": "purchases" in rows,
        "totals": totals,
        "agents": agent_rows,
        "exceptions": {
            "completed_without_edge": {"count": len(missing), "by_reason": dict(reasons),
                                       "rows": missing[:MAX_EXCEPTION_ROWS]},
            "partner_edge_without_agent": {"count": len(no_agent),
                                           "rows": no_agent[:MAX_EXCEPTION_ROWS]},
            "agent_mismatch": {"count": len(mismatch), "rows": mismatch[:MAX_EXCEPTION_ROWS]},
        },
        "errors": rows.get("_errors") or {},
    }


def _pct(n, d):
    return f"{100.0 * n / d:.0f}%" if d else "-"


def _money(minor_by_cur):
    if not minor_by_cur:
        return "-"
    return ", ".join(f"{cur} {m / 100:,.2f}" for cur, m in sorted(minor_by_cur.items()))


def render(funnel):
    t = funnel["totals"]
    lines = [
        f"AGENT ATTRIBUTION FUNNEL  last {funnel['window_days']}d  (generated {funnel['generated_at']})",
        "",
        f"referral lane : issued {t['issued']}  ->  clicked {t['clicked']} ({_pct(t['clicked'], t['issued'])})",
        f"partner lane  : opened {t['opened']}  ->  completed {t['completed']} ({_pct(t['completed'], t['opened'])})"
        f"  ->  credited to an agent {t['credited_partner_to_agent']} (partner edges: {t['credited_partner']})",
        f"all credited edges (any source): {t['credited']}",
    ]
    if not funnel["purchases_table"]:
        lines.append("NOTE: reap_agentic_purchases is absent; the partner lane is not measured.")
    lines += ["", f"{'agent':<44} {'issued':>6} {'click':>6} {'open':>5} {'fly':>4} {'done':>5} "
              f"{'fail':>5} {'cred':>5}  credited GMV"]
    for a in funnel["agents"]:
        lines.append(
            f"{a['agent'][:44]:<44} {a['issued']:>6} {a['clicked']:>6} {a['opened']:>5} "
            f"{a['in_flight']:>4} {a['completed']:>5} {a['failed']:>5} {a['credited']:>5}  "
            f"{_money(a['credited_minor'])}" + (f"  refunds:{a['refunded_edges']}" if a["refunded_edges"] else "")
        )
    if t["agents_truncated"]:
        lines.append(f"... {t['agents_truncated']} more agents not shown")
    ex = funnel["exceptions"]
    lines += ["", "INTEGRITY"]
    cwe = ex["completed_without_edge"]
    lines.append(f"completed purchases with no edge : {cwe['count']} {cwe['by_reason'] or ''}")
    for r in cwe["rows"]:
        lines.append(f"    {r['purchase_id']}  agent={r['agent'] or NO_AGENT}  {r['merchant']}  "
                     f"order={r['order_id']}  reason={r['reason']}")
    pna = ex["partner_edge_without_agent"]
    lines.append(f"partner edges crediting no agent : {pna['count']}")
    for r in pna["rows"]:
        lines.append(f"    {r['edge_id']}  purchase={r['purchase_id']}  purchase_agent={r['purchase_agent'] or NO_AGENT}")
    mm = ex["agent_mismatch"]
    lines.append(f"edge agent != purchase agent     : {mm['count']}")
    for r in mm["rows"]:
        lines.append(f"    {r['edge_id']}  edge={r['edge_agent']}  purchase={r['purchase_agent'] or NO_AGENT}")
    if funnel["errors"]:
        lines += ["", f"QUERY ERRORS (numbers above are incomplete): {funnel['errors']}"]
    return "\n".join(lines)


async def collect(days):
    from db.database import database

    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = {"_errors": {}}
    opened_here = not database.is_connected
    if opened_here:
        await database.connect()
    try:
        try:
            cols = [r["column_name"] for r in await database.fetch_all(purchase_columns_sql())]
        except Exception as exc:  # noqa: BLE001 -- reported, never fatal
            rows["_errors"]["purchase_columns"] = type(exc).__name__ + " " + str(exc)[:200]
            cols = []
        for name, sql in build_queries(cols).items():
            params = {"since": since}
            if ":none" in sql:
                params["none"] = NO_AGENT
            try:
                rows[name] = [dict(r) for r in await database.fetch_all(sql, params)]
            except Exception as exc:  # noqa: BLE001 -- one failed query must not hide the rest
                rows["_errors"][name] = type(exc).__name__ + " " + str(exc)[:200]
    finally:
        if opened_here:
            await database.disconnect()
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="Per-agent attribution funnel (read-only).")
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args([] if argv is None and sys.argv[:1] == ["-c"] else argv)
    rows = asyncio.run(collect(args.days))
    funnel = build_funnel(rows, args.days)
    print(render(funnel), flush=True)
    print("FUNNEL_JSON " + json.dumps(funnel, default=str, separators=(",", ":")), flush=True)
    if os.getenv("CLOUD_RUN_JOB"):
        time.sleep(20)  # let Cloud Logging ingest the tail before the container exits
    return 1 if funnel["errors"] else 0


if __name__ == "__main__":
    sys.path.insert(0, os.getcwd())
    raise SystemExit(main())
