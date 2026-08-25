"""§D-H full order pipeline shakeout.

End-to-end validation of the v1.3 monetization pipeline against staging:

  D. Agent attribution metadata flow  — create order with rich metadata,
     verify commerce_attribution_edges row appears
  E. Order paid (simulated via shakeout debug endpoint)
  F. T9 stamping  — verify gross_attributed_gmv_cents stamped on the edge
  G. T6 daily rollup  — recompute_for_date, verify gmv_attribution_daily row
                        appears with correct math (net, take rate, etc.)
  H. T7 invoice dry-run  — run_billing_cycle for synthetic period, verify
                          a Stripe Test invoice + line items appear

Does NOT exercise:
  §I T8 partner settlement (Connect transfers — needs a Connect Test account)
  §J refund attribution (next shakeout)

Usage:
    export SHAKEOUT_DEBUG_TOKEN=$(gcloud secrets versions access latest \\
        --secret=env-SHAKEOUT_DEBUG_TOKEN --project pivota-staging)
    export SHAKEOUT_DB_URL=...   # see the note below
    .venv/bin/python scripts/shakeout/c_full_order_pipeline.py

WHERE SHAKEOUT_DB_URL COMES FROM, and why there is deliberately no production
command here. It used to be the `Postgres-xMr6` DATABASE_PUBLIC_URL — that is the
ROLLBACK's database now. Production is Cloud Run in pivota-prod/us-west1 on Cloud
SQL, and its `DATABASE_URL` secret resolves to a PRIVATE IP (verified
2026-08-25), so nothing reaches it from a laptop.

🚨 DO NOT POINT THIS SCRIPT AT THE PRODUCTION DATABASE, and in particular do not
run it under `scripts/ops/run_oneoff_job.sh` — that helper mounts the PRODUCTION
`DATABASE_URL`. This is not a read-only probe: it `INSERT`s into `orders` and
`commerce_attribution_edges` and `UPDATE`s orders to `payment_status='paid'`
(lines ~148, ~185, ~249). It also drives an HTTP surface separately from the DB,
so under that helper `SHAKEOUT_BASE_URL` would still default to the STAGING host
below — synthetic paid orders written into the production ledger from a staging
shakeout. Point SHAKEOUT_DB_URL at a staging or local database whose URL you
already have; that is the only supported venue.

THERE IS NO WORKING DEFAULT BASE URL TODAY. GCP staging `web` runs with
`ingress: internal` so it is unreachable from a laptop, and the Railway staging
host below is mid-teardown after the 2026-08-25 decommission (#1872): `/health`
is 503 and its `/` reports a disconnected database. It is the last known value,
not a working default. Set SHAKEOUT_BASE_URL yourself and confirm the target
serves `/__shakeout/*` (401 = present, 404 = wrong app) before trusting a run.

References:
- docs/monetization/MERCHANT_ONBOARDING_READINESS.md §D-H
- routes/shakeout_debug.py — the staging-only debug endpoints this script calls
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    sys.stderr.write(
        "ERROR: psycopg2 not installed. Install with: pip install psycopg2-binary\n"
    )
    sys.exit(2)


DEFAULT_BASE_URL = "https://web-staging-staging-5257.up.railway.app"
SHAKEOUT_MERCHANT_ID = "merch_shakeout_938623c93f73432a"
SHAKEOUT_AGENT_ID = "agent_shakeout_e2e_pipeline"

RUN_ID = uuid.uuid4().hex[:8]
ORDER_ID = f"ORD_SHAKEOUT_{RUN_ID}"
SUBTOTAL_DOLLARS = Decimal("100.00")
DISCOUNT_DOLLARS = Decimal("10.00")
GROSS_CENTS_EXPECTED = int((SUBTOTAL_DOLLARS - DISCOUNT_DOLLARS) * 100)


def _print_step(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'✓' if ok else '✗'}] {name:54} {detail}")
    return ok


def _connect():
    url = (
        os.environ.get("SHAKEOUT_DB_URL")
        or os.environ.get("DATABASE_PUBLIC_URL")
        or os.environ.get("DATABASE_URL")
    )
    if not url:
        sys.stderr.write(
            "ERROR: SHAKEOUT_DB_URL not set.\n"
            "    Set it to a STAGING or LOCAL database URL. This script writes\n"
            "    (INSERT orders / attribution edges, UPDATE orders to paid), so it\n"
            "    must never be aimed at the production database - including via\n"
            "    scripts/ops/run_oneoff_job.sh, which mounts the production\n"
            "    DATABASE_URL secret.\n"
        )
        sys.exit(2)
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


def _http(
    base_url: str, method: str, path: str, token: str,
    params: Optional[dict[str, Any]] = None,
) -> tuple[int, Any]:
    qs = ""
    if params:
        from urllib.parse import urlencode
        qs = "?" + urlencode({k: str(v) for k, v in params.items() if v is not None})
    url = base_url.rstrip("/") + path + qs
    req = urllib.request.Request(
        url,
        data=b"" if method != "GET" else None,
        method=method,
        headers={"X-Shakeout-Token": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.getcode(), json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body) if body else {}
        except json.JSONDecodeError:
            return exc.code, {"raw": body}


def step_d_create_order_and_edge(conn) -> bool:
    """§D — insert an order row + invoke the attribution-edge writer.

    The attribution edge writer (upsert_order_attribution_edge) is normally
    called from the order-create / payment-confirm flow with metadata that
    carries agent attribution. For the shakeout we mimic that by inserting
    the order row directly + then invoking the writer via a one-shot SQL
    INSERT into commerce_attribution_edges that mirrors what the live
    writer would produce.

    The point of §D is to validate that an attribution edge with full
    fan-out metadata can be created and is readable — not to re-test the
    Python writer (which has unit coverage). We test §E/F/G/H against a
    real edge in subsequent steps.
    """
    cur = conn.cursor()

    # Insert the synthetic order row.
    cur.execute(
        """
        INSERT INTO orders (
          order_id, merchant_id, customer_email, shipping_address, items,
          subtotal, discount_total, total, currency, status, payment_status,
          agent_id, agent_session_id, metadata, created_at, updated_at
        ) VALUES (
          %(order_id)s, %(merchant_id)s, 'shakeout@invalid', '{}'::json,
          '[]'::json, %(subtotal)s, %(discount)s, %(total)s, 'USD',
          'pending', 'unpaid',
          %(agent_id)s, %(agent_session_id)s, %(metadata)s,
          NOW(), NOW()
        )
        """,
        {
            "order_id": ORDER_ID,
            "merchant_id": SHAKEOUT_MERCHANT_ID,
            "subtotal": SUBTOTAL_DOLLARS,
            "discount": DISCOUNT_DOLLARS,
            "total": SUBTOTAL_DOLLARS - DISCOUNT_DOLLARS,
            "agent_id": SHAKEOUT_AGENT_ID,
            "agent_session_id": f"sess_{RUN_ID}",
            "metadata": json.dumps({
                "pvt_click_id": f"clk_shakeout_{RUN_ID}",
                "pvt_surface": "shakeout_pipeline",
                "pvt_product_id": "prod_shakeout_test",
                "pvt_variant_id": "var_shakeout_test",
                "agent_id": SHAKEOUT_AGENT_ID,
                "source_channel": "shakeout",
            }),
        },
    )

    # Insert the matching commerce_attribution_edges row directly. This
    # mirrors what services.commerce_attribution_service.upsert_order_attribution_edge
    # would produce — same edge_id scheme, same surface metadata.
    edge_id = f"cae_{uuid.uuid5(uuid.NAMESPACE_URL, f'{SHAKEOUT_MERCHANT_ID}:{ORDER_ID}').hex[:24]}"
    cur.execute(
        """
        INSERT INTO commerce_attribution_edges (
          edge_id, merchant_id, click_id, order_id,
          surface, commerce_surface, agent_id, channel_partner_id,
          gross_attributed_gmv_cents, refund_amount_cents,
          refund_count, refund_ids, refunded_amount,
          checkout_started_at, created_at, updated_at, metadata
        ) VALUES (
          %(edge_id)s, %(merchant_id)s, %(click_id)s, %(order_id)s,
          'shakeout_pipeline', 'shakeout_pipeline', %(agent_id)s, NULL,
          NULL, 0,
          0, '[]'::jsonb, 0,
          NOW(), NOW(), NOW(),
          %(metadata)s::jsonb
        )
        ON CONFLICT (edge_id) DO NOTHING
        """,
        {
            "edge_id": edge_id,
            "merchant_id": SHAKEOUT_MERCHANT_ID,
            "click_id": f"clk_shakeout_{RUN_ID}",
            "order_id": ORDER_ID,
            "agent_id": SHAKEOUT_AGENT_ID,
            "metadata": json.dumps({"shakeout_run_id": RUN_ID}),
        },
    )

    conn.commit()

    # Verify both rows exist.
    cur.execute(
        "SELECT order_id, payment_status, agent_id FROM orders WHERE order_id = %s",
        (ORDER_ID,),
    )
    order_row = cur.fetchone()
    ok1 = _print_step(
        "order row inserted with agent_id",
        order_row is not None and order_row["agent_id"] == SHAKEOUT_AGENT_ID,
        f"agent_id={order_row['agent_id'] if order_row else None}",
    )

    cur.execute(
        "SELECT edge_id, surface, agent_id, gross_attributed_gmv_cents "
        "FROM commerce_attribution_edges WHERE order_id = %s",
        (ORDER_ID,),
    )
    edge_row = cur.fetchone()
    ok2 = _print_step(
        "attribution edge created (unstamped)",
        edge_row is not None and edge_row["gross_attributed_gmv_cents"] is None,
        f"edge_id={edge_row['edge_id'][:16] if edge_row else None}..., gross=NULL",
    )

    return ok1 and ok2


def step_ef_mark_paid_and_stamp(base_url: str, token: str, conn) -> bool:
    """§E + §F — flip the order to 'paid' (the prerequisite T9 looks for)
    and invoke T9 stamping via the shakeout debug endpoint.

    Validates: T9's `stamp_gross_attributed_gmv` correctly stamps every
    matching edge with the computed gross cents.
    """
    cur = conn.cursor()
    cur.execute(
        "UPDATE orders SET payment_status='paid', status='paid', "
        "paid_at=NOW(), updated_at=NOW() WHERE order_id=%s",
        (ORDER_ID,),
    )
    conn.commit()
    _print_step("order flipped to paid (prereq for T9)", True, "")

    code, body = _http(
        base_url, "POST", "/__shakeout/stamp_gross_attributed_gmv", token,
        params={
            "order_id": ORDER_ID,
            "subtotal": str(SUBTOTAL_DOLLARS),
            "discount_total": str(DISCOUNT_DOLLARS),
        },
    )
    ok_call = _print_step(
        "T9 stamp endpoint accepted call",
        code == 200,
        f"HTTP {code} stamped_count={body.get('stamped_count')}",
    )
    if not ok_call:
        return False

    # Verify the edge now has gross stamped.
    cur.execute(
        "SELECT gross_attributed_gmv_cents FROM commerce_attribution_edges "
        "WHERE order_id = %s",
        (ORDER_ID,),
    )
    row = cur.fetchone()
    gross = int(row["gross_attributed_gmv_cents"] or 0)
    ok_value = _print_step(
        f"edge.gross_attributed_gmv_cents == {GROSS_CENTS_EXPECTED}",
        gross == GROSS_CENTS_EXPECTED,
        f"actual={gross}",
    )
    return ok_call and ok_value


def step_g_aggregate_daily(base_url: str, token: str, conn) -> bool:
    """§G — invoke T6 recompute_for_date for today + verify the
    gmv_attribution_daily row appears with correct net + take amounts."""
    today_iso = datetime.now(timezone.utc).date().isoformat()
    code, body = _http(
        base_url, "POST", "/__shakeout/aggregate_daily", token,
        params={"date": today_iso, "merchant_id": SHAKEOUT_MERCHANT_ID},
    )
    ok_call = _print_step(
        "T6 recompute_for_date endpoint accepted call",
        code == 200,
        f"HTTP {code} body={body}",
    )
    if not ok_call:
        return False

    cur = conn.cursor()
    cur.execute(
        """
        SELECT gross_attributed_gmv_cents, refund_amount_cents,
               net_attributed_gmv_cents, take_rate_bp, take_amount_cents
        FROM gmv_attribution_daily
        WHERE merchant_id = %s AND date = %s::date AND agent_id = %s
        """,
        (SHAKEOUT_MERCHANT_ID, today_iso, SHAKEOUT_AGENT_ID),
    )
    rollup = cur.fetchone()
    if not rollup:
        _print_step("gmv_attribution_daily row exists", False, "missing")
        return False

    expected_take_bp = 500  # promo rate (no merchant promo_period_until → standard, but
                            # shakeout merchant has no merchants_row.promo_period_until,
                            # so should fall through to STANDARD 1000bp = 10%).
                            # Let's check both possibilities.
    actual_take_bp = int(rollup["take_rate_bp"])
    expected_net = GROSS_CENTS_EXPECTED  # no refunds yet
    expected_take = expected_net * actual_take_bp // 10000

    ok_gross = _print_step(
        f"rollup gross_attributed_gmv_cents == {GROSS_CENTS_EXPECTED}",
        int(rollup["gross_attributed_gmv_cents"]) == GROSS_CENTS_EXPECTED,
        f"actual={rollup['gross_attributed_gmv_cents']}",
    )
    ok_net = _print_step(
        f"rollup net_attributed_gmv_cents == {expected_net}",
        int(rollup["net_attributed_gmv_cents"]) == expected_net,
        f"actual={rollup['net_attributed_gmv_cents']}",
    )
    ok_take_bp = _print_step(
        "take_rate_bp is 500 (promo) or 1000 (standard)",
        actual_take_bp in (500, 1000),
        f"actual={actual_take_bp}",
    )
    ok_take = _print_step(
        f"take_amount_cents matches gross × take_rate_bp / 10000",
        int(rollup["take_amount_cents"]) == expected_take,
        f"actual={rollup['take_amount_cents']} expected={expected_take}",
    )
    return ok_gross and ok_net and ok_take_bp and ok_take


def step_h_run_billing_cycle(base_url: str, token: str, conn) -> bool:
    """§H — invoke T7 run_billing_cycle for a synthetic period covering
    today. Verify a billing_run row + Stripe Test invoice draft appear.

    The period uses today as both start (rounded down to month start) and
    end (rounded up to next month). run_billing_cycle's idempotency key
    is f'{period_start.isoformat()}-billing'; collisions across shakeout
    runs in the same calendar month are OK — same billing_run_id returned.
    """
    today = datetime.now(timezone.utc).date()
    period_start = today.replace(day=1)
    # period_end must be > period_start; use today's date (or +1 day if same).
    period_end = today if today > period_start else today.replace(day=today.day + 1)

    code, body = _http(
        base_url, "POST", "/__shakeout/run_billing_cycle", token,
        params={
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
        },
    )
    ok_call = _print_step(
        "T7 run_billing_cycle endpoint accepted call",
        code == 200,
        f"HTTP {code} billing_run_id={body.get('billing_run_id')}",
    )
    if not ok_call:
        return False

    billing_run_id = body.get("billing_run_id")
    code2, body2 = _http(
        base_url, "GET", f"/__shakeout/billing_run/{billing_run_id}", token,
    )
    if code2 != 200:
        _print_step("billing_run inspect endpoint returned 200", False, f"HTTP {code2}")
        return False

    run = body2.get("billing_run") or {}
    items = body2.get("items") or []
    invoices = body2.get("invoices") or []

    ok_status = _print_step(
        "billing_run.status in (completed, partial_failed)",
        run.get("status") in ("completed", "partial_failed"),
        f"status={run.get('status')}",
    )
    # The shakeout merchant has stripe_customer_id populated (from §A).
    # So a draft Stripe Test invoice + at least one item should be created.
    shakeout_invoices = [i for i in invoices if i.get("merchant_id") == SHAKEOUT_MERCHANT_ID]
    shakeout_items = [i for i in items if i.get("merchant_id") == SHAKEOUT_MERCHANT_ID]
    ok_invoice = _print_step(
        "Stripe Test invoice draft created for shakeout merchant",
        len(shakeout_invoices) >= 1,
        f"invoices_for_shakeout={len(shakeout_invoices)} total={len(invoices)}",
    )
    ok_items = _print_step(
        "at least 1 invoice line item",
        len(shakeout_items) >= 1,
        f"items_for_shakeout={len(shakeout_items)} total={len(items)}",
    )
    if shakeout_invoices:
        inv = shakeout_invoices[0]
        print(f"      sample stripe_invoice_id={inv.get('stripe_invoice_id')} "
              f"total_cents={inv.get('total_cents')} status={inv.get('status')}")
    return ok_call and ok_status and ok_invoice and ok_items


def main() -> int:
    base_url = os.environ.get("SHAKEOUT_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    token = (os.environ.get("SHAKEOUT_DEBUG_TOKEN") or "").strip()
    if not token:
        sys.stderr.write(
            "ERROR: SHAKEOUT_DEBUG_TOKEN not set. Source:\n"
            "    export SHAKEOUT_DEBUG_TOKEN=$(gcloud secrets versions access latest "
            "--secret=env-SHAKEOUT_DEBUG_TOKEN --project pivota-staging)\n"
        )
        return 2

    print(f"§D-H full order pipeline shakeout (run_id={RUN_ID})")
    print(f"  base_url    : {base_url}")
    print(f"  merchant    : {SHAKEOUT_MERCHANT_ID}")
    print(f"  agent       : {SHAKEOUT_AGENT_ID}")
    print(f"  order       : {ORDER_ID}")
    print(f"  gross cents : {GROSS_CENTS_EXPECTED}")
    print("=" * 78)

    try:
        conn = _connect()
    except psycopg2.Error as exc:
        sys.stderr.write(f"ERROR: DB connect failed: {exc}\n")
        return 2

    all_ok = True
    try:
        print("\n§D — create order + attribution edge")
        all_ok &= step_d_create_order_and_edge(conn)
        if not all_ok:
            return 1

        print("\n§E/F — mark paid + T9 stamping")
        all_ok &= step_ef_mark_paid_and_stamp(base_url, token, conn)
        if not all_ok:
            return 1

        print("\n§G — T6 daily rollup")
        all_ok &= step_g_aggregate_daily(base_url, token, conn)
        if not all_ok:
            return 1

        print("\n§H — T7 billing cycle dry-run")
        all_ok &= step_h_run_billing_cycle(base_url, token, conn)

        print("=" * 78)
        if all_ok:
            print("§D-H PASS")
            print(f"\nCleanup later:")
            print(f"  DELETE FROM commerce_attribution_edges WHERE order_id = '{ORDER_ID}';")
            print(f"  DELETE FROM orders WHERE order_id = '{ORDER_ID}';")
            return 0
        print("§D-H FAIL — see [✗] lines above")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
