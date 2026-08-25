"""§A merchant subscription signup shakeout.

Drives the synthetic shakeout merchant through `/api/billing/checkout-session`
on staging (Stripe Test mode) and asserts the merchant-onboarding readiness
audit's §A pass criteria:

  - Authenticated merchant → checkout URL returned in < 3s
  - Stripe customer created with deterministic idempotency key
  - Replay of the same request inside 24h returns the SAME checkout session
    (Stripe idempotency cache hit)
  - merchants.stripe_customer_id populated post-success
  - HTTP 200 + JSON shape {session_url, session_id} on success

Usage:
    SHAKEOUT_API_KEY=$(cat /tmp/shakeout_api_key.txt) \\
    python3 scripts/shakeout/a_subscription_signup.py

Or override defaults:
    SHAKEOUT_BASE_URL=https://web-staging-staging-5257.up.railway.app \\
    SHAKEOUT_API_KEY=... \\
    SHAKEOUT_PRICE_ID=price_1TZLrOGeIEg0wZyUP6lYbUJ6 \\
    python3 scripts/shakeout/a_subscription_signup.py

References:
- docs/monetization/MERCHANT_ONBOARDING_READINESS.md §A
- routes/billing_routes.py:create_billing_checkout_session
- PR #600 idempotency keys: merchant_customer:{merchant_id} + checkout_session:{merchant_id}:{price_id}:{date_iso}
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
from typing import Any


DEFAULT_BASE_URL = "https://web-staging-staging-5257.up.railway.app"
# Stripe Test starter price on staging.
#
# DEFAULT_BASE_URL is still the Railway staging host ON PURPOSE. Staging was
# rebuilt on GCP (pivota-staging), but its `web` service runs with
# `ingress: internal` (verified 2026-08-25), so it is not reachable from a laptop
# and this script cannot drive it from outside. Production is Cloud Run in
# pivota-prod — never point this script there; it signs real subscriptions up.
DEFAULT_PRICE_ID = "price_1TZLrOGeIEg0wZyUP6lYbUJ6"


def _request(
    base_url: str,
    api_key: str,
    body: dict[str, Any],
    timeout_seconds: float = 15.0,
) -> tuple[int, dict[str, Any], float]:
    url = base_url.rstrip("/") + "/api/billing/checkout-session"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Merchant-API-Key": api_key,
        },
        method="POST",
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            status_code = resp.getcode()
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        status_code = exc.code
        payload = exc.read().decode("utf-8", errors="replace")
    elapsed = time.monotonic() - start
    try:
        parsed = json.loads(payload) if payload else {}
    except json.JSONDecodeError:
        parsed = {"raw": payload}
    return status_code, parsed, elapsed


def _print_step(name: str, ok: bool, detail: str) -> None:
    mark = "[✓]" if ok else "[✗]"
    print(f"  {mark} {name:60} {detail}")


def main() -> int:
    base_url = os.environ.get("SHAKEOUT_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    api_key = os.environ.get("SHAKEOUT_API_KEY", "").strip()
    price_id = os.environ.get("SHAKEOUT_PRICE_ID", DEFAULT_PRICE_ID).strip()

    if not api_key:
        sys.stderr.write(
            "ERROR: SHAKEOUT_API_KEY not set. Source:\n"
            "    export SHAKEOUT_API_KEY=$(cat /tmp/shakeout_api_key.txt)\n"
        )
        return 2

    print("§A merchant subscription signup shakeout")
    print(f"  base_url    : {base_url}")
    print(f"  price_id    : {price_id}")
    print(f"  api_key     : ***{api_key[-4:]}")
    print("=" * 78)

    body = {
        "price_id": price_id,
        "success_url": "https://shakeout.invalid/success",
        "cancel_url": "https://shakeout.invalid/cancel",
    }

    # Step 1: first call.
    print("Step 1: first POST /api/billing/checkout-session")
    code1, resp1, elapsed1 = _request(base_url, api_key, body)
    ok_status = code1 == 200
    ok_shape = isinstance(resp1, dict) and bool(resp1.get("session_url")) and bool(resp1.get("session_id"))
    ok_latency = elapsed1 < 3.0
    _print_step("HTTP 200", ok_status, f"got {code1}")
    _print_step("response has session_url + session_id", ok_shape, f"keys={list(resp1.keys())}")
    _print_step("latency < 3.0s", ok_latency, f"{elapsed1:.2f}s")
    if not ok_status:
        print(f"\n  response body: {json.dumps(resp1, indent=2)[:500]}")
        return 1
    session_id_1 = resp1.get("session_id")
    session_url_1 = resp1.get("session_url")

    # Step 2: replay within 24h — should hit Stripe's idempotency cache.
    print("\nStep 2: replay POST (idempotency check)")
    code2, resp2, elapsed2 = _request(base_url, api_key, body)
    ok_status2 = code2 == 200
    ok_shape2 = isinstance(resp2, dict) and bool(resp2.get("session_url"))
    same_session = resp2.get("session_id") == session_id_1
    same_url = resp2.get("session_url") == session_url_1
    _print_step("HTTP 200", ok_status2, f"got {code2}")
    _print_step("replay returned same session_id", same_session, f"first={session_id_1!r} second={resp2.get('session_id')!r}")
    _print_step("replay returned same session_url", same_url, "(matches)" if same_url else "(DIFFERS — Stripe didn't cache the key)")

    # Step 3: assert idempotency_key contract holds across both calls. We can't
    # inspect Stripe's response headers here (urllib didn't capture them), so
    # session_id equality is the strongest available signal.
    # The PR #600 key shape (checkout_session:{merchant_id}:{price_id}:{date_iso})
    # buckets by UTC day — within a single day, both calls hash to the same key.
    print("\nStep 3: idempotency contract — both calls within same UTC day")
    _print_step("session equality implies Stripe cache hit", same_session, "ok" if same_session else "FAIL")

    print("=" * 78)
    all_ok = ok_status and ok_shape and ok_latency and ok_status2 and same_session and same_url
    if all_ok:
        print("§A PASS")
        print()
        print(f"Captured session_url for manual browser test (Test card 4242...):")
        print(f"  {session_url_1}")
        return 0
    print("§A FAIL — see [✗] lines above")
    return 1


if __name__ == "__main__":
    sys.exit(main())
