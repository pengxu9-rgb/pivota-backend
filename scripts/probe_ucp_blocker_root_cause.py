#!/usr/bin/env python3
"""Root-cause probe for merchants whose UCP checkout does NOT reach `ready_for_complete`.

Drives the SHIPPED caller (`services/merchant_ucp_checkout.py`) exactly as the readiness sweep
does — search_catalog → create_checkout → update_checkout(destination) — but keeps everything
the sweep threw away:

  * the FULL `messages[]` entries (code, severity, content, path) on both create and update,
    not just the codes;
  * the full `continue_url`, and the checkout HTML behind it (saved to `--html-dir`) so the
    installed checkout UI extensions, their TARGETS, and the payment-gateway configuration
    can be read offline without re-opening carts.

It never pays: `complete_checkout` / `cancel_checkout` are refused by the caller's
`_ALLOWED_TOOLS` before any I/O, and this script does not name them.

Usage:
    MERCHANT_UCP_CHECKOUT_ENABLED=1 .venv/bin/python scripts/probe_ucp_blocker_root_cause.py \
        --merged reports/ucp_payment_readiness_2026_09_03/merged_results.json \
        --out <results.json> --html-dir <dir> [--domains a,b] [--concurrency 4]
"""

from __future__ import annotations

import argparse
import asyncio
import html as htmllib
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from services import merchant_ucp_checkout as M  # noqa: E402
from scripts.probe_merchant_ucp_payment_readiness import (  # noqa: E402
    MARKETS,
    PROBE_BUYER,
    pick_variant,
    totals_map,
)

PROBE_CLICK_ID = "ucp_rootcause_probe"

# Extra synthetic destinations for the cross-border falsification runs. Same rules as the
# readiness probe's MARKETS: a real deliverable address shape, a phone that cannot ring a real
# person (KR 010-0000-0000 is unallocated), no real person anywhere.
MARKETS = dict(MARKETS)
MARKETS["KR"] = {
    "context": {"address_country": "KR", "currency": "KRW", "language": "ko-KR"},
    "address": {
        "first_name": "Pivota", "last_name": "Probe",
        "street_address": "110 Sejong-daero", "address_locality": "Jung-gu",
        "address_region": "Seoul", "postal_code": "04519", "address_country": "KR",
        "phone_number": "+821000000000",
    },
}
UA = {
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "accept": "text/html,application/xhtml+xml",
    "accept-language": "en-US,en;q=0.9",
}

# Regexes over the checkout HTML. Every one of these was found by reading the page, not guessed.
RE_EXT = re.compile(r"ui_extension/handle/([A-Za-z0-9_-]+)/version/([A-Za-z0-9_.-]+)")
RE_GATEWAY = re.compile(r'PaymentsPartners::Entities::(\w+)/(\d+)","name":"([^"]+)"')
RE_TARGET = re.compile(r'"(purchase\.[a-z0-9.-]+|customer-account\.[a-z0-9.-]+)"')


def full_messages(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for entry in payload.get("messages") or []:
        if isinstance(entry, dict):
            out.append(
                {
                    k: entry.get(k)
                    for k in ("code", "type", "severity", "content", "path", "content_type")
                    if k in entry
                }
            )
    return out


def html_signals(text: str) -> Dict[str, Any]:
    """Cheap offline-able extraction; the raw HTML is also saved for anything missed here."""
    t = htmllib.unescape(text)
    exts = sorted(set(RE_EXT.findall(t)))
    gws = sorted(set((k, i, n) for k, i, n in RE_GATEWAY.findall(t)))
    targets = sorted(set(RE_TARGET.findall(t)))
    flags = {}
    for key in (
        "paymentMethodUiExtension",
        "checkoutHostedFields",
        "supportsNetworkSelection",
        "alternative",
        "hostedFields",
        "offsite",
        "requiresLogin",
        "customerAccountsV2",
        "guestCheckout",
        "loginRequired",
        "shopPayEnabled",
        "extensionInteraction",
    ):
        flags[key] = len(re.findall(re.escape(key), t))
    return {
        "html_len": len(t),
        "extensions": [f"{h} ({v})" for h, v in exts],
        "gateways": [{"kind": k, "id": i, "name": n} for k, i, n in gws],
        "targets": targets,
        "flag_counts": flags,
    }


# Titles that mark a variant the merchant does not ship. The readiness sweep's "cheapest
# available variant" picked an e-gift card (rovectin), a $1.00 sample (ponds) and a $1.75 line
# (plackers), all of which Shopify priced with `fulfillment.methods: []` — the checkout reached
# ready_for_complete WITHOUT a shipment, so it says nothing about a physical order. The UCP
# catalog carries no requires_shipping flag (probed 2026-09-03), so this is a heuristic, and the
# result records the variant so a reader can judge it.
_NON_SHIPPING_TITLE = re.compile(r"gift ?card|e-gift|sample|digital|download", re.I)


def pick_shippable_variant(products, min_amount: int):
    best = None
    for p in products or []:
        if _NON_SHIPPING_TITLE.search(str(p.get("title") or "")):
            continue
        for v in p.get("variants") or []:
            avail = v.get("availability")
            ok = avail.get("available") is True if isinstance(avail, dict) else avail == "in_stock"
            if not ok or _NON_SHIPPING_TITLE.search(str(v.get("title") or "")):
                continue
            try:
                amount = int((v.get("price") or {}).get("amount"))
            except (TypeError, ValueError):
                continue
            vid = str(v.get("id") or "").strip()
            if not vid or amount < min_amount:
                continue
            cand = {"variant_id": vid, "amount": amount,
                    "currency": (v.get("price") or {}).get("currency"),
                    "product_title": p.get("title"), "variant_title": v.get("title"),
                    "url": p.get("url")}
            if best is None or cand["amount"] < best["amount"]:
                best = cand
    return best


async def probe_one(
    row: Dict[str, Any], html_dir: str, market_override: Optional[str] = None,
    min_amount: int = 0,
) -> Dict[str, Any]:
    domain = row["domain"]
    market = market_override or row.get("market") or "US"
    mk = MARKETS[market]
    buyer = dict(PROBE_BUYER, phone_number=mk["address"]["phone_number"])
    out: Dict[str, Any] = {
        "domain": domain,
        "brand": row.get("brand"),
        "market": market,
        "sweep_status": row.get("checkout_status"),
        "sweep_messages": row.get("messages"),
        "stage": "start",
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        cat = await M._call_tool(
            domain,
            "search_catalog",
            {"meta": M.build_meta(), "catalog": {"query": "", "context": mk["context"]}},
        )
    except Exception as err:
        out["stage"] = "search_failed"
        out["error"] = f"{type(err).__name__}: {err}"
        return out
    variant = (pick_shippable_variant(cat.get("products") or [], min_amount)
               if min_amount else pick_variant(cat.get("products") or []))
    if not variant:
        out["stage"] = "no_purchasable_variant"
        return out
    out["variant"] = variant
    line_items = [{"variant_id": variant["variant_id"], "quantity": 1}]
    try:
        created = await M.create_checkout(
            domain, line_items=line_items, click_id=PROBE_CLICK_ID, buyer=buyer
        )
    except Exception as err:
        out["stage"] = "create_failed"
        out["error"] = f"{type(err).__name__}: {err}"
        return out
    checkout_id = str(created.get("id") or "")
    out["checkout_id"] = checkout_id
    out["create"] = {
        "status": created.get("status"),
        "messages": full_messages(created),
        "continue_url": created.get("continue_url"),
        "keys": sorted(created.keys()),
    }
    line_item_ids = [str(li.get("id")) for li in created.get("line_items") or [] if li.get("id")]
    if not checkout_id or not line_item_ids:
        out["stage"] = "create_incomplete"
        return out
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
        priced = None
    if priced is not None:
        out["priced"] = {
            "status": priced.get("status"),
            "messages": full_messages(priced),
            "continue_url": priced.get("continue_url"),
            "totals_map": totals_map(priced.get("totals")),
            "fulfillment": priced.get("fulfillment"),
            "keys": sorted(priced.keys()),
        }
        out["stage"] = "priced"
    # --- read-back, in case get_checkout says something update did not ------------------
    try:
        got = await M.get_checkout(domain, checkout_id)
        out["readback"] = {"status": got.get("status"), "messages": full_messages(got)}
    except Exception as err:
        out["readback"] = {"error": f"{type(err).__name__}: {err}"}
    # --- the checkout page ------------------------------------------------------------
    url = (priced or {}).get("continue_url") or created.get("continue_url")
    if url:
        try:
            async with httpx.AsyncClient(timeout=40, follow_redirects=True, headers=UA) as c:
                resp = await c.get(url)
            text = resp.text
            suffix = f".{market}" + (f".min{min_amount}" if min_amount else "")
            path = os.path.join(html_dir, f"{domain}{suffix}.html")
            with open(path, "w") as fh:
                fh.write(text)
            out["page"] = {
                "final_url": str(resp.url),
                "http_status": resp.status_code,
                "saved": path,
                **html_signals(text),
            }
        except Exception as err:
            out["page"] = {"error": f"{type(err).__name__}: {err}"}
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--merged", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--html-dir", required=True)
    ap.add_argument("--domains", default="")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--market", default="", choices=[""] + sorted(MARKETS),
                    help="override the sweep's market for every domain (falsification runs)")
    ap.add_argument("--min-amount", type=int, default=0,
                    help="pick the cheapest variant at or above this minor-unit price, skipping "
                         "gift-card/sample titles (0 = the sweep's cheapest-variant rule)")
    args = ap.parse_args()
    if not M.write_ops_enabled():
        raise SystemExit("MERCHANT_UCP_CHECKOUT_ENABLED must be set for this probe")
    os.makedirs(args.html_dir, exist_ok=True)
    rows = [r for r in json.load(open(args.merged)) if r.get("checkout_status")]
    if args.domains:
        wanted = {d.strip() for d in args.domains.split(",") if d.strip()}
        rows = [r for r in rows if r["domain"] in wanted]
    sem = asyncio.Semaphore(max(1, args.concurrency))

    async def run(row):
        async with sem:
            try:
                return await probe_one(row, args.html_dir, args.market or None, args.min_amount)
            except Exception as err:
                return {"domain": row["domain"], "stage": "probe_crashed",
                        "error": f"{type(err).__name__}: {err}"}

    results = await asyncio.gather(*[run(r) for r in rows])
    json.dump(results, open(args.out, "w"), indent=1, sort_keys=True, ensure_ascii=False)
    for r in sorted(results, key=lambda x: x["domain"]):
        pr = r.get("priced") or {}
        pg = r.get("page") or {}
        print(
            f"{r['domain']:24s} {r['stage']:14s} status={str(pr.get('status') or '-'):20s} "
            f"msgs={','.join(m.get('code','?') for m in pr.get('messages') or []) or '-':40s} "
            f"exts={len(pg.get('extensions') or [])} gws={len(pg.get('gateways') or [])} "
            f"{r.get('error') or pg.get('error') or ''}"
        )
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
