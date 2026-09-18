"""Operator tool: run the Tier B cart-permalink preflight over a list of OUR merchants.

    python scripts/ops/tierb_cart_link_preflight.py merchants.json --out report.json
    python scripts/ops/tierb_cart_link_preflight.py merchants.json --out report.json --with-probe-buyer
    python scripts/ops/tierb_cart_link_preflight.py merchants.json --out report.json --only judydoll.com

INPUT is a JSON list of `{domain, market, variant_id?, product_handle?}`. The key `variant` is
read as `variant_id`, so reports/tierb_cart_permalink_2026_09_18/population.json works as-is.
A row with neither a variant nor a handle is a merchant-level probe: the preflight picks the
first representative variant in the store's catalog.

MARKET IS REQUIRED PER ROW. Shopify's `available` is scoped to the market it resolves for the
request, so each row's `market` is passed through and every catalog read pins `country=<market>`.
Without it, availability would be read for THIS machine's market (podl.us read as sold out from
a JP IP while it sells to the US). A row with a missing or malformed market comes back
INVALID_INPUT.

WHAT IT DOES TO EACH STORE. It reads the public product JSON and follows one cart permalink,
which CREATES ONE ABANDONED CHECKOUT per merchant (per retry). Nothing is paid; no payment step
is reached. Run it only over Pivota's own merchant list — never over a domain someone handed
you (see services/shopify_cart_link_preflight.py).

NO BUYER BY DEFAULT — that is the Reap path: the link carries only variant, qty and the click
id, and the buyer's email and address travel in Reap's quote body. `--with-probe-buyer` opts in
to the human-handoff prefill with a synthetic buyer per market (the values the 2026-09-18 probe
used: a registered-agent address in Wilmington DE, 1 Raffles Place, 1-1 Marunouchi, 10 Downing
Street) and the placeholder mailbox ucp-probe@pivota.cc. With it, a market with no probe buyer
is refused, not given a foreign address.

WHAT IT CANNOT TELL YOU: whether the store ships the item. ELIGIBLE means "lands on a checkout
carrying our variant and click id", and heartpercent.us (no US delivery) passes it.
`shipping_verified` is always false; on the Reap path the shipping proof is Reap's quote.

There is no DB write. Concurrency is capped at 6. A TRANSPORT_ERROR is retried once — this
machine's local proxy flakes, and a transport failure is never proof a store is ineligible.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import Counter
from typing import Any, Awaitable, Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from services.outbound_links_service import CartPrefill  # noqa: E402
from services.shopify_cart_link_preflight import PreflightResult, Verdict, preflight  # noqa: E402

MAX_CONCURRENCY = 6
RETRY_DELAY_S = 2.0
PROBE_EMAIL = "ucp-probe@pivota.cc"

PROBE_BUYERS: Dict[str, CartPrefill] = {
    "US": CartPrefill(email=PROBE_EMAIL, first_name="Pivota", last_name="Probe",
                      address1="1209 Orange Street", city="Wilmington", province="DE",
                      zip="19801", country="US", phone="+12025550142"),
    "SG": CartPrefill(email=PROBE_EMAIL, first_name="Pivota", last_name="Probe",
                      address1="1 Raffles Place", city="Singapore", zip="048616",
                      country="SG", phone="+6561234567"),
    "JP": CartPrefill(email=PROBE_EMAIL, first_name="Pivota", last_name="Probe",
                      address1="1-1 Marunouchi", city="Chiyoda-ku", province="JP-13",
                      zip="100-0005", country="JP", phone="+81312345678"),
    "GB": CartPrefill(email=PROBE_EMAIL, first_name="Pivota", last_name="Probe",
                      address1="10 Downing Street", city="London", zip="SW1A 2AA",
                      country="GB", phone="+442071234567"),
}

PreflightFn = Callable[..., Awaitable[PreflightResult]]


def normalize_row(row: Any) -> Dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError("each input row must be an object")
    domain = str(row.get("domain") or "").strip()
    if not domain:
        raise ValueError("row without a domain")
    variant = row.get("variant_id", row.get("variant"))
    handle = row.get("product_handle")
    return {
        "domain": domain,
        "market": str(row.get("market") or "").strip().upper(),
        "variant_id": (str(variant).strip() or None) if variant is not None else None,
        "product_handle": (str(handle).strip() or None) if handle is not None else None,
    }


def click_id_for(domain: str, stamp: str) -> str:
    return f"clk_tierbpf_{stamp}_{re.sub(r'[^a-z0-9]+', '_', domain.lower()).strip('_')}"


async def run_rows(
    rows: List[Dict[str, Any]],
    *,
    use_buyer: bool = False,
    concurrency: int = MAX_CONCURRENCY,
    preflight_fn: PreflightFn = preflight,
    retry_delay_s: float = RETRY_DELAY_S,
    stamp: Optional[str] = None,
) -> List[Dict[str, Any]]:
    stamp = stamp or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    gate = asyncio.Semaphore(max(1, min(int(concurrency), MAX_CONCURRENCY)))

    async def one(row: Dict[str, Any]) -> Dict[str, Any]:
        buyer = PROBE_BUYERS.get(row["market"]) if use_buyer else None
        out: Dict[str, Any] = {**row, "buyer": ("probe_" + row["market"]) if buyer else None}
        if use_buyer and buyer is None:
            out.update(attempts=0, result={"host": row["domain"], "verdict": Verdict.INVALID_INPUT.value,
                                           "retryable": False, "detail": "no_probe_buyer_for_market",
                                           "shipping_verified": False})
            return out
        attempts = 0
        result: Optional[PreflightResult] = None
        async with gate:
            while attempts < 2:
                attempts += 1
                result = await preflight_fn(
                    row["domain"], market=row["market"], variant_id=row["variant_id"],
                    product_handle=row["product_handle"], quantity=1, buyer=buyer,
                    click_id=click_id_for(row["domain"], stamp),
                )
                if result.verdict is not Verdict.TRANSPORT_ERROR:
                    break
                if attempts < 2:
                    await asyncio.sleep(retry_delay_s)
        assert result is not None
        out.update(attempts=attempts, result=result.to_dict())
        return out

    return list(await asyncio.gather(*(one(r) for r in rows)))


def summary_line(entry: Dict[str, Any]) -> str:
    r = entry["result"]
    missing = ",".join(r.get("missing") or []) or "-"
    return (
        f"{entry['domain']:22} {entry['market'] or '-':3} {r['verdict']:26} "
        f"retry={'Y' if r.get('retryable') else 'n'} att={entry['attempts']} "
        f"variant={r.get('variant_id') or '-'}({r.get('variant_source') or '-'}) "
        f"final={r.get('final_status') or '-'}@{r.get('final_host') or '-'} "
        f"missing={missing} ship_verified={str(r.get('shipping_verified', False)).lower()} "
        f"detail={r.get('detail') or '-'}"
    )


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="JSON list of {domain, market, variant_id?, product_handle?}")
    ap.add_argument("--out", required=True, help="where to write the JSON report")
    ap.add_argument("--with-probe-buyer", action="store_true",
                    help="prefill a synthetic buyer per market (human-handoff shape); default: no buyer")
    ap.add_argument("--concurrency", type=int, default=MAX_CONCURRENCY, help="capped at 6")
    ap.add_argument("--only", action="append", default=None, metavar="DOMAIN",
                    help="restrict to these domains (repeatable)")
    args = ap.parse_args(argv)

    with open(args.input, encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, list):
        raise SystemExit("input must be a JSON list")
    rows = [normalize_row(r) for r in raw]
    if args.only:
        wanted = {d.strip().lower() for d in args.only}
        rows = [r for r in rows if r["domain"].lower() in wanted]

    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entries = asyncio.run(run_rows(rows, use_buyer=args.with_probe_buyer, concurrency=args.concurrency))
    report = {
        "generated_at": started,
        "buyer": "synthetic_probe_per_market" if args.with_probe_buyer else "none",
        "market_scoped_availability": True,
        "shipping_verified": False,
        "note": "HTTP-only preflight: shipping rates load via JS and are NOT checked.",
        "counts": dict(Counter(e["result"]["verdict"] for e in entries)),
        "rows": entries,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1, ensure_ascii=False)
    for entry in entries:
        print(summary_line(entry))
    print("counts", json.dumps(report["counts"], sort_keys=True))
    print("report", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
