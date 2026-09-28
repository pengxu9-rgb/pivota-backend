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

RUN IT as one prod one-off that takes its own sample (--from-db) and fetches from the crawl subnet:
    PROBE=1 IMAGE=us-west1-docker.pkg.dev/pivota-shared/pivota/backend:<merged sha> \\
        bash scripts/ops/run_price_parse_census.sh
The launcher refuses 04:15-06:15Z and a busy pivota-pg (CPU >= 35% over 5 min), forces
SUBNET=pivota-crawl and a 60-minute task timeout. `--from-db` reads the database ONLY through the
census's `collect` (READ ONLY, 30s statement timeout, application_name, index-build and second-copy
refusals) and closes the connection before the first fetch; it refuses 04:15-06:15Z itself too.
The fetch is paced GLOBALLY (GLOBAL_INTERVAL between any two requests, across hosts), because the
crawl NAT trips a cross-domain 429 at ~50 requests over 37 domains a minute, and stops starting new
fetches after DEADLINE_MINUTES (the rest are `not_attempted`, in no share).

Or from any machine that can reach the merchants, on a saved census output (no credentials):
    python scripts/ops/external_seed_currency_probe.py census_output.txt
(A laptop egress is likely to be 429'd wholesale: 2026-09-28, 865 of 930 pages.)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from collections import Counter
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

try:
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
except NameError:
    _REPO_ROOT = "/app"
sys.path.insert(0, _REPO_ROOT)

CLASSES = ("currency_named", "currency_named_other", "no_currency", "no_price", "fetch_failed", "not_attempted")
# Pages this run did not read, for whatever reason: excluded from every share.
UNREAD_CLASSES = frozenset({"fetch_failed", "not_attempted"})
HOST_CONCURRENCY = 2
# One request at most every GLOBAL_INTERVAL seconds ACROSS hosts. The crawl NAT is shared with the
# nightly refresh, and ~50 requests over 37 Cloudflare-fronted domains in ~1 minute trips a
# cross-domain IP-level 429 for ~15 minutes (infra/gcp/setup_egress_nat.sh); per-host pacing
# alone does not prevent that. 930 URLs at 1.5s is ~25 minutes.
GLOBAL_INTERVAL = 1.5
# No new fetch after this many minutes; the rest are `not_attempted`. With the start window below,
# a run can never reach the 05:15Z refresh.
DEADLINE_MINUTES = 45
# `--from-db` refuses to START inside [04:15, 06:15) UTC: DEADLINE_MINUTES before the refresh
# window, and the window itself.
NO_START_UTC = ((4, 15), (6, 15))
SAMPLE_PREFIX = "CURRENCY_SAMPLE_JSON "
_PART_RE = re.compile(r"CURRENCY_SAMPLE_JSON_PART (\d+)/(\d+) (\[.*\])\s*$")


def load_sample(text: str) -> List[Dict[str, Any]]:
    """The census sample, from any of: the census's whole output (its `CURRENCY_SAMPLE_JSON_PART
    i/n` lines, in any order, with or without a log prefix), the old single `CURRENCY_SAMPLE_JSON`
    line, or the bare JSON list. A missing or duplicated part is refused, never probed around."""
    parts: Dict[int, list] = {}
    totals = set()
    for line in text.splitlines():
        m = _PART_RE.search(line)
        if m:
            i, n = int(m.group(1)), int(m.group(2))
            if i in parts:
                raise SystemExit(f"CURRENCY_SAMPLE_JSON_PART {i}/{n} appears twice")
            parts[i] = json.loads(m.group(3))
            totals.add(n)
    if parts:
        if len(totals) != 1 or sorted(parts) != list(range(1, totals.pop() + 1)):
            raise SystemExit(f"incomplete sample: have parts {sorted(parts)} of {sorted(totals) or '?'}")
        data: Any = [item for i in sorted(parts) for item in parts[i]]
    else:
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
        read = sum(classes[c] for c in CLASSES if c not in UNREAD_CLASSES)
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
    global_interval: float = 0.0,
    deadline: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> Dict[str, List[Dict[str, Any]]]:
    """Hosts in parallel, each host's URLs one at a time (the crawl's own per-host pacing also
    applies), and at most one request every `global_interval` seconds across all hosts. Past
    `deadline` (a `clock()` value) no new fetch starts; those URLs are `not_attempted`."""
    gate = asyncio.Semaphore(max(1, host_concurrency))
    pace = asyncio.Lock()
    last = [float("-inf")]
    out: Dict[str, List[Dict[str, Any]]] = {}

    async def take_slot() -> bool:
        """Wait for this request's turn; False when the turn comes at or past the deadline (the
        check is AFTER the wait, or a queued request would start late)."""
        async with pace:
            wait = last[0] + global_interval - clock()
            if wait > 0:
                await sleep(wait)
            if deadline is not None and clock() >= deadline:
                return False
            last[0] = clock()
            return True

    async def one_host(entry: Dict[str, Any]) -> None:
        async with gate:
            stored = default_currency_for(entry.get("market"))
            seen: List[Dict[str, Any]] = []
            for url in entry["urls"]:
                if not await take_slot():
                    seen.append({"url": url, "class": "not_attempted"})
                    continue
                seen.append(await observe(url, stored, fetch))
            out[f"{entry['host']}|{entry.get('market')}"] = seen

    await asyncio.gather(*(one_host(e) for e in sample))
    return out


def start_refusal(now: datetime) -> Optional[str]:
    """Why `--from-db` must not start at `now` (UTC), or None."""
    (h0, m0), (h1, m1) = NO_START_UTC
    minutes = now.hour * 60 + now.minute
    if h0 * 60 + m0 <= minutes < h1 * 60 + m1:
        return (
            f"refusing: {now:%H:%M}Z is inside {h0:02d}:{m0:02d}-{h1:02d}:{m1:02d}Z "
            "(the 05:15-06:15Z refresh window, less this run's deadline)"
        )
    return None


async def _production_fetch(url: str) -> str:
    from services.external_offers_service import _fetch_html

    html, _content_type = await _fetch_html(url)
    return html


async def sample_from_db(dsn: str) -> List[Dict[str, Any]]:
    """The census's own sample, by the census's own read: one READ ONLY transaction with its
    statement timeout, application_name and index-build / second-copy refusals. The connection is
    closed BEFORE any page is fetched, so the crawl never holds a database session."""
    import asyncpg

    census = _census_module()
    conn = await asyncpg.connect(dsn)
    try:
        summary, suspects, sample = await census.collect(conn)
    finally:
        await conn.close()
    for line in census.render(summary):
        print(line)
    for line in census.output_lines(summary, suspects, sample):
        print(line)
    return list(sample)


async def _main(argv: Optional[Sequence[str]] = None, *, now: Optional[datetime] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("sample", nargs="?", help="the census output (or its CURRENCY_SAMPLE_JSON lines, or the bare JSON)")
    source.add_argument("--from-db", action="store_true", help="take the sample by running the census read (DATABASE_URL)")
    parser.add_argument("--max-hosts", type=int, default=0, help="probe only the N largest hosts (0 = all)")
    parser.add_argument("--out", help="also write the full result, every page's observation included, here")
    parser.add_argument("--global-interval", type=float, default=GLOBAL_INTERVAL, help="seconds between any two requests")
    parser.add_argument("--deadline-minutes", type=float, default=DEADLINE_MINUTES, help="no new fetch after this")
    args = parser.parse_args(argv)
    if args.from_db:
        refusal = start_refusal(now or datetime.now(timezone.utc))
        if refusal:
            print(refusal, file=sys.stderr)
            return 2
        sample = await sample_from_db(os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://"))
    else:
        with open(args.sample) as fh:
            sample = load_sample(fh.read())
    if args.max_hosts:
        sample = sample[: args.max_hosts]
    observations = await probe(
        sample,
        _production_fetch,
        global_interval=args.global_interval,
        deadline=time.monotonic() + args.deadline_minutes * 60,
    )
    summary = summarise(sample, observations)
    for line in render(summary):
        print(line)
    for line in output_lines(summary):
        print(line)
    if args.out:
        with open(args.out, "w") as fh:
            json.dump({**summary, "observations": observations}, fh, default=str)
    return 0


def output_lines(summary: Dict[str, Any]) -> List[str]:
    """`PROBE_JSON {totals}` and `PROBE_HOSTS_JSON_PART i/n [...]`, each under the census's line
    cap (one line of every host overflowed Cloud Logging's 102,400-character cut)."""
    census = _census_module()
    return [
        "PROBE_JSON " + json.dumps({"totals": summary["totals"]}, default=str, separators=(",", ":")),
        *census.json_part_lines("PROBE_HOSTS_JSON", summary["hosts"]),
    ]


def _census_module():
    import importlib.util

    path = os.path.join(_REPO_ROOT, "scripts", "ops", "external_seed_price_parse_census.py")
    spec = importlib.util.spec_from_file_location("external_seed_price_parse_census", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
