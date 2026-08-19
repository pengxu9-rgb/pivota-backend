#!/usr/bin/env python3
"""
Redact plaintext API keys still stored in agents.api_key.

Rows registered before the hash-only change keep the live key in `agents.api_key`. This script
moves each such row onto the hash path and replaces the plaintext with the redacted marker:

  for every agents row whose api_key looks like a real key (ak_… and not already redacted):
    1. ensure an ACTIVE api_keys row exists with key_hash = sha256(plaintext)
       (uses api_key_hash when present and consistent; recomputes otherwise)
    2. UPDATE agents SET api_key = 'redacted:<agent_id>'

Dry-run by default; pass --apply to write. Prints one line per agent, never the key.
Requires the `api_keys` table (the default auth path). Refuses to run if it is missing,
because legacy deployments authenticate against agents.api_key itself.

Run in-cluster (railway ssh / run) — the prod DB is not reachable from a laptop.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys

from db.agents import AGENT_API_KEY_REDACTED_PREFIX, redacted_agent_api_key
from db.database import database


def _looks_like_plaintext_key(value: str | None) -> bool:
    raw = str(value or "").strip()
    if not raw or raw.startswith(AGENT_API_KEY_REDACTED_PREFIX):
        return False
    return raw.startswith("ak_") and len(raw) >= 20


async def main(apply: bool, limit: int | None) -> int:
    await database.connect()
    try:
        has_table = await database.fetch_one("SELECT to_regclass('public.api_keys') AS t")
        if not (has_table and dict(has_table).get("t")):
            print("api_keys table missing; this deployment authenticates on agents.api_key — refusing.")
            return 2

        rows = await database.fetch_all(
            "SELECT agent_id, api_key, api_key_hash FROM agents ORDER BY created_at NULLS LAST, agent_id"
        )
        candidates = [dict(r) for r in rows if _looks_like_plaintext_key(dict(r).get("api_key"))]
        if limit is not None:
            candidates = candidates[:limit]
        print(f"agents with plaintext api_key: {len(candidates)} (apply={apply})")

        redacted = 0
        for row in candidates:
            agent_id = row["agent_id"]
            plaintext = row["api_key"]
            computed = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
            stored = (row.get("api_key_hash") or "").strip().lower()
            key_hash = computed
            note = "" if stored == computed else " (api_key_hash column disagreed; using sha256 of plaintext)"

            existing = await database.fetch_one(
                "SELECT id, status FROM api_keys WHERE key_hash = :h LIMIT 1", {"h": key_hash}
            )
            action = []
            if existing is None:
                action.append("insert api_keys row")
                if apply:
                    await database.execute(
                        """
                        INSERT INTO api_keys (agent_id, name, key_hash, key_prefix, status)
                        VALUES (:agent_id, 'Primary Key', :key_hash, :key_prefix, 'active')
                        """,
                        {"agent_id": agent_id, "key_hash": key_hash, "key_prefix": plaintext[:10]},
                    )
            elif dict(existing).get("status") != "active":
                action.append("reactivate api_keys row")
                if apply:
                    await database.execute(
                        "UPDATE api_keys SET status = 'active', agent_id = :agent_id WHERE id = :id",
                        {"id": dict(existing)["id"], "agent_id": agent_id},
                    )
            action.append("redact agents.api_key")
            if apply:
                await database.execute(
                    "UPDATE agents SET api_key = :marker, updated_at = NOW() WHERE agent_id = :agent_id",
                    {"marker": redacted_agent_api_key(agent_id), "agent_id": agent_id},
                )
                redacted += 1
            print(f"{agent_id}: {', '.join(action)}{note}")

        print(f"done. redacted={redacted if apply else 0} (dry-run shows planned actions only)")
        return 0
    finally:
        await database.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    parser.add_argument("--limit", type=int, default=None, help="process at most N agents")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(apply=args.apply, limit=args.limit)))
