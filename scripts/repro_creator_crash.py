"""
Repro / characterization script for Creator Agent multi-turn load.

This script issues concurrent "conversations" against the Shopping Agent
gateway:

    POST /agent/shop/v1/invoke  (operation=find_products_multi)

Each conversation sends M sequential turns; N conversations can run in
parallel. The goal is to reproduce or approximate the crash scenario
observed when the Creator Agent is queried repeatedly in multi-turn
sessions.

Usage (from repo root, with a running backend):

  python3 scripts/repro_creator_crash.py \\
      --base-url http://localhost:8000 \\
      --conversations 10 \\
      --turns-per-conversation 8 \\
      --concurrency 5

The script prints:
  - Total requests / per-status counts
  - Per-request latency stats (min/avg/p95)
  - Optional rough process memory usage when run on Unix (via resource.getrusage)

NOTE: This script is for local diagnostics and load/regression testing.
It does not introduce any production dependency; it only uses stdlib.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import time
from typing import Any, Dict, List, Tuple

import urllib.request
import urllib.error


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repro / load tester for Creator Agent multi-turn flows.",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("PIVOTA_BACKEND_BASE_URL", "http://localhost:8000"),
        help="Base URL for the backend (default: %(default)s)",
    )
    parser.add_argument(
        "--conversations",
        type=int,
        default=8,
        help="Number of logical conversations to start (default: %(default)s)",
    )
    parser.add_argument(
        "--turns-per-conversation",
        type=int,
        default=6,
        help="Sequential turns per conversation (default: %(default)s)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Number of concurrent conversations (default: %(default)s)",
    )
    parser.add_argument(
        "--burst",
        action="store_true",
        help="If set, start all conversations immediately instead of staggering.",
    )
    return parser.parse_args()


def _build_body(
    creator_id: str,
    creator_name: str,
    user_id: str,
    turn_index: int,
    recent_queries: List[str],
) -> Dict[str, Any]:
    """
    Build a request body that closely matches the Creator Agent UI's
    call pattern (see pivota-creator-ui/src/lib/pivotaAgentClient.ts).
    """
    query = recent_queries[-1] if recent_queries else f"load-test-turn-{turn_index}"
    return {
        "operation": "find_products_multi",
        "payload": {
            "search": {
                "query": query,
                "page": 1,
                "limit": 90,
                "in_stock_only": False,
            },
            "user": {
                "id": user_id,
                "recent_queries": recent_queries[-5:],
            },
        },
        "metadata": {
            "creator_id": creator_id,
            "creator_name": creator_name,
            "source": "creator-agent-ui",
            # Optional: stable trace id per conversation for backend budgeting.
            "trace_id": f"repro-{creator_id}-{user_id}",
        },
    }


def _post_json(url: str, body: Dict[str, Any]) -> Tuple[int, float, str]:
    """
    Synchronous POST using urllib (no external deps).

    Returns (status_code, latency_seconds, error_or_empty_string).
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            _ = resp.read()  # Drain body to avoid connection reuse issues.
            latency = time.monotonic() - start
            return resp.getcode(), latency, ""
    except urllib.error.HTTPError as e:
        # HTTPError is also a file-like object.
        try:
            _ = e.read()
        except Exception:
            pass
        latency = time.monotonic() - start
        return e.code, latency, str(e)
    except Exception as e:
        latency = time.monotonic() - start
        return 0, latency, str(e)


def _maybe_memory_usage_kb() -> int:
    """
    Best-effort process memory usage in kilobytes (Unix only).
    """
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        # ru_maxrss is kilobytes on Linux, bytes on some BSDs; treat as KB.
        return int(usage.ru_maxrss)
    except Exception:
        return -1


def run_load(args: argparse.Namespace) -> None:
    url = f"{args.base_url.rstrip('/')}/agent/shop/v1/invoke"

    total_requests = 0
    status_counts: Dict[int, int] = {}
    latencies: List[float] = []
    errors: List[str] = []

    def run_conversation(conv_id: int) -> None:
        nonlocal total_requests
        creator_id = f"creator-repro-{conv_id % 3}"
        creator_name = f"Creator {conv_id % 3}"
        user_id = f"user-repro-{conv_id}"
        recent_queries: List[str] = []

        for turn in range(args.turns_per_conversation):
            # Simulate evolving user queries.
            if not recent_queries:
                recent_queries.append(f"hoodie for cold weather {conv_id}")
            else:
                recent_queries.append(
                    recent_queries[-1] + f" (turn {turn})"
                )

            body = _build_body(
                creator_id=creator_id,
                creator_name=creator_name,
                user_id=user_id,
                turn_index=turn,
                recent_queries=recent_queries,
            )

            status, latency, err = _post_json(url, body)
            total_requests += 1
            status_counts[status] = status_counts.get(status, 0) + 1
            latencies.append(latency)
            if err:
                errors.append(err)

    ids = list(range(args.conversations))

    if not args.burst:
        # Staggered start: run up to `concurrency` conversations at a time.
        for i in range(0, len(ids), args.concurrency):
            batch = ids[i : i + args.concurrency]
            for conv_id in batch:
                run_conversation(conv_id)
    else:
        # Simple interleaving: iterate turns per conversation in a round-robin fashion.
        for turn in range(args.turns_per_conversation):
            for conv_id in ids:
                run_conversation(conv_id)

    print("=== Creator Agent Repro Summary ===")
    print(f"Base URL: {args.base_url}")
    print(f"Conversations: {args.conversations}")
    print(f"Turns per conversation: {args.turns_per_conversation}")
    print(f"Total requests: {total_requests}")
    print("Status counts:")
    for code, count in sorted(status_counts.items(), key=lambda kv: kv[0]):
        label = str(code) if code != 0 else "network/error"
        print(f"  {label}: {count}")

    if latencies:
        print("Latency (seconds):")
        print(f"  min:  {min(latencies):.3f}")
        print(f"  avg:  {statistics.mean(latencies):.3f}")
        print(f"  p95:  {statistics.quantiles(latencies, n=20)[-1]:.3f}")

    mem_kb = _maybe_memory_usage_kb()
    if mem_kb >= 0:
        print(f"Approx. process max RSS: {mem_kb} KB")

    if errors:
        sample = errors[:5]
        print("Sample errors (up to 5):")
        for e in sample:
            print(f"  {e}")


def main() -> None:
    args = _parse_args()
    run_load(args)


if __name__ == "__main__":
    main()

