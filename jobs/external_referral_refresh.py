from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any, Dict

from routes.employee_products import _refresh_external_seed_by_id
from services.external_referral_readiness import run_external_referral_refresh_batch


logger = logging.getLogger(__name__)


async def run_daily_external_referral_refresh(*, limit: int = 500) -> Dict[str, Any]:
    return await run_external_referral_refresh_batch(
        refresh_seed_by_id=_refresh_external_seed_by_id,
        limit=limit,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh active external referral seeds for runtime gating.")
    parser.add_argument("--limit", type=int, default=500, help="Maximum number of referral seeds to refresh")
    args = parser.parse_args()
    summary = asyncio.run(run_daily_external_referral_refresh(limit=args.limit))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    logger.info("external referral refresh completed", extra={"summary": summary})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
