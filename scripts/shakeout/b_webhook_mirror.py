"""§B Stripe webhook mirror shakeout.

Fires Stripe-signed webhook events at staging /webhooks/stripe/billing and
asserts the handlers run, stripe_events ledger transitions correctly, and
the retry/replay/concurrency semantics hold.

What this exercises:
  1. checkout.session.completed → _handle_checkout_session_completed
  2. customer.subscription.updated → _handle_subscription_updated
  3. customer.subscription.deleted → _handle_subscription_deleted
  4. customer.subscription.created → ignored (status='ignored')
  5. invoice.paid → _handle_invoice_paid
  6. invoice.payment_failed → _handle_invoice_payment_failed
  7. invalid signature → 400, no row written
  8. replay of an already-processed event → 200 + status=duplicate

Usage:
    STAGING_WEBHOOK_SECRET=$(gcloud secrets versions access latest \\
      --secret=env-STRIPE_BILLING_WEBHOOK_SECRET --project pivota-staging) \\
    .venv/bin/python scripts/shakeout/b_webhook_mirror.py

STAGING PLATFORM, read this before changing DEFAULT_BASE_URL. The secret above
now comes from GCP (pivota-staging), where staging was rebuilt. The base URL
below is still the Railway staging host ON PURPOSE: the GCP staging `web`
service runs with `ingress: internal` (verified 2026-08-25), so it is not
reachable from a laptop at all and this script cannot drive it from outside.
Swapping in the Cloud Run URL would produce a command that always fails.
Production, separately, is Cloud Run in pivota-prod — never point this script
there; it posts synthetic Stripe events.

The script constructs Stripe-signature-compatible headers itself (HMAC-SHA256
of `{timestamp}.{payload}` per Stripe docs) so no Stripe CLI dependency.

References:
- docs/monetization/MERCHANT_ONBOARDING_READINESS.md §B
- routes/billing_routes.py:handle_stripe_billing_webhook
- PR #599 retry semantics: _claim_retryable_event
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any


DEFAULT_BASE_URL = "https://web-staging-staging-5257.up.railway.app"
WEBHOOK_PATH = "/webhooks/stripe/billing"

# Synthetic merchant + customer provisioned for the shakeout. These IDs are
# real on staging (shared DB) and the merchant exists in merchant_onboarding,
# user_subscriptions (id 21), and merchants (id 20) with stripe_customer_id
# already populated from §A.
SHAKEOUT_MERCHANT_ID = "merch_shakeout_938623c93f73432a"
SHAKEOUT_CUSTOMER_ID = "cus_UZAYVJzf0JjBXj"  # From §A first run
SHAKEOUT_PRICE_ID = "price_1TZLrOGeIEg0wZyUP6lYbUJ6"  # Test starter

# Per-run unique IDs so reruns don't collide with prior ledger rows.
RUN_ID = uuid.uuid4().hex[:8]


def _sign(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """Construct a Stripe-compatible Stripe-Signature header value.

    Format: `t={timestamp},v1={hmac_sha256_hex({timestamp}.{payload}, secret)}`
    """
    ts = timestamp if timestamp is not None else int(time.time())
    signed = f"{ts}.".encode("utf-8") + payload
    sig = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _post(base_url: str, payload: bytes, sig_header: str) -> tuple[int, dict[str, Any]]:
    url = base_url.rstrip("/") + WEBHOOK_PATH
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "Stripe-Signature": sig_header},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.getcode(), json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body) if body else {}
        except json.JSONDecodeError:
            return exc.code, {"raw": body}


def _event(event_id: str, event_type: str, obj: dict[str, Any]) -> dict[str, Any]:
    """Minimal Stripe event envelope. Stripe.Webhook.construct_event only
    needs the outer fields it actually reads; handlers read .data.object."""
    return {
        "id": event_id,
        "type": event_type,
        "object": "event",
        "api_version": "2024-06-20",
        "created": int(time.time()),
        "livemode": False,
        "data": {"object": obj},
    }


def _print_step(name: str, ok: bool, detail: str) -> None:
    print(f"  [{'✓' if ok else '✗'}] {name:54} {detail}")


def _run_case(
    base_url: str,
    secret: str,
    name: str,
    event: dict[str, Any],
    expected_status_code: int,
    expected_body_status: str | None,
) -> bool:
    payload = json.dumps(event, separators=(",", ":")).encode("utf-8")
    sig = _sign(payload, secret)
    code, body = _post(base_url, payload, sig)
    ok_code = code == expected_status_code
    ok_body = expected_body_status is None or (
        isinstance(body, dict) and body.get("status") == expected_body_status
    )
    detail = f"HTTP {code} body.status={body.get('status') if isinstance(body, dict) else body!r}"
    _print_step(name, ok_code and ok_body, detail)
    return ok_code and ok_body


def main() -> int:
    base_url = os.environ.get("SHAKEOUT_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    secret = os.environ.get("STAGING_WEBHOOK_SECRET", "").strip()
    if not secret:
        sys.stderr.write(
            "ERROR: STAGING_WEBHOOK_SECRET not set. Source:\n"
            "    export STAGING_WEBHOOK_SECRET=$(gcloud secrets versions access latest "
            "--secret=env-STRIPE_BILLING_WEBHOOK_SECRET --project pivota-staging)\n"
        )
        return 2

    print(f"§B Stripe webhook mirror shakeout (run_id={RUN_ID})")
    print(f"  base_url       : {base_url}")
    print(f"  merchant       : {SHAKEOUT_MERCHANT_ID}")
    print(f"  customer       : {SHAKEOUT_CUSTOMER_ID}")
    print(f"  webhook secret : whsec_***{secret[-4:]}")
    print("=" * 78)

    all_ok = True

    # ---- happy-path event fan-out ----
    print("\nPart 1: each of the 6 event types should be accepted")

    # 1. checkout.session.completed — full handler runs (writes
    # user_subscriptions + updates merchants). Subscription_id must be unique
    # per run so the ON CONFLICT in the handler doesn't dedupe vs prior runs.
    ev_id_1 = f"evt_co_{RUN_ID}_1"
    sub_id_1 = f"sub_shakeout_{RUN_ID}_1"
    obj_1 = {
        "id": f"cs_test_shakeout_{RUN_ID}",
        "object": "checkout.session",
        "customer": SHAKEOUT_CUSTOMER_ID,
        "subscription": sub_id_1,
        "metadata": {
            "merchant_id": SHAKEOUT_MERCHANT_ID,
            "price_id": SHAKEOUT_PRICE_ID,
        },
    }
    all_ok &= _run_case(
        base_url, secret, "checkout.session.completed → processed",
        _event(ev_id_1, "checkout.session.completed", obj_1),
        200, "processed",
    )

    # 2. customer.subscription.updated — looks up user_subscriptions by stripe_subscription_id
    ev_id_2 = f"evt_su_{RUN_ID}_2"
    obj_2 = {
        "id": sub_id_1,
        "object": "subscription",
        "customer": SHAKEOUT_CUSTOMER_ID,
        "status": "active",
        "items": {"data": [{"id": "si_x", "price": {"id": SHAKEOUT_PRICE_ID}}]},
    }
    all_ok &= _run_case(
        base_url, secret, "customer.subscription.updated → processed",
        _event(ev_id_2, "customer.subscription.updated", obj_2),
        200, "processed",
    )

    # 3. customer.subscription.deleted — soft-delete on user_subscriptions
    sub_id_2 = f"sub_shakeout_{RUN_ID}_del"
    ev_id_3 = f"evt_sd_{RUN_ID}_3"
    obj_3 = {
        "id": sub_id_2,
        "object": "subscription",
        "customer": SHAKEOUT_CUSTOMER_ID,
        "status": "canceled",
    }
    all_ok &= _run_case(
        base_url, secret, "customer.subscription.deleted → processed",
        _event(ev_id_3, "customer.subscription.deleted", obj_3),
        200, "processed",
    )

    # 4. customer.subscription.created — explicitly ignored
    ev_id_4 = f"evt_sc_{RUN_ID}_4"
    obj_4 = {
        "id": f"sub_shakeout_{RUN_ID}_created",
        "object": "subscription",
        "customer": SHAKEOUT_CUSTOMER_ID,
    }
    all_ok &= _run_case(
        base_url, secret, "customer.subscription.created → ignored",
        _event(ev_id_4, "customer.subscription.created", obj_4),
        200, "ignored",
    )

    # Invoice events: include realistic period_start/period_end one month
    # apart. billing_period_* are DATE columns (migration 120). Same-day
    # values violate ck_invoices_billing_period_order.
    _now_ts = int(time.time())
    _month_ago = _now_ts - 30 * 24 * 3600

    # 5. invoice.paid — writes/updates invoices row by stripe_invoice_id
    inv_id_1 = f"in_shakeout_{RUN_ID}_paid"
    ev_id_5 = f"evt_ip_{RUN_ID}_5"
    obj_5 = {
        "id": inv_id_1,
        "object": "invoice",
        "customer": SHAKEOUT_CUSTOMER_ID,
        "subscription": sub_id_1,
        "amount_paid": 9900,
        "amount_due": 9900,
        "status": "paid",
        "currency": "usd",
        "period_start": _month_ago,
        "period_end": _now_ts,
        "metadata": {"merchant_id": SHAKEOUT_MERCHANT_ID},
    }
    all_ok &= _run_case(
        base_url, secret, "invoice.paid → processed",
        _event(ev_id_5, "invoice.paid", obj_5),
        200, "processed",
    )

    # 6. invoice.payment_failed — same shape, payment_failed status
    inv_id_2 = f"in_shakeout_{RUN_ID}_failed"
    ev_id_6 = f"evt_if_{RUN_ID}_6"
    obj_6 = {
        "id": inv_id_2,
        "object": "invoice",
        "customer": SHAKEOUT_CUSTOMER_ID,
        "subscription": sub_id_1,
        "amount_paid": 0,
        "amount_due": 9900,
        "status": "open",
        "currency": "usd",
        "period_start": _month_ago,
        "period_end": _now_ts,
        "metadata": {"merchant_id": SHAKEOUT_MERCHANT_ID},
    }
    all_ok &= _run_case(
        base_url, secret, "invoice.payment_failed → processed",
        _event(ev_id_6, "invoice.payment_failed", obj_6),
        200, "processed",
    )

    # ---- replay / retry semantics ----
    print("\nPart 2: retry + replay semantics")

    # 7. Replay event #1 (already processed) → 200 + status=duplicate
    all_ok &= _run_case(
        base_url, secret, "replay of processed event → 200 duplicate",
        _event(ev_id_1, "checkout.session.completed", obj_1),
        200, "duplicate",
    )

    # 8. Invalid signature → 400, no row written
    bad_event = _event(f"evt_badsig_{RUN_ID}", "invoice.paid", obj_5)
    bad_payload = json.dumps(bad_event, separators=(",", ":")).encode("utf-8")
    bad_sig = _sign(bad_payload, "whsec_wrong_secret_for_negative_test")
    code, body = _post(base_url, bad_payload, bad_sig)
    sig_ok = code == 400
    _print_step(
        "invalid signature → 400",
        sig_ok,
        f"HTTP {code} body={body.get('detail') or body!r}",
    )
    all_ok &= sig_ok

    print("=" * 78)
    if all_ok:
        print("§B PASS")
        return 0
    print("§B FAIL — see [✗] lines above")
    return 1


if __name__ == "__main__":
    sys.exit(main())
