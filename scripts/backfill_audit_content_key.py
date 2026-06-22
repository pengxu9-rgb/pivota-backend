"""Backfill merchant_audit_runs.content_keys[] for historical audit runs.

P0.2 W4. Migrations 156/157 added content_key to the audit deposit spine and
new audits populate it (audit_evidence_builder W1/W2/W3). This script fills the
run-level content_keys[] for runs that predate the change.

Resolution mirrors the live deposit gate exactly:
  - brand/title/content_key come from catalog_products (joined on the run's
    product_keys[]),
  - the canonical GTIN comes from agent_pdp_view.gtin13,
  - services.catalog_identity.resolve_deposit_content_key picks the basis;
    only DEPOSITABLE (resolved) keys are recorded.

NULL-key policy (per the adversarial review): a run whose products don't
resolve is LEFT with content_keys = NULL and COUNTED as `runs_unresolved` —
never silently dropped, never given a fabricated key. The report surfaces the
coverage gap so the identity-enrichment backlog is visible.

Usage:
  Dry-run (default — reports coverage, writes nothing):
    python3 scripts/backfill_audit_content_key.py --limit 500
  Apply:
    python3 scripts/backfill_audit_content_key.py --apply --limit 500 --offset 0
  Full (small DBs / staging only):
    python3 scripts/backfill_audit_content_key.py --apply --limit 0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from db.catalog import agent_pdp_view, catalog_products  # noqa: E402
from db.merchant_audit_runs import (  # noqa: E402
    merchant_audit_runs,
    record_audit_run_content_keys,
)
from services.catalog_identity import resolve_deposit_content_key  # noqa: E402

logger = logging.getLogger(__name__)


async def _resolve_for_run(
    merchant_id: str, product_keys: List[str]
) -> Tuple[List[str], Dict[str, str]]:
    """Return (sorted depositable content_keys, {product_key: basis}) for one
    run, using the same source as the live deposit (catalog_products +
    agent_pdp_view.gtin13)."""
    if not merchant_id or not product_keys:
        return [], {}
    rows = await database.fetch_all(
        catalog_products.select().where(
            catalog_products.c.merchant_id == merchant_id,
            catalog_products.c.product_key.in_(product_keys),
        )
    )
    rows = rows or []
    content_keys = [r["content_key"] for r in rows if r["content_key"]]
    gtin_by_ck: Dict[str, Any] = {}
    if content_keys:
        view_rows = await database.fetch_all(
            agent_pdp_view.select().where(
                agent_pdp_view.c.content_key.in_(content_keys)
            )
        )
        for vr in view_rows or []:
            gtin_by_ck[vr["content_key"]] = vr["gtin13"]

    depositable = set()
    basis: Dict[str, str] = {}
    for r in rows:
        ck = r["content_key"]
        resolved = resolve_deposit_content_key(
            brand=r["brand"],
            title=r["title"],
            gtin=gtin_by_ck.get(ck) if ck else None,
            existing_content_key=ck,
        )
        basis[r["product_key"]] = resolved.basis
        if resolved.is_depositable and resolved.content_key:
            depositable.add(resolved.content_key)
    return sorted(depositable), basis


async def _drive(args: argparse.Namespace) -> Dict[str, Any]:
    await database.connect()
    try:
        q = (
            merchant_audit_runs.select()
            .where(merchant_audit_runs.c.content_keys.is_(None))
            .order_by(merchant_audit_runs.c.run_id)
        )
        if args.limit and args.limit > 0:
            q = q.limit(args.limit).offset(args.offset or 0)
        runs = await database.fetch_all(q)

        report: Dict[str, Any] = {
            "apply": bool(args.apply),
            "runs_scanned": 0,
            "runs_with_resolved": 0,
            "runs_unresolved": 0,  # NULL-key policy: left NULL, counted here
            "content_keys_total": 0,
            "applied": 0,
            "samples": [],
        }
        for r in runs or []:
            report["runs_scanned"] += 1
            run_id = str(r["run_id"])
            cks, basis = await _resolve_for_run(
                r["merchant_id"], list(r["product_keys"] or [])
            )
            if cks:
                report["runs_with_resolved"] += 1
                report["content_keys_total"] += len(cks)
                if args.apply:
                    await record_audit_run_content_keys(
                        run_id=run_id, content_keys=cks, content_key_basis=basis
                    )
                    report["applied"] += 1
            else:
                report["runs_unresolved"] += 1
            if len(report["samples"]) < (args.samples or 5):
                report["samples"].append({"run_id": run_id, "content_keys": cks[:5]})

        scanned = report["runs_scanned"] or 1
        report["coverage_pct"] = round(
            100.0 * report["runs_with_resolved"] / scanned, 1
        )
        return report
    finally:
        await database.disconnect()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--limit", type=int, default=500, help="0 = no limit")
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--samples", type=int, default=5)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    report = asyncio.run(_drive(args))
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
