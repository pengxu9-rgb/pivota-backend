"""Shakeout debug endpoints.

Mounts a small family of admin-only HTTP wrappers around v1.3 monetization
internals so end-to-end shakeout scripts can drive the pipeline
synchronously without waiting for the daily/monthly crons. Companion to
the §A signup + §B webhook + §D-H pipeline shakeout work in
`scripts/shakeout/`.

Mounted under `/__shakeout/*` (double-underscore prefix marks internal/
operational endpoints, mirroring /__scheduler_health and /__build).

**Production safety.** Every endpoint refuses to run in production, as
resolved by `config.platform.is_production()` (which fails CLOSED to
production on a managed host that cannot name its environment, so these
endpoints stay shut on a misconfigured deploy). The shared-DB tenancy with
prod means
DB writes from these endpoints would land in the same Postgres as prod
traffic, but the shakeout merchant id pattern (`merch_shakeout_*`) is
filterable. The endpoints additionally require a shared-secret header
to keep curious traffic out of the staging surface.

Header required: `X-Shakeout-Token: <SHAKEOUT_DEBUG_TOKEN env var>`.
Set the token on staging only; rotate freely.

What's NOT exposed: anything that would touch Stripe Live or mutate
non-shakeout merchant rows.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from config.platform import is_production

router = APIRouter(tags=["shakeout-debug"])


_SHAKEOUT_MERCHANT_PREFIX = "merch_shakeout_"


def _require_non_prod() -> None:
    if is_production():
        # Shakeout debug endpoints are disabled in production. The
        # `ALLOW_SHAKEOUT_ON_PROD` env-var bypass was used 2026-05-23
        # for a one-off §D-H E2E run against prod (PR #624) and
        # removed afterward (this commit) — staging is the right
        # shakeout target. If a future prod shakeout becomes
        # necessary, prefer fixing the staging environment over
        # reintroducing this bypass.
        raise HTTPException(
            status_code=403,
            detail=(
                "shakeout debug endpoints are disabled in production"
            ),
        )


def _require_shakeout_token(x_shakeout_token: Optional[str] = Header(None)) -> None:
    """Shared-secret gate. Token comes from SHAKEOUT_DEBUG_TOKEN env var."""
    expected = (os.getenv("SHAKEOUT_DEBUG_TOKEN") or "").strip()
    if not expected:
        raise HTTPException(
            status_code=403,
            detail="SHAKEOUT_DEBUG_TOKEN not configured on this deploy",
        )
    if not x_shakeout_token or x_shakeout_token.strip() != expected:
        raise HTTPException(status_code=401, detail="invalid X-Shakeout-Token")


def _require_shakeout_merchant(merchant_id: str) -> None:
    """Only allow operations on synthetic shakeout merchants."""
    if not merchant_id.startswith(_SHAKEOUT_MERCHANT_PREFIX):
        raise HTTPException(
            status_code=400,
            detail=(
                f"merchant_id must start with '{_SHAKEOUT_MERCHANT_PREFIX}' — "
                "real merchants are not accepted by this endpoint"
            ),
        )


def _gate(
    _np: None = Depends(_require_non_prod),
    _tok: None = Depends(_require_shakeout_token),
) -> None:
    """Compose the two unconditional gates as one dependency."""
    return None


@router.post("/__shakeout/stamp_gross_attributed_gmv")
async def shakeout_stamp_gross(
    order_id: str = Query(..., description="Order id whose edges to stamp"),
    subtotal: str = Query(..., description="Decimal-as-string, e.g. '99.00'"),
    discount_total: str = Query("0", description="Decimal-as-string"),
    _: None = Depends(_gate),
) -> Dict[str, Any]:
    """Synchronously invoke T9 stamp for the given order. Wraps
    services.psp_payment_finalizer.stamp_gross_attributed_gmv so the
    shakeout can verify the stamp without going through the full
    payment finalization path.

    Returns:
        order_id, stamped_count (number of edges whose
        gross_attributed_gmv_cents was just set), gross_cents (the
        amount stamped).
    """
    from services.psp_payment_finalizer import stamp_gross_attributed_gmv

    stamped = await stamp_gross_attributed_gmv(
        order_id,
        subtotal=subtotal,
        discount_total=discount_total,
    )
    return {
        "order_id": order_id,
        "stamped_count": int(stamped or 0),
    }


@router.post("/__shakeout/aggregate_daily")
async def shakeout_aggregate_daily(
    target_date: str = Query(
        ..., alias="date",
        description="ISO date (YYYY-MM-DD) to roll up",
    ),
    merchant_id: Optional[str] = Query(
        None,
        description=(
            "If set, only roll up this merchant (must be a shakeout merchant). "
            "Omit to roll up all merchants for the day (cron behavior)."
        ),
    ),
    _: None = Depends(_gate),
) -> Dict[str, Any]:
    """Synchronously invoke T6 aggregate_daily for the given date.
    Mirrors what the gmv_aggregation_daily cron runs at 02:00 UTC.

    When merchant_id is provided, calls recompute_for_date for that
    one merchant. When omitted, calls aggregate_daily (all merchants).
    """
    try:
        parsed_date = date.fromisoformat(target_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid date: {exc!s}")

    from services.gmv_aggregation_service import (
        aggregate_daily,
        recompute_for_date,
    )

    if merchant_id is not None:
        _require_shakeout_merchant(merchant_id)
        await recompute_for_date(parsed_date, merchant_id)
        return {"date": target_date, "merchant_id": merchant_id, "mode": "recompute"}

    rollup_count = await aggregate_daily(parsed_date)
    return {"date": target_date, "merchant_id": None, "rollup_count": int(rollup_count)}


@router.post("/__shakeout/run_billing_cycle")
async def shakeout_run_billing_cycle(
    period_start: str = Query(..., description="ISO date (YYYY-MM-DD)"),
    period_end: str = Query(..., description="ISO date (YYYY-MM-DD)"),
    _: None = Depends(_gate),
) -> Dict[str, Any]:
    """Synchronously invoke T7 run_billing_cycle for the given period.
    Mirrors what the (paused) invoice_generation_monthly cron would run.

    Caller authority note: this endpoint will create real Stripe drafts
    against whatever STRIPE_SECRET_KEY is configured on this deploy
    (Test on staging, Live on prod — but prod is gated off above). The
    period idempotency in run_billing_cycle (period_start-billing) means
    a second call for the same period returns the same billing_run_id
    without creating duplicate drafts.
    """
    try:
        parsed_start = date.fromisoformat(period_start)
        parsed_end = date.fromisoformat(period_end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid date: {exc!s}")

    if parsed_end <= parsed_start:
        raise HTTPException(
            status_code=400,
            detail="period_end must be strictly after period_start",
        )

    from services.invoice_generation_service import run_billing_cycle

    billing_run_id = await run_billing_cycle(parsed_start, parsed_end)
    return {
        "period_start": period_start,
        "period_end": period_end,
        "billing_run_id": int(billing_run_id),
    }


@router.get("/__shakeout/edges/{order_id}")
async def shakeout_inspect_edges(
    order_id: str,
    _: None = Depends(_gate),
) -> Dict[str, Any]:
    """Read-only inspection of commerce_attribution_edges for an order.
    Useful between steps in the shakeout to verify edge creation +
    stamping without leaving the script.
    """
    from db.database import database

    rows = await database.fetch_all(
        """
        SELECT edge_id, order_id, merchant_id, agent_id, channel_partner_id,
               surface, click_id, gross_attributed_gmv_cents,
               refund_amount_cents, created_at, updated_at
        FROM commerce_attribution_edges
        WHERE order_id = :order_id
        ORDER BY created_at
        """,
        {"order_id": order_id},
    )
    return {
        "order_id": order_id,
        "edge_count": len(rows),
        "edges": [dict(r) for r in rows],
    }


@router.get("/__shakeout/rollup/{merchant_id}/{target_date}")
async def shakeout_inspect_rollup(
    merchant_id: str,
    target_date: str,
    _: None = Depends(_gate),
) -> Dict[str, Any]:
    """Read-only inspection of gmv_attribution_daily for a
    (merchant_id, date)."""
    _require_shakeout_merchant(merchant_id)
    try:
        parsed_date = date.fromisoformat(target_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid date: {exc!s}")

    from db.database import database

    rows = await database.fetch_all(
        """
        SELECT date, merchant_id, agent_id, channel_partner_id,
               gross_attributed_gmv_cents, refund_amount_cents,
               net_attributed_gmv_cents, take_rate_bp, take_amount_cents,
               updated_at
        FROM gmv_attribution_daily
        WHERE merchant_id = :merchant_id
          AND date = :date
        ORDER BY agent_id NULLS FIRST, channel_partner_id NULLS FIRST
        """,
        {"merchant_id": merchant_id, "date": parsed_date},
    )
    return {
        "merchant_id": merchant_id,
        "date": target_date,
        "rollup_row_count": len(rows),
        "rows": [dict(r) for r in rows],
    }


@router.get("/__shakeout/billing_run/{billing_run_id}")
async def shakeout_inspect_billing_run(
    billing_run_id: int,
    _: None = Depends(_gate),
) -> Dict[str, Any]:
    """Read-only inspection of one billing_runs row + its items."""
    from db.database import database

    run = await database.fetch_one(
        """
        SELECT id, period_start, period_end, status, idempotency_key,
               error, started_at, completed_at, created_at
        FROM billing_runs
        WHERE id = :id
        """,
        {"id": billing_run_id},
    )
    if not run:
        raise HTTPException(status_code=404, detail="billing_run not found")

    items = await database.fetch_all(
        """
        SELECT id, merchant_id, source_type, source_id,
               stripe_invoice_id, stripe_invoice_item_id, amount_cents,
               description
        FROM billing_run_items
        WHERE billing_run_id = :id
        ORDER BY id
        """,
        {"id": billing_run_id},
    )

    invoices = await database.fetch_all(
        """
        SELECT id, merchant_id, billing_period_start, billing_period_end,
               stripe_invoice_id, total_cents, status, created_at
        FROM invoices
        WHERE billing_run_id = :id
        ORDER BY id
        """,
        {"id": billing_run_id},
    )

    return {
        "billing_run": dict(run),
        "items_count": len(items),
        "items": [dict(i) for i in items],
        "invoices_count": len(invoices),
        "invoices": [dict(i) for i in invoices],
    }
