from __future__ import annotations

import asyncio
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.catalog_backfill_verify as module  # noqa: E402


def test_verify_summary_with_platform_does_not_bind_platform_to_unscoped_queries(monkeypatch) -> None:
    fetch_one_calls = []
    fetch_all_calls = []

    async def fake_fetch_one(sql, params):
        fetch_one_calls.append((sql, dict(params)))
        return {"count": 0}

    async def fake_fetch_all(sql, params):
        fetch_all_calls.append((sql, dict(params)))
        return []

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)

    result = asyncio.run(
        module._verify_summary(
            merchant_id="merch_1",
            platform="wix",
            payloads=[{"platform": "wix", "product_id": "prod_123"}],
            sample_limit=5,
        )
    )

    assert result["missing_product_keys_count"] == 1

    scoped_queries = [
        params
        for sql, params in fetch_one_calls
        if "catalog_products WHERE merchant_id = :merchant_id AND platform = :platform" in sql
        or "catalog_skus WHERE merchant_id = :merchant_id AND platform = :platform" in sql
    ]
    assert scoped_queries
    assert all(params["platform"] == "wix" for params in scoped_queries)

    unscoped_queries = [
        params
        for sql, params in fetch_one_calls
        if "catalog_offers WHERE merchant_id = :merchant_id" in sql
        or "beauty_product_profiles WHERE merchant_id = :merchant_id" in sql
        or "catalog_quote_snapshots WHERE merchant_id = :merchant_id" in sql
        or "catalog_sync_jobs WHERE merchant_id = :merchant_id" in sql
    ]
    assert unscoped_queries
    assert all("platform" not in params for params in unscoped_queries)

    assert fetch_all_calls[0][1] == {"merchant_id": "merch_1", "platform": "wix"}
