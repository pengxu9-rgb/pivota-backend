#!/usr/bin/env python3
"""ADR-010 D-2 — propose-mode sweep runner (Phase A3; PROPOSE ONLY).

Runs every strategy plugin (services/identity_resolution_strategies.py) over
the live working-set classification and upserts the resulting proposals into
identity_resolution_proposals. Writes NOTHING else: no suppression, no seed
changes, no approvals. Apply happens separately, on approved proposals only,
via services.identity_resolution.apply_approved (Phase B wires the weekly
cadence + the mechanical-lane auto-approve allowlist).

Re-runs are idempotent: proposals dedupe on proposal_key (same strategy +
subject + member set); a changed member set mints a new proposal.

  Dry-run (default; prints per-strategy counts, writes nothing):
    python3 scripts/run_identity_resolution_propose.py
  Write proposals:
    python3 scripts/run_identity_resolution_propose.py --write

Access notes as in scripts/step5_working_set.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.step5_lane2_same_url_dedup import DETAIL_SQL  # noqa: E402
from scripts.step5_working_set import (  # noqa: E402
    ORPHAN_MIRRORS_SQL,
    WORKING_ROWS_SQL,
    build_report,
)
from services.identity_resolution import upsert_proposals  # noqa: E402
from services.identity_resolution_strategies import build_all_proposals  # noqa: E402


async def _connect_with_retry(dsn: str, attempts: int = 6):
    import asyncpg

    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            return await asyncpg.connect(dsn, timeout=30, command_timeout=180)
        except Exception as e:  # public proxy flakes intermittently
            last = e
            await asyncio.sleep(2 * (i + 1))
    raise last  # type: ignore[misc]


async def _run(write: bool) -> int:
    conn = await _connect_with_retry(os.environ["DATABASE_URL"])
    try:
        working = [dict(r) for r in await conn.fetch(WORKING_ROWS_SQL)]
        orphans = [dict(r) for r in await conn.fetch(ORPHAN_MIRRORS_SQL)]
        report = build_report(working, orphans)

        keys = sorted({
            r["product_key"]
            for groups in report["lanes"].values()
            for g in groups
            for r in g["rows"]
        })
        detail = [dict(r) for r in await conn.fetch(DETAIL_SQL, keys)] if keys else []
        detail_by_key = {d["product_key"]: d for d in detail}

        per_strategy = build_all_proposals(report, detail_by_key)
        summary: Dict[str, Any] = {}
        for s, ps in per_strategy.items():
            kinds: Dict[str, int] = {}
            for p in ps:
                kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
            summary[s] = {"proposals": len(ps), "by_kind": kinds}

        if write:
            for s, ps in per_strategy.items():
                summary[s].update(await upsert_proposals(conn, ps))
        print(json.dumps({"write": write, "summary": summary}, indent=2))
        if not write:
            print("DRY-RUN — nothing written. Re-run with --write to upsert proposals.")
        return 0
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help="Upsert proposals (default is dry-run)")
    args = parser.parse_args()
    return asyncio.run(_run(write=args.write))


if __name__ == "__main__":
    sys.exit(main())
