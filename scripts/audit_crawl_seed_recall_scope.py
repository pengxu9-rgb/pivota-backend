"""Audit exact crawl seed IDs and prepare a guarded recall-scope repair.

    python -m scripts.audit_crawl_seed_recall_scope --seed-ids-file ids.json --output plan.json
    python -m scripts.audit_crawl_seed_recall_scope --apply-plan reviewed-plan.json --output result.json

The default command is read-only. No domain-wide or inferred cohort expansion:
ids.json must be a nonempty JSON array of exact external_brand_crawl:: IDs.
Apply is a separate invocation of the saved plan, checks every before value under
row locks, and aborts the entire transaction on a changed row or scope collision.
It changes only tool and updated_at; identities and catalog content are untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PREFIX = "external_brand_crawl::"
OLD_TOOL = "external_brand_crawl"
NEW_TOOL = "*"
VERSION = "crawl_seed_recall_scope_v1"
MAX_IDS = 1000
FIELDS = ("id", "market", "tool", "external_product_id", "status", "updated_at")
ROW_SQL = "SELECT id, market, tool, external_product_id, status, updated_at FROM external_product_seeds WHERE id=:id"
COLLISION_SQL = """
SELECT id FROM external_product_seeds
WHERE market=:market AND external_product_id=:external_product_id
  AND tool=:new_tool AND status='active' AND id<>:id
"""


def validate_ids(ids: Any) -> list[str]:
    if not isinstance(ids, list) or not ids or len(ids) > MAX_IDS:
        raise ValueError(f"seed IDs must be a nonempty array of at most {MAX_IDS} exact IDs")
    if any(not isinstance(seed_id, str) or not seed_id.startswith(PREFIX)
           or seed_id == PREFIX or seed_id != seed_id.strip() for seed_id in ids):
        raise ValueError("every ID must be an exact external_brand_crawl:: seed ID")
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate seed IDs are not allowed")
    return sorted(ids)


def snapshot(row: Any) -> dict:
    result = {key: row[key] for key in FIELDS}
    if isinstance(result["updated_at"], datetime):
        result["updated_at"] = result["updated_at"].isoformat()
    return result


async def collisions(db: Any, row: dict) -> list[str]:
    rows = await db.fetch_all(COLLISION_SQL, {
        "id": row["id"], "market": row["market"],
        "external_product_id": row["external_product_id"], "new_tool": NEW_TOOL,
    })
    return sorted(r["id"] for r in rows)


async def audit(db: Any, ids: list[str]) -> dict:
    ids = validate_ids(ids)
    changes, blocked, already_visible = [], [], []
    for seed_id in ids:
        row = await db.fetch_one(ROW_SQL, {"id": seed_id})
        if row is None:
            blocked.append({"id": seed_id, "reason": "not_found"})
            continue
        before = snapshot(row)
        if before["tool"] == NEW_TOOL and before["status"] == "active":
            already_visible.append(seed_id)
            continue
        if before["tool"] != OLD_TOOL or before["status"] != "active":
            blocked.append({"id": seed_id, "reason": "outside_active_crawl_scope", "before": before})
            continue
        other_ids = await collisions(db, before)
        if other_ids:
            blocked.append({"id": seed_id, "reason": "active_scope_collision", "collision_ids": other_ids})
            continue
        changes.append({"before": before, "after_tool": NEW_TOOL})
    return {"version": VERSION, "generated_at": datetime.now(timezone.utc).isoformat(),
            "requested_ids": ids, "changes": changes, "blocked": blocked,
            "already_visible": already_visible, "mode": "dry_run"}


async def apply_plan(db: Any, plan: dict) -> dict:
    if plan.get("version") != VERSION or plan.get("mode") != "dry_run":
        raise ValueError("expected a saved dry-run recall-scope plan")
    requested = validate_ids(plan.get("requested_ids"))
    if plan.get("blocked"):
        raise ValueError("plan has blocked rows; review and audit a narrower exact-ID cohort")
    changes = plan.get("changes")
    if not isinstance(changes, list):
        raise ValueError("plan changes must be an array")
    seen = set()
    for change in changes:
        before = change.get("before") or {}
        seed_id = before.get("id")
        if (seed_id not in requested or seed_id in seen or set(before) != set(FIELDS)
                or before.get("tool") != OLD_TOOL or before.get("status") != "active"
                or change.get("after_tool") != NEW_TOOL):
            raise ValueError("plan contains an invalid, duplicated, or out-of-scope change")
        seen.add(seed_id)
    applied = []
    async with db.transaction():
        for change in sorted(changes, key=lambda c: c["before"]["id"]):
            before = change["before"]
            current = await db.fetch_one(ROW_SQL + " FOR UPDATE", {"id": before["id"]})
            if current is None or snapshot(current) != before:
                raise ValueError(f"seed changed since audit: {before['id']}")
            if await collisions(db, before):
                raise ValueError(f"scope collision appeared since audit: {before['id']}")
            row = await db.fetch_one(
                "UPDATE external_product_seeds SET tool=:new_tool, updated_at=NOW() "
                "WHERE id=:id AND tool=:old_tool AND status='active' RETURNING id",
                {"id": before["id"], "new_tool": NEW_TOOL, "old_tool": OLD_TOOL},
            )
            if row is None:
                raise ValueError(f"seed no longer matches reviewed scope: {before['id']}")
            applied.append(row["id"])
    return {"version": VERSION, "mode": "applied", "applied_ids": applied,
            "before_values": [c["before"] for c in changes]}


async def _run(args: argparse.Namespace) -> dict:
    from db.database import database

    payload = json.loads(Path(args.apply_plan or args.seed_ids_file).read_text())
    if not args.apply_plan:
        validate_ids(payload)
    await database.connect()
    try:
        return await apply_plan(database, payload) if args.apply_plan else await audit(database, payload)
    finally:
        await database.disconnect()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--seed-ids-file")
    mode.add_argument("--apply-plan")
    parser.add_argument("--output", required=True, help="save the dry-run plan or apply result")
    args = parser.parse_args(argv)
    # Reserve a new output before any database mutation: do not overwrite the
    # reviewed plan, and fail early if its result cannot be recorded locally.
    with Path(args.output).open("x") as output:
        result = asyncio.run(_run(args))
        json.dump(result, output, indent=2, ensure_ascii=False)
        output.write("\n")
        output.flush()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
