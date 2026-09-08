"""Resolve one of our catalog rows to a Reap variant and, optionally, quote it. Moves no money.

RUN THIS FROM THE SESSION THAT HOLDS THE SANDBOX CREDENTIALS. It reads them from the environment
and never prints them, so nobody has to hand the key to anyone.

    export REAP_API_BASE_URL="https://sandbox.api.reap.global"
    export REAP_API_KEY="..."                       # never echoed, never logged, never in a URL
    python3 scripts/ops/reap_resolve_and_quote.py                  # resolve only (2 GETs of data)
    python3 scripts/ops/reap_resolve_and_quote.py --quote          # also ask for a quote

WHAT IT IS FOR. `resolve_our_row` is the step PR #2136 did not have, and it is the one part of
the rebuild that cannot be proven from the spec: whether a real row of ours actually finds its
way to a real Reap variant depends on Reap's index, not on our code. Everything else in the
module is pinned by tests against the published schema; THIS is the part that needs a live call.

WHAT A RUN TELLS YOU, in order of how much it is worth:

  1. Does our (domain, product name, variant title) triple resolve at all? A refusal here is the
     real finding, and the reason string names which step refused -- `search:merchant_not_in_
     results` and `options:axes_not_determined_by_title` are different problems with different
     fixes, and neither is "Reap is broken".
  2. Does Reap's price for the variant we RESOLVED match our row? Measured once already: it does,
     to the cent, for the row below. Comparing against what Reap SHOWS instead (`previewVariant`,
     which is availability-ordered) reads as 32% staleness on a row that is correct.
  3. Only with `--quote`: does `POST /agentic/quotes` accept it, and what does the shipping and
     tax breakdown look like.

THE ROW. Fenty Eau de Parfum, Standard, $140.00, on fentybeauty.com -- the largest domain in the
purchasable cohort. Standard rather than a cheaper size on purpose: it is the size our prod row
holds, and it is the one Reap's index currently marks unavailable, so the run also shows what a
resolvable-but-unavailable variant looks like end to end. Gift cards sit higher in that cohort by
price and are deliberately not used: they often skip shipping and take a different checkout path,
which would confound a first result.

IT MOVES NO MONEY. A quote is an ask. Nothing here authorises, captures or funds anything, and
this script cannot reach `POST /agentic/checkouts` -- that endpoint is not called anywhere in the
module.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Real prod row, measured 2026-09-08. Storefront variant id 41669483823149 -- recorded here only
# so the mapping can be checked by eye. It is NOT sent: Reap's namespace is `var_...`, and
# sending our id was the central defect of the client this replaces.
MERCHANT_DOMAIN = "fentybeauty.com"
BRAND = "Fenty Beauty"
PRODUCT_NAME = "Fenty Eau de Parfum"
VARIANT_TITLE = "Standard"
OUR_PRICE = 140.00
OUR_STOREFRONT_VARIANT_ID = "41669483823149"
PDP = "https://fentybeauty.com/products/fenty-eau-de-parfum"

# A quote needs a buyer email and, for shipping, a real address. This is a well-known public
# postal address and a placeholder mailbox: a sandbox call should not carry a real buyer's
# details to a third party.
QUOTE_EMAIL = "sandbox-probe@pivota.cc"
SHIPPING_ADDRESS = {
    "firstName": "Pivota", "lastName": "Sandbox", "phone": "+14155550123",
    "addressLine1": "900 Brannan St", "city": "San Francisco",
    "region": "CA", "postalCode": "94103", "country": "US",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quote", action="store_true",
                    help="after resolving, also call POST /agentic/quotes")
    args = ap.parse_args()

    from services import reap_agentic_client as rc

    print(f"\nOur row  : {PRODUCT_NAME} / {VARIANT_TITLE}  ${OUR_PRICE:.2f}  on {MERCHANT_DOMAIN}")
    print(f"           storefront variant {OUR_STOREFRONT_VARIANT_ID} (ours, never sent)")
    print(f"PDP      : {PDP}")
    print(f"\nBase URL set : {bool(rc.base_url())}")
    print(f"API key set  : {bool(os.getenv('REAP_API_KEY'))}   (value never printed)")
    if not rc.is_configured():
        print("\nNOT CONFIGURED — set REAP_API_BASE_URL and REAP_API_KEY. Nothing was sent.")
        return 1
    try:
        rc.validate_base_url()
        print(f"Host check   : OK ({rc.base_url()})")
    except rc.ReapConfigError as exc:
        print(f"\nHOST REFUSED: {exc}")
        print(f"\nThe allowlist is {rc.ALLOWED_HOST_SUFFIXES}, taken from the spec's `servers`.\n"
              "If the host above is genuinely Reap's, report it so the list can be corrected.\n"
              "DO NOT disable the check: a mistyped base URL delivers the API key to the typo.")
        return 2

    resolved = asyncio.run(rc.resolve_our_row(
        merchant_domain=MERCHANT_DOMAIN, product_name=PRODUCT_NAME, brand=BRAND,
        variant_title=VARIANT_TITLE, our_price=OUR_PRICE, country="US", currency="USD",
    ))

    print("\n--- RESOLUTION " + "-" * 56)
    print(f"ok              : {resolved.ok}")
    print(f"reason          : {resolved.reason}")
    print(f"reap product    : {resolved.product_id}")
    print(f"reap variant    : {resolved.variant_id}")
    print(f"matched options : {resolved.matched_options}")
    print(f"reap price      : {resolved.price}")
    print(f"reap available  : {resolved.available}")
    print(f"price disagrees : {resolved.price_disagrees}")
    print(f"asked-for avail : {resolved.chosen_available}   (the value WE ASKED FOR)")
    print(f"resolved at     : {resolved.resolved_at}")
    print(f"queries tried   : {resolved.queries_tried}")
    if resolved.warnings:
        print(f"warnings        : {resolved.warnings}")
    if resolved.candidates:
        print(f"candidates      : {json.dumps(resolved.candidates, indent=2)[:1200]}")

    if not resolved.ok:
        print("\n--- WHAT THIS TELLS US " + "-" * 48)
        print("  " + rc.explain_refusal(resolved.reason).replace("\n", "\n  "))
        return 3

    if resolved.price_disagrees:
        print("\nNOTE: prices differ AFTER resolving the exact variant. That is a real signal —\n"
              "unlike a preview-vs-row comparison, which is a size mismatch, not drift.")
    if resolved.single_variant_product:
        print("\nNOTE: option-less product — the variant came from `defaultVariant`, which is the\n"
              "one legitimate read of that field: with no sibling variants there is no\n"
              "availability ordering and so no substitution possible.")
    if resolved.available is False:
        print("\nNOTE: Reap marks this variant unavailable. One third-party observation about a\n"
              "variant the merchant's own storefront may still list. Not a delisting.")

    print("\nNOTE: Reap's `prd_`/`var_` ids are minted PER SEARCH — five searches for one product\n"
          "in a day returned five different product ids, each with its own variant set. They stay\n"
          "quotable for hours, so they are durable handles, but they are not an identity. Do not\n"
          "store one on a catalog row: resolve fresh, quote, discard.")

    if not args.quote:
        print("\nResolved only. Re-run with --quote to ask for a quote.")
        return 0

    quote = asyncio.run(rc.request_quote(
        items=[{"variantId": resolved.variant_id, "quantity": 1}],
        email=QUOTE_EMAIL, shipping_address=SHIPPING_ADDRESS,
    ))
    print("\n--- QUOTE " + "-" * 61)
    print(f"ok      : {quote.ok}")
    print(f"status  : {quote.status}")
    print(f"error   : {quote.error}")
    if quote.merchant_probably_not_completable:
        print("\n503 AGENTIC_SERVICE_UNAVAILABLE on the quote. Measured across nine merchants,\n"
              "the two that answer this way are the two that are not UCP merchants — Reap's\n"
              "search index is far wider than its checkout coverage, so a search hit is not a\n"
              "quotable product. Treat as per-merchant, not as an outage. n=2: record it, do not\n"
              "suppress the merchant permanently on one sample.")
    if quote.ok:
        data = quote.data
        print(f"quoteId : {data.get('id')}")
        print(f"expires : {data.get('expiresAt')}")
        print(f"total   : {json.dumps(data.get('amountBreakdown', {}).get('finalAmount'))}")
        print(f"shipping options: {len(data.get('shippingOptions') or [])}")
    else:
        print("\nThe response BODY is deliberately not captured: it can echo the request, and the\n"
              "request carries a shipping address. Reproduce with a throwaway address if you\n"
              "need the payload.")
    return 0 if quote.ok else 4


if __name__ == "__main__":
    raise SystemExit(main())
