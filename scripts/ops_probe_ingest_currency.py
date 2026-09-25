#!/usr/bin/env python3
"""READ-ONLY: what currency did an ingested storefront's rows actually land in?

Written after #2084 made the ingest lane carry a storefront's own currency instead of stamping
USD on everything. The lane's own tests prove the PLAN is right; this proves the ROWS are —
which is a different claim, and the one that matters after a job has run against production.

WHY IT EXISTS AS A SCRIPT. `search_catalog` cannot answer this. Rows land in `shadow` until
`promote_brand_official_canonicals` runs, so they do not serve; and a brand-name query is
routinely classified `ambiguous_or_non_shopping`, which returns `total: 0` WITHOUT running
retrieval at all. A zero there means nothing about what is stored.

SELECT ONLY. No UPDATE, no INSERT, no DDL. It mounts production credentials — see
scripts/ops/run_oneoff_job.sh's own warning.

THE DENOMINATOR IS PART OF THE ANSWER. "0 rows in the wrong currency" is only reassuring if the
filter saw every row, so this prints the total for each domain beside the breakdown and flags a
domain that matched nothing at all — the shape that otherwise reads as a clean pass.

Usage (locally, against whatever DATABASE_URL is set):
    python -m scripts.ops_probe_ingest_currency --domain jsmbeauty.sg --domain cocomo.sg

In production, this script is NOT in the deployed image unless it has been merged and deployed.
Pass it inline instead — see reference_running_a_read_only_prod_query_via_a_oneoff_job.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# (label, table, currency column, domain column)
_TARGETS = [
    ("catalog_offers", "catalog_offers", "currency", "source_domain"),
    ("catalog_skus", "catalog_skus", "currency", "source_domain"),
    ("external_product_seeds", "external_product_seeds", "price_currency", "domain"),
]


async def probe(domains: List[str]) -> Dict[str, Any]:
    from db.database import database  # noqa: E402 - after sys.path

    if not getattr(database, "is_connected", False):
        await database.connect()
    out: Dict[str, Any] = {}
    try:
        for label, table, currency_col, domain_col in _TARGETS:
            rows = await database.fetch_all(
                f"SELECT {domain_col} AS domain, {currency_col} AS currency, COUNT(*) AS n "
                f"FROM {table} WHERE {domain_col} = ANY(:domains) "
                f"GROUP BY {domain_col}, {currency_col} ORDER BY 1, 3 DESC",
                {"domains": list(domains)},
            )
            out[label] = [dict(r) for r in rows or []]
    finally:
        if getattr(database, "is_connected", False):
            await database.disconnect()
    return out


def render(result: Dict[str, Any], domains: List[str]) -> int:
    worst = 0
    for label, rows in result.items():
        print(f"\n=== {label} ===")
        by_domain: Dict[str, List[Dict[str, Any]]] = {d: [] for d in domains}
        for r in rows:
            by_domain.setdefault(r["domain"], []).append(r)
        for domain in domains:
            got = by_domain.get(domain) or []
            total = sum(int(r["n"]) for r in got)
            if not got:
                # A domain with no rows is NOT a pass: it means the ingest wrote nothing here,
                # or wrote it under a different domain value than the one asked about.
                print(f"  {domain:20s} NO ROWS  <- the filter matched nothing; not a clean result")
                worst = max(worst, 2)
                continue
            breakdown = ", ".join(f"{r['currency'] or 'NULL'}={r['n']}" for r in got)
            mixed = len({r["currency"] for r in got}) > 1
            flag = "  <- MIXED CURRENCIES" if mixed else ""
            print(f"  {domain:20s} total={total:<5d} {breakdown}{flag}")
            if mixed:
                worst = max(worst, 1)
    return worst


def main(argv: List[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--domain", action="append", required=True, help="repeatable")
    args = p.parse_args(argv)
    domains = list(dict.fromkeys(args.domain))
    result = asyncio.run(probe(domains))
    rc = render(result, domains)
    print()
    if rc == 0:
        print("OK: every domain returned rows, each in a single currency.")
    elif rc == 1:
        print("MIXED: a domain holds more than one currency — expected after a partial re-ingest.")
    else:
        print("INCOMPLETE: at least one domain matched no rows; the denominator is not proven.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
