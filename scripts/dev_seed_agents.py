#!/usr/bin/env python3
"""
Seed one or more dev agents into the local database.

Usage:
  python3 scripts/dev_seed_agents.py --count 2
  python3 scripts/dev_seed_agents.py --allowed-merchants merch_1,merch_2
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Optional

from db.agents import create_agent
from db.database import database, engine, metadata


def _parse_csv(value: Optional[str]) -> Optional[list[str]]:
    if value is None:
        return None
    raw = str(value or "").strip()
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",")]
    cleaned = [p for p in parts if p]
    return cleaned or None


async def _run(args: argparse.Namespace) -> int:
    metadata.create_all(engine)
    await database.connect()
    try:
        created = []
        for idx in range(int(args.count)):
            name_suffix = f" {idx + 1}" if int(args.count) > 1 else ""
            created.append(
                await create_agent(
                    agent_name=f"{args.name}{name_suffix}",
                    agent_type=str(args.type),
                    allowed_merchants=_parse_csv(args.allowed_merchants),
                    webhook_url=str(args.webhook_url or "").strip() or None,
                )
            )
    finally:
        await database.disconnect()

    print(json.dumps({"agents": created}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed dev agents into the local DB")
    parser.add_argument("--count", type=int, default=1, help="How many agents to create (default: 1)")
    parser.add_argument("--name", type=str, default="Dev Agent", help="Base agent name (default: Dev Agent)")
    parser.add_argument("--type", type=str, default="custom", help="Agent type (default: custom)")
    parser.add_argument(
        "--allowed-merchants",
        type=str,
        default="",
        help="Comma-separated merchant_ids to restrict access (default: none = all)",
    )
    parser.add_argument("--webhook-url", type=str, default="", help="Optional agent webhook url")
    args = parser.parse_args()

    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

