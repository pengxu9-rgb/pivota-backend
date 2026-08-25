"""Stage 2 historical attribution-edge backfill.

Retroactively creates `commerce_attribution_edges` rows for paid orders
that predate T9 stamping. Recomputes the affected `gmv_attribution_daily`
rollup days so T6's downstream math reflects the backfill.

Usage. Production is Cloud Run (pivota-prod/us-west1); run it there via a
throwaway job on the production image, which mounts the DATABASE_URL secret (a
job inherits NO env and NO secrets) and takes its verdict from the exit code:

    # Dry-run preview (DEFAULT — no writes, no auth required):
    scripts/ops/run_oneoff_job.sh scripts/stage2_backfill_attribution_edges.py \\
        --merchant-id merch_efbc46b4619cfbdf

    # Live backfill (AUTHORIZATION REQUIRED per STAGE_2 §3.3):
    scripts/ops/run_oneoff_job.sh scripts/stage2_backfill_attribution_edges.py \\
        --merchant-id merch_efbc46b4619cfbdf \\
        --commit

    # Locally, against a database you already have a URL for:
    DATABASE_URL=... python3 scripts/stage2_backfill_attribution_edges.py \\
        --merchant-id merch_efbc46b4619cfbdf

Full pattern and its footguns: docs/runbooks/operating_on_gcp_production.md.

Idempotency:
- Each edge_id is derived from uuid5(NAMESPACE_URL, "{merchant_id}:{order_id}")
  matching the live writer in services/commerce_attribution_service.py:312.
- INSERT uses ON CONFLICT (edge_id) DO NOTHING — re-runs are safe.

Cohort filter (encoded; see STAGE_2_HISTORICAL_BACKFILL.md §0):
- Includes: orders with concrete agent_id (NOT NULL AND NOT 'ops_canary')
- Skips: ops_canary (operational test canaries, not real attribution)
- NULL-agent orders: SKIPPED by default per the recommended design (§1.1
  Option A). Pass --include-direct to flip to Option B (backfill as direct).

Surface tag: 'historical_backfill_v1.3.2' — distinguishes from live ucp/agent.
Click id scheme: 'clk_backfill_<sha8(order_id)>' — deterministic.
Channel partner id: NULL — no historical partner attribution recoverable.

References:
- docs/monetization/deploy/STAGE_2_HISTORICAL_BACKFILL.md
- services/commerce_attribution_service.py (live writer being mirrored)
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    sys.stderr.write(
        "ERROR: psycopg2 not installed. Install with: pip install psycopg2-binary\n"
    )
    sys.exit(2)


SURFACE_TAG = "historical_backfill_v1.3.2"

# Mirrors the live writer's edge_id generation in
# services/commerce_attribution_service.py upsert_order_attribution_edge.
def _edge_id_for(merchant_id: str, order_id: str) -> str:
    return f"cae_{uuid.uuid5(uuid.NAMESPACE_URL, f'{merchant_id}:{order_id}').hex[:24]}"


def _click_id_for(order_id: str) -> str:
    return f"clk_backfill_{hashlib.sha256(order_id.encode()).hexdigest()[:8]}"


def _gross_cents(subtotal: Any, discount_total: Any) -> int:
    sub = Decimal(str(subtotal or "0"))
    disc = Decimal(str(discount_total or "0"))
    gross = sub - disc
    if gross < Decimal("0"):
        gross = Decimal("0")
    return int((gross * Decimal("100")).quantize(Decimal("1")))


def _connect():
    url = os.environ.get("DATABASE_PUBLIC_URL") or os.environ.get("DATABASE_URL")
    if not url:
        sys.stderr.write(
            "ERROR: neither DATABASE_PUBLIC_URL nor DATABASE_URL is set.\n"
            "    In production, run this inside Cloud Run - it mounts the secret:\n"
            "      scripts/ops/run_oneoff_job.sh scripts/stage2_backfill_attribution_edges.py\n"
            "    Locally, set DATABASE_URL to a database you already have a URL for.\n"
        )
        sys.exit(2)
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


def _fetch_candidates(cur, merchant_id: str, include_direct: bool) -> list[dict[str, Any]]:
    # ops_canary is filtered out unconditionally — it's not real attribution.
    # NULL agent_id is included only when --include-direct is set.
    where_agent = (
        "(o.agent_id IS NOT NULL AND o.agent_id NOT IN ('ops_canary'))"
        if not include_direct
        else "(o.agent_id IS NULL OR (o.agent_id IS NOT NULL AND o.agent_id NOT IN ('ops_canary')))"
    )
    cur.execute(
        f"""
        SELECT o.order_id, o.subtotal, o.discount_total, o.agent_id,
               o.agent_session_id, o.created_at,
               o.shopify_order_id IS NOT NULL AS is_shopify
        FROM orders o
        WHERE o.merchant_id = %s
          AND o.payment_status = 'paid'
          AND {where_agent}
          AND NOT EXISTS (
            SELECT 1 FROM commerce_attribution_edges cae
            WHERE cae.order_id = o.order_id
          )
        ORDER BY o.created_at
        """,
        (merchant_id,),
    )
    return list(cur.fetchall())


def _insert_edge(cur, merchant_id: str, order: dict[str, Any]) -> bool:
    """INSERT one edge with ON CONFLICT idempotency. Returns True if inserted,
    False if the row already existed (deterministic edge_id collided)."""
    order_id = order["order_id"]
    edge_id = _edge_id_for(merchant_id, order_id)
    click_id = _click_id_for(order_id)
    gross = _gross_cents(order["subtotal"], order["discount_total"])
    agent_id = order["agent_id"]  # may be None when --include-direct

    cur.execute(
        """
        INSERT INTO commerce_attribution_edges (
          edge_id, merchant_id, click_id, order_id,
          surface, commerce_surface, agent_id, channel_partner_id,
          gross_attributed_gmv_cents,
          refund_amount_cents,
          checkout_started_at, created_at, updated_at,
          metadata
        ) VALUES (
          %s, %s, %s, %s,
          %s, %s, %s, NULL,
          %s,
          0,
          %s, %s, %s,
          %s::jsonb
        )
        ON CONFLICT (edge_id) DO NOTHING
        RETURNING edge_id
        """,
        (
            edge_id,
            merchant_id,
            click_id,
            order_id,
            SURFACE_TAG,
            SURFACE_TAG,
            agent_id,
            gross,
            order["created_at"],
            order["created_at"],
            datetime.now(timezone.utc),
            psycopg2.extras.Json(
                {
                    "backfill_source": "stage2_backfill_attribution_edges.py",
                    "backfill_version": "v1.3.2",
                    "agent_session_id": order["agent_session_id"],
                    "is_shopify": order["is_shopify"],
                }
            ),
        ),
    )
    return cur.fetchone() is not None


def _affected_dates(orders: Iterable[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    for o in orders:
        ts = o["created_at"]
        if isinstance(ts, datetime):
            seen.add(ts.astimezone(timezone.utc).date().isoformat())
    return sorted(seen)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--merchant-id", required=True, help="Target merchant_id")
    p.add_argument(
        "--commit",
        action="store_true",
        help="Apply the backfill. Without this, runs in dry-run preview mode.",
    )
    p.add_argument(
        "--include-direct",
        action="store_true",
        help=(
            "Also backfill orders with NULL agent_id (direct/organic). "
            "Default skips them — see STAGE_2 §1.1."
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional cap on number of rows processed (for incremental rollout).",
    )
    args = p.parse_args()

    try:
        conn = _connect()
    except psycopg2.Error as exc:
        sys.stderr.write(f"ERROR: DB connect failed: {exc}\n")
        return 2

    try:
        with conn.cursor() as cur:
            candidates = _fetch_candidates(cur, args.merchant_id, args.include_direct)

        if args.limit is not None:
            candidates = candidates[: args.limit]

        if not candidates:
            print(f"No backfill candidates for merchant_id={args.merchant_id}.")
            return 0

        total_gross = sum(_gross_cents(o["subtotal"], o["discount_total"]) for o in candidates)
        dates = _affected_dates(candidates)
        mode = "COMMIT" if args.commit else "DRY-RUN"
        print(f"Stage 2 historical attribution-edge backfill — {mode}")
        print("=" * 78)
        print(f"  merchant_id          : {args.merchant_id}")
        print(f"  candidate orders     : {len(candidates)}")
        print(f"  total gross to stamp : {total_gross} cents (${total_gross / 100:.2f})")
        print(f"  affected dates       : {len(dates)} unique calendar days")
        print(f"    range              : {dates[0]} .. {dates[-1]}")
        print(f"  surface tag          : {SURFACE_TAG}")
        print(f"  include_direct       : {args.include_direct}")
        print("=" * 78)

        if not args.commit:
            print("First 5 candidates (full preview omitted; use psql for the full list):")
            for o in candidates[:5]:
                gross = _gross_cents(o["subtotal"], o["discount_total"])
                edge_id = _edge_id_for(args.merchant_id, o["order_id"])
                click_id = _click_id_for(o["order_id"])
                print(
                    f"  {o['order_id']} → {edge_id} click={click_id} "
                    f"agent={o['agent_id'] or '<NULL>'} gross={gross}"
                )
            print()
            print("DRY-RUN: no rows written. Re-run with --commit to apply.")
            return 0

        # --- live write path ---
        print("AUTHORIZATION assumed (caller passed --commit). Writing rows...")
        inserted = 0
        skipped = 0
        with conn.cursor() as cur:
            for o in candidates:
                if _insert_edge(cur, args.merchant_id, o):
                    inserted += 1
                else:
                    skipped += 1
        conn.commit()

        print(f"Backfill complete: inserted={inserted}, skipped (already exists)={skipped}")
        print()
        print("Next step (STAGE_2 §3.6): trigger T6 recompute for the affected dates.")
        print("Run (within app context):")
        print("  from services.gmv_aggregation_service import recompute_for_date")
        for d in dates:
            print(
                f"  await recompute_for_date(date.fromisoformat('{d}'), "
                f"'{args.merchant_id}')"
            )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
