#!/usr/bin/env python3
"""Read-only pre-check for the day-10 settlement-file transfer cron.

The `settlement_file_transfer` cron runs monthly on day 10 and executes REAL
Stripe Connect transfers for every settlement_files row where
`transfer_status = 'pending'` AND `calendar_month = <prior month>` (matching
services/audit_scheduler._transfer_prior_month_for_all_partners). Partner
settlement generation (T8) is paused, so there should be nothing to transfer —
this script proves that before the cron fires, writing nothing.

It reports:
  * pending files for the NEXT transfer window (prior month of --as-of / today),
    i.e. exactly what the cron would move;
  * ALL pending files (any month), in case a stray/test row has another date;
  * each partner's stripe_connect_account_id — a pending file for a partner with
    no Connect account can't transfer (it fails no_stripe_connect_account), so
    no money moves.

SELECT-only.

Usage (PRODUCTION IS CLOUD RUN — pivota-prod/us-west1. Railway is the ROLLBACK,
and this pre-check exists to prove no REAL Stripe Connect transfer is pending, so
running it against the rollback would clear a cron that fires against Cloud Run):

There is no `railway run` equivalent. Run a throwaway job on the production
image; the helper wraps the verified pattern and takes its verdict from the job's
EXIT CODE, not from a log read:

  scripts/ops/run_oneoff_job.sh scripts/check_pending_settlement_files.py
  scripts/ops/run_oneoff_job.sh scripts/check_pending_settlement_files.py --as-of 2026-07-10
  scripts/ops/run_oneoff_job.sh scripts/check_pending_settlement_files.py --json

Full pattern and its footguns: docs/runbooks/operating_on_gcp_production.md.
Locally, against a database you have a URL for:
  DATABASE_URL=... python scripts/check_pending_settlement_files.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DB_POOL_MIN_SIZE", "1")
os.environ.setdefault("DB_POOL_MAX_SIZE", "2")
if os.getenv("DATABASE_PUBLIC_URL"):
    os.environ["DATABASE_URL"] = os.getenv("DATABASE_PUBLIC_URL", "")

from db.database import IS_POSTGRES, database  # noqa: E402


def _prior_month(as_of: date) -> date:
    """First day of the month before as_of — matches the cron's prior_month."""
    return (as_of.replace(day=1) - timedelta(days=1)).replace(day=1)


def _dollars(cents: Any) -> str:
    return f"${int(cents or 0) / 100:,.2f}"


async def _pending_rows(where_month: date | None) -> list[dict[str, Any]]:
    clause = "AND sf.calendar_month = :month" if where_month is not None else ""
    params: dict[str, Any] = {}
    if where_month is not None:
        params["month"] = where_month
    rows = await database.fetch_all(
        f"""
        SELECT sf.id, sf.channel_partner_id, sf.calendar_month,
               sf.transfer_amount_cents, sf.transfer_status,
               cp.legal_name, cp.stripe_connect_account_id
        FROM settlement_files sf
        JOIN channel_partners cp ON cp.id = sf.channel_partner_id
        WHERE sf.transfer_status = 'pending'
          {clause}
        ORDER BY sf.calendar_month, sf.id
        """,
        params,
    )
    return [dict(r) for r in rows or []]


async def _drive(args: argparse.Namespace) -> None:
    if not IS_POSTGRES:
        raise SystemExit(
            "Refusing to run against non-Postgres DATABASE_URL; point at the "
            "production DB (e.g. `scripts/ops/run_oneoff_job.sh "
            "scripts/check_pending_settlement_files.py`, which mounts the "
            "DATABASE_URL secret — a Cloud Run job inherits nothing)."
        )

    as_of = date.fromisoformat(args.as_of) if args.as_of else datetime.utcnow().date()
    window = _prior_month(as_of)

    await database.connect()
    try:
        window_rows = await _pending_rows(window)
        all_rows = await _pending_rows(None)
    finally:
        await database.disconnect()

    # Rows that would actually move money: pending, in the window, with a
    # Connect account to receive the transfer.
    would_transfer = [r for r in window_rows if r.get("stripe_connect_account_id")]
    payload = {
        "as_of": as_of.isoformat(),
        "next_transfer_window_month": window.isoformat(),
        "pending_in_window": len(window_rows),
        "pending_in_window_would_transfer": len(would_transfer),
        "pending_any_month": len(all_rows),
        "safe": len(would_transfer) == 0,
        "window_rows": window_rows,
        "all_pending_rows": all_rows,
    }

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        return

    print("=" * 74)
    print("SETTLEMENT-FILE TRANSFER PRE-CHECK — READ ONLY, NOTHING WRITTEN")
    print("=" * 74)
    print(f"as-of date            : {as_of}")
    print(f"next transfer window  : calendar_month = {window} (day-10 cron)")
    print(f"pending in window     : {len(window_rows)}")
    print(f"  of those, transferable (has Stripe Connect): {len(would_transfer)}")
    print(f"pending in ANY month  : {len(all_rows)}")
    print("-" * 74)
    for r in all_rows:
        flag = "MONEY" if (
            r["calendar_month"] == window and r.get("stripe_connect_account_id")
        ) else "no-op"
        connect = r.get("stripe_connect_account_id") or "(no Connect acct)"
        print(
            f"[{flag}] file #{r['id']} partner #{r['channel_partner_id']} "
            f"{r.get('legal_name')!r} month={r['calendar_month']} "
            f"{_dollars(r['transfer_amount_cents'])} connect={connect}"
        )
    print("-" * 74)
    if payload["safe"]:
        print("VERDICT: SAFE — the day-10 cron would transfer $0 (no eligible pending files).")
    else:
        print(
            f"VERDICT: ⚠️  {len(would_transfer)} file(s) WOULD transfer real money on the "
            f"day-10 run for {window}. Review before then (set them to 'skipped' or "
            "revoke) if unintended."
        )
    print("=" * 74)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,)
    p.add_argument(
        "--as-of",
        default=None,
        help="YYYY-MM-DD to evaluate the transfer window from (default: today UTC).",
    )
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(_drive(_parse_args()))
