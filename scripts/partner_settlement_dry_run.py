#!/usr/bin/env python3
"""Read-only dry run of the v2 partner settlement engine.

Blocker #3 of the channel-partner onboarding review is that the rev-share v2
engine and the T7/T8 settlement crons have never run against real data. Before
flipping PARTNER_REV_SHARE_USE_V2=true and resuming the paused crons, this
script exercises the engine against production data and reports what each
partner WOULD be paid — writing nothing.

It is faithful to what T8 does: it settles the latest completed billing_runs
row (same selection as audit_scheduler._run_partner_settlement_latest), scopes
partners the same way run_settlement() does, and calls
partner_rev_share_engine_v2.compute_partner_comp_v2() — the pure, write-free
computation. It deliberately never calls run_settlement() or
write_settlement_snapshot() (those INSERT immutable snapshots even in v2 mode).

Safety:
- SELECT-only. No INSERT/UPDATE/DELETE is issued by any code path it calls.
- Tiny connection pool; short-lived.

Usage (PRODUCTION IS CLOUD RUN — pivota-prod/us-west1 since the 2026-08-22
cutover; Railway is RETIRED (#1872), so a `railway run` here would compute the
settlement of a platform nobody is served from):

There is no `railway run` equivalent — Cloud Run has no host to attach to. Run a
throwaway job on the production image. The helper wraps the verified pattern and
takes its verdict from the job's EXIT CODE:

  scripts/ops/run_oneoff_job.sh scripts/partner_settlement_dry_run.py
  # or target a specific billing run / explicit period:
  scripts/ops/run_oneoff_job.sh scripts/partner_settlement_dry_run.py --billing-run-id 42
  scripts/ops/run_oneoff_job.sh scripts/partner_settlement_dry_run.py \\
      --period-start 2025-06-01 --period-end 2025-06-30
  scripts/ops/run_oneoff_job.sh scripts/partner_settlement_dry_run.py --json

The raw gcloud form, its three footguns, and the promotion procedure this feeds
are in docs/runbooks/operating_on_gcp_production.md and
docs/monetization/partner_settlement_promotion_runbook.md. The one that bites
here: a job inherits NO env and NO secrets, so without DATABASE_URL mounted this
fails looking like a database outage rather than a missing mount.

Locally, against a database you have a URL for, it is just:
  DATABASE_URL=... python scripts/partner_settlement_dry_run.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Short-lived operator script; keep the production pool tiny.
os.environ.setdefault("DB_POOL_MIN_SIZE", "1")
os.environ.setdefault("DB_POOL_MAX_SIZE", "2")
if os.getenv("DATABASE_PUBLIC_URL"):
    os.environ["DATABASE_URL"] = os.getenv("DATABASE_PUBLIC_URL", "")

from db.database import IS_POSTGRES, database  # noqa: E402
from services import partner_rev_share_engine_v2  # noqa: E402


async def _latest_completed_billing_run() -> dict[str, Any] | None:
    row = await database.fetch_one(
        "SELECT id, period_start, period_end FROM billing_runs "
        "WHERE status = 'completed' ORDER BY completed_at DESC LIMIT 1"
    )
    return dict(row) if row else None


async def _billing_run_by_id(billing_run_id: int) -> dict[str, Any] | None:
    row = await database.fetch_one(
        "SELECT id, period_start, period_end FROM billing_runs WHERE id = :id",
        {"id": billing_run_id},
    )
    return dict(row) if row else None


async def _partners_in_scope(period_start: date, period_end: date) -> list[int]:
    """Mirror run_settlement()'s partner-scoping query exactly."""
    rows = await database.fetch_all(
        """
        SELECT channel_partner_id
        FROM (
          SELECT DISTINCT pa.channel_partner_id
          FROM partner_attribution pa
          WHERE EXISTS (
            SELECT 1
            FROM commerce_attribution_edges cae
            WHERE cae.merchant_id = pa.merchant_id
              AND DATE(cae.created_at) BETWEEN :period_start AND :period_end
          )
          UNION
          SELECT DISTINCT gad.channel_partner_id
          FROM gmv_attribution_daily gad
          WHERE gad.channel_partner_id IS NOT NULL
            AND gad.date BETWEEN :period_start AND :period_end
        ) partner_scope
        WHERE channel_partner_id IS NOT NULL
        ORDER BY channel_partner_id
        """,
        {"period_start": period_start, "period_end": period_end},
    )
    return [int(r["channel_partner_id"]) for r in rows]


async def _all_active_partners() -> list[int]:
    """Fallback scope: every non-inactive partner (catches partners with

    attributions but no in-period edges/GMV yet, so operators can still see a
    zeroed computation and confirm the engine runs end-to-end).
    """
    rows = await database.fetch_all(
        "SELECT id FROM channel_partners WHERE status <> 'inactive' ORDER BY id"
    )
    return [int(r["id"]) for r in rows]


def _to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _dollars(cents: Any) -> str:
    return f"${int(cents or 0) / 100:,.2f}"


async def _drive(args: argparse.Namespace) -> None:
    if not IS_POSTGRES:
        raise SystemExit(
            "Refusing to run against non-Postgres DATABASE_URL; point at the "
            "production DB (e.g. `scripts/ops/run_oneoff_job.sh "
            "scripts/partner_settlement_dry_run.py`, which mounts the "
            "DATABASE_URL secret — a Cloud Run job inherits nothing)."
        )

    await database.connect()
    try:
        if args.period_start and args.period_end:
            period_start = date.fromisoformat(args.period_start)
            period_end = date.fromisoformat(args.period_end)
            billing_run_id = None
        else:
            run = (
                await _billing_run_by_id(args.billing_run_id)
                if args.billing_run_id
                else await _latest_completed_billing_run()
            )
            if not run:
                raise SystemExit(
                    "No completed billing_runs row found. Run T7 (invoice "
                    "generation) first, or pass --period-start/--period-end."
                )
            billing_run_id = int(run["id"])
            period_start = _to_date(run["period_start"])
            period_end = _to_date(run["period_end"])

        scoped = await _partners_in_scope(period_start, period_end)
        used_fallback = False
        if not scoped and args.include_all_partners:
            scoped = await _all_active_partners()
            used_fallback = True

        results: list[dict[str, Any]] = []
        grand_total = 0
        for partner_id in scoped:
            comp = await partner_rev_share_engine_v2.compute_partner_comp_v2(
                partner_id, period_start, period_end
            )
            meta = comp.get("v2_metadata", {})
            net = int(comp.get("net_comp_cents") or 0)
            grand_total += net
            results.append(
                {
                    "channel_partner_id": partner_id,
                    "net_comp_cents": net,
                    "subscription_rev_cents": int(comp.get("subscription_rev_cents") or 0),
                    "credit_overage_rev_cents": int(comp.get("credit_overage_rev_cents") or 0),
                    "gmv_take_rev_cents": int(comp.get("gmv_take_rev_cents") or 0),
                    "brand_count_computed": meta.get("brand_count_computed", 0),
                    "brand_count_skipped_no_activation": meta.get(
                        "brand_count_skipped_no_activation", 0
                    ),
                    "brand_count_skipped_tail_exhausted": meta.get(
                        "brand_count_skipped_tail_exhausted", 0
                    ),
                    "brand_count_suspended_nonpayment": meta.get(
                        "brand_count_suspended_nonpayment", 0
                    ),
                    "active_rate_scope": meta.get("active_rate_scope"),
                    "gmv_take_definition": meta.get("gmv_take_definition"),
                }
            )

        payload = {
            "dry_run": True,
            "wrote_anything": False,
            "billing_run_id": billing_run_id,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "partner_scope": "all_active_fallback" if used_fallback else "run_settlement_scope",
            "partner_count": len(results),
            "grand_total_net_comp_cents": grand_total,
            "partners": results,
        }

        if args.json:
            print(json.dumps(payload, indent=2, default=str))
            return

        print("=" * 78)
        print("PARTNER SETTLEMENT DRY RUN (v2 engine) — READ ONLY, NOTHING WRITTEN")
        print("=" * 78)
        print(f"billing_run_id : {billing_run_id}")
        print(f"period         : {period_start} .. {period_end}")
        print(f"partner scope  : {payload['partner_scope']}")
        print(f"partners       : {len(results)}")
        print("-" * 78)
        if not results:
            print("No partners in scope for this period.")
            if not args.include_all_partners:
                print("(Pass --include-all-partners to compute a zeroed run for every partner.)")
        for r in results:
            print(
                f"partner #{r['channel_partner_id']:<5} "
                f"net={_dollars(r['net_comp_cents']):>13}  "
                f"sub={_dollars(r['subscription_rev_cents']):>11}  "
                f"ovg={_dollars(r['credit_overage_rev_cents']):>10}  "
                f"gmv={_dollars(r['gmv_take_rev_cents']):>12}  "
                f"[scope {r['active_rate_scope']}/{r['gmv_take_definition']}] "
                f"brands ✓{r['brand_count_computed']} "
                f"⊘act{r['brand_count_skipped_no_activation']} "
                f"⊘tail{r['brand_count_skipped_tail_exhausted']} "
                f"⊘nonpay{r['brand_count_suspended_nonpayment']}"
            )
        print("-" * 78)
        print(f"GRAND TOTAL net comp across partners: {_dollars(grand_total)}")
        print("=" * 78)
    finally:
        await database.disconnect()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,)
    parser.add_argument(
        "--billing-run-id",
        type=int,
        default=None,
        help="Settle a specific billing_runs id (default: latest completed).",
    )
    parser.add_argument("--period-start", default=None, help="YYYY-MM-DD (with --period-end).")
    parser.add_argument("--period-end", default=None, help="YYYY-MM-DD (with --period-start).")
    parser.add_argument(
        "--include-all-partners",
        action="store_true",
        help="If no partner is in run_settlement scope, compute a zeroed run for every active partner.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(_drive(_parse_args()))
