#!/usr/bin/env python3.11
"""P1 PR-B — Stripe SharedPaymentToken (SPT) sandbox conformance run.

WHAT THIS IS FOR
================
Two things the mocked unit tests cannot do:

1. **Prove the lane works.** Mint a real TEST-MODE SharedPaymentToken and charge
   it through the REAL `services.acp_offsession_capture.capture_offsession`
   (test lane), so the exact code production runs is the code that gets proven —
   parameter shape, preview API version, idempotency key, the lot.

2. **Enumerate Stripe's real failure codes.** Stripe does not document what it
   returns when an SPT violates its `usage_limits`. Until those codes are known
   from evidence, every SPT error stays UNKNOWN and therefore AMBIGUOUS on the
   money path (`_SPT_DEFINITIVE_ERROR_CODES` in
   services/acp_checkout_session_service.py is deliberately EMPTY — an
   undocumented code that can occur *after* a charge landed would, if listed
   there, release the completion claim and let the next attempt double-charge).
   This script attempts four violations and prints Stripe's exact
   `error.code` / `error.type` for each, so that set can be populated from
   evidence rather than from guesswork.

NEVER RUN AUTOMATICALLY. NEVER IN CI. NO KEY IS EMBEDDED HERE.
It refuses outright to run against an `sk_live`/`rk_live` key: this script must
not be able to touch live money, no matter what is in the environment.

USAGE
=====
    export STRIPE_TEST_SECRET_KEY=sk_test_...
    python3.11 scripts/acp_spt_sandbox_conformance.py

    # Options
    --amount-cents 100        the charge amount (also the token cap, unless --cap-cents)
    --currency usd            token + charge currency (US/CA program: usd/cad)
    --expiry-seconds 3600     how long the happy-path token stays valid
    --skip-expired            skip the (slow) expired-token probe
    --expired-wait-seconds 90 how long to wait for a short-lived token to expire

Availability note: Stripe's SPT program is US/CA only and the account must have
accepted the agentic-commerce seller terms. A 4xx complaining about the account
rather than the token means enrollment, not a code defect.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import settings  # noqa: E402
from services import acp_offsession_capture as cap  # noqa: E402

MERCHANT_ID = "spt_conformance_merchant"
TEST_CARD_PM = "pm_card_visa"


# --------------------------------------------------------------------------
# Safety
# --------------------------------------------------------------------------
def _resolve_test_key() -> str:
    """The ONLY key source, and it must be a test key.

    Refuses loudly on a live key. `capture_offsession`'s own test-lane guard
    would refuse it too, but this script mints tokens and enumerates failures
    *outside* that guard as well, so the refusal belongs at the front door."""
    key = (
        os.getenv("STRIPE_TEST_SECRET_KEY")
        or os.getenv("STRIPE_SECRET_KEY")
        or ""
    ).strip()
    if not key:
        raise SystemExit(
            "REFUSING: no key. Set STRIPE_TEST_SECRET_KEY to a Stripe TEST secret key "
            "(sk_test_...). This script never reads a key from anywhere else."
        )
    if key.startswith("sk_live_") or key.startswith("rk_live_"):
        raise SystemExit(
            "REFUSING: that is a LIVE Stripe key. This script mints tokens and "
            "deliberately provokes failures — it must never touch live money. "
            "Use a test key (sk_test_...)."
        )
    if not (key.startswith("sk_test_") or key.startswith("rk_test_")):
        raise SystemExit(
            f"REFUSING: unrecognized key prefix {key[:8]!r}. Expected sk_test_/rk_test_."
        )
    return key


# --------------------------------------------------------------------------
# Stripe plumbing (stripe SDK only — no new HTTP libs)
# --------------------------------------------------------------------------
def _client(api_key: str):
    import stripe

    return stripe.StripeClient(api_key)


def mint_test_spt(
    api_key: str,
    *,
    currency: str,
    max_amount_cents: int,
    expires_at_epoch: int,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Mint a TEST-MODE SharedPaymentToken via Stripe's test helper.

    POST /v1/test_helpers/shared_payment/granted_tokens
        payment_method=pm_card_visa
        usage_limits[currency|max_amount|expires_at]
    under the preview API version. The installed SDK (15.x) has no typed
    resource for this preview endpoint, so it goes through the SDK's own
    `raw_request` — still the stripe SDK, still its auth/retry/telemetry.

    Returns (token_id, error_dict). Exactly one is non-None.
    """
    try:
        resp = _client(api_key).raw_request(
            "post",
            "/v1/test_helpers/shared_payment/granted_tokens",
            stripe_version=cap.STRIPE_SPT_API_VERSION,
            payment_method=TEST_CARD_PM,
            usage_limits={
                "currency": currency,
                "max_amount": int(max_amount_cents),
                "expires_at": int(expires_at_epoch),
            },
        )
    except Exception as exc:  # noqa: BLE001 — the mint error IS evidence
        return None, _describe_error(exc)

    token_id = None
    for getter in (lambda: resp.get("id"), lambda: getattr(resp, "id", None)):
        try:
            token_id = getter()
        except Exception:  # noqa: BLE001
            token_id = None
        if token_id:
            break
    if not token_id:
        return None, {"code": "no_token_in_response", "type": "script_error", "message": str(resp)[:300]}
    return str(token_id), None


def _describe_error(exc: BaseException) -> Dict[str, Any]:
    """Stripe's own error identity, flattened for printing. This is the payload
    `_SPT_DEFINITIVE_ERROR_CODES` gets populated from."""
    err = getattr(exc, "error", None)
    return {
        "exception_class": type(exc).__name__,
        "code": getattr(exc, "code", None) or getattr(err, "code", None),
        "decline_code": getattr(exc, "decline_code", None) or getattr(err, "decline_code", None),
        "type": getattr(err, "type", None) or getattr(exc, "http_status", None),
        "http_status": getattr(exc, "http_status", None),
        "param": getattr(err, "param", None),
        "message": str(exc)[:300],
    }


# --------------------------------------------------------------------------
# The capture path under test
# --------------------------------------------------------------------------
def _install_merchant_key(api_key: str) -> None:
    """Point `capture_offsession`'s merchant-PSP resolution at the founder's test
    key for the duration of this run.

    This is the ONLY substitution the script makes: everything downstream —
    the amount cap, the test/live key lane guard, the adapter, the parameter
    shape, the preview version, the idempotency option — is the real code.
    """

    async def _row(*, merchant_id, provider=None, psp_id=None, database_override=None):
        return {"api_key": api_key, "provider": "stripe", "environment": "test"}

    cap.fetch_active_runtime_merchant_psp = _row  # type: ignore[assignment]


async def charge_spt(token: str, *, amount_cents: int, currency: str, idem: str):
    """Charge through the REAL capture lane, test lane (allow_live=False)."""
    return await cap.capture_offsession(
        merchant_id=MERCHANT_ID,
        amount_cents=amount_cents,
        currency=currency,
        idempotency_key=idem,
        payment_method=token,
        metadata={"pivota_spt_conformance": "true"},
        max_cents=max(amount_cents, settings.agent_acp_test_max_cents),
        allow_live=False,
    )


def _row(label: str, ok: bool, detail: str) -> Dict[str, str]:
    return {"label": label, "verdict": "PASS" if ok else "SEE-OUTPUT", "detail": detail}


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------
async def run(args: argparse.Namespace) -> int:
    api_key = _resolve_test_key()
    _install_merchant_key(api_key)

    # The lane is flag-gated; this run is exactly what the flag gates, so arm it
    # in-process (loudly) instead of demanding the operator also set the env.
    settings.acp_spt_capture_enabled = True  # type: ignore[misc]
    print("ACP_SPT_CAPTURE_ENABLED forced ON for this process only.")
    print(f"Stripe preview version on SPT calls: {cap.STRIPE_SPT_API_VERSION}")
    print(f"amount={args.amount_cents} {args.currency}  cap={args.cap_cents or args.amount_cents}\n")

    currency = args.currency.lower()
    cap_cents = int(args.cap_cents or args.amount_cents)
    now = int(time.time())
    summary: List[Dict[str, str]] = []
    findings: List[Tuple[str, Dict[str, Any]]] = []

    # ---- 0. happy path -----------------------------------------------------
    print("=" * 72)
    print("[0] HAPPY PATH — mint a capped test SPT and charge it")
    print("=" * 72)
    token, mint_err = mint_test_spt(
        api_key,
        currency=currency,
        max_amount_cents=cap_cents,
        expires_at_epoch=now + int(args.expiry_seconds),
    )
    if not token:
        print(f"  MINT FAILED: {mint_err}")
        print(
            "\n  If this complains about the account rather than the parameters, the "
            "Stripe account is not enrolled in the agentic-commerce/SPT program "
            "(US/CA only, seller terms required). That is an ops prerequisite, not a bug."
        )
        return 2
    print(f"  minted token ...{token[-6:]}  (cap={cap_cents} {currency}, expires in {args.expiry_seconds}s)")

    result = await charge_spt(
        token, amount_cents=args.amount_cents, currency=currency, idem=f"spt_conf_ok_{now}"
    )
    print(f"  capture -> success={result.success} status={result.status} "
          f"intent={result.payment_intent_id} code={result.error_code}")
    if result.error:
        print(f"  error: {result.error}")
    summary.append(_row("happy path charge", bool(result.success), f"status={result.status}"))
    if not result.success:
        print("\n  The happy path did not succeed — fix that before trusting the failure "
              "enumeration below (a mis-shaped request fails for the wrong reason).")

    # ---- 1. re-use of a consumed token ------------------------------------
    # Runs FIRST among the failure probes, and only if the happy path charged:
    # this is the one probe whose precondition is a genuinely consumed token.
    print("\n" + "=" * 72)
    print("[1] REUSE — charge the token that was just consumed")
    print("=" * 72)
    if result.success:
        reuse = await charge_spt(
            token, amount_cents=args.amount_cents, currency=currency, idem=f"spt_conf_reuse_{now}"
        )
        print(f"  capture -> success={reuse.success} status={reuse.status} code={reuse.error_code}")
        print(f"  error: {reuse.error}")
        findings.append(("reuse_of_consumed_token", {"code": reuse.error_code, "error": reuse.error}))
        summary.append(_row("reuse refused", not reuse.success, f"code={reuse.error_code}"))
    else:
        print("  SKIPPED (nothing was consumed — the happy path did not charge).")
        summary.append(_row("reuse refused", False, "skipped"))

    # ---- 2. over-cap charge ------------------------------------------------
    print("\n" + "=" * 72)
    print("[2] OVER-CAP — charge more than the token's max_amount")
    print("=" * 72)
    over_token, err = mint_test_spt(
        api_key, currency=currency, max_amount_cents=cap_cents, expires_at_epoch=now + int(args.expiry_seconds)
    )
    if over_token:
        over_amount = cap_cents + 100
        over = await charge_spt(
            over_token,
            amount_cents=over_amount,
            currency=currency,
            idem=f"spt_conf_over_{now}",
        )
        print(f"  cap={cap_cents}, charged={over_amount}")
        print(f"  capture -> success={over.success} status={over.status} code={over.error_code}")
        print(f"  error: {over.error}")
        findings.append(("over_cap", {"code": over.error_code, "error": over.error}))
        summary.append(_row("over-cap refused", not over.success, f"code={over.error_code}"))
        if over.success:
            print("  !! Stripe ACCEPTED an over-cap charge. That contradicts the documented "
                  "usage_limits enforcement — stop and re-read before enabling anything.")
    else:
        print(f"  MINT FAILED: {err}")
        summary.append(_row("over-cap refused", False, "mint failed"))

    # ---- 3. currency mismatch ---------------------------------------------
    print("\n" + "=" * 72)
    print("[3] CURRENCY MISMATCH — token scoped to one currency, charged in another")
    print("=" * 72)
    other_currency = "cad" if currency == "usd" else "usd"
    cur_token, err = mint_test_spt(
        api_key, currency=currency, max_amount_cents=cap_cents, expires_at_epoch=now + int(args.expiry_seconds)
    )
    if cur_token:
        mism = await charge_spt(
            cur_token, amount_cents=args.amount_cents, currency=other_currency,
            idem=f"spt_conf_cur_{now}",
        )
        print(f"  token currency={currency}, charged in {other_currency}")
        print(f"  capture -> success={mism.success} status={mism.status} code={mism.error_code}")
        print(f"  error: {mism.error}")
        findings.append(("currency_mismatch", {"code": mism.error_code, "error": mism.error}))
        summary.append(_row("currency mismatch refused", not mism.success, f"code={mism.error_code}"))
    else:
        print(f"  MINT FAILED: {err}")
        summary.append(_row("currency mismatch refused", False, "mint failed"))

    # ---- 4. expired token --------------------------------------------------
    print("\n" + "=" * 72)
    print("[4] EXPIRED — charge a token past its expires_at")
    print("=" * 72)
    if args.skip_expired:
        print("  SKIPPED (--skip-expired).")
        summary.append(_row("expired refused", False, "skipped"))
    else:
        # Preferred: mint with an already-past expires_at (instant). Stripe may
        # reject that at mint time — in which case the mint error is itself
        # evidence, and we fall back to a short-lived token plus a wait.
        past_token, past_err = mint_test_spt(
            api_key, currency=currency, max_amount_cents=cap_cents, expires_at_epoch=now - 600
        )
        if not past_token:
            print(f"  past-dated mint rejected (expected; that is evidence too): {past_err}")
            wait_s = int(args.expired_wait_seconds)
            short_token, short_err = mint_test_spt(
                api_key, currency=currency, max_amount_cents=cap_cents,
                expires_at_epoch=int(time.time()) + max(1, wait_s - 30),
            )
            if not short_token:
                print(f"  MINT FAILED: {short_err}")
                summary.append(_row("expired refused", False, "mint failed"))
                short_token = None
            else:
                print(f"  minted short-lived token ...{short_token[-6:]}; waiting {wait_s}s for expiry")
                await asyncio.sleep(wait_s)
                past_token = short_token
        if past_token:
            exp = await charge_spt(
                past_token, amount_cents=args.amount_cents, currency=currency,
                idem=f"spt_conf_exp_{int(time.time())}",
            )
            print(f"  capture -> success={exp.success} status={exp.status} code={exp.error_code}")
            print(f"  error: {exp.error}")
            findings.append(("expired_token", {"code": exp.error_code, "error": exp.error}))
            summary.append(_row("expired refused", not exp.success, f"code={exp.error_code}"))

    # ---- copy-pasteable summary -------------------------------------------
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    for row in summary:
        print(f"  {row['verdict']:<11} {row['label']:<28} {row['detail']}")

    print("\n" + "-" * 72)
    print("OBSERVED STRIPE ERROR CODES (copy into the review, then decide per code)")
    print("-" * 72)
    for name, data in findings:
        print(f"  {name:<26} error_code={data.get('code')!r}")
        print(f"  {'':<26} error={str(data.get('error'))[:160]!r}")

    print("\n" + "-" * 72)
    print("CANDIDATE for _SPT_DEFINITIVE_ERROR_CODES")
    print("(services/acp_checkout_session_service.py — currently frozenset())")
    print("-" * 72)
    codes = sorted({str(d.get("code")) for _, d in findings if d.get("code")})
    if codes:
        print("_SPT_DEFINITIVE_ERROR_CODES: frozenset = frozenset(")
        print("    {")
        for c in codes:
            print(f'        "{c}",')
        print("    }")
        print(")")
    else:
        print("  (no codes observed)")
    print(
        "\n  DO NOT paste this blindly. A code belongs in that set ONLY if it "
        "provably means NO CHARGE LANDED. Any code that can also be returned "
        "after money moved must stay out — listing it releases the completion "
        "claim and the next attempt mints a new PSP key, i.e. a double charge. "
        "When in doubt, leave it out: unknown ⇒ ambiguous ⇒ claim held is already "
        "the safe default."
    )
    print(
        "\n  Also note: these codes came from a TEST-mode account. Confirm they "
        "are the same identifiers in live mode before relying on them there."
    )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Founder-run Stripe SPT sandbox conformance + failure-code enumeration. "
                    "TEST KEYS ONLY — refuses a live key.",
    )
    p.add_argument("--amount-cents", type=int, default=100)
    p.add_argument("--cap-cents", type=int, default=None,
                   help="Token max_amount. Defaults to --amount-cents.")
    p.add_argument("--currency", default="usd", help="usd or cad (SPT is US/CA only).")
    p.add_argument("--expiry-seconds", type=int, default=3600)
    p.add_argument("--skip-expired", action="store_true",
                   help="Skip the expired-token probe (it may need to wait).")
    p.add_argument("--expired-wait-seconds", type=int, default=90,
                   help="Wait budget when a past-dated mint is rejected.")
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
