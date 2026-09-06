#!/usr/bin/env python3
"""Corpus-wide UCP payment-readiness probe for the agentic-card rail.

Answers three questions per merchant domain, each a hard gate on the one before:

  1. UCP?          does `https://<apex>/.well-known/ucp` serve a profile, and does its
                   MCP door answer `tools/list`?
  2. PAYABLE?      can an agent actually drive that door to a priced checkout that
                   advertises a card payment handler — real catalog read, real cart,
                   real destination, real totals?
  3. REAP-ELIGIBLE? is that merchant reachable by Reap's agentic module, whose
                   `POST /quotes` takes `source.type=CLIENT_SUPPLIED_UCP` + `merchant`
                   + `items[].ucpItemId` + `shippingAddress`?

SCOPE — THIS SCRIPT NEVER PAYS. It drives `services/merchant_ucp_checkout.py`, whose
`_ALLOWED_TOOLS` refuses `complete_checkout` and `cancel_checkout` before any I/O. Stage 2
stops at a priced checkout; the merchant is left holding an abandoned cart, which is the
same artefact a human who closed the tab leaves. Question 3 is answered by MEASUREMENT of
the merchant side plus the settled Reap contract — not by a Reap call, because the agentic
module's sandbox is not open yet and a live one would be a real charge.

Usage:
    MERCHANT_UCP_CHECKOUT_ENABLED=1 python scripts/probe_merchant_ucp_payment_readiness.py \
        --corpus <corpus.json> --out <results.json> [--domains a.com,b.com] [--concurrency 4]
"""

from __future__ import annotations

import argparse
import asyncio
import re
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from services import merchant_ucp_checkout as M  # noqa: E402

# A synthetic destination. The street/city/zip are a real deliverable US address shape so the
# merchant's tax and shipping engines actually run; the phone is in the 555-01xx block reserved
# for fictional use, and the email is on a domain we own. Nothing here is a real person.
# Synthetic destinations, one per market. A store that only ships domestically answers
# `delivery_no_delivery_available_for_merchandise_line` to a foreign address — that is the
# PROBE being wrong about the market, not the merchant being unable to sell, and scoring it
# as a merchant limitation understates the corpus. Street/city/postcode are real deliverable
# address shapes so tax and shipping engines actually run; every phone is in a range reserved
# for fictional use, and the email is on a domain we own. No real person appears here.
MARKETS: Dict[str, Dict[str, Any]] = {
    "US": {
        "context": {"address_country": "US", "currency": "USD", "language": "en-US"},
        "address": {
            "first_name": "Pivota", "last_name": "Probe",
            "street_address": "1209 Orange Street", "address_locality": "Wilmington",
            "address_region": "DE", "postal_code": "19801", "address_country": "US",
            "phone_number": "+12025550142",
        },
    },
    "JP": {
        "context": {"address_country": "JP", "currency": "JPY", "language": "ja-JP"},
        "address": {
            "first_name": "Pivota", "last_name": "Probe",
            "street_address": "2-11-3 Meguro", "address_locality": "Meguro-ku",
            "address_region": "Tokyo", "postal_code": "153-0063", "address_country": "JP",
            "phone_number": "+81357778888",
        },
    },
    "KR": {
        "context": {"address_country": "KR", "currency": "KRW", "language": "ko-KR"},
        "address": {
            "first_name": "Pivota", "last_name": "Probe",
            "street_address": "29 Seolleung-ro 152-gil", "address_locality": "Gangnam-gu",
            "address_region": "Seoul", "postal_code": "06010", "address_country": "KR",
            "phone_number": "+82212345678",
        },
    },
    "CN": {
        "context": {"address_country": "CN", "currency": "CNY", "language": "zh-CN"},
        "address": {
            "first_name": "Pivota", "last_name": "Probe",
            "street_address": "1000 Lujiazui Ring Road", "address_locality": "Pudong",
            "address_region": "Shanghai", "postal_code": "200120", "address_country": "CN",
            # 13800138000 is the canonical documentation/example mobile number in CN.
            "phone_number": "+8613800138000",
        },
    },
    "CA": {
        "context": {"address_country": "CA", "currency": "CAD", "language": "en-CA"},
        "address": {"first_name": "Pivota", "last_name": "Probe",
            "street_address": "100 King Street West", "address_locality": "Toronto",
            "address_region": "ON", "postal_code": "M5H 1A1", "address_country": "CA",
            "phone_number": "+14165550142"},
    },
    "SG": {
        "context": {"address_country": "SG", "currency": "SGD", "language": "en-SG"},
        "address": {"first_name": "Pivota", "last_name": "Probe",
            "street_address": "1 Raffles Place", "address_locality": "Singapore",
            "address_region": "Singapore", "postal_code": "048616", "address_country": "SG",
            "phone_number": "+6564385000"},
    },
    "HK": {
        "context": {"address_country": "HK", "currency": "HKD", "language": "en-HK"},
        "address": {"first_name": "Pivota", "last_name": "Probe",
            "street_address": "1 Connaught Road Central", "address_locality": "Central",
            "address_region": "Hong Kong Island", "postal_code": "999077",
            "address_country": "HK", "phone_number": "+85228228228"},
    },
    "AU": {
        "context": {"address_country": "AU", "currency": "AUD", "language": "en-AU"},
        "address": {"first_name": "Pivota", "last_name": "Probe",
            "street_address": "1 Martin Place", "address_locality": "Sydney",
            "address_region": "NSW", "postal_code": "2000", "address_country": "AU",
            "phone_number": "+61255501234"},
    },
    "GB": {
        "context": {"address_country": "GB", "currency": "GBP", "language": "en-GB"},
        "address": {
            "first_name": "Pivota", "last_name": "Probe",
            "street_address": "1 Coleman Street", "address_locality": "London",
            "address_region": "England", "postal_code": "EC2R 5AA", "address_country": "GB",
            "phone_number": "+442079460000",
        },
    },
}
PROBE_BUYER = {"email": "ucp-probe@pivota.cc", "phone_number": "+12025550142"}
PROBE_CLICK_ID = "ucp_readiness_probe"


def _minor(amount: Any) -> Optional[int]:
    try:
        return int(amount)
    except (TypeError, ValueError):
        return None


_NON_SHIPPING_TITLE = re.compile(
    r"gift\s*card|e-?gift|voucher|digital|sample\s*kit|donation", re.I
)


def pick_variant(products: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The MEDIAN-priced available variant, skipping things that never ship.

    An earlier cut took the CHEAPEST variant, and that quietly biased the whole sweep: the
    cheapest thing in a beauty catalog is very often an e-gift card or a $1 placeholder, which
    completes trivially because `fulfillment.methods` is empty — no address to validate, no
    carrier to quote, no cross-border rule to trip. Two merchants were scored completable on a
    non-shipping line that way, and a placeholder store (every item $0.00/$1.00) was scored
    completable on a $1.00 item.

    Median rather than most-expensive: the dearest line runs into order-value limits and
    signature-required shipping, which is a different bias, not an absence of one.

    `availability` is an OBJECT (`{"available": true}`) on this protocol version.
    """
    cands: List[Dict[str, Any]] = []
    for p in products or []:
        title = str(p.get("title") or "")
        if _NON_SHIPPING_TITLE.search(title):
            continue
        for v in p.get("variants") or []:
            avail = v.get("availability")
            ok = avail.get("available") is True if isinstance(avail, dict) else avail == "in_stock"
            if not ok:
                continue
            amount = _minor((v.get("price") or {}).get("amount"))
            vid = str(v.get("id") or "").strip()
            if not vid or amount is None or amount <= 0:
                continue
            cands.append({
                "variant_id": vid, "amount": amount,
                "currency": (v.get("price") or {}).get("currency"),
                "product_title": title, "variant_title": v.get("title"), "url": p.get("url"),
            })
    if not cands:
        return None
    cands.sort(key=lambda c: c["amount"])
    return cands[len(cands) // 2]


def payment_shape(payload: Dict[str, Any]) -> Dict[str, Any]:
    """What the CHECKOUT says about payment.

    Probed 2026-09-03: this is `{"instruments": []}` even at `ready_for_complete`. It is the
    slot the PLATFORM fills at `complete_checkout`, NOT a list of what the merchant accepts —
    the accepted handlers live in the static `/.well-known/ucp` `payment_handlers`. An earlier
    cut of this probe read it as an accept-list and scored every merchant zero; recording the
    raw shape keeps that mistake visible rather than encoding it.
    """
    pay = payload.get("payment")
    return pay if isinstance(pay, dict) else {"_absent": True}


def totals_map(totals: Any) -> Dict[str, int]:
    """`totals` is a LIST of typed entries, not a keyed object.

    Shipping is typed `fulfillment`, never `shipping`. A dict-style read returns None for
    every merchant and looks like "the merchant did not price it".
    """
    out: Dict[str, int] = {}
    for entry in totals or []:
        if isinstance(entry, dict) and entry.get("type") is not None:
            amount = _minor(entry.get("amount"))
            if amount is not None:
                out[str(entry["type"])] = amount
    return out


async def probe_one(row: Dict[str, Any], market: str = "US") -> Dict[str, Any]:
    domain = row["domain"]
    mk = MARKETS[market]
    buyer = dict(PROBE_BUYER, phone_number=mk["address"]["phone_number"])
    out: Dict[str, Any] = {
        "domain": domain,
        "brand": row.get("brand"),
        "stage": "start",
        "market": market,
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    # --- catalog read -------------------------------------------------------------
    try:
        cat = await M._call_tool(
            domain,
            "search_catalog",
            {
                "meta": M.build_meta(),
                # An EMPTY query returns the storefront's own top products. Any keyword biases
                # the sample toward stores that happen to sell that word, and several of these
                # merchants are not cosmetics at all (dental floss, air freshener, collagen).
                "catalog": {"query": "", "context": mk["context"]},
            },
        )
    except Exception as err:
        out["stage"] = "search_failed"
        out["error"] = f"{type(err).__name__}: {err}"
        return out

    products = cat.get("products") or []
    out["catalog_products"] = len(products)
    variant = pick_variant(products)
    if not variant:
        out["stage"] = "no_purchasable_variant"
        out["available_variants"] = 0
        return out
    out["variant"] = variant

    # --- create -------------------------------------------------------------------
    line_items = [{"variant_id": variant["variant_id"], "quantity": 1}]
    try:
        created = await M.create_checkout(
            domain,
            line_items=line_items,
            click_id=PROBE_CLICK_ID,
            buyer=buyer,
        )
    except Exception as err:
        out["stage"] = "create_failed"
        out["error"] = f"{type(err).__name__}: {err}"
        return out

    checkout_id = str(created.get("id") or "")
    out["checkout_id"] = checkout_id
    out["create_status"] = (created.get("ucp") or {}).get("status")
    out["create_checkout_status"] = created.get("status")
    out["create_messages"] = M.message_codes(created)
    out["create_payment"] = payment_shape(created)
    out["attribution_echoed"] = bool(created.get("attribution"))
    line_item_ids = [
        str(li.get("id")) for li in created.get("line_items") or [] if li.get("id")
    ]
    out["line_item_ids"] = len(line_item_ids)
    if not checkout_id or not line_item_ids:
        out["stage"] = "create_incomplete"
        return out
    out["stage"] = "created"

    # --- price a destination ------------------------------------------------------
    try:
        priced = await M.update_checkout(
            domain,
            checkout_id,
            line_items=line_items,
            address=mk["address"],
            line_item_ids=line_item_ids,
            buyer=buyer,
        )
    except Exception as err:
        out["stage"] = "update_failed"
        out["error"] = f"{type(err).__name__}: {err}"
        return out

    totals = priced.get("totals")
    out["totals"] = totals
    out["priced_status"] = (priced.get("ucp") or {}).get("status")
    out["priced_checkout_status"] = priced.get("status")
    out["priced_messages"] = M.message_codes(priced)
    out["priced_payment"] = payment_shape(priced)
    out["totals_map"] = totals_map(totals)
    out["continue_url_present"] = bool(priced.get("continue_url"))
    # Did the merchant actually quote a SHIPMENT? A checkout that completes with no fulfillment
    # method has not exercised address validation, carrier rates, or any cross-border rule, so it
    # is not evidence the merchant can ship to this market.
    methods = (priced.get("fulfillment") or {}).get("methods") or []
    out["fulfillment_methods"] = len(methods)
    # ONLY a quoted fulfillment METHOD counts. The `fulfillment` entry in `totals` can be
    # present at amount 0 with no methods at all (plackers.com does exactly this in every
    # market), and reading that as a shipment scored a no-ship merchant as globally
    # deliverable. A total line naming zero is not a carrier agreeing to carry anything.
    out["ships"] = len(methods) > 0
    out["stage"] = "priced" if totals else "priced_no_totals"
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--domains", default="")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--market", default="US", choices=sorted(MARKETS))
    # These merchant doors rate-limit under sustained probing: a full-corpus sweep repeated
    # across several markets in one afternoon drew escalating 429s (4 -> 19 by the sixth market),
    # which reads in the results as "merchant does not ship here" unless you check the error
    # field. Pacing is therefore part of the measurement, not a courtesy.
    ap.add_argument("--delay", type=float, default=0.0,
                    help="seconds to pause after each merchant, per worker")
    ap.add_argument("--retries", type=int, default=0,
                    help="retries on HTTP 429, with exponential backoff")
    args = ap.parse_args()

    if not M.write_ops_enabled():
        raise SystemExit("MERCHANT_UCP_CHECKOUT_ENABLED must be set for this probe")

    corpus = json.load(open(args.corpus))
    if args.domains:
        wanted = {d.strip() for d in args.domains.split(",") if d.strip()}
        corpus = [r for r in corpus if r["domain"] in wanted]

    sem = asyncio.Semaphore(max(1, args.concurrency))

    async def run(row):
        async with sem:
            result = None
            for attempt in range(args.retries + 1):
                try:
                    result = await probe_one(row, args.market)
                except Exception as err:  # a probe must never take the whole sweep down
                    result = {"domain": row["domain"], "brand": row.get("brand"),
                              "stage": "probe_crashed", "error": f"{type(err).__name__}: {err}"}
                # A 429 is OUR pacing, not a merchant verdict — back off and ask again rather
                # than recording a rate limit as an unmeasurable merchant.
                if "429" not in str(result.get("error") or "") or attempt == args.retries:
                    break
                await asyncio.sleep(20 * (2 ** attempt))
            if args.delay:
                await asyncio.sleep(args.delay)
            result["attempts"] = attempt + 1
            return result

    results = await asyncio.gather(*[run(r) for r in corpus])
    json.dump(results, open(args.out, "w"), indent=1, sort_keys=True)

    for r in sorted(results, key=lambda x: x["domain"]):
        tm = r.get("totals_map") or {}
        print(
            f"{r['domain']:24s} {r['stage']:16s} "
            f"status={str(r.get('priced_checkout_status') or '-'):20s} "
            f"total={str(tm.get('total','-')):8s} "
            f"msgs={','.join(r.get('priced_messages') or []) or '-':32s} {r.get('error','')}"
        )
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
