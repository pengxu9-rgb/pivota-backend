"""LLM-driven product-identity reviewer (DeepSeek) — CLI.

Thin wrapper over services.llm_identity_reviewer (the reusable core, also driven
by the audit_scheduler tick). For each review_required merchant listing it finds
the approved canonical candidate(s) of the same brand, asks DeepSeek "is this the
same physical product?" (brand/title/url/image/description evidence), and on a
confident YES records a force_exact_group override + recomputes catalog_row_trust
so the listing deposits into the index. See the service module for the full design
and abstain policy.

Modes:
  --queue            drain pending pdp_identity_review_queue rows (all brands)
  --brand NAME       targeted: review review_required listings of one brand

Dry-run by default. --apply writes overrides + recomputes trust + updates the queue.

Local run against prod (DeepSeek key lives on the `web` Railway service):
  railway run --service web bash -lc 'DB_POOL_MIN_SIZE=1 DB_POOL_MAX_SIZE=1 \
    DB_POOL_ACQUIRE_TIMEOUT_SECONDS=30 DATABASE_URL="$DATABASE_PUBLIC_URL" \
    .venv/bin/python scripts/llm_identity_review.py --queue --limit 30'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.llm_identity_reviewer import (  # noqa: E402
    drain_review_queue, reconcile_grouping, review_brand,
)


async def _run(args: argparse.Namespace) -> dict:
    await database.connect()
    try:
        if args.reconcile_grouping:
            return await reconcile_grouping(limit=args.limit, apply=args.apply)
        if args.queue:
            return await drain_review_queue(
                limit=args.limit, offset=args.offset,
                min_confidence=args.min_confidence, apply=args.apply,
            )
        return await review_brand(
            brand=args.brand, limit=args.limit,
            min_confidence=args.min_confidence, apply=args.apply,
        )
    finally:
        await database.disconnect()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--queue", action="store_true", help="drain pending pdp_identity_review_queue")
    src.add_argument("--brand", help="targeted: review review_required listings of one brand")
    src.add_argument("--reconcile-grouping", action="store_true",
                     help="re-point content_key to canonical for already-approved overrides")
    ap.add_argument("--apply", action="store_true", help="write overrides + queue updates (default: dry-run)")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--min-confidence", type=float, default=0.85, help="min LLM confidence to auto-approve")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    print(json.dumps(asyncio.run(_run(args)), indent=2, default=str))


if __name__ == "__main__":
    main()
