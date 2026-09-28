"""Which served seeds sit on pages that name NO currency? Re-reads a sample; touches no database.

WHY. `resolve_external_offer` used to write USD (JPY for JP) when a page named no currency. It
now stores no price, and the seed refresh reports `skipped_unreadable` / `currency_unread`: the
row keeps its last price and goes stale. How many rows that is cannot be read from the database,
because nothing recorded whether a stored currency was read or invented. It can only be measured
by reading the pages again.

INPUT. The `CURRENCY_SAMPLE_JSON [...]` line of scripts/ops/external_seed_price_parse_census.py
(the line itself, or just its JSON, saved to a file): per host x market, the served rows whose
currency is the one the old code invented for that market (`default_shaped`), the offers their
read feeds, and up to three destination URLs.

WHAT IT DOES per URL, with the production extractor (the code in this image):
  _fetch_html (robots, per-host pacing, the crawl user agent) -> _extract_from_html ->
  snapshot_price_fields. Nothing is written: not the snapshot table, not the seed.
and classifies the page:
  currency_named        the page states a currency, and it is the stored one: unaffected
  currency_named_other  the page states a DIFFERENT currency: the stored one was invented and
                        wrong (the refresh already refused these as `skipped_currency_mismatch`)
  no_currency           the page has a price and names no currency: THE COHORT. These rows used
                        to be refreshed in the invented currency and are now `skipped_unreadable`
  no_price              the page has no price at all (unaffected: `unavailable` either way)
  fetch_failed          not answered (unaffected by the rule; it could not refresh anyway)

OUTPUT per host: the classes, then an ESTIMATE of rows and offers on no-currency pages:
default_shaped x (no_currency / pages read). A host renders every product page from one theme, so
the sample is usually all-or-nothing. `all_sampled_no_currency` marks the hosts where it is.
Then a `PROBE_JSON {...}` line.

RUN IT from any machine that can reach the merchants. It needs no credentials:
    python scripts/ops/external_seed_currency_probe.py sample.json
As a prod one-off it needs the crawl subnet (SUBNET=pivota-crawl); see run_oneoff_job.sh.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

try:
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
except NameError:
    _REPO_ROOT = "/app"
sys.path.insert(0, _REPO_ROOT)

CLASSES = ("currency_named", "currency_named_other", "no_currency", "no_price", "fetch_failed")
HOST_CONCURRENCY = 6
SAMPLE_PREFIX = "CURRENCY_SAMPLE_JSON "


def load_sample(text: str) -> List[Dict[str, Any]]:
    text = text.strip()
    if text.startswith(SAMPLE_PREFIX):
        text = text[len(SAMPLE_PREFIX):]
    data = json.loads(text)
    if not isinstance(data, list):
        raise SystemExit("the sample must be the JSON list from CURRENCY_SAMPLE_JSON")
    return [d for d in data if isinstance(d, dict) and d.get("urls")]


def default_currency_for(market: Any) -> str:
    """What `resolve_external_offer` wrote for a page with no currency, before the fix."""
    return "JPY" if str(market or "US").strip().upper() == "JP" else "USD"


def classify_page(extracted: Dict[str, Any], stored_currency: str) -> Dict[str, Any]:
    """Pure: one page's class, from one `_extract_from_html` result."""
    from services.external_offers_service import snapshot_price_fields

    _amount, currency, price_read = snapshot_price_fields(extracted)
    has_price_text = price_read.get("raw") is not None
    if currency:
        cls = "currency_named" if currency == stored_currency else "currency_named_other"
    elif has_price_text:
        cls = "no_currency"
    else:
        cls = "no_price"
    return {"class": cls, "currency": currency, "status": price_read.get("status"), "raw": price_read.get("raw")}


async def observe(url: str, stored_currency: str, fetch: Callable[[str], Awaitable[str]]) -> Dict[str, Any]:
    from services.external_offers_service import _extract_from_html

    try:
        html = await fetch(url)
    except Exception as exc:  # noqa: BLE001 - any failure to read is one outcome, not a crash
        return {"url": url, "class": "fetch_failed", "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    return {"url": url, **classify_page(_extract_from_html(url, html), stored_currency)}


def summarise(sample: Sequence[Dict[str, Any]], observations: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Pure: per-host classes and estimates. `observations` is keyed by `host|market`."""
    hosts: List[Dict[str, Any]] = []
    totals: Counter = Counter()
    for entry in sample:
        key = f"{entry['host']}|{entry.get('market')}"
        seen = observations.get(key, [])
        classes = Counter(o["class"] for o in seen)
        read = sum(classes[c] for c in CLASSES if c != "fetch_failed")
        share = classes["no_currency"] / read if read else None
        rows = int(entry.get("default_shaped") or 0)
        offers = int(entry.get("default_shaped_attached_offers") or 0)
        est_rows = round(rows * share) if share is not None else None
        est_offers = round(offers * share) if share is not None else None
        hosts.append({
            "host": entry["host"], "market": entry.get("market"),
            "default_shaped": rows, "default_shaped_attached_offers": offers,
            **{c: classes[c] for c in CLASSES},
            "all_sampled_no_currency": bool(read) and classes["no_currency"] == read,
            "est_rows_no_currency": est_rows, "est_offers_no_currency": est_offers,
            "statuses": dict(Counter(str(o.get("status")) for o in seen if o["class"] != "fetch_failed")),
        })
        totals["hosts"] += 1
        totals["default_shaped"] += rows
        totals["default_shaped_attached_offers"] += offers
        for c in CLASSES:
            totals[c] += classes[c]
        if read == 0:
            totals["hosts_unread"] += 1
            totals["rows_on_unread_hosts"] += rows
        else:
            totals["est_rows_no_currency"] += est_rows or 0
            totals["est_offers_no_currency"] += est_offers or 0
            if classes["no_currency"]:
                totals["hosts_with_no_currency_pages"] += 1
    hosts.sort(key=lambda h: (-(h["est_rows_no_currency"] or 0), -h["default_shaped"], h["host"]))
    return {"totals": dict(totals), "hosts": hosts}


def render(summary: Dict[str, Any]) -> List[str]:
    t = summary["totals"]
    lines = [
        "== pages that name no currency (rows/offers are estimates: default_shaped x no_currency share)",
        json.dumps(t, sort_keys=True),
        f"{'host':<36} {'mkt':<4} {'rows':>6} {'offers':>6} {'named':>5} {'other':>5} {'none':>5} "
        f"{'noprc':>5} {'fail':>4} {'est_rows':>8} {'est_offers':>10}",
    ]
    for h in summary["hosts"]:
        lines.append(
            f"{h['host']:<36} {str(h['market']):<4} {h['default_shaped']:>6} {h['default_shaped_attached_offers']:>6} "
            f"{h['currency_named']:>5} {h['currency_named_other']:>5} {h['no_currency']:>5} {h['no_price']:>5} "
            f"{h['fetch_failed']:>4} {str(h['est_rows_no_currency']):>8} {str(h['est_offers_no_currency']):>10}"
        )
    return lines


async def probe(
    sample: Sequence[Dict[str, Any]],
    fetch: Callable[[str], Awaitable[str]],
    *,
    host_concurrency: int = HOST_CONCURRENCY,
) -> Dict[str, List[Dict[str, Any]]]:
    """Hosts in parallel, each host's URLs one at a time (the crawl's own pacing also applies)."""
    gate = asyncio.Semaphore(max(1, host_concurrency))
    out: Dict[str, List[Dict[str, Any]]] = {}

    async def one_host(entry: Dict[str, Any]) -> None:
        async with gate:
            stored = default_currency_for(entry.get("market"))
            out[f"{entry['host']}|{entry.get('market')}"] = [
                await observe(url, stored, fetch) for url in entry["urls"]
            ]

    await asyncio.gather(*(one_host(e) for e in sample))
    return out


async def _production_fetch(url: str) -> str:
    from services.external_offers_service import _fetch_html

    html, _content_type = await _fetch_html(url)
    return html


async def _main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("sample", help="file holding the census's CURRENCY_SAMPLE_JSON line or its JSON")
    parser.add_argument("--max-hosts", type=int, default=0, help="probe only the N largest hosts (0 = all)")
    args = parser.parse_args(argv)
    with open(args.sample) as fh:
        sample = load_sample(fh.read())
    if args.max_hosts:
        sample = sample[: args.max_hosts]
    observations = await probe(sample, _production_fetch)
    summary = summarise(sample, observations)
    for line in render(summary):
        print(line)
    print("PROBE_JSON " + json.dumps({**summary, "observations": observations}, default=str, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
