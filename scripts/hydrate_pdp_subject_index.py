#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from db.database import database  # noqa: E402
from services.pdp_governance_service import (  # noqa: E402
    REVIEW_ACTOR_SYSTEM,
    get_pdp_subject_index_stats,
    hydrate_pdp_subject_index,
)


def _json_default(value: Any) -> str:
    return str(value)


async def _run(args: argparse.Namespace) -> Dict[str, Any]:
    if not database.is_connected:
        await database.connect()
    try:
        if not args.apply:
            stats = await get_pdp_subject_index_stats()
            return {"status": "dry_run", "limit": args.limit, "current": stats}
        return await hydrate_pdp_subject_index(
            limit=args.limit,
            actor_type=REVIEW_ACTOR_SYSTEM,
            actor_id=args.actor_id,
        )
    finally:
        if database.is_connected:
            await database.disconnect()


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hydrate the PDP subject index for the employee governance dashboard.")
    parser.add_argument("--limit", type=int, default=int(os.getenv("PDP_HYDRATION_LIMIT", "1000")), help="Max recent internal groups and external seeds to materialize.")
    parser.add_argument("--actor-id", default=os.getenv("PDP_HYDRATION_ACTOR_ID", "pdp_subject_hydration_job"), help="Audit actor id.")
    parser.add_argument("--apply", action="store_true", help="Write refreshed subjects. Without this flag, only current stats are printed.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    result = asyncio.run(_run(args))
    print(json.dumps(result, default=_json_default, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
