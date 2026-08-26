"""Reap orphaned agent_pdp_view rows (content_key no longer in catalog_products).

agent_pdp_view is a content_key-keyed cache refreshed with UPSERT-on-content_key.
content_key = make_content_key(brand, title, gtin) is derived from mutable fields,
so when a product is re-keyed on re-sync (title/barcode change) catalog_products
moves to the new content_key in place and the OLD content_key's view row is left
behind — an orphan that squats on the still-live pivota_signature_id (a latent
/products/sig_* mis-serve, and it blocks the live row from materializing under the
unique sig index).

The inline reap in services.catalog_sync_service handles the common case at
re-key time; this script is the catch-all sweep for every OTHER re-key site and
the periodic backstop. Safe + idempotent: only deletes view rows whose content_key
has NO catalog_products row (NOT-EXISTS guard, re-checked at delete time).

Scheduled as the `agent-pdp-orphan-reaper` Cloud Run Job, daily at 04:37 UTC via
the `agent-pdp-orphan-reaper-cron` Cloud Scheduler entry (infra/gcp/setup_scheduler.sh).
It ran as .github/workflows/agent-pdp-orphan-reaper.yml until 2026-08-26; that
lane is gone and cannot come back, because Cloud SQL `pivota-pg` has no public IP
and a GitHub-hosted runner has no route into the VPC.

Usage:
  python -m scripts.reap_orphaned_agent_pdp_view                 # dry-run (default)
  python -m scripts.reap_orphaned_agent_pdp_view --apply         # delete orphans
  python -m scripts.reap_orphaned_agent_pdp_view --apply --limit 500

  # by hand against prod — dry-run first, since the Job's baked args include --apply
  gcloud run jobs execute agent-pdp-orphan-reaper --region us-west1 --project pivota-prod --wait \
    --args 'scripts/reap_orphaned_agent_pdp_view.py,--limit,0'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict

# The Cloud Run Job invokes this BY PATH (`--args scripts/reap_orphaned_agent_pdp_view.py`),
# which puts scripts/ on sys.path rather than the repo root, so `from db.database
# import ...` would raise ModuleNotFoundError. `python -m scripts.…` — how the
# deleted workflow ran it — happens to work because -m prepends the cwd. Making
# the file work either way is what lets the Job line read like every other python
# job in setup_scheduler.sh. Same bootstrap as scripts/elect_content_canonicals.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.agent_pdp_view_assembler import (  # noqa: E402
    reap_orphaned_agent_pdp_view_rows,
)

logger = logging.getLogger("reap_orphaned_agent_pdp_view")


async def _connect_if_needed(db: Any) -> bool:
    was = bool(getattr(db, "is_connected", False))
    if not was and callable(getattr(db, "connect", None)):
        await db.connect()
    return was


async def _disconnect_if_needed(db: Any, was: bool) -> None:
    if not was and bool(getattr(db, "is_connected", False)) and callable(getattr(db, "disconnect", None)):
        await db.disconnect()


async def _drive(args: argparse.Namespace, *, db: Any = database) -> Dict[str, Any]:
    was = await _connect_if_needed(db)
    try:
        return await reap_orphaned_agent_pdp_view_rows(
            db=db, limit=args.limit, dry_run=not args.apply
        )
    finally:
        await _disconnect_if_needed(db, was)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--apply", action="store_true",
        help="Actually DELETE orphaned rows. Default: dry-run (report only).",
    )
    p.add_argument(
        "--limit", type=int, default=0,
        help="Max orphans to process this run (0 = all). Default 0.",
    )
    return p.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args()
    report = asyncio.run(_drive(args))
    print(json.dumps(report, indent=2, default=str))

    # Emitted at WARNING so it carries a severity into Cloud Logging, where this
    # sweep now runs. The GitHub workflow it replaces uploaded both reports as a
    # 14-day build artifact, which was the only place a non-zero result was
    # visible at all; deleting the workflow would otherwise have left this
    # readable only by whoever thought to open a JSON blob logged at INFO.
    #
    # On a clean database this sweep is a no-op (see the module docstring), so a
    # non-zero count is by definition not steady state: either a re-key site is
    # skipping its inline reap, or catalog rows are vanishing. Both want a human.
    if report["orphans"]:
        logger.warning(
            "%d orphaned agent_pdp_view row(s) found, %d deleted%s",
            report["orphans"],
            report["deleted"],
            "" if args.apply else " (dry-run: pass --apply to delete)",
        )
        # Called out separately because reap_orphaned_agent_pdp_view_rows'
        # docstring names it as the unusual case: an orphan that still carries an
        # evidence_profile is evidence built for a content_key nothing resolves to.
        if report["with_evidence"]:
            logger.warning(
                "  %d of them still carried an evidence_profile - unusual, worth chasing",
                report["with_evidence"],
            )
        for row in report["sample"]:
            logger.warning(
                "  %s -> %s (%s)",
                row.get("content_key"),
                row.get("pivota_signature_id"),
                row.get("refresh_source"),
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
