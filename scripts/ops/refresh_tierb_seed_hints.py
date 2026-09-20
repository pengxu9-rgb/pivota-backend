"""Operator tool: refresh the `product_handle` hints in config/tierb_cart_link_merchants.json.

    python scripts/ops/refresh_tierb_seed_hints.py                      # print the plan
    python scripts/ops/refresh_tierb_seed_hints.py --write              # also rewrite the file
    python scripts/ops/refresh_tierb_seed_hints.py --write --replace-gone --only robinsons.com.sg
    python scripts/ops/refresh_tierb_seed_hints.py --write --replace-unfit

WHAT IT DOES. For every row that names a `variant_id`:

  1. GET https://<domain>/variants/<id>, following redirects BY HAND. Shopify answers an existing
     variant with a redirect to `/products/<handle>?variant=<id>`, and a variant that no longer
     exists with 404. (metro.com.sg/variants/50755485991233 -> 302 to
     /products/mac-m-a-cximal-matte-silky-lipstick?variant=50755485991233; robinsons'
     40975353675861 -> 404, verified 2026-09-18.)
  2. Confirm the handle: GET /products/<handle>.js?country=<market> must list the variant, and
     its `available` flag for that market is reported (never acted on — an unavailable variant
     is a finding for a human, as podl's was).
  3. A variant that is GONE is reported. With `--replace-gone` a replacement is picked from
     /products.json?country=<market> (at most `--max-pages` pages): an available, shipped, priced
     (>= 5) variant of a product whose title contains none of "test" / "sample" / "gift",
     preferring, in order, a MAC lipstick, then any lip product (the Meitu try-on line is lips),
     then anything else that qualifies. A replacement must also look full-size: none of
     "mini" / "travel" / "sachet" / "kit" in its title or handle either.
  4. With `--replace-unfit`, a seed that resolves fine but whose title or handle says it is a
     test / sample / gift / trial, a sachet / travel size / mini, a duplicated `copy` / `사본`
     listing, or short-dated (`Exp 01/28`) is swapped the same way: the eligibility probe should
     exercise the full-size product a buyer would actually purchase.

WHAT IT NEVER DOES. It never follows a cart permalink and never touches /cart or /checkouts, so
it creates NO checkouts. It may request ONLY /variants/<id>, /products/<handle>[.js] and
/products.json, optionally under one locale segment; anything else, a redirect target included,
is refused before it is sent. Every request is a read-only GET of a public storefront JSON or redirect,
paced through the same global limiter as the eligibility job (>= 1.5 s between request starts).
Run it from a laptop or the crawl subnet — never from the worker (the payment-allowlisted NAT).

Rows without a `variant_id` are merchant-level probes and are left alone.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from jobs.tierb_cart_link_eligibility import (  # noqa: E402
    PacedTransport,
    RequestPacer,
    _default_inner_transport,
)
from services.tierb_cart_link_merchants import DEFAULT_MERCHANTS_PATH, parse_merchants  # noqa: E402

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
MAX_HOPS = 5
_PRODUCT_PATH = re.compile(r"/products/([^/?#]+)")
# A seed whose product title or handle carries one of these is not something a buyer would
# actually purchase (a tester, a sample, a gift-with-purchase, a trial kit): `--replace-unfit`
# swaps it. A REPLACEMENT must additionally look full-size.
#   * a tester / sample / gift / trial: not sold as the product;
#   * a sachet, a travel size, a mini: sold, but not the full-size product a buyer asks for
#     (mixsoon's 1.5 ml sachet, murad's travel size — review of #2213);
#   * `copy` / `사본` ("copy" in Korean): a duplicated listing (`...-copy`, `...-사본` on skin1004,
#     lador, mealit), which a merchant can delete or leave stale at any time.
_UNFIT_WORDS = ("test", "tester", "sample", "gift", "trial", "sachet", "travel", "mini", "copy", "사본")
_EXCLUDED_WORDS = _UNFIT_WORDS + ("kit",)
# WHOLE WORDS (an optional plural s), never substrings: "luminizer" is not "mini", "latest" is
# not "test", "copycat" is not "copy". Matched against the title and the handle, split on
# anything that is not a letter or digit in ANY script (so `사본` survives the split).
_UNFIT_RE = re.compile(r"\b(?:" + "|".join(_UNFIT_WORDS) + r")s?\b")
_EXCLUDED_RE = re.compile(r"\b(?:" + "|".join(_EXCLUDED_WORDS) + r")s?\b")
# A SHORT-DATED listing: "(Exp 01/28)", "EXP: 2026-03", "Expiry 03.2027" (pupsik.sg). Read on the
# raw title, because the date's punctuation is the signal.
_EXPIRY_RE = re.compile(r"\bexp(?:iry|ires|iration)?\b\.?\s*[:\-]?\s*\d{1,4}\s*[/.\-]\s*\d{1,4}", re.IGNORECASE)


# A LIP product by whole word, not by substring: "Advanced Liposomal NMN" (haroutine, live
# 2026-09-18) is a supplement, and a substring match ranked it as a lip product.
_LIP_RE = re.compile(r"\blip(?:s|stick|sticks|gloss|balm|tint|liner)?\b")
_LIPSTICK_RE = re.compile(r"\blipsticks?\b")


def _words(*parts: Any) -> str:
    return " ".join(re.split(r"[\W_]+", " ".join(str(p or "") for p in parts).lower()))


# THE ONLY PATHS THIS SCRIPT MAY REQUEST, with at most one leading locale segment (`/en-us`,
# `/ja`, `/en-sg`). An ALLOWLIST, not a denylist: the earlier guard refused paths STARTING with
# /cart or /checkouts, so a redirect to `/en-us/cart/123:1` or `/<shop_id>/checkouts/...` passed
# (review of #2213). Anything not listed here is refused, and the check runs on every request the
# client sends, so it holds after redirects too.
_ALLOWED_PATH = re.compile(
    r"^(?:/[a-z]{2}(?:-[a-z0-9]{2,4})?)?"
    r"/(?:variants/[0-9]+|products\.json|products/[^/]+)$"
)


def path_allowed(path: str) -> bool:
    return bool(_ALLOWED_PATH.fullmatch(path or ""))


class ReadOnlyTransport(httpx.AsyncBaseTransport):
    """Refuses anything but a GET of an allowlisted storefront read (`_ALLOWED_PATH`). The
    script's own code never builds anything else; this makes a redirect that tries it fail
    loudly too."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "GET" or not path_allowed(request.url.path):
            raise RuntimeError(f"refused non-read-only request: {request.method} {request.url.host}{request.url.path}")
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def handle_from_url(url: str) -> Optional[str]:
    match = _PRODUCT_PATH.search(urlparse(url).path)
    return unquote(match.group(1)) if match else None


async def variant_redirect(client: httpx.AsyncClient, domain: str, variant_id: str) -> Tuple[str, Optional[str]]:
    """('found', handle) | ('gone', None) | ('unknown:<why>', None), from /variants/<id>."""
    url = f"https://{domain}/variants/{variant_id}"
    for _ in range(MAX_HOPS):
        response = await client.get(url, headers=HEADERS)
        if response.status_code == 404:
            return "gone", None
        if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
            url = urljoin(str(response.url), response.headers["location"])
            handle = handle_from_url(url)
            if handle:
                return "found", handle
            continue
        return f"unknown:status_{response.status_code}", None
    return "unknown:too_many_redirects", None


async def confirm_handle(
    client: httpx.AsyncClient, domain: str, handle: str, variant_id: str, market: str
) -> Tuple[bool, Optional[bool], Optional[str]]:
    """(variant listed on the product, available in `market`, product title)."""
    response = await client.get(
        f"https://{domain}/products/{handle}.js", params={"country": market}, headers=HEADERS,
        follow_redirects=True,
    )
    if response.status_code != 200:
        return False, None, None
    try:
        payload = response.json()
    except ValueError:
        return False, None, None
    for variant in payload.get("variants") or []:
        if str(variant.get("id")) == str(variant_id):
            return True, bool(variant.get("available")), payload.get("title")
    return False, None, payload.get("title")


def is_unfit(title: Optional[str], handle: Optional[str]) -> bool:
    return bool(_UNFIT_RE.search(_words(title, handle)) or _EXPIRY_RE.search(str(title or "")))


def _qualifies(product: Dict[str, Any], variant: Dict[str, Any]) -> bool:
    try:
        price = float(variant.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    return (
        variant.get("available") is True
        and variant.get("requires_shipping", True) is not False
        and price >= 5
        and not _EXCLUDED_RE.search(_words(product.get("title"), product.get("handle")))
        and not _EXPIRY_RE.search(str(product.get("title") or ""))
    )


def _preference(product: Dict[str, Any]) -> int:
    """0 = a MAC lipstick, 1 = any lip product, 2 = anything else that qualifies."""
    vendor = str(product.get("vendor") or "").lower().replace("·", "").replace(".", "").replace(" ", "")
    text = _words(product.get("title"), product.get("product_type"))
    if vendor.startswith("mac") and _LIPSTICK_RE.search(text):
        return 0
    if _LIP_RE.search(text):
        return 1
    return 2


# How many ranked candidates a swap may try to CONFIRM before giving up. A storefront can list a
# product in /products.json whose /products/<handle>.js 404s (skin1004's vitamin-c-96-powder,
# 2026-09-19), so the best-ranked candidate is not always usable.
MAX_CANDIDATES = 5


async def rank_replacements(
    client: httpx.AsyncClient, domain: str, market: str, max_pages: int
) -> List[Dict[str, Any]]:
    """Every qualifying product (its first qualifying variant), best first: rank (MAC lipstick,
    lip, any), then catalog order. Stops early once MAX_CANDIDATES rank-0 candidates are in hand."""
    found: List[Tuple[int, int, Dict[str, Any]]] = []
    order = 0
    for page in range(1, max_pages + 1):
        response = await client.get(
            f"https://{domain}/products.json", params={"limit": 250, "page": page, "country": market},
            headers=HEADERS, follow_redirects=True,
        )
        if response.status_code != 200:
            break
        try:
            products = response.json().get("products") or []
        except ValueError:
            break
        if not products:
            break
        for product in products:
            for variant in product.get("variants") or []:
                if not _qualifies(product, variant):
                    continue
                rank = _preference(product)
                found.append((rank, order, {"variant_id": str(variant["id"]), "product_handle": product.get("handle"),
                                            "title": product.get("title"), "vendor": product.get("vendor"),
                                            "rank": rank}))
                order += 1
                break  # one variant per product is enough to rank it
        if sum(1 for r, _, _ in found if r == 0) >= MAX_CANDIDATES:
            break
        if len(products) < 250:
            break
    return [c for _, _, c in sorted(found, key=lambda t: (t[0], t[1])) if c.get("product_handle")]


async def pick_replacement(
    client: httpx.AsyncClient, domain: str, market: str, max_pages: int
) -> Optional[Dict[str, Any]]:
    ranked = await rank_replacements(client, domain, market, max_pages)
    return ranked[0] if ranked else None


def dump_rows(rows: List[Dict[str, Any]]) -> str:
    ordered = []
    for row in rows:
        ordered.append({k: row[k] for k in ("domain", "market", "variant_id", "product_handle") if row.get(k)})
    return "[\n" + ",\n".join("  " + json.dumps(r, ensure_ascii=False) for r in ordered) + "\n]\n"


async def _replace(client: httpx.AsyncClient, row: Dict[str, Any], entry: Dict[str, Any],
                   reason: str, max_pages: int) -> Optional[str]:
    """Swap the row's variant for the best candidate that CONFIRMS (listed on its own
    /products/<handle>.js and available in the row's market), trying at most MAX_CANDIDATES in
    rank order; the new handle, or None if none does."""
    candidates = await rank_replacements(client, row["domain"], row["market"], max_pages)
    entry["candidates_tried"] = []
    for candidate in candidates[:MAX_CANDIDATES]:
        listed, available, _title = await confirm_handle(
            client, row["domain"], candidate["product_handle"], candidate["variant_id"], row["market"])
        entry["candidates_tried"].append(candidate["product_handle"])
        if listed and available:
            entry["replacement"] = candidate
            entry["replaced_variant_id"], entry["replaced_because"] = row["variant_id"], reason
            row["variant_id"] = entry["variant_id"] = candidate["variant_id"]
            entry["lookup"] = "replaced"
            return candidate["product_handle"]
    entry["replacement"] = None
    return None


async def refresh(rows: List[Dict[str, Any]], *, only: Optional[List[str]], replace_gone: bool,
                  max_pages: int, replace_unfit: bool = False) -> List[Dict[str, Any]]:
    report: List[Dict[str, Any]] = []
    pacer = RequestPacer()
    transport = ReadOnlyTransport(PacedTransport(_default_inner_transport(), pacer))
    async with httpx.AsyncClient(transport=transport, timeout=30.0, follow_redirects=False) as client:
        for row in rows:
            if not row.get("variant_id") or (only and row["domain"] not in only):
                continue
            entry: Dict[str, Any] = {"domain": row["domain"], "market": row["market"], "variant_id": row["variant_id"],
                                     "gone_variant_id": None}
            # A SWAP IS ALL OR NOTHING. `_replace` moves the row to the new variant before its
            # handle is confirmed; if the confirmation fails (a 404, a proxy flake mid-read), the
            # row must go back to what it was — measured live 2026-09-19: skin1004's swap lost its
            # confirm to a ConnectError and the row was written with the NEW variant and the OLD
            # handle, a pair that exists nowhere.
            original = dict(row)
            try:
                status, handle = await variant_redirect(client, row["domain"], row["variant_id"])
                entry["lookup"] = status
                if status == "gone":
                    entry["gone_variant_id"] = row["variant_id"]
                if status == "gone" and replace_gone:
                    handle = await _replace(client, row, entry, "gone", max_pages) or handle
                if handle:
                    listed, available, title = await confirm_handle(
                        client, row["domain"], handle, row["variant_id"], row["market"])
                    if listed and replace_unfit and is_unfit(title, handle):
                        entry.update(unfit_title=title, unfit_handle=handle)
                        new_handle = await _replace(client, row, entry, "unfit", max_pages)
                        if new_handle:
                            handle = new_handle
                            listed, available, title = await confirm_handle(
                                client, row["domain"], handle, row["variant_id"], row["market"])
                    entry.update(handle=handle, listed=listed, available=available, title=title)
                    if listed:
                        row["product_handle"] = handle
                    else:
                        entry["lookup"] = f"{entry['lookup']}:handle_does_not_list_variant"
            except httpx.HTTPError as exc:
                entry["lookup"] = f"unknown:{type(exc).__name__}"
            if row.get("variant_id") != original.get("variant_id") and not (
                entry.get("lookup") == "replaced" and entry.get("listed")
            ):
                row.clear()
                row.update(original)
                entry["variant_id"] = row["variant_id"]
                entry["rolled_back"] = True
            report.append(entry)
            print(json.dumps(entry, ensure_ascii=False), flush=True)
    return report


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=str(DEFAULT_MERCHANTS_PATH))
    ap.add_argument("--write", action="store_true", help="rewrite the file with the resolved hints")
    ap.add_argument("--replace-gone", action="store_true", help="pick a replacement for a variant that 404s")
    ap.add_argument("--replace-unfit", action="store_true",
                    help="swap a seed whose product is a test/sample/gift/trial for a full-size one")
    ap.add_argument("--max-pages", type=int, default=40, help="catalog pages to scan for a replacement")
    ap.add_argument("--only", action="append", default=None, metavar="DOMAIN")
    args = ap.parse_args(argv)

    path = Path(args.file)
    rows = json.loads(path.read_text(encoding="utf-8"))
    parse_merchants(rows)  # refuse to start on a malformed list
    report = asyncio.run(refresh(rows, only=args.only, replace_gone=args.replace_gone, max_pages=args.max_pages,
                                 replace_unfit=args.replace_unfit))
    parse_merchants(rows)  # and refuse to write one
    unresolved = [e for e in report if e["lookup"] not in ("found", "replaced") or not e.get("listed")]
    print(f"resolved {len(report) - len(unresolved)}/{len(report)}; unresolved: "
          f"{[(e['domain'], e['lookup']) for e in unresolved]}", flush=True)
    if args.write:
        path.write_text(dump_rows(rows), encoding="utf-8")
        print(f"wrote {path}", flush=True)
    return 1 if unresolved else 0


if __name__ == "__main__":
    raise SystemExit(main())
