from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any, Dict

from routes.employee_products import _refresh_external_seed_by_id
from services.external_referral_readiness import run_external_referral_refresh_batch


logger = logging.getLogger(__name__)


async def _refresh_unbounded(seed_id: str) -> Dict[str, Any]:
    """The batch's patience is UNBOUNDED, and it has to say so.

    `crawl_politeness` refuses a slot further out than the caller allows, and the default
    ceiling (CRAWL_MAX_WAIT_SECONDS, 10s) exists for the interactive route where a human is
    waiting. In a batch that ceiling is actively harmful: most of the backoff curve sits
    beyond it, so a host that has 429'd a couple of times can never be waited for. The
    refusal surfaces as a generic failure, every remaining row on that host resolves in
    milliseconds, and the run reports them as unreadable when in fact we declined to wait.

    `max_wait=0` disables the ceiling (`crawl_politeness.before_request`: the refusal is
    guarded on `ceiling > 0`). The pacing itself is unchanged — we still wait our turn; we
    simply stop giving up on the turn. The sibling destination sweep already does this.
    """
    return await _refresh_external_seed_by_id(seed_id, max_wait=0)


async def run_daily_external_referral_refresh(*, limit: int = 500) -> Dict[str, Any]:
    return await run_external_referral_refresh_batch(
        refresh_seed_by_id=_refresh_unbounded,
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
