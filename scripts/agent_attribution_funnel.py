#!/usr/bin/env python3
"""Per-agent attribution funnel: does an agent's work actually become a credited order?

Read-only. For a time window it reports, per agent:

  referral lane   links issued (surface_click_events rows with issued_at, ADR-025 D1)
                  -> clicked (click_count > 0); rows /r created with no issue record are
                  reported apart as legacy, so the issued -> clicked ratio cannot be inflated
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
#: services.traffic_taxonomy_service.UNKNOWN_TOKEN: written into agent_id when the traffic has no
#: agent. It names nobody, so it is reported as no agent, never as an agent called "unknown".
UNKNOWN_AGENT_TOKEN = "unknown"

# Purchases: the ledger's own states (migration 224). Everything not listed as terminal is
# still in flight.
PURCHASE_TERMINAL = ("completed", "failed", "refused", "expired")


def columns_sql(table):
    return (
        "SELECT column_name FROM information_schema.columns "
        f"WHERE table_schema = 'public' AND table_name = '{table}'"
    )


def purchase_columns_sql():
    return columns_sql("reap_agentic_purchases")


def build_queries(purchase_cols, click_cols=None):
    """The read-only queries, adapted to the columns prod actually has.

    `item_source` arrived in migration 229, and numbered migrations are not applied by deploy
    in prod, so a missing column is reported as the default lane rather than failing the report.

    `issued_at` (migration 239, ADR-025 D1) separates links recorded when they were ISSUED from
    LEGACY rows that `/r` created at click time. Without the column every row is legacy: nothing
    was recorded at issue time, so nothing is claimed as issued.
    """
    cols = set(purchase_cols or ())
    src = "coalesce(p.item_source, 'reap_variant')" if "item_source" in cols else "'reap_variant'"
    if "issued_at" in set(click_cols or ()):
        click_counts = (
            "count(*) FILTER (WHERE c.issued_at IS NOT NULL) AS issued, "
            "count(*) FILTER (WHERE c.issued_at IS NOT NULL AND c.click_count > 0) AS clicked, "
            "count(*) FILTER (WHERE c.issued_at IS NULL) AS legacy, "
            "count(*) FILTER (WHERE c.issued_at IS NULL AND c.click_count > 0) AS legacy_clicked "
            "FROM surface_click_events c WHERE c.created_at >= :since"
        )
    else:
        click_counts = (
            "0 AS issued, 0 AS clicked, count(*) AS legacy, "
            "count(*) FILTER (WHERE c.click_count > 0) AS legacy_clicked "
            "FROM surface_click_events c WHERE c.created_at >= :since"
        )
    queries = {
        "clicks": (
            "SELECT coalesce(nullif(nullif(c.agent_id, ''), :unknown), :none) AS agent, coalesce(c.surface, '') AS surface, "
            + click_counts + " GROUP BY 1, 2"
        ),
        "edges": (
            "SELECT coalesce(nullif(nullif(e.agent_id, ''), :unknown), :none) AS agent, "
            "coalesce(e.metadata ->> 'agent_source', '') AS agent_source, "
            "(e.metadata -> 'partner_provenance' ->> 'partner_reported') = 'true' AS partner, "
            "coalesce(e.state, '') AS state, coalesce(e.currency, '') AS currency, "
            "count(*) AS n, coalesce(sum(e.gross_attributed_gmv_cents), 0) AS gmv_minor, "
            "count(*) FILTER (WHERE coalesce(e.refund_count, 0) > 0) AS refunded_edges, "
            "coalesce(sum(e.refunded_amount), 0) AS refunded_amount "
            "FROM commerce_attribution_edges e WHERE e.created_at >= :since GROUP BY 1, 2, 3, 4, 5"
        ),
        # MCP OAuth connectors by the platform their OAuth client registered (claude.ai, chatgpt.com,
        # loopback, ...). An UNVERIFIED label unless oauth_platform_verified: analytics, never credit,
        # which is why it is reported beside the agents, not as one.
        "platforms": (
            "SELECT c.context ->> 'oauth_platform' AS platform, "
            "coalesce(c.context ->> 'oauth_platform_verified', 'false') = 'true' AS verified, "
            "count(*) AS issued, count(*) FILTER (WHERE c.click_count > 0) AS clicked, "
            "count(DISTINCT e.edge_id) FILTER (WHERE e.state = 'converted') AS converted "
            "FROM surface_click_events c "
            "LEFT JOIN commerce_attribution_edges e ON e.click_id = c.click_id "
            "WHERE c.created_at >= :since AND c.context ->> 'oauth_platform' IS NOT NULL "
            "GROUP BY 1, 2"
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
        # The closure's ON CONFLICT keeps the FIRST writer for (merchant, order): another path may
        # have closed this order without our purchase id on it. Found, but not as ours.
        "WHEN EXISTS (SELECT 1 FROM commerce_attribution_edges e2 "
        "WHERE e2.external_order_id = p.reap_order_id) THEN 'edge_without_provenance' "
        "ELSE 'missing_edge' END AS reason "
        "FROM reap_agentic_purchases p WHERE p.state = 'completed' AND p.created_at >= :since "
        "AND NOT EXISTS (" + edge_for_purchase + ") ORDER BY p.terminal_at DESC NULLS LAST"
    )
    queries["partner_edge_agent_check"] = (
        "SELECT e.edge_id AS edge_id, coalesce(nullif(e.agent_id, :unknown), '') AS edge_agent, "
        "coalesce(p.agent_id, '') AS purchase_agent, (p.id IS NOT NULL) AS purchase_found, "
        "e.metadata -> 'partner_provenance' ->> 'purchase_id' AS purchase_id, e.created_at AS created_at "
        "FROM commerce_attribution_edges e "
        "LEFT JOIN reap_agentic_purchases p ON p.id = e.metadata -> 'partner_provenance' ->> 'purchase_id' "
        "WHERE (e.metadata -> 'partner_provenance' ->> 'partner_reported') = 'true' "
        "AND e.created_at >= :since "
        "AND (coalesce(nullif(e.agent_id, :unknown), '') = '' OR coalesce(e.agent_id, '') <> coalesce(p.agent_id, '')) "
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
        "issued": 0, "clicked": 0, "legacy": 0, "legacy_clicked": 0,
        "opened": 0, "completed": 0, "in_flight": 0, "failed": 0,
        "credited": 0, "credited_partner": 0,
        "credited_minor": defaultdict(int), "refunded_edges": 0,
        "states": defaultdict(int), "lanes": defaultdict(int),
    })
    for r in rows.get("clicks") or []:
        a = agents[r["agent"]]
        a["issued"] += _int(r["issued"])
        a["clicked"] += _int(r["clicked"])
        a["legacy"] += _int(r.get("legacy"))
        a["legacy_clicked"] += _int(r.get("legacy_clicked"))
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
            "legacy": a["legacy"], "legacy_clicked": a["legacy_clicked"],
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
    # An edge whose purchase row is gone is its own problem, not an agent disagreement.
    orphan = [r for r in checks if not r.get("purchase_found", True)]
    found = [r for r in checks if r.get("purchase_found", True)]
    no_agent = [r for r in found if not r["edge_agent"]]
    mismatch = [r for r in found if r["edge_agent"] and r["edge_agent"] != r["purchase_agent"]]

    agent_rows = all_rows[:MAX_AGENTS]
    # Totals are over EVERY agent; only the per-agent table is truncated.
    totals = {k: sum(r[k] for r in all_rows) for k in
              ("issued", "clicked", "legacy", "legacy_clicked", "opened", "completed", "credited",
               "credited_partner")}
    # "Credited to an agent" means a named agent: an edge with no agent credits nobody, however
    # it was closed, and counting it here would report the exact gap this report exists to find.
    totals["credited_partner_to_agent"] = sum(
        r["credited_partner"] for r in all_rows if r["agent"] != NO_AGENT
    )
    totals["agents_truncated"] = max(0, len(ordered) - MAX_AGENTS)
    platforms = sorted(
        ({"platform": str(r["platform"]), "verified": bool(r.get("verified")), "issued": _int(r["issued"]),
          "clicked": _int(r["clicked"]), "converted": _int(r.get("converted"))}
         for r in rows.get("platforms") or []),
        key=lambda p: (-p["converted"], -p["clicked"], -p["issued"], p["platform"], not p["verified"]),
    )
    return {
        "window_days": days,
        "generated_at": now.isoformat(),
        "purchases_table": "purchases" in rows,
        "totals": totals,
        "agents": agent_rows,
        "platforms": platforms[:MAX_AGENTS],
        "exceptions": {
            "completed_without_edge": {"count": len(missing), "by_reason": dict(reasons),
                                       "rows": missing[:MAX_EXCEPTION_ROWS]},
            "partner_edge_without_agent": {"count": len(no_agent),
                                           "rows": no_agent[:MAX_EXCEPTION_ROWS]},
            "agent_mismatch": {"count": len(mismatch), "rows": mismatch[:MAX_EXCEPTION_ROWS]},
            "orphan_edge": {"count": len(orphan), "rows": orphan[:MAX_EXCEPTION_ROWS]},
        },
        "errors": rows.get("_errors") or {},
    }


def _pct(n, d):
    return f"{100.0 * n / d:.0f}%" if d else "-"


# Display only; the JSON keeps raw minor units. The repo's authoritative lists live in
# services.reap_webhooks / services.reap_agentic_purchase; this file stays self-contained so it
# can run inline before it is deployed.
_ZERO_DECIMAL = frozenset({"JPY", "KRW", "VND", "CLP", "ISK", "UGX", "XAF", "XOF", "PYG", "RWF"})
_THREE_DECIMAL = frozenset({"KWD", "BHD", "JOD", "OMR", "TND", "LYD", "IQD"})


def _money(minor_by_cur):
    if not minor_by_cur:
        return "-"
    out = []
    for cur, m in sorted(minor_by_cur.items()):
        exp = 0 if cur in _ZERO_DECIMAL else 3 if cur in _THREE_DECIMAL else 2
        out.append(f"{cur} {m / (10 ** exp):,.{exp}f}")
    return ", ".join(out)


def render(funnel):
    t = funnel["totals"]
    lines = [
        f"AGENT ATTRIBUTION FUNNEL  last {funnel['window_days']}d  (generated {funnel['generated_at']})",
        "",
        f"referral lane : issued {t['issued']}  ->  clicked {t['clicked']} ({_pct(t['clicked'], t['issued'])})"
        f"   | legacy (no issue record): {t['legacy']} rows, {t['legacy_clicked']} clicked",
        f"partner lane  : opened {t['opened']}  ->  completed {t['completed']} ({_pct(t['completed'], t['opened'])})"
        f"  ->  credited to an agent {t['credited_partner_to_agent']} (partner edges: {t['credited_partner']})",
        f"all credited edges (any source): {t['credited']}",
        "(each stage is windowed on its own created_at, so a purchase opened before the window and",
        " credited inside it counts as credited but not opened)",
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
    if funnel.get("platforms"):
        lines += ["", "MCP OAUTH PLATFORMS  (from each client's registered redirect URIs; analytics, never credit;",
                  " 'claimed' = a public client, which anyone can register with any callback)",
                  f"{'platform':<44} {'label':<9} {'issued':>6} {'click':>6} {'conv':>5}"]
        for p in funnel["platforms"]:
            lines.append(f"{p['platform'][:44]:<44} {'verified' if p['verified'] else 'claimed':<9} "
                         f"{p['issued']:>6} {p['clicked']:>6} {p['converted']:>5}")
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
    oe = ex["orphan_edge"]
    lines.append(f"partner edges with no purchase row: {oe['count']}")
    for r in oe["rows"]:
        lines.append(f"    {r['edge_id']}  purchase={r['purchase_id']}  edge_agent={r['edge_agent'] or NO_AGENT}")
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
        try:
            click_cols = [
                r["column_name"] for r in await database.fetch_all(columns_sql("surface_click_events"))
            ]
        except Exception as exc:  # noqa: BLE001 -- reported, never fatal
            rows["_errors"]["click_columns"] = type(exc).__name__ + " " + str(exc)[:200]
            click_cols = []
        for name, sql in build_queries(cols, click_cols).items():
            params = {"since": since}
            if ":none" in sql:
                params["none"] = NO_AGENT
            if ":unknown" in sql:
                params["unknown"] = UNKNOWN_AGENT_TOKEN
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
    # Under `python -c`, sys.argv is ["-c", <user args>...], so the default argv=None already
    # reads exactly the user's arguments -- inline runs honour --days like file runs do.
    args = ap.parse_args(argv)
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
