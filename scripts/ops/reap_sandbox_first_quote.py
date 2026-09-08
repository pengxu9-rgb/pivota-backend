"""The first real call to Reap. One quote, one product, and three answers we cannot get any other way.

RUN THIS FROM THE SESSION THAT HOLDS THE SANDBOX CREDENTIALS. It reads them from the environment
and never prints them. Whoever runs it does not need to hand the key to anyone.

    export REAP_API_BASE_URL="https://<the sandbox host>/<version>"
    export REAP_API_KEY="..."            # never echoed, never logged, never in a URL
    python3 scripts/ops/reap_sandbox_first_quote.py            # shows the exact body, sends nothing
    python3 scripts/ops/reap_sandbox_first_quote.py --send     # makes the call

WHAT IT ANSWERS. Three things that are unknown from the code side, and that block the rail until
somebody asks Reap directly:

  1. THE HOST. `services/reap_quote_client` refuses a base URL whose host is not on an allowlist,
     BEFORE attaching the key — because a mistyped REAP_API_BASE_URL does not fail the call, it
     delivers our API key to whatever host the typo names. The shipped list
     ("reap.global", "reap.so", "reapfin.com") is INFERRED. If this script stops with a config
     error naming your host, that list is wrong and the fix is to correct the list — NOT to
     disable the check.
  2. THE SHAPE OF `merchant`. Reap named the field on 2 Sep and never its form: domain, UCP
     endpoint, or a Reap-side id. This sends the domain, because that is what our catalog holds.
     A 4xx that names the field is itself the answer, which is why the status is reported rather
     than swallowed.
  3. WHETHER ATTRIBUTION SURVIVES. Reap said "at its current iteration the attribution won't
     survive checkout". We send the block and report what came BACK. This is the commercial
     question — if it does not survive, a purchase completes and Pivota cannot prove it caused
     it — and no amount of code answers it.

WHY THIS PRODUCT. Fenty Eau de Parfum, variant 41669483823149, is a real in-stock physical item
on the largest domain in the purchasable cohort (fentybeauty.com, 746 products). Its variant id
is one the 8 Sep identity backfill recovered, so this call also exercises the thing that work
produced. Gift cards sit higher in that cohort by price and are deliberately NOT used: they often
skip shipping and take a different checkout path, which would confound a first result.

IT MOVES NO MONEY. A quote is an ask. Nothing here authorizes, captures or funds anything, and
`services/reap_quote_client` has a test that fails if it ever addresses an endpoint other than
/quotes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# --- the call ---------------------------------------------------------------------------------
# Real row, measured on prod 2026-09-08. `ucpItemId` is the storefront variant id, which is what
# UCP's build_line_items uses and what the backfill wrote.
MERCHANT = "fentybeauty.com"
ITEMS = [{"ucpItemId": "41669483823149", "quantity": 1}]
PRODUCT_TITLE = "Fenty Eau de Parfum"
QUOTED_PRICE = "140.00 USD"
PDP = "https://fentybeauty.com/products/fenty-eau-de-parfum"

# A real US address is required for a shipping quote. This is a well-known public postal address
# (a US Postal Service facility), not a person's home: a first sandbox call should not carry a
# real buyer's details to a third party.
SHIPPING_ADDRESS = {
    "line1": "900 Brannan St",
    "city": "San Francisco",
    "state": "CA",
    "postalCode": "94103",
    "country": "US",
}

# Sent so we can learn whether it survives. `ref` identifies us; `click_id` is the join key our
# own outcome ledger uses.
ATTRIBUTION = {
    "ref": "pivota",
    "campaign_source": "pivota",
    "campaign_medium": "agent",
    "campaign_name": "reap-sandbox-first-quote",
    "click_id": "clk_sandbox_first_quote",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--send", action="store_true",
                    help="actually call Reap. Without it, prints the body and exits.")
    args = ap.parse_args()

    from services import reap_quote_client as rq

    body = rq.build_quote_request(
        merchant=MERCHANT, items=ITEMS,
        shipping_address=SHIPPING_ADDRESS, attribution=ATTRIBUTION,
    )

    print(f"\nProduct : {PRODUCT_TITLE}  ({QUOTED_PRICE})")
    print(f"PDP     : {PDP}")
    print(f"Body    : {json.dumps(body, indent=2, sort_keys=True)}")
    print(f"Idempotency-Key: {rq.idempotency_key(body)}")

    # Configuration, reported WITHOUT the key. `is_configured` needs both halves.
    print(f"\nBase URL set : {bool(rq.base_url())}")
    print(f"API key set  : {bool(os.getenv('REAP_API_KEY'))}   (value never printed)")
    if not rq.is_configured():
        print("\nNOT CONFIGURED — set REAP_API_BASE_URL and REAP_API_KEY. Nothing was sent.")
        return 1

    try:
        rq.validate_base_url()
        print(f"Host check   : OK ({rq.base_url()})")
    except rq.ReapConfigError as exc:
        print(f"\nHOST REFUSED: {exc}")
        print(
            "\nThis is finding #1 and it is expected to be possible: the allowlist\n"
            f"  {rq.ALLOWED_HOST_SUFFIXES}\n"
            "was inferred, not confirmed by Reap. If the host above is genuinely Reap's,\n"
            "report it so ALLOWED_HOST_SUFFIXES can be corrected. DO NOT disable the check —\n"
            "it exists because a mistyped base URL delivers the API key to the typo's host."
        )
        return 2

    if not args.send:
        print("\nDry run. Nothing sent. Re-run with --send to make the call.")
        return 0

    result = asyncio.run(rq.request_quote(
        merchant=MERCHANT, items=ITEMS,
        shipping_address=SHIPPING_ADDRESS, attribution=ATTRIBUTION,
    ))

    print("\n--- RESULT " + "-" * 60)
    print(f"ok                 : {result.ok}")
    print(f"http status        : {result.status}")
    print(f"quote_id           : {result.quote_id}")
    print(f"checkout_url       : {result.checkout_url}")
    print(f"attribution_echoed : {result.attribution_echoed}")
    if result.error:
        print(f"error              : {result.error}")

    print("\n--- WHAT THIS TELLS US " + "-" * 48)
    if result.ok:
        print(f"2. `merchant` as a bare domain ({MERCHANT!r}) WAS ACCEPTED.")
        if result.attribution_echoed:
            print("3. Attribution CAME BACK — Reap has shipped pass-through since 2 Sep.")
            print("   Verify it survives to the completed order before relying on it.")
        else:
            print("3. Attribution did NOT come back. This matches Reap's 2 Sep answer.")
            print("   Consequence: a purchase completes and Pivota cannot prove it caused it.")
            print("   That is a commercial conversation with Reap, not a code change.")
    else:
        print(f"2. The call did not succeed ({result.error}).")
        print("   A 4xx naming a field is itself an answer about `merchant`'s expected shape —")
        print("   report the status and any field name Reap gave you.")
        print("   The response BODY is deliberately not captured: it can echo the request, and")
        print("   the request carries a shipping address.")

    # Full payload only on success, and only to stdout — never logged.
    if result.ok and result.raw:
        print("\n--- RAW QUOTE " + "-" * 57)
        print(json.dumps(result.raw, indent=2, sort_keys=True)[:4000])
    return 0 if result.ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
