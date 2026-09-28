#!/usr/bin/env python3
"""Per-host purchasability coverage: for every merchant x market the sweep's population holds,
is its fact POSITIVE, CONFIRMED-NEGATIVE, UNVERIFIABLE or NEVER-CHECKED? READ ONLY.

WHY. pivota-backend #2411 is a fail-closed cart gate: a seed or connected card gets a prefilled
cart only on a fresh positive fact for its cart host x the buyer's market. It can be armed on real
data only once every host the cart minter builds on HAS a measured fact. This census is how that
is read, after two or three days of hourly sweeps, per host.

THE POPULATION IS THE SWEEP'S OWN. It calls `jobs.merchant_purchasability_sweep.collect_population`
— the one function the sweep builds its population with, including the cart-mint lane that runs
the cart minter over every active seed — so this cannot report on a set the sweep does not sweep,
and the lane each key came from is in its row.

THE FOUR STATES, read from the BUYER vantage (`is_purchasable`'s, `worker` in prod):
  positive            `positive_until` is in the future: `is_purchasable` answers True, and the
                      cart gate keeps the cart.
  confirmed_negative  not positive, and the LAST check was a confirmed negative
                      (`NEGATIVE_VERDICTS`: the store answering about itself, e.g. no card).
  unverifiable        a row exists, not positive, and the last check was not a confirmed negative
                      (blocked, transport error, variant unavailable, ELIGIBLE without a readable
                      card list, or a positive window that expired). Never becomes positive by
                      itself; see rule 3 in db/merchant_purchasability.py.
  never_checked       no row from this vantage at all. After a full rotation this should be ~0;
                      a key that stays here is one the sweep is not reaching.
Only `positive` opens a cart; the other three all read `browse_only` at the gate.

READ ONLY, AT THE DATABASE. Everything runs on ONE connection inside ONE transaction opened
`READ ONLY` (`SET TRANSACTION READ ONLY` as its first statement), and the script refuses to go on
unless `SHOW transaction_read_only` answers `on` through the same `database` object every reader
here uses. A statement that tried to write would fail at the server. `statement_timeout` is 30 s.

CPU-GATED, and paced. Run it through `scripts/ops/merchant_purchasability_census.sh`, which reads
pivota-pg's CPU from Cloud Monitoring first and refuses above 35 % (the 2-vCPU primary: see the
runbook). The seed scan inside is the sweep's own, 500 rows a page with a pause between pages.

OUTPUT. Cloud Logging drops lines, so every line is FENCED and NUMBERED:
    MPCENSUS>>>{"kind": "row", "i": 0, "n": 57, ...}<<<MPCENSUS
    MPCENSUS>>>{"kind": "summary", "n": 57, ...}<<<MPCENSUS
`decode` (run by the wrapper on the job's log) refuses a log missing any row or the summary
rather than reporting over the lines that happened to arrive. No buyer data exists on this path;
rows carry merchant hosts, markets, verdicts and counts.

    python scripts/merchant_purchasability_census.py            # in the job: run the census
    python scripts/merchant_purchasability_census.py decode --log job.log [--json out.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPORT_BEGIN = "MPCENSUS>>>"
REPORT_END = "<<<MPCENSUS"

STATES = ("positive", "confirmed_negative", "unverifiable", "never_checked")

#: The buyer vantage's rows, with freshness computed by the SERVER (a client clock binds as local
#: wall time under asyncpg; db/merchant_purchasability.py's driver notes).
_FACTS_SQL = """
SELECT merchant_domain, market_country, verdict, card_available, consecutive_failures,
       CAST(checked_at AS TEXT) AS checked_at, CAST(positive_until AS TEXT) AS positive_until,
       CASE WHEN positive_until IS NOT NULL AND positive_until > CURRENT_TIMESTAMP
            THEN 1 ELSE 0 END AS fresh
  FROM merchant_purchasability
 WHERE vantage = :vantage
"""


def classify(fact: Optional[Dict[str, Any]], negative_verdicts) -> str:
    """One key's state from its fact row (None = no row). See the module docstring."""
    if fact is None:
        return "never_checked"
    if int(fact.get("fresh") or 0):
        return "positive"
    if str(fact.get("verdict") or "") in negative_verdicts:
        return "confirmed_negative"
    return "unverifiable"


def build_rows(
    lanes: Dict[str, Dict[Tuple[str, str], Any]],
    facts_by_key: Dict[Tuple[str, str], Dict[str, Any]],
    seeds_by_key: Dict[Tuple[str, str], int],
    negative_verdicts,
) -> List[Dict[str, Any]]:
    """One row per population key, host-ordered. Pure, so it is tested without a database."""
    keys = sorted({key for lane in lanes.values() for key in lane})
    rows = []
    for key in keys:
        fact = facts_by_key.get(key)
        rows.append({
            "host": key[0],
            "market": key[1],
            "state": classify(fact, negative_verdicts),
            "lanes": [name for name, lane in lanes.items() if key in lane],
            "cart_mint_seeds": int(seeds_by_key.get(key, 0)),
            "verdict": (fact or {}).get("verdict"),
            "card_available": (fact or {}).get("card_available"),
            "consecutive_failures": (fact or {}).get("consecutive_failures"),
            "checked_at": (fact or {}).get("checked_at"),
            "positive_until": (fact or {}).get("positive_until"),
        })
    return rows


def summarise(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Counts by state: over every key, over the cart-mint lane's keys (what #2411 gates), and
    over hosts (a host is counted once, in its BEST state across markets)."""
    def _by_state(items):
        out = {state: 0 for state in STATES}
        for item in items:
            out[item["state"]] += 1
        return out

    best: Dict[str, str] = {}
    for row in rows:
        seen = best.get(row["host"])
        if seen is None or STATES.index(row["state"]) < STATES.index(seen):
            best[row["host"]] = row["state"]
    cart = [r for r in rows if "cart-mint" in r["lanes"]]
    return {
        "keys": _by_state(rows),
        "cart_mint_keys": _by_state(cart),
        "cart_mint_seeds": {state: sum(r["cart_mint_seeds"] for r in cart if r["state"] == state)
                            for state in STATES},
        "hosts_best_state": _by_state([{"state": s} for s in best.values()]),
    }


def _fence(payload: Dict[str, Any]) -> str:
    return REPORT_BEGIN + json.dumps(payload, separators=(",", ":"), default=str) + REPORT_END


class NotReadOnly(RuntimeError):
    """The transaction did not come back read-only; nothing was read."""


async def census() -> Dict[str, Any]:
    """The report, on the ALREADY-CONNECTED `database`. Postgres only (`SET TRANSACTION READ
    ONLY`). Returns {"rows", "summary", "incomplete"}; raises NotReadOnly before reading anything
    if the transaction is not read-only."""
    import db.merchant_purchasability as facts
    import jobs.merchant_purchasability_sweep as sweep
    from db.database import database

    async with database.connection():
        async with database.transaction():
            # FIRST statement of the transaction, or Postgres refuses it.
            await database.execute("SET TRANSACTION READ ONLY")
            await database.execute("SET LOCAL statement_timeout = '30s'")
            ro = await database.fetch_one("SHOW transaction_read_only")
            if str(dict(ro).get("transaction_read_only") if ro else "") != "on":
                raise NotReadOnly("SHOW transaction_read_only did not answer 'on'")
            tally: Dict[str, int] = {}
            detail: Dict[str, Any] = {}
            lanes = await sweep.collect_population(
                tally=tally, cart_mint=detail, fresh_cart_mint_scan=True
            )
            vantage = facts.buyer_vantage()
            facts_by_key = {
                (str(r["merchant_domain"]), str(r["market_country"])): dict(r)
                for r in await database.fetch_all(_FACTS_SQL, {"vantage": vantage})
            }
    rows = build_rows(lanes, facts_by_key, detail.get("seeds_by_key") or {}, facts.NEGATIVE_VERDICTS)
    summary = {
        "kind": "summary",
        "n": len(rows),
        "vantage": vantage,
        "population_counts": tally,
        "cart_mint_lane": {k: v for k, v in detail.items() if k != "seeds_by_key"},
        **summarise(rows),
    }
    # Incomplete when a lane could not be read (or came back incomplete): the census then
    # under-reports exactly like the sweep would, and says so in its exit code.
    return {"rows": rows, "summary": summary, "incomplete": bool(tally.get(sweep.UNREADABLE_TALLY))}


async def run() -> int:
    from db.database import database

    await database.connect()
    try:
        report = await census()
    except NotReadOnly as exc:
        print(f"MPCENSUS refused: {exc}", flush=True)
        return 2
    finally:
        await database.disconnect()
    rows = report["rows"]
    for i, row in enumerate(rows):
        print(_fence({"kind": "row", "i": i, "n": len(rows), **row}), flush=True)
    print(_fence(report["summary"]), flush=True)
    # Cloud Logging ingests asynchronously; give the tail a moment before the container exits.
    await asyncio.sleep(20)
    return 3 if report["incomplete"] else 0


_LINE = re.compile(re.escape(REPORT_BEGIN) + r"(\{.*?\})" + re.escape(REPORT_END))


def decode(text: str) -> Dict[str, Any]:
    """The report from a job log, or ValueError when any row or the summary is missing."""
    rows: Dict[int, Dict[str, Any]] = {}
    summary = None
    for match in _LINE.finditer(text):
        payload = json.loads(match.group(1))
        if payload.get("kind") == "summary":
            summary = payload
        elif payload.get("kind") == "row":
            rows[int(payload["i"])] = payload
    if summary is None:
        raise ValueError("no summary line in the log: the census did not finish, or its tail was dropped")
    missing = [i for i in range(int(summary["n"])) if i not in rows]
    if missing:
        raise ValueError(f"{len(missing)} of {summary['n']} rows missing from the log (first: {missing[:5]})")
    return {"summary": summary, "rows": [rows[i] for i in range(int(summary["n"]))]}


def render(report: Dict[str, Any]) -> str:
    summary, rows = report["summary"], report["rows"]
    out = [
        f"vantage={summary['vantage']}  keys={summary['n']}  population_counts={summary['population_counts']}",
        f"cart-mint lane: {summary['cart_mint_lane']}",
        f"keys by state:            {summary['keys']}",
        f"cart-mint keys by state:  {summary['cart_mint_keys']}   (what #2411 gates)",
        f"cart-mint seeds by state: {summary['cart_mint_seeds']}",
        f"hosts by best state:      {summary['hosts_best_state']}",
        "",
        f"{'host':<40} {'mkt':<3} {'state':<18} {'seeds':>5} {'verdict':<22} {'checked_at':<26} lanes",
    ]
    for row in sorted(rows, key=lambda r: (STATES.index(r["state"]), -r["cart_mint_seeds"], r["host"])):
        out.append(
            f"{row['host']:<40} {row['market']:<3} {row['state']:<18} {row['cart_mint_seeds']:>5} "
            f"{str(row['verdict'] or '-'):<22} {str(row['checked_at'] or '-')[:26]:<26} "
            f"{','.join(row['lanes'])}"
        )
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="merchant_purchasability_census.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd")
    dec = sub.add_parser("decode", help="decode a job log into the report")
    dec.add_argument("--log", required=True)
    dec.add_argument("--json", help="also write the decoded report here")
    args = parser.parse_args(argv)
    if args.cmd == "decode":
        report = decode(Path(args.log).read_text(errors="replace"))
        if args.json:
            Path(args.json).write_text(json.dumps(report, indent=2, default=str))
        print(render(report))
        return 0
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
