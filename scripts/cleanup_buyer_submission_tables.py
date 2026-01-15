#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import psycopg2


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _connect_db(database_url: str):
    return psycopg2.connect(database_url)


@dataclass(frozen=True)
class CleanupPlan:
    delete_jtis_before: datetime
    delete_idempotency_before: datetime


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Cleanup buyer review replay/idempotency tables (safe dry-run by default).")
    p.add_argument("--database-url", default=os.getenv("DATABASE_URL") or "", help="Postgres connection string.")
    p.add_argument("--apply", action="store_true", help="Apply changes (default: dry-run).")
    p.add_argument("--delete-jtis-after-hours", type=int, default=24, help="Delete JTIs expired more than N hours ago.")
    p.add_argument("--delete-idempotency-after-days", type=int, default=14, help="Delete idempotency keys older than N days.")
    return p.parse_args()


def _build_plan(args: argparse.Namespace) -> CleanupPlan:
    now = _utcnow()
    delete_jtis_before = now - timedelta(hours=max(0, int(args.delete_jtis_after_hours)))
    delete_idempotency_before = now - timedelta(days=max(0, int(args.delete_idempotency_after_days)))
    return CleanupPlan(delete_jtis_before=delete_jtis_before, delete_idempotency_before=delete_idempotency_before)


def _exec(cur, sql: str, params: Tuple) -> int:
    cur.execute(sql, params)
    try:
        return int(cur.rowcount or 0)
    except Exception:
        return 0


def main() -> int:
    args = _parse_args()
    if not args.database_url:
        print("ERROR: missing --database-url (or env DATABASE_URL)")
        return 2

    plan = _build_plan(args)
    mode = "apply" if args.apply else "dry_run"
    print(f"mode={mode}")
    print(f"delete_jtis_before={plan.delete_jtis_before.isoformat()}")
    print(f"delete_idempotency_before={plan.delete_idempotency_before.isoformat()}")

    conn = _connect_db(args.database_url)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            # JTIs: safe to delete after expiry; they only prevent replay.
            del_jtis = _exec(
                cur,
                "DELETE FROM buyer_review_submission_jtis WHERE expires_at < %s",
                (plan.delete_jtis_before,),
            )

            # Idempotency keys: retain for some time to support retry semantics.
            del_keys = _exec(
                cur,
                "DELETE FROM buyer_review_idempotency_keys WHERE created_at < %s",
                (plan.delete_idempotency_before,),
            )

            print(f"deleted_jtis={del_jtis}")
            print(f"deleted_idempotency_keys={del_keys}")

        if args.apply:
            conn.commit()
            print("ok=1")
        else:
            conn.rollback()
            print("ok=1 (dry-run, rolled back)")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

