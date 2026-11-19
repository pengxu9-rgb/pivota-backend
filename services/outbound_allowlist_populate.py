"""Outbound allowlist populate: derive, per pricing market, the destination domains Pivota already sends buyers to,
and stage them in `outbound_link_allowed_domains`. Step 1 of "populate, then enforce" (2026-11-19).

WHY STAGED. The mint-time rule (services.outbound_links_service.is_destination_domain_allowed) is "a market with at
least one ACTIVE row allows only matching domains; a market with none allows every domain". Inserting one ACTIVE row
for a market would switch that market to enforcement at insert time. So this job inserts with status `disabled`, a
value the admin API already accepts, which every reader ignores (they filter on `active`). Enforcement (flipping a
batch's rows to `active`) is a separate, later, reviewed step.

SOURCES, read at run time from the database (no domain is ever in the repository): published `outbound_link_rules`
(destination_url) and active `external_product_seeds` (canonical_url and destination_url, since the mints check
`canonical_url or destination_url` and some lanes compose from the destination). Each row keys under its own market,
read by the mint's own ASCII-first rule (outbound_links_service.normalize_market); only pricing markets are kept.

THE ENTRY matches what the mint checks: the host is `outbound_links_service.url_domain(url)` (the mint's own
function) and the entry is `seller_identity.etld1(host)` (the system's registrable-domain helper, with the curated
multi-label suffixes), so `_domain_matches(host, entry)` holds for the host and its subdomains.

Plan is read only. Apply is insert-only in ONE transaction (each row tagged `created_by = allowlist_populate:<batch>`);
rollback deletes only that batch's rows, verified against the apply receipt. See scripts/ops/outbound_allowlist_populate.py.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections import Counter, defaultdict
from hashlib import sha256
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCHEMA_VERSION = 1
OPERATION = "outbound_allowlist_populate"
CREATED_BY_PREFIX = "allowlist_populate:"
STAGED_STATUS = "disabled"
MAX_ENTRIES = 5000
LINE_PREFIX = "OLP "

SKIP_UNPARSABLE = "unparsable_url"
SKIP_NON_HTTPS = "non_https"
SKIP_IP_HOST = "ip_host"
SKIP_INVALID_HOST = "invalid_host"
SKIP_UNLISTED_MARKET = "unlisted_market"
SKIP_ALREADY_PRESENT = "already_present"
SKIP_REASONS = (SKIP_UNPARSABLE, SKIP_NON_HTTPS, SKIP_IP_HOST, SKIP_INVALID_HOST, SKIP_UNLISTED_MARKET, SKIP_ALREADY_PRESENT)

_BATCH = re.compile(r"[a-z0-9][a-z0-9-]{2,62}\Z")

RULES_SQL = "SELECT market, destination_url FROM outbound_link_rules WHERE status = 'published'"
SEEDS_SQL = "SELECT market, canonical_url, destination_url FROM external_product_seeds WHERE status = 'active'"
PRESENT_SQL = "SELECT market, domain FROM outbound_link_allowed_domains"


class Refused(RuntimeError):
    """A guard refused. Before a transaction opens nothing was written; inside one, it rolls back."""


def require(test: Any, message: str) -> None:
    if not test:
        raise Refused(message)


def compact(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return sha256(compact(value).encode()).hexdigest()


def pricing_markets() -> List[str]:
    from services import market_registry

    return sorted(market_registry.pricing_region_currency())


def market_key(raw: Any) -> str:
    """The allowlist key a mint uses for this market (ASCII-first; absent reads US)."""
    from services.outbound_links_service import normalize_market

    return normalize_market(raw)


def derive_entry(url: Any) -> Tuple[Optional[str], Optional[str]]:
    """(entry, None) for a URL whose host can be allowlisted, else (None, skip reason)."""
    from urllib.parse import urlparse

    from services.brand_claim_service import is_valid_public_hostname
    from services.outbound_links_service import url_domain
    from services.seller_identity import etld1

    text = url.strip() if isinstance(url, str) else ""
    if not text:
        return None, SKIP_UNPARSABLE
    try:
        parsed = urlparse(text)
    except ValueError:
        return None, SKIP_UNPARSABLE
    if not parsed.scheme or not parsed.netloc:
        return None, SKIP_UNPARSABLE
    if parsed.scheme.lower() != "https":
        return None, SKIP_NON_HTTPS
    host = url_domain(text)
    if not host:
        return None, SKIP_UNPARSABLE
    try:
        ipaddress.ip_address(host.strip("[]"))
        return None, SKIP_IP_HOST
    except ValueError:
        pass
    if not host.isascii() or not is_valid_public_hostname(host):
        return None, SKIP_INVALID_HOST
    entry = etld1(host)
    if not entry or "." not in entry:
        return None, SKIP_INVALID_HOST
    return entry, None


def build_plan(*, batch_id: str, rules: Iterable[Any], seeds: Iterable[Any], present: Iterable[Any],
               markets: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """The plan document (pure). `rules` rows: (market, destination_url); `seeds`: (market, canonical, destination);
    `present`: (market, domain) already in the allowlist, any status."""
    require(isinstance(batch_id, str) and _BATCH.fullmatch(batch_id), "batch id must be a lowercase slug")
    scope = set(markets if markets is not None else pricing_markets())
    have = {(str(m or "").strip().upper(), str(d or "").strip().lower().lstrip(".")) for m, d in present}
    sources: Dict[Tuple[str, str], Counter] = defaultdict(Counter)
    skips: Counter = Counter()

    def consider(market_raw: Any, urls: List[Any], source: str) -> None:
        """One source ROW: each distinct entry it yields is counted once; each URL that yields none is a skip."""
        key = market_key(market_raw)
        if key not in scope:
            skips[("-", SKIP_UNLISTED_MARKET)] += 1
            return
        found = set()
        for url in urls:
            entry, reason = derive_entry(url)
            if reason:
                skips[(key, reason)] += 1
            else:
                found.add(entry)
        for entry in found:
            sources[(key, entry)][source] += 1

    for market_raw, url in rules:
        consider(market_raw, [url], "rules")
    for market_raw, canonical, destination in seeds:
        urls = sorted({u.strip() for u in (canonical, destination) if isinstance(u, str) and u.strip()})
        consider(market_raw, urls or [None], "seeds")

    entries = []
    for (key, entry), counts in sorted(sources.items()):
        if (key, entry) in have:
            skips[(key, SKIP_ALREADY_PRESENT)] += 1
            continue
        entries.append({"market": key, "domain": entry, "rules": counts["rules"], "seeds": counts["seeds"]})
    require(len(entries) <= MAX_ENTRIES, f"plan has more than {MAX_ENTRIES} entries")
    plan = {
        "schema_version": SCHEMA_VERSION,
        "operation": OPERATION,
        "batch_id": batch_id,
        "status": STAGED_STATUS,
        "entries": entries,
        "skips": [{"market": m, "reason": r, "count": n} for (m, r), n in sorted(skips.items())],
    }
    plan["plan_sha256"] = digest({k: v for k, v in plan.items() if k != "plan_sha256"})
    return plan


def verify_plan(plan: Any) -> Dict[str, Any]:
    require(isinstance(plan, dict) and plan.get("schema_version") == SCHEMA_VERSION and plan.get("operation") == OPERATION,
            "not an allowlist populate plan")
    require(plan.get("status") == STAGED_STATUS, "a populate plan only stages rows")
    require(isinstance(plan.get("batch_id"), str) and _BATCH.fullmatch(plan["batch_id"]), "bad batch id")
    require(plan.get("plan_sha256") == digest({k: v for k, v in plan.items() if k != "plan_sha256"}),
            "plan document does not match its own sha")
    seen = set()
    for e in plan.get("entries") or []:
        require(isinstance(e, dict) and isinstance(e.get("market"), str) and isinstance(e.get("domain"), str), "bad entry")
        require(e["market"] in set(pricing_markets()), "entry market is not a pricing market")
        require(derive_entry("https://" + e["domain"] + "/")[0] == e["domain"], "entry domain is not a normalised entry")
        require((e["market"], e["domain"]) not in seen, "duplicate entry")
        seen.add((e["market"], e["domain"]))
    require(len(seen) <= MAX_ENTRIES, "too many entries")
    return plan


def plan_lines(plan: Dict[str, Any]) -> List[str]:
    """The dry-run report: per market counts, the full domain list, and the skips (counts only, never a URL)."""
    entries = plan["entries"]
    lines = [f"{LINE_PREFIX}plan batch={plan['batch_id']} markets={len({e['market'] for e in entries})} "
             f"domains={len(entries)} status={plan['status']} plan_sha256={plan['plan_sha256']}"]
    by_market: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for e in entries:
        by_market[e["market"]].append(e)
    for market in sorted(by_market):
        rows = by_market[market]
        lines.append(f"{LINE_PREFIX}market={market} domains={len(rows)} from_rules={sum(1 for r in rows if r['rules'])} "
                     f"from_seeds={sum(1 for r in rows if r['seeds'])}")
        for r in rows:
            lines.append(f"{LINE_PREFIX}domain market={market} domain={r['domain']} rules={r['rules']} seeds={r['seeds']}")
    for s in plan["skips"]:
        lines.append(f"{LINE_PREFIX}skip market={s['market']} reason={s['reason']} count={s['count']}")
    return lines


async def _read_only(db) -> None:
    if str(getattr(getattr(db, "url", None), "dialect", "")).startswith("postgres"):
        await db.execute("SET TRANSACTION READ ONLY")
        await db.execute("SET LOCAL statement_timeout = '5s'")
        await db.execute("SET LOCAL lock_timeout = '1s'")


def _pairs(rows, *cols):
    return [tuple(dict(r).get(c) for c in cols) for r in rows]


async def read_plan(db, *, batch_id: str) -> Dict[str, Any]:
    """Read the sources inside one READ ONLY transaction and build the plan. Never writes."""
    async with db.transaction():
        await _read_only(db)
        rules = _pairs(await db.fetch_all(RULES_SQL), "market", "destination_url")
        seeds = _pairs(await db.fetch_all(SEEDS_SQL), "market", "canonical_url", "destination_url")
        present = _pairs(await db.fetch_all(PRESENT_SQL), "market", "domain")
    return build_plan(batch_id=batch_id, rules=rules, seeds=seeds, present=present)


def _receipt(batch_id: str, rows: List[Tuple[int, str, str]], plan_sha: Optional[str]) -> Dict[str, Any]:
    rows = sorted(rows)
    per_market = Counter(m for _, m, _ in rows)
    receipt = {"operation": OPERATION, "batch_id": batch_id, "created_by": CREATED_BY_PREFIX + batch_id,
               "plan_sha256": plan_sha, "inserted": len(rows), "per_market": dict(sorted(per_market.items())),
               "rows": [list(r) for r in rows]}
    receipt["receipt_sha256"] = digest({k: v for k, v in receipt.items() if k != "receipt_sha256"})
    return receipt


def receipt_line(receipt: Dict[str, Any]) -> str:
    per_market = ",".join(f"{m}:{n}" for m, n in receipt["per_market"].items()) or "-"
    return (f"{LINE_PREFIX}receipt batch={receipt['batch_id']} inserted={receipt['inserted']} per_market={per_market} "
            f"receipt_sha256={receipt['receipt_sha256']}")


async def apply_plan(db, plan: Dict[str, Any]) -> Dict[str, Any]:
    """Insert the plan's rows (status disabled, tagged with the batch) in ONE transaction; never update or delete."""
    plan = verify_plan(plan)
    batch_id, created_by = plan["batch_id"], CREATED_BY_PREFIX + plan["batch_id"]
    wanted = [(e["market"], e["domain"]) for e in plan["entries"]]
    inserted: List[Tuple[int, str, str]] = []
    async with db.transaction():
        present = {(str(m).strip().upper(), str(d).strip().lower()) for m, d in _pairs(await db.fetch_all(PRESENT_SQL), "market", "domain")}
        clash = [p for p in wanted if p in present]
        require(not clash, f"{len(clash)} planned pair(s) are already present; re-plan (nothing written)")
        tagged = await db.fetch_one("SELECT count(*) AS n FROM outbound_link_allowed_domains WHERE created_by = :c",
                                    {"c": created_by})
        require(int(dict(tagged)["n"]) == 0, "this batch id already has rows; a batch is applied once")
        for market, domain in wanted:
            row = await db.fetch_one(
                "INSERT INTO outbound_link_allowed_domains (market, domain, status, notes, created_by, created_at, updated_at) "
                "VALUES (:market, :domain, :status, :notes, :created_by, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) RETURNING id",
                {"market": market, "domain": domain, "status": STAGED_STATUS,
                 "notes": f"reviewed allowlist populate {batch_id}", "created_by": created_by},
            )
            require(row is not None, "insert returned no id")
            inserted.append((int(dict(row)["id"]), market, domain))
        require(len(inserted) == len(wanted), "inserted count differs from the plan")
    return _receipt(batch_id, inserted, plan["plan_sha256"])


def verify_receipt(receipt: Any) -> Dict[str, Any]:
    require(isinstance(receipt, dict) and receipt.get("operation") == OPERATION, "not an allowlist populate receipt")
    require(isinstance(receipt.get("batch_id"), str) and _BATCH.fullmatch(receipt["batch_id"]), "bad batch id")
    require(receipt.get("created_by") == CREATED_BY_PREFIX + receipt["batch_id"], "receipt tag mismatch")
    require(receipt.get("receipt_sha256") == digest({k: v for k, v in receipt.items() if k != "receipt_sha256"}),
            "receipt does not match its own sha")
    require(receipt.get("inserted") == len(receipt.get("rows") or []), "receipt count mismatch")
    return receipt


async def rollback_batch(db, receipt: Dict[str, Any]) -> Dict[str, Any]:
    """Delete exactly the receipt's rows of that batch, in ONE transaction, after checking the batch still is them."""
    receipt = verify_receipt(receipt)
    created_by = receipt["created_by"]
    expected = sorted(tuple(r) for r in receipt["rows"])
    async with db.transaction():
        current = sorted(
            (int(dict(r)["id"]), dict(r)["market"], dict(r)["domain"])
            for r in await db.fetch_all(
                "SELECT id, market, domain FROM outbound_link_allowed_domains WHERE created_by = :c", {"c": created_by})
        )
        require(current == expected, "the batch's rows no longer match the receipt; nothing deleted")
        deleted = 0
        for row_id, _, _ in expected:
            gone = await db.fetch_one(
                "DELETE FROM outbound_link_allowed_domains WHERE id = :id AND created_by = :c RETURNING id",
                {"id": row_id, "c": created_by})
            deleted += 1 if gone is not None else 0
        require(deleted == len(expected), "deleted count differs from the receipt")
    return {"operation": OPERATION, "batch_id": receipt["batch_id"], "deleted": deleted,
            "receipt_sha256": receipt["receipt_sha256"]}
