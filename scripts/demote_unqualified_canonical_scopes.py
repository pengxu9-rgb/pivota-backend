"""P4 of docs/PDP_SCOPE_REDESIGN.md — the one-off demotion pass.

Demotes `multi_merchant_canonical` bucket rows that do NOT qualify under the
classifier's rule to `'unverified'`. Measured 2026-08-04 before this script was
written (the doc's Rule 1): demotion set = 2,201 rows; 23 of 37 sampled queries
affected; 164 top-10 slots vacate; the replacements are `merch_obs_*`
observed-seller rows (correct attribution, same product universe); 0 of 2,201
rows lose Node market-filtered recall (every one has a US-market seed).

WHY 'unverified' AND NOT 'merchant_owned' (D1, decided 2026-08-04): every
promotion writer is gated `WHERE pdp_scope='unverified'`, so `merchant_owned`
would park these rows OUTSIDE every promotion path permanently — the real
one-way door. `'unverified'` is read-side equivalent (migration 070) and keeps
each row re-promotable by the scheduled backfill the moment it gains a second
seller. `merchant_owned` is also semantically an AFFIRMED state
(brand_verified_graduation: "resolved, NOT canonical"), which is not what a
withdrawn label means.

THE QUALIFICATION RULE IS NOT SPELLED HERE. Selection interpolates
`services.pdp_identity_recovery.CANONICAL_SCOPE_PREDICATE` — the same string
the promotion writer runs — inside `NOT (...)`. One rule, one spelling; a
measurement-only copy in a script is how the 3,182-row estimate drifted from
the 2,201-row fact.

Discipline (the #1663 lessons, all three):
  * DRY-RUN BY DEFAULT — `--apply` to write.
  * REVERSIBLE — every planned row is reported with its previous
    (pdp_scope, pdp_scope_source, pdp_scope_set_at) BEFORE any write, to
    stdout as JSON and to --report-file if given.
  * CAS — the UPDATE is guarded on the pre-image
    (`AND pdp_scope = 'multi_merchant_canonical'`), so a row whose scope
    changed between select and write is skipped and counted, never clobbered.

Usage:
  python -m scripts.demote_unqualified_canonical_scopes                # dry run
  python -m scripts.demote_unqualified_canonical_scopes --apply
  python -m scripts.demote_unqualified_canonical_scopes --apply --report-file p4.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database  # noqa: E402
from services.pdp_identity_recovery import CANONICAL_SCOPE_PREDICATE  # noqa: E402
from services.pdp_scope_classifier import NON_SELLER_MERCHANT_ID  # noqa: E402

logger = logging.getLogger("demote_unqualified_canonical_scopes")

# pdp_scope_source marker (varchar(32)). Greppable back to this script + the doc.
DEMOTION_SOURCE = "p4_scope_demotion"

_SELECT_SQL = """
    SELECT cp.product_key, cp.pdp_scope, cp.pdp_scope_source, cp.pdp_scope_set_at
    FROM catalog_products cp
    WHERE cp.merchant_id = :bucket
      AND cp.pdp_scope = 'multi_merchant_canonical'
      AND cp.suppressed_at IS NULL
      AND NOT (
{predicate}
      )
    ORDER BY cp.product_key
    LIMIT :limit
""".format(predicate=CANONICAL_SCOPE_PREDICATE)


async def fetch_demotion_candidates(limit: int = 10_000) -> List[Dict[str, Any]]:
    """Canonical bucket rows the classifier's rule does NOT qualify.

    Rule-1 rows (agent-authored) are excluded automatically: rule 1 is the
    first arm of the interpolated predicate, so they qualify and fail NOT().
    """
    rows = await database.fetch_all(
        _SELECT_SQL, {"bucket": NON_SELLER_MERCHANT_ID, "limit": limit}
    )
    return [dict(r) for r in rows or []]


async def demote_row(product_key: str) -> bool:
    """CAS demotion of one row. Returns True if the row was written.

    The pre-image guard means a row promoted/changed between select and write
    is left alone — the same shape as #1663's phase-2 guard, for the same
    reason: an UPDATE that assumes a stale read clobbers a concurrent truth.
    """
    # RETURNING, because databases.execute() returns None for UPDATE on this
    # stack (driven on 0.7.0 and 0.9.0 — review finding F4 on PR #1680): the
    # written/skipped verdict must come from the write itself, not a
    # re-SELECT that races the very concurrency the CAS guard exists for.
    row = await database.fetch_one(
        """
        UPDATE catalog_products
        SET pdp_scope = 'unverified',
            pdp_scope_source = :src,
            pdp_scope_set_at = NOW()
        WHERE product_key = :pk
          AND pdp_scope = 'multi_merchant_canonical'
        RETURNING product_key
        """,
        {"pk": product_key, "src": DEMOTION_SOURCE},
    )
    return row is not None


async def run_demotion(*, apply: bool, limit: int = 10_000) -> Dict[str, Any]:
    """One pass. Dry-run returns the full plan; apply executes it with CAS."""
    candidates = await fetch_demotion_candidates(limit)
    report: Dict[str, Any] = {
        "mode": "apply" if apply else "dry_run",
        "candidates": len(candidates),
        "previous_values": [
            {
                "product_key": c["product_key"],
                "pdp_scope": c["pdp_scope"],
                "pdp_scope_source": c["pdp_scope_source"],
                "pdp_scope_set_at": str(c["pdp_scope_set_at"]),
            }
            for c in candidates
        ],
        "demoted": 0,
        "cas_skipped": 0,
    }
    if not apply:
        return report
    for c in candidates:
        if await demote_row(c["product_key"]):
            report["demoted"] += 1
        else:
            report["cas_skipped"] += 1
    return report


async def _main(args: argparse.Namespace) -> int:
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        report = await run_demotion(apply=args.apply, limit=args.limit)
    finally:
        await database.disconnect()

    out = json.dumps(report, indent=1)
    if args.report_file:
        Path(args.report_file).write_text(out)
        # stdout gets the summary only; the reversibility record is in the file.
        summary = {k: v for k, v in report.items() if k != "previous_values"}
        print(json.dumps(summary, indent=1))
        print(f"previous values -> {args.report_file}")
    else:
        print(out)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="execute the demotion (default: dry run)")
    parser.add_argument("--limit", type=int, default=10_000)
    parser.add_argument("--report-file", default=None,
                        help="write the full reversibility record here")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
