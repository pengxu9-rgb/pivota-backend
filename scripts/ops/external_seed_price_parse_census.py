"""Which SERVED seed prices look misread, and which seed currencies were never read? Read-only.

WHY. Until utils/crawled_price.py, the crawl read a price by keeping digits and dots:

  "28,80"      -> 2880      (x100: every comma-decimal page -- EU, SE, parts of CA/CH)
  "2 400,00 €" -> 240000    (x100)
  "1.234,56"   -> 1.23456   (/1000)

and `resolve_external_offer` wrote USD (JPY in the JP market) for a page that stated no currency.
Both reached `external_product_seeds.price_amount` / `price_currency` through the refresh and at
seed creation. The raw price text was never stored, so the misreads can only be found by what
they look like. This census sizes them per host and market.

SIGNALS, per active seed (the seed lane's served filter, imported from the freshness census):

  STRONG (a suspect -- each is a pattern the old parser produced and a correct read almost never
  does):
    peer_x100       price / median(peers) in [60, 160]. Peers: other active seeds on the same
                    attached product, the attached product's catalog_offers (not the
                    `external_seed` merchant's, which were projected from seeds), and seeds with
                    the same normalised title on OTHER hosts; all in the same currency.
    peer_div1000    price / median(peers) in [1/1600, 1/600]: the "1.234,56" -> 1.23456 shape.
    variant_x100    product price / median(its own stored variant prices), same bands. Variant
    variant_div1000 prices come from JSON-LD offers, which are usually machine-formatted, while
                    the product price often fell back to the meta tag EU themes format.
    excess_decimals more than 2 decimals in a 2-decimal currency, any fraction in a zero-decimal
                    one (1.23456 EUR, 2.4 JPY).
    raw_comma_x100  a price TEXT still stored in seed_data (price / variants[].price as a string)
                    that reads, with the new parser, to exactly price/100.
  WEAK (context, never a suspect alone):
    peer_outlier    ratio >= 8 or <= 1/8 outside the bands above
    cents_integer   an integer >= 1000 in a 2-decimal currency whose last two digits are not 00
    above_ceiling   above a per-currency ceiling for a beauty product

CURRENCY NEVER READ. No stored field records whether a currency was read. The census counts:
  default_shaped  the stored currency is the one `resolve_external_offer` invented for the row's
                  market partition (USD, or JPY for JP)
  contradicted    ...and one of the seed's own stored variants carries a DIFFERENT currency
                  (a variant currency was always read from the offer or the page)
  tld_mismatch    ...and the destination's country TLD names another currency (.de -> EUR,
                  .co.uk -> GBP, .jp -> JPY, ...): likely invented
  uncorroborated  ...and no stored variant carries any currency: cannot be told apart
Default-shaped rows are an upper bound; contradicted + tld_mismatch is the likely count.

SAFETY. One READ ONLY transaction, `SET LOCAL statement_timeout = '30s'`, three statements, none
re-executed. Before reading, it refuses to run while an index build is in progress
(pg_stat_progress_create_index) or while this census already runs elsewhere. pivota-pg has
2 vCPU and serves live search: START IT ONLY WHILE CPU IS UNDER 35%, and never during the
05:15-06:15Z refresh. `scripts/ops/run_price_parse_census.sh` enforces both before it launches.

RUN IT (it fetches nothing, so no crawl subnet):

    bash scripts/ops/run_price_parse_census.sh

Output: a text report, then `CENSUS_JSON {...}` and `SUSPECTS_JSON [...]` lines (prefixed so they
stay textPayload). SUSPECTS_JSON is the candidate list a correction pass would start from; it is
not a manifest and nothing reads it automatically.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
except NameError:
    _REPO_ROOT = "/app"
sys.path.insert(0, _REPO_ROOT)

X100_BAND = (60.0, 160.0)
DIV1000_BAND = (1 / 1600.0, 1 / 600.0)
OUTLIER_RATIO = 8.0
TOP_HOSTS = 40
MAX_SUSPECTS_LISTED = 300
APPLICATION_NAME = "external_seed_price_parse_census"

# Per-currency ceilings for one beauty product. Context only (a weak signal).
CEILINGS = {
    "USD": 1000, "EUR": 1000, "GBP": 1000, "AUD": 1500, "CAD": 1500, "SGD": 1500, "CHF": 1000,
    "HKD": 8000, "SEK": 10000, "NOK": 10000, "DKK": 8000, "JPY": 150000, "KRW": 1500000,
}

# Country TLD -> the currency a shop there prices in. Only unambiguous ones.
TLD_CURRENCY = {
    "de": "EUR", "fr": "EUR", "it": "EUR", "es": "EUR", "nl": "EUR", "at": "EUR", "be": "EUR",
    "ie": "EUR", "fi": "EUR", "pt": "EUR", "gr": "EUR", "hr": "EUR", "sk": "EUR", "si": "EUR",
    "lu": "EUR", "ee": "EUR", "lv": "EUR", "lt": "EUR",
    "uk": "GBP", "jp": "JPY", "kr": "KRW", "au": "AUD", "se": "SEK", "no": "NOK", "dk": "DKK",
    "ca": "CAD", "ch": "CHF", "sg": "SGD", "hk": "HKD", "nz": "NZD", "pl": "PLN",
}

_COMMA_DECIMAL_TEXT = re.compile(r"\d,\d{1,2}(?!\d)")


def _comma_decimal_only(text: Any, currency: Optional[str] = None) -> Any:
    """Stand-in for utils.crawled_price when the image predates it (a run before the fix merges).

    Used ONLY by the raw-text signal, and only answers the one shape that signal asks about: a
    text whose LAST separator is a comma followed by one or two digits ("28,80", "1.234,56",
    "2 400,00 €"). Anything else is None. After the merge the real parser is used.
    """
    cleaned = re.sub(r"[^0-9.,]", "", str(text or ""))
    head, sep, tail = cleaned.rpartition(",")
    amount = None
    if sep and head and 1 <= len(tail) <= 2 and tail.isdigit() and "." not in tail:
        digits = head.replace(".", "")
        if digits.isdigit():
            amount = float(f"{digits}.{tail}")
    return type("Read", (), {"amount": amount})()


def _price_parser():
    try:
        from utils.crawled_price import parse_crawled_price
    except ImportError:
        return _comma_decimal_only
    return parse_crawled_price


def _load_freshness_census():
    """The served-row classification, from the census that already owns it (#2413)."""
    path = os.path.join(_REPO_ROOT, "scripts", "ops", "external_seed_freshness_census.py")
    spec = importlib.util.spec_from_file_location("external_seed_freshness_census", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _num(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _load(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _norm_title(title: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(title or "").lower()).strip()


def default_currency_for(market: Any) -> str:
    """What `resolve_external_offer` wrote for a page with no currency, before the fix."""
    return "JPY" if str(market or "US").strip().upper() == "JP" else "USD"


def tld_currency(host: str) -> Optional[str]:
    return TLD_CURRENCY.get(host.rsplit(".", 1)[-1]) if "." in host else None


def decimals(amount: float) -> int:
    text = f"{amount:.10f}".rstrip("0")
    return len(text.split(".", 1)[1]) if "." in text else 0


def in_band(ratio: Optional[float], band: Tuple[float, float]) -> bool:
    return ratio is not None and band[0] <= ratio <= band[1]


def price_flags(
    *,
    amount: Optional[float],
    currency: str,
    peer_median: Optional[float],
    variant_median: Optional[float],
    raw_texts: Sequence[str],
    zero_decimal: Iterable[str],
    parse,
) -> List[str]:
    """Pure: the signals for one seed price. `parse` is utils.crawled_price.parse_crawled_price."""
    if amount is None or amount <= 0:
        return []
    flags: List[str] = []
    zero = currency in set(zero_decimal)
    peer_ratio = amount / peer_median if peer_median else None
    variant_ratio = amount / variant_median if variant_median else None
    if in_band(peer_ratio, X100_BAND):
        flags.append("peer_x100")
    elif in_band(peer_ratio, DIV1000_BAND):
        flags.append("peer_div1000")
    elif peer_ratio is not None and (peer_ratio >= OUTLIER_RATIO or peer_ratio <= 1 / OUTLIER_RATIO):
        flags.append("peer_outlier")
    if in_band(variant_ratio, X100_BAND):
        flags.append("variant_x100")
    elif in_band(variant_ratio, DIV1000_BAND):
        flags.append("variant_div1000")
    places = decimals(amount)
    if (zero and places > 0) or (not zero and places > 2):
        flags.append("excess_decimals")
    for text in raw_texts:
        if not _COMMA_DECIMAL_TEXT.search(text):
            continue
        read = parse(text, currency=currency or None).amount
        if read is not None and abs(read * 100 - amount) < 0.005:
            flags.append("raw_comma_x100")
            break
    if not zero and amount >= 1000 and float(amount).is_integer() and int(amount) % 100 != 0:
        flags.append("cents_integer")
    ceiling = CEILINGS.get(currency)
    if ceiling is not None and amount > ceiling:
        flags.append("above_ceiling")
    return flags


STRONG = frozenset({"peer_x100", "peer_div1000", "variant_x100", "variant_div1000", "excess_decimals", "raw_comma_x100"})


def currency_flags(*, market: Any, currency: str, host: str, variant_currencies: Iterable[str]) -> List[str]:
    """Pure: was this row's currency plausibly invented by the old default?"""
    if not currency or currency != default_currency_for(market):
        return []
    flags = ["default_shaped"]
    others = {c for c in variant_currencies if c}
    if others and others != {currency}:
        flags.append("contradicted")
    elif not others:
        flags.append("uncorroborated")
    tld = tld_currency(host)
    if tld and tld != currency:
        flags.append("tld_mismatch")
    return flags


SEED_EXTRA_COLUMNS = """
  price_amount, title, attached_product_key AS attached_key,
  CASE WHEN jsonb_typeof(seed_data -> 'price') = 'string' THEN seed_data ->> 'price' END AS sd_price_text,
  (SELECT coalesce(jsonb_agg(jsonb_build_array(
            v -> 'price_amount', v -> 'price', v ->> 'price_currency', v ->> 'currency')), '[]'::jsonb)
     FROM jsonb_array_elements(CASE WHEN jsonb_typeof(seed_data -> 'variants') = 'array'
                                    THEN seed_data -> 'variants' ELSE '[]'::jsonb END) v
    WHERE jsonb_typeof(v) = 'object') AS variant_prices,
"""

# Offers on the attached products, excluding the synthetic merchant whose offers were projected
# from these very seeds. The key list is bound, never interpolated.
OFFERS_SQL = """
SELECT product_key, upper(trim(coalesce(currency, ''))) AS currency,
       coalesce(merchant_effective_price, list_price) AS price
FROM catalog_offers
WHERE product_key = ANY($1::text[])
  AND merchant_id <> 'external_seed'
  AND coalesce(merchant_effective_price, list_price) > 0
"""

GUARD_SQL = f"""
SELECT
  (SELECT count(*) FROM pg_stat_progress_create_index) AS index_builds,
  (SELECT count(*) FROM pg_stat_activity
    WHERE application_name = '{APPLICATION_NAME}' AND pid <> pg_backend_pid()) AS other_runs
"""


def build_seed_sql(freshness) -> str:
    base = freshness.build_seed_sql()
    head, sep, rest = base.partition("SELECT\n")
    assert sep, "the freshness census SQL no longer starts with SELECT"
    return head + sep + SEED_EXTRA_COLUMNS + rest


def _variant_rows(row: Dict[str, Any]) -> List[Tuple[Optional[float], Optional[str], Optional[str]]]:
    """(amount, text-if-the-price-was-stored-as-text, currency) per stored variant."""
    out = []
    for item in _load(row.get("variant_prices")) or []:
        if not isinstance(item, list) or len(item) != 4:
            continue
        amount_raw, price_raw, cur_a, cur_b = item
        text = next((x for x in (amount_raw, price_raw) if isinstance(x, str)), None)
        amount = next((a for a in (_num(amount_raw), _num(price_raw)) if a is not None), None)
        cur = str(cur_a or cur_b or "").strip().upper() or None
        out.append((amount, text, cur))
    return out


def summarise(
    rows: Sequence[Dict[str, Any]],
    offers: Sequence[Dict[str, Any]],
    *,
    classify,
    parse,
    zero_decimal: Iterable[str],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Every census number, and the suspect list, from fetched rows. Pure."""
    zero_decimal = frozenset(zero_decimal)
    facts = []
    for row in rows:
        fact = classify(row)
        currency = str(row.get("price_currency") or "").strip().upper()
        facts.append(dict(row, **fact, currency=currency, amount=_num(row.get("price_amount"))))

    by_key: Dict[Tuple[str, str], List[Tuple[str, float]]] = defaultdict(list)
    by_title: Dict[Tuple[str, str], List[Tuple[str, str, float]]] = defaultdict(list)
    for f in facts:
        if f["amount"] is None or f["amount"] <= 0:
            continue
        if f.get("attached_key"):
            by_key[(f["attached_key"], f["currency"])].append((f["id"], f["amount"]))
        title = _norm_title(f.get("title"))
        if title:
            by_title[(title, f["currency"])].append((f["id"], f["host"], f["amount"]))
    offer_prices: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    for o in offers:
        price = _num(o.get("price"))
        if price and price > 0:
            offer_prices[(o["product_key"], o.get("currency") or "")].append(price)

    host_market: Dict[Tuple[str, str], Counter] = defaultdict(Counter)
    totals: Counter = Counter()
    suspects: List[Dict[str, Any]] = []
    for f in facts:
        if not f["served"]:
            continue
        key = (f["host"], str(f.get("market") or ""))
        counts = host_market[key]
        counts["served"] += 1
        totals["served"] += 1
        # Peer SEEDS keyed by id: a sibling on another host with the same title is one peer.
        peer_seeds: Dict[str, float] = {}
        offer_peers: List[float] = []
        if f.get("attached_key"):
            peer_seeds.update((sid, a) for sid, a in by_key[(f["attached_key"], f["currency"])] if sid != f["id"])
            offer_peers = offer_prices.get((f["attached_key"], f["currency"]), [])
        title = _norm_title(f.get("title"))
        if title:
            peer_seeds.update(
                (sid, a) for sid, host, a in by_title[(title, f["currency"])] if sid != f["id"] and host != f["host"]
            )
        peers = list(peer_seeds.values()) + list(offer_peers)
        variants = _variant_rows(f)
        variant_amounts = [a for a, _t, cur in variants if a and a > 0 and (cur in (None, f["currency"]))]
        raw_texts = [t for _a, t, _c in variants if t] + ([f["sd_price_text"]] if f.get("sd_price_text") else [])
        flags = price_flags(
            amount=f["amount"],
            currency=f["currency"],
            peer_median=statistics.median(peers) if peers else None,
            variant_median=statistics.median(variant_amounts) if variant_amounts else None,
            raw_texts=raw_texts,
            zero_decimal=zero_decimal,
            parse=parse,
        )
        cflags = currency_flags(
            market=f.get("market"), currency=f["currency"], host=f["host"],
            variant_currencies=[c for _a, _t, c in variants],
        )
        if peers:
            counts["with_peers"] += 1
            totals["with_peers"] += 1
        for flag in flags + cflags:
            counts[flag] += 1
            totals[flag] += 1
        strong = [fl for fl in flags if fl in STRONG]
        if strong:
            counts["suspect"] += 1
            totals["suspect"] += 1
        likely_invented = "contradicted" in cflags or "tld_mismatch" in cflags
        if likely_invented:
            counts["currency_likely_invented"] += 1
            totals["currency_likely_invented"] += 1
        if strong or likely_invented:
            suspects.append({
                "id": f["id"], "host": f["host"], "market": f.get("market"),
                "price_amount": f["amount"], "price_currency": f["currency"] or None,
                "attached": bool(f.get("attached_key")),
                "flags": flags + cflags,
                "peer_median": round(statistics.median(peers), 4) if peers else None,
                "peers": len(peers),
                "variant_median": round(statistics.median(variant_amounts), 4) if variant_amounts else None,
            })

    def _rank(item: Tuple[Tuple[str, str], Counter]) -> Tuple[int, int, int]:
        c = item[1]
        return (-c["suspect"], -c["currency_likely_invented"], -c["served"])

    hosts = [
        {"host": h, "market": m, **{k: int(v) for k, v in sorted(c.items())}}
        for (h, m), c in sorted(host_market.items(), key=_rank)
        if c["suspect"] or c["currency_likely_invented"] or c["cents_integer"]
    ][:TOP_HOSTS]
    by_market: Dict[str, Counter] = defaultdict(Counter)
    for (_h, m), c in host_market.items():
        by_market[m].update(c)
    suspects.sort(key=lambda s: (s["host"], s["id"]))
    summary = {
        "totals": {k: int(v) for k, v in sorted(totals.items())},
        "by_market": {m: {k: int(v) for k, v in sorted(c.items())} for m, c in sorted(by_market.items())},
        "top_hosts": hosts,
        "suspects_listed": min(len(suspects), MAX_SUSPECTS_LISTED),
        "suspects_total": len(suspects),
    }
    return summary, suspects[:MAX_SUSPECTS_LISTED]


def render(summary: Dict[str, Any]) -> List[str]:
    lines = ["== totals (served active seeds)", json.dumps(summary["totals"], sort_keys=True)]
    lines.append("== by market partition")
    for market, c in summary["by_market"].items():
        lines.append(f"{market:<6} " + " ".join(f"{k}={v}" for k, v in c.items()))
    lines.append("== top hosts (suspect, then likely-invented currency, then served)")
    for h in summary["top_hosts"]:
        rest = " ".join(f"{k}={v}" for k, v in h.items() if k not in ("host", "market"))
        lines.append(f"{h['host']:<36} {h['market']:<5} {rest}")
    lines.append(f"suspects: {summary['suspects_total']} (listed {summary['suspects_listed']})")
    return lines


async def collect(conn: Any) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """One READ ONLY transaction on an open asyncpg connection, then summarise."""
    from services.crawl_politeness import host_of
    from services.external_seed_search import seed_serving_currency
    from services.external_seed_stock import seed_stock
    from utils.money import ZERO_DECIMAL_CURRENCIES

    freshness = _load_freshness_census()
    await conn.execute(f"SET application_name = '{APPLICATION_NAME}'")
    async with conn.transaction(readonly=True):
        await conn.execute("SET LOCAL statement_timeout = '30s'")
        guard = await conn.fetchrow(GUARD_SQL)
        if guard["index_builds"] or guard["other_runs"]:
            raise SystemExit(
                f"refusing to run: index_builds={guard['index_builds']} other_runs={guard['other_runs']}"
            )
        rows = [dict(r) for r in await conn.fetch(build_seed_sql(freshness))]
        keys = sorted({r["attached_key"] for r in rows if r.get("attached_key")})
        offers = [dict(r) for r in await conn.fetch(OFFERS_SQL, keys)] if keys else []

    def classify(row: Dict[str, Any]) -> Dict[str, Any]:
        fact = freshness.classify(row, serving_currency=seed_serving_currency, seed_stock=seed_stock, host_of=host_of)
        return {"served": fact["served"], "host": fact["host"]}

    return summarise(rows, offers, classify=classify, parse=_price_parser(), zero_decimal=ZERO_DECIMAL_CURRENCIES)


async def _main() -> int:
    import asyncpg

    dsn = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        summary, suspects = await collect(conn)
    finally:
        await conn.close()
    for line in render(summary):
        print(line)
    print("CENSUS_JSON " + json.dumps(summary, default=str, separators=(",", ":")))
    print("SUSPECTS_JSON " + json.dumps(suspects, default=str, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
