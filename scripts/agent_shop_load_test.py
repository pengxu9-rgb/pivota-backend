"""
Simple load/regression script for the Shopping Agent gateway.

This script issues repeated POST /agent/shop/v1/invoke requests against a
running Pivota backend to exercise the bounded queue + worker pool and
per-session budgets.

Usage (from repo root):

    python scripts/agent_shop_load_test.py \
        --base-url http://localhost:8000 \
        --concurrency 8 \
        --requests-per-worker 20

Environment variables:
    PIVOTA_BACKEND_BASE_URL  Base URL for the FastAPI service
                             (default: http://localhost:8000)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from typing import Any, Dict

import httpx


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load/regression tester for /agent/shop/v1/invoke",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("PIVOTA_BACKEND_BASE_URL", "http://localhost:8000"),
        help="Base URL for the Pivota backend (default: %(default)s)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="Number of concurrent workers (default: %(default)s)",
    )
    parser.add_argument(
        "--requests-per-worker",
        type=int,
        default=20,
        help="Number of requests per worker (default: %(default)s)",
    )
    parser.add_argument(
        "--mode",
        choices=["anonymous", "session"],
        default="session",
        help=(
            "anonymous: no session id (tests global queue); "
            "session: fixed creator/user (tests single-flight + budgets) "
            "(default: %(default)s)"
        ),
    )
    return parser.parse_args()


def _build_body(mode: str, idx: int) -> Dict[str, Any]:
    """
    Construct a basic find_products_multi payload.
    """
    payload: Dict[str, Any] = {
        "operation": "find_products_multi",
        "payload": {
            "search": {
                "query": f"load-test-{idx}",
                "page": 1,
                "limit": 24,
                "in_stock_only": False,
            },
        },
        "metadata": {},
    }

    if mode == "session":
        # Fixed creator/user identity to exercise per-session budgets and
        # single-flight semantics.
        payload["metadata"] = {
            "creator_id": "creator-load-test",
            "creator_name": "Load Test Creator",
            "source": "creator-agent-ui",
            "trace_id": "load-test-session-1",
        }
        payload["payload"]["user"] = {
            "id": "user-load-test",
            "recent_queries": ["hoodie", "coat"],
        }

    return payload


async def _worker(
    client: httpx.AsyncClient,
    base_url: str,
    worker_id: int,
    requests_per_worker: int,
    mode: str,
) -> Dict[str, int]:
    url = f"{base_url.rstrip('/')}/agent/shop/v1/invoke"
    stats = {
        "ok": 0,
        "429": 0,
        "409": 0,
        "other_error": 0,
    }

    for i in range(requests_per_worker):
        body = _build_body(mode, i)
        try:
            resp = await client.post(url, json=body, timeout=15.0)
        except Exception:
            stats["other_error"] += 1
            continue

        if resp.status_code == 200:
            stats["ok"] += 1
        elif resp.status_code == 429:
            stats["429"] += 1
        elif resp.status_code == 409:
            stats["409"] += 1
        else:
            stats["other_error"] += 1

    return stats


async def main_async(args: argparse.Namespace) -> None:
    start = time.time()
    async with httpx.AsyncClient() as client:
        tasks = [
            _worker(
                client,
                args.base_url,
                worker_id=i,
                requests_per_worker=args.requests_per_worker,
                mode=args.mode,
            )
            for i in range(args.concurrency)
        ]
        results = await asyncio.gather(*tasks)

    elapsed = time.time() - start

    aggregated = {
        "ok": 0,
        "429": 0,
        "409": 0,
        "other_error": 0,
    }
    for res in results:
        for key, value in res.items():
            aggregated[key] += value

    total = sum(aggregated.values())
    print(f"Completed load test in {elapsed:.2f}s")
    print(f"Total requests: {total}")
    for key in ["ok", "429", "409", "other_error"]:
        print(f"{key}: {aggregated[key]}")


def main() -> None:
    args = _parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()

