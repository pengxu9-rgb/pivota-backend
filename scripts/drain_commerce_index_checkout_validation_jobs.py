"""One-shot Cloud Run Job entrypoint for checkout-validation requests."""

from __future__ import annotations

import asyncio
import json
import os

from db.database import database
from services.commerce_index_checkout_validation_service import request_next_checkout_validation


async def _main() -> int:
    if str(os.getenv("COMMERCE_INDEX_CHECKOUT_VALIDATION_ENABLED") or "").lower() not in {"1", "true", "yes", "on"}:
        print(json.dumps({"ok": True, "claimed": 0, "reason": "feature_disabled"}))
        return 0
    worker_id = str(os.getenv("COMMERCE_INDEX_WORKER_ID") or "cloud-run-checkout-validation")
    limit = max(1, min(200, int(os.getenv("COMMERCE_INDEX_CHECKOUT_VALIDATION_LIMIT") or "50")))
    await database.connect()
    try:
        results = []
        for _ in range(limit):
            result = await request_next_checkout_validation(worker_id=worker_id)
            if result is None:
                break
            results.append(result)
        print(json.dumps({"ok": True, "claimed": len(results), "results": results}))
        return 0
    finally:
        await database.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
