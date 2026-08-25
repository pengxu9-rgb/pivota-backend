"""Recover regressed PDP content from a Postgres backup snapshot.

Background — see conversation 2026-05-09 + PR #426. Codex skills + the
Path B mirror were silently overwriting vetted PDP descriptions /
ingredients / how-to-use with lower-quality re-extracts. The structural
fix (PR #426) installed a writer service + content_lock + proposals
table so future writes are gated. But the *historical* damage is still
in the live DB: rows whose top-level seed_data fields are now shorter
or dirtier than they were 3+ days ago.

This script does the recovery:

  1. Connects to the LIVE prod DB (via the runtime DATABASE_URL the
     backend already uses). Production is Cloud Run (pivota-prod/us-west1);
     there is no `railway ssh` equivalent, and Railway is the ROLLBACK, so
     recovering into it repairs a catalog nobody is served from. Run it as a
     throwaway job on the production image:
       scripts/ops/run_oneoff_job.sh scripts/recover_seed_data_from_archive.py \\
           --archive-url "$ARCHIVE_URL"
     A job inherits NO env and NO secrets: the helper mounts DATABASE_URL, and
     --archive-url must be passed explicitly. Full pattern:
     docs/runbooks/operating_on_gcp_production.md.
  2. Connects to an ARCHIVE DB, restored by the operator from a backup of the
     production instance (Cloud SQL: restore the relevant backup into a
     temporary instance). The archive's URL is passed via --archive-url.
  3. Iterates external_product_seeds in the archive. For each row, looks
     up the matching row in live and compares per-field quality scores
     (services.seed_data_writer._score_field).
  4. When the archive's value scores strictly higher than live's, calls
     services.seed_data_writer.upsert_seed_data with the archive payload
     as the proposal. The writer decides what to do —
       - field is unlocked OR archive beats lock → merges + relocks
       - field is locked AND archive doesn't beat → rejects (forensic
         row in seed_data_proposals)
     The recovery script never writes to seed_data directly. The writer
     service is the only path. This is exactly what makes "let codex
     drive the recovery" safe — codex runs THIS script, the script
     calls the writer, the writer enforces the policy.

Usage:
    # Dry run — score everything, write proposals as 'pending', no merges:
    python3 scripts/recover_seed_data_from_archive.py \\
        --archive-url postgresql://...archive... \\
        --dry-run

    # Apply — writer decides merge/reject per row:
    python3 scripts/recover_seed_data_from_archive.py \\
        --archive-url postgresql://...archive... \\
        --apply

    # Bound the run for a chunked rollout (recommended for first pass):
    python3 scripts/recover_seed_data_from_archive.py \\
        --archive-url postgresql://...archive... \\
        --apply --limit 200 --offset 0

The archive_label argument tags the proposer field on every emitted
proposal row, so the operator can later filter the proposals table
by recovery batch (e.g. proposer='recovery_archive_20260506').
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

# Ensure project root is importable when running via `python3 scripts/...`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from databases import Database  # noqa: E402

from db.database import database as live_database  # noqa: E402
from services import seed_data_writer  # noqa: E402
from services.seed_data_writer import (  # noqa: E402
    LOCKABLE_FIELDS,
    _score_field,
    _coerce_jsonb,
)

logger = logging.getLogger(__name__)


SELECT_ARCHIVE_ROWS = """
    SELECT id, external_product_id, seed_data
    FROM external_product_seeds
    ORDER BY id ASC
    LIMIT :limit OFFSET :offset
"""

SELECT_LIVE_ROW = """
    SELECT seed_data, content_lock
    FROM external_product_seeds
    WHERE id = :seed_id
"""


def _archive_beats_live_on_any_lockable_field(
    archive_seed_data: Dict[str, Any],
    live_seed_data: Dict[str, Any],
) -> Tuple[bool, Dict[str, Any]]:
    """True if archive scores strictly higher than live on at least one
    LOCKABLE field. Returns (yes/no, per-field scoring breakdown).

    Key invariant: if archive == live for every lockable field, return
    False so we don't even propose. The writer would no-op anyway, but
    this saves a proposal-row insert per identical-row pair (cheaper
    on a 4,500-row archive)."""
    breakdown: Dict[str, Any] = {}
    any_higher = False
    for fname in LOCKABLE_FIELDS:
        a_val = archive_seed_data.get(fname)
        l_val = live_seed_data.get(fname)
        a_score = _score_field(fname, a_val)
        l_score = _score_field(fname, l_val)
        breakdown[fname] = {"archive": a_score, "live": l_score}
        if a_score > l_score:
            any_higher = True
    return any_higher, breakdown


async def _fetch_archive_rows(
    archive: Database, *, limit: int, offset: int
) -> List[Dict[str, Any]]:
    rows = await archive.fetch_all(
        SELECT_ARCHIVE_ROWS,
        {"limit": int(limit), "offset": int(offset)},
    )
    return [dict(r) for r in rows or []]


async def _fetch_live_row(seed_id: str) -> Optional[Dict[str, Any]]:
    row = await live_database.fetch_one(SELECT_LIVE_ROW, {"seed_id": seed_id})
    return dict(row) if row else None


async def _process_one(
    archive_row: Dict[str, Any],
    *,
    proposer: str,
    apply: bool,
) -> Dict[str, Any]:
    """Score archive vs live for one seed_id; if archive wins, propose
    via the writer service. Returns per-row outcome dict for the
    summary report."""
    seed_id = archive_row["id"]
    external_product_id = archive_row["external_product_id"]

    archive_seed_data = _coerce_jsonb(archive_row["seed_data"])
    if not archive_seed_data:
        return {"seed_id": seed_id, "outcome": "skipped_archive_unparseable"}

    live_row = await _fetch_live_row(seed_id)
    if live_row is None:
        # Row exists in archive but not in live. We could insert it but
        # that's a different recovery story (re-population); skip here.
        return {"seed_id": seed_id, "outcome": "skipped_no_live_row"}

    live_seed_data = _coerce_jsonb(live_row["seed_data"]) or {}

    archive_higher, breakdown = _archive_beats_live_on_any_lockable_field(
        archive_seed_data, live_seed_data
    )
    if not archive_higher:
        return {
            "seed_id": seed_id,
            "outcome": "skipped_archive_not_better",
            "score_breakdown": breakdown,
        }

    if not apply:
        # Dry-run: emit a 'pending' proposal so the operator can review
        # candidates before flipping --apply.
        # We deliberately call the writer in this mode too — writer
        # inserts the proposal row (status decided by policy) but the
        # operator can override after seeing the per-row breakdown.
        # However, in v1 the writer auto-decides on every call; we
        # don't want it to merge under --dry-run. So short-circuit:
        return {
            "seed_id": seed_id,
            "outcome": "dry_run_archive_higher",
            "score_breakdown": breakdown,
        }

    # Apply path: hand the archive seed_data to the writer service and
    # let it decide merge/reject per field.
    result = await seed_data_writer.upsert_seed_data(
        seed_id=seed_id,
        external_product_id=external_product_id,
        proposed_seed_data=archive_seed_data,
        proposer=proposer,
        source="archive_restore",
    )
    return {
        "seed_id": seed_id,
        "outcome": result.status,
        "merged_fields": [d.field for d in result.field_decisions if d.decision == "merge"],
        "rejected_fields": [d.field for d in result.field_decisions if d.decision == "reject"],
        "score_breakdown": breakdown,
    }


async def _drive(args: argparse.Namespace) -> Dict[str, Any]:
    archive = Database(args.archive_url)
    await archive.connect()
    if not getattr(live_database, "is_connected", False):
        await live_database.connect()

    try:
        rows = await _fetch_archive_rows(
            archive, limit=args.limit, offset=args.offset
        )
        logger.info("loaded %d archive rows (limit=%d offset=%d)",
                    len(rows), args.limit, args.offset)

        outcomes: List[Dict[str, Any]] = []
        for row in rows:
            outcomes.append(await _process_one(
                row, proposer=args.proposer, apply=args.apply
            ))

        # Aggregate
        counts: Dict[str, int] = {}
        for o in outcomes:
            counts[o["outcome"]] = counts.get(o["outcome"], 0) + 1
        merged_field_counts: Dict[str, int] = {}
        rejected_field_counts: Dict[str, int] = {}
        for o in outcomes:
            for f in o.get("merged_fields", []):
                merged_field_counts[f] = merged_field_counts.get(f, 0) + 1
            for f in o.get("rejected_fields", []):
                rejected_field_counts[f] = rejected_field_counts.get(f, 0) + 1

        return {
            "rows_considered": len(rows),
            "outcome_counts": counts,
            "merged_field_counts": merged_field_counts,
            "rejected_field_counts": rejected_field_counts,
            "sample_outcomes": outcomes[:5],
        }
    finally:
        await archive.disconnect()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,    formatter_class=argparse.RawDescriptionHelpFormatter,)
    p.add_argument(
        "--archive-url",
        required=True,
        help="DATABASE_URL of the restored backup (a separate, temporary Postgres instance)",
    )
    p.add_argument(
        "--apply", action="store_true",
        help="Actually call upsert_seed_data on candidates. Default: dry-run.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Explicit dry-run flag; equivalent to omitting --apply.",
    )
    p.add_argument(
        "--limit", type=int, default=200,
        help="Max archive rows to consider this run (default 200).",
    )
    p.add_argument(
        "--offset", type=int, default=0,
        help="Skip the first N archive rows (paginate large recovery batches).",
    )
    p.add_argument(
        "--proposer", type=str, default="recovery_archive",
        help="Tag emitted proposals with this proposer (default 'recovery_archive'). "
        "Use e.g. 'recovery_archive_20260506' so the operator can filter by batch.",
    )
    args = p.parse_args()
    if args.dry_run:
        args.apply = False
    return args


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    report = asyncio.run(_drive(args))
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
