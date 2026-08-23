"""One-shot Cloud Run Job entrypoint for reviewed Product Insights refreshes."""

from __future__ import annotations

import asyncio
import json
import os

from db.database import database
from services.commerce_index_insight_refresh_service import request_next_insight_refresh


async def _main() -> int:
    if str(os.getenv("COMMERCE_INDEX_INSIGHT_REFRESH_ENABLED") or "").lower() not in {"1", "true", "yes", "on"}:
        print(json.dumps({"ok": True, "claimed": 0, "reason": "feature_disabled"}))
        return 0
    worker_id = str(os.getenv("COMMERCE_INDEX_WORKER_ID") or "cloud-run-insight-refresh")
    limit = max(1, min(200, int(os.getenv("COMMERCE_INDEX_INSIGHT_REFRESH_LIMIT") or "25")))
    await database.connect()
    try:
        results = []
        for _ in range(limit):
            result = await request_next_insight_refresh(worker_id=worker_id)
            if result is None:
                break
            results.append(result)
        print(json.dumps({"ok": True, "claimed": len(results), "results": results}))
        return 0
    finally:
        await database.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
