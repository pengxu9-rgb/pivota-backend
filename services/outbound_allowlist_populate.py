"""Outbound allowlist populate: derive, per pricing market, the destination hosts Pivota's stored-row mints send buyers
to, and STAGE them in `outbound_link_allowed_domains`. Step 1 of "populate, then enforce" (2026-11-19).

WHY STAGED. The mint-time rule (services.outbound_links_service.is_destination_domain_allowed) is "a market with at
least one ACTIVE row allows only matching domains; a market with none allows every domain". One ACTIVE row would switch
its market to enforcement at insert time. So this job inserts with status `disabled` (Peng, 2026-11-19), which every
reader ignores: `_fetch_allowed_domains_for_market` reads `status = 'active'` only (pinned by a test that applies a
batch and reads it back through the real reader). Enforcement is a separate, later, reviewed step.

SOURCES, read at run time (no host is ever in the repository), each keyed the way its mint keys the allowlist:
  * published `outbound_link_rules.destination_url`, under the rule's market (resolve_outbound_link mints under the
    request market, which selected that rule);
  * active `external_product_seeds`, under the row's stored partition (every stored-row seed mint keys on it): the
    columns `canonical_url` / `destination_url`, the `seed_data` and `seed_data.snapshot` fallbacks the mints use when
    the columns are empty, and the cart-permalink host (`domain` / `seed_data.domain`, the shop host the gateway's
    cart mint builds `https://<host>/cart/...` on).
NOT COVERED, because the mint keys them on the BUYER's request market, which no stored row names: offers.resolve's
catalog-offer lane (`catalog_offers` destinations) and the connected-merchant product lane. Before enforce, a
mint-side "would refuse" count must be taken from the mints themselves (see the design's enable checklist).

THE ENTRY RULE (keep the exact host when in doubt): the entry is the source host itself, lower-cased; only a host that
is its registrable domain or `www.` + it collapses to the registrable domain (`www.` + it is marked `widened`). Any entry
that IS a registrable domain is marked `apex`: it admits every subdomain under it (the mint matches subdomains), so the
review sees each one, an uncurated platform's apex included. A bare public or platform suffix (co.uk, myshopify.com, ...) is never an
entry (`public_suffix`). Every entry matches the mint check for its source hosts (`_domain_matches`: equal, or a
subdomain), pinned by a test.

Plan is read only. Apply (Postgres only) is insert-only in ONE locked transaction, each row tagged
`created_by = allowlist_populate:<batch>`; rollback deletes only that batch's rows, only while every one is still
staged and matches the receipt. See scripts/ops/outbound_allowlist_populate.py.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import stat
from collections import Counter, defaultdict
from hashlib import sha256
from hmac import compare_digest
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, urlparse

SCHEMA_VERSION = 3  # 3: the plan names the redirector lists it was built with (redirector_lists_sha256)
OPERATION = "outbound_allowlist_populate"
CREATED_BY_PREFIX = "allowlist_populate:"
STAGED_STATUS = "disabled"
MAX_ENTRIES = 5000
LINE_PREFIX = "OLP "
DOC_CHUNK_BYTES = 150_000  # base64 text per log line, well under Cloud Logging's 256 KB entry limit
LOCK_TIMEOUT = "2s"
MULTI_MARKET_THRESHOLD = 3

SKIP_NO_URL = "no_url"
SKIP_UNPARSABLE = "unparsable_url"
SKIP_NON_HTTPS = "non_https"
SKIP_IP_HOST = "ip_host"
SKIP_TRAILING_DOT = "trailing_dot_host"
SKIP_IDN = "idn_host"
SKIP_INVALID_HOST = "invalid_host"
SKIP_PUBLIC_SUFFIX = "public_suffix"
SKIP_UNLISTED_MARKET = "unlisted_market"
SKIP_ALREADY_PRESENT = "already_present"
SKIP_REASONS = (SKIP_NO_URL, SKIP_UNPARSABLE, SKIP_NON_HTTPS, SKIP_IP_HOST, SKIP_TRAILING_DOT, SKIP_IDN,
                SKIP_INVALID_HOST, SKIP_PUBLIC_SUFFIX, SKIP_UNLISTED_MARKET, SKIP_ALREADY_PRESENT)

# A URL that forwards elsewhere: our allowlist would bound the forwarder, not the final destination. Heuristics only;
# a mark for the review, never a skip.
_REDIRECT_QUERY_KEYS = frozenset({"url", "u", "redirect", "redirect_url", "redirect_uri", "redir", "dest",
                                  "destination", "target", "to", "goto", "out", "link", "murl", "r", "ued", "dl",
                                  "deeplink", "deep_link", "next", "continue", "return_url", "returnurl", "return_to",
                                  "lp", "adurl", "href", "q", "rurl", "forward", "jump", "landing"})
_REDIRECT_PATH_HEADS = frozenset({"r", "go", "out", "redirect", "redir", "click", "clk", "track", "trk", "link", "l",
                                  "aff", "ref", "jump", "away", "exit", "outbound", "visit", "deeplink"})
# An opaque-id head: `/c/<id>`, `/t/<id>`, ... on a host that is only a link (marked only with a following id).
_OPAQUE_ID_HEADS = frozenset({"c", "t", "s", "x", "y"})
_PATH_EXTENSIONS = (".php", ".html", ".htm", ".asp", ".aspx", ".jsp", ".cgi", ".pl")
# Link shorteners and affiliate-network click domains beyond outbound_warm_handoff.AFFILIATE_HOST_SUFFIXES (which is
# also read) are PRIVATE: no real host is in the repository. They load at run time from a reviewed JSON file, its
# path and SHA-256 pin in two envs (the private-manifest pattern of services/exact_retailer_cohorts.py and
# services/external_seed_review_pilot.py). Read only. Unset path, a missing file or an empty file: no list marks, and
# the plan says `OLP lists=absent`. A file whose bytes miss the pin, or that is not the schema, refuses the run.
# Shape: {"version": 1, "shorteners": [host suffix, ...], "affiliate_networks": [host suffix, ...]}.
REDIRECTOR_LISTS_PATH_ENV = "OUTBOUND_ALLOWLIST_REDIRECTOR_LISTS_PATH"
REDIRECTOR_LISTS_SHA256_ENV = "OUTBOUND_ALLOWLIST_REDIRECTOR_LISTS_SHA256"
REDIRECTOR_LISTS_LIMIT = 262_144
REDIRECTOR_LISTS_MAX_HOSTS = 2000
_LIST_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\Z")
ABSENT_LISTS: Dict[str, Any] = {"sha256": None, "shorteners": (), "affiliate_networks": ()}


def _host_has_suffix(host: str, suffixes: Iterable[str]) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


def _looks_like_url(value: str) -> bool:
    """A redirect target: a URL with a scheme or `//`, a scheme-less `host.tld/...`, or base64 of either."""
    from urllib.parse import unquote

    text = unquote(unquote(value or "")).strip()
    low = text.lower()
    if "://" in low or low.startswith("//"):
        return True
    if re.match(r"(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?/", low):
        return True
    if len(text) >= 12 and re.fullmatch(r"[A-Za-z0-9+/_=-]+", text):
        try:
            decoded = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_" if ("-" in text or "_" in text) else None)
            dec = decoded.decode("utf-8", "strict").lower()
        except Exception:
            return False
        return "://" in dec or dec.startswith("//")
    return False


_BATCH = re.compile(r"[a-z0-9][a-z0-9-]{2,62}\Z")
_KEY_MASK = re.compile(r"[^A-Za-z0-9*_.-]")

RULES_SQL = "SELECT market, destination_url FROM outbound_link_rules WHERE status = 'published'"
SEEDS_SQL = """
    SELECT market, canonical_url, destination_url, domain,
           seed_data->>'canonical_url' AS sd_canonical_url,
           seed_data->>'destination_url' AS sd_destination_url,
           seed_data->>'domain' AS sd_domain,
           seed_data->'snapshot' AS snapshot_raw
      FROM external_product_seeds
     WHERE status = 'active'
"""
SEED_URL_COLUMNS = ("canonical_url", "destination_url", "sd_canonical_url", "sd_destination_url")
SEED_HOST_COLUMNS = ("domain", "sd_domain")
PRESENT_SQL = "SELECT market, domain FROM outbound_link_allowed_domains"
# The table the unqualified INSERT will hit (to_regclass resolves it by the search_path, so its own schema), and a
# valid, NON-partial, non-expression unique index on exactly (market, domain) in that order.
UNIQUE_INDEX_SQL = """
    SELECT count(*) AS n
      FROM pg_index i
     WHERE i.indrelid = to_regclass('outbound_link_allowed_domains')
       AND i.indisunique AND i.indisvalid AND i.indpred IS NULL AND i.indexprs IS NULL
       AND i.indnkeyatts = 2
       AND (SELECT array_agg(a.attname::text ORDER BY k.ord)
              FROM unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)
              JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
             WHERE k.ord <= 2) = ARRAY['market', 'domain']
"""


class Refused(RuntimeError):
    """A guard refused. Before a transaction opens nothing was written; inside one, it rolls back. `reason`, when
    set, is the job's `reason=` token (else the class name)."""

    def __init__(self, message: str, reason: Optional[str] = None) -> None:
        super().__init__(message)
        self.reason = reason


def require(test: Any, message: str, reason: Optional[str] = None) -> None:
    if not test:
        raise Refused(message, reason)


def _unique_object(pairs):
    out: Dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _valid_list(value: Any) -> bool:
    return (isinstance(value, list) and len(value) <= REDIRECTOR_LISTS_MAX_HOSTS
            and all(isinstance(h, str) and _LIST_HOST.fullmatch(h) for h in value) and len(set(value)) == len(value))


def load_redirector_lists(environ=None) -> Dict[str, Any]:
    """The private shortener / affiliate-network host lists, read only, pinned by SHA-256. ABSENT_LISTS when the path
    is unset, the file is missing or it is empty (no list marks). Refuses (`lists_sha_mismatch`, `lists_invalid`) a
    file that misses its pin or the schema: a pinned list that silently stopped marking would be worse than a stop."""
    env = os.environ if environ is None else environ
    path, pin = env.get(REDIRECTOR_LISTS_PATH_ENV), env.get(REDIRECTOR_LISTS_SHA256_ENV)
    if not path:
        return dict(ABSENT_LISTS)
    try:
        mode = os.stat(path).st_mode
    except FileNotFoundError:
        return dict(ABSENT_LISTS)
    except OSError as error:
        raise Refused(f"redirector lists unreadable ({type(error).__name__})", "lists_invalid") from None
    require(stat.S_ISREG(mode), "redirector lists path is not a regular file", "lists_invalid")
    try:
        with open(path, "rb") as source:
            data = source.read(REDIRECTOR_LISTS_LIMIT + 1)
    except OSError as error:
        raise Refused(f"redirector lists unreadable ({type(error).__name__})", "lists_invalid") from None
    if not data:
        return dict(ABSENT_LISTS)
    require(len(data) <= REDIRECTOR_LISTS_LIMIT, "redirector lists file is too large", "lists_invalid")
    require(isinstance(pin, str) and re.fullmatch(r"[0-9a-f]{64}", pin) and compare_digest(sha256(data).hexdigest(), pin),
            "redirector lists bytes do not match the SHA-256 pin", "lists_sha_mismatch")
    try:
        doc = json.loads(data, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        raise Refused("redirector lists are not JSON", "lists_invalid") from None
    require(isinstance(doc, dict) and doc.keys() == {"version", "shorteners", "affiliate_networks"}
            and doc["version"] == 1 and _valid_list(doc["shorteners"]) and _valid_list(doc["affiliate_networks"]),
            "redirector lists do not match the schema", "lists_invalid")
    return {"sha256": pin, "shorteners": tuple(doc["shorteners"]), "affiliate_networks": tuple(doc["affiliate_networks"])}


def lists_line(lists: Dict[str, Any]) -> str:
    """Counts only, never a host."""
    if not lists.get("sha256"):
        return f"{LINE_PREFIX}lists=absent"
    return (f"{LINE_PREFIX}lists sha256={lists['sha256']} shorteners={len(lists['shorteners'])} "
            f"affiliate={len(lists['affiliate_networks'])}")


def compact(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return sha256(compact(value).encode()).hexdigest()


def pricing_markets() -> List[str]:
    """The registry's pricing markets. NZ is listed but `off` with pricing_region false, so it is not one (12 today)."""
    from services import market_registry

    return sorted(market_registry.pricing_region_currency())


def market_key(raw: Any) -> str:
    """The allowlist key the mint uses (ASCII-first; absent reads US). NOTE the stored-row seed lanes upper() first
    today, so a non-ASCII 'ſg' reads SG there; their alignment is the P2-2 helper's (normalize_market) change."""
    from services.outbound_links_service import normalize_market

    return normalize_market(raw)


def _masked_key(key: str) -> str:
    return _KEY_MASK.sub("?", key)[:8] or "(blank)"


def derive(url: Any) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """(entry, source host, None) for a URL whose host can be staged, else (None, host or None, skip reason)."""
    from services.brand_claim_service import _is_registrable_base, is_valid_public_hostname
    from services.outbound_links_service import url_domain
    from services.seller_identity import etld1

    text = url.strip() if isinstance(url, str) else ""
    if not text:
        return None, None, SKIP_UNPARSABLE
    try:
        parsed = urlparse(text)
    except ValueError:
        return None, None, SKIP_UNPARSABLE
    if not parsed.scheme or not parsed.netloc:
        return None, None, SKIP_UNPARSABLE
    if parsed.scheme.lower() != "https":
        return None, None, SKIP_NON_HTTPS
    host = url_domain(text)
    if not host:
        return None, None, SKIP_UNPARSABLE
    try:
        ipaddress.ip_address(host.strip("[]"))
        return None, host, SKIP_IP_HOST
    except ValueError:
        pass
    if host.endswith("."):
        return None, host, SKIP_TRAILING_DOT
    if not host.isascii():
        return None, host, SKIP_IDN
    if not is_valid_public_hostname(host):
        return None, host, SKIP_INVALID_HOST
    registrable = etld1(host)
    entry = registrable if host in (registrable, "www." + registrable) else host
    if not entry or not _is_registrable_base(entry):
        return None, host, SKIP_PUBLIC_SUFFIX
    return entry, host, None


def is_apex(entry: str) -> bool:
    """An entry that is a registrable domain admits EVERY subdomain under it (the mint matches subdomains). Marked
    `apex` so the review sees each one, including an uncurated platform's own apex, which would admit every tenant."""
    from services.seller_identity import etld1

    return bool(entry) and etld1(entry) == entry


def derive_entry(url: Any) -> Tuple[Optional[str], Optional[str]]:
    entry, _, reason = derive(url)
    return entry, reason


def redirector_marks(url: str, lists: Optional[Dict[str, Any]] = None) -> Set[str]:
    """Why a source URL may forward elsewhere (heuristics, advisory marks for the review; never a skip). `lists`: the
    private host lists (load_redirector_lists); without them only the in-code heuristics and the existing
    outbound_warm_handoff list mark."""
    from urllib.parse import unquote

    from services.outbound_links_service import url_domain
    from services.outbound_warm_handoff import is_affiliate_destination

    lists = ABSENT_LISTS if lists is None else lists
    marks: Set[str] = set()
    try:
        parsed = urlparse(url)
    except ValueError:
        return marks
    host = url_domain(url)
    if is_affiliate_destination(url) or _host_has_suffix(host, lists["affiliate_networks"]):
        marks.add("affiliate_host")
    if _host_has_suffix(host, lists["shorteners"]):
        marks.add("shortener_host")
    for where, text in (("query", parsed.query), ("fragment", parsed.fragment)):
        for key, value in parse_qsl(text, keep_blank_values=False):
            if key.lower() in _REDIRECT_QUERY_KEYS and _looks_like_url(value):
                marks.add(f"{where}_{key.lower()}")
        if where == "fragment" and text and "=" not in text and _looks_like_url(text):
            marks.add("fragment_url")
    segments = [seg for seg in (parsed.path or "").split("/") if seg]
    for seg in segments[:2]:
        head = seg.lower()
        for ext in _PATH_EXTENSIONS:
            if head.endswith(ext):
                head = head[: -len(ext)]
                break
        if head in _REDIRECT_PATH_HEADS:
            marks.add("path_" + head)
    if len(segments) >= 2 and segments[0].lower() in _OPAQUE_ID_HEADS and re.fullmatch(r"[A-Za-z0-9_-]{4,}", segments[1]):
        marks.add("path_" + segments[0].lower() + "_id")
    decoded_path = unquote(unquote(parsed.path or ""))
    if re.search(r"/(?:https?:)?//", decoded_path) or re.search(r"/https?:/", decoded_path):
        marks.add("path_url")
    return marks


def seed_snapshot(raw: Any) -> Dict[str, Any]:
    """The seed's snapshot as the mint reads it: `seed_data` is parsed, then `seed_data["snapshot"]` goes through the
    mint's `_ensure_json_obj` (an object, or a JSON object stored as a string). `raw` is the driver's value of
    `seed_data->'snapshot'`: JSON text (Postgres jsonb, sqlite), or an already decoded value."""
    from services.external_seed_search import ensure_json_obj  # the same rule as the mints' _ensure_json_obj

    value = raw
    if isinstance(raw, (bytes, bytearray)):
        value = raw.decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            value = json.loads(value)  # the jsonb value itself: an object, or a string holding one
        except ValueError:
            return {}
    return ensure_json_obj(value)


def _seed_urls(row: Dict[str, Any]) -> List[str]:
    from services.outbound_links_service import normalize_shop_host

    urls = {str(row.get(c)).strip() for c in SEED_URL_COLUMNS if isinstance(row.get(c), str) and row.get(c).strip()}
    snapshot = seed_snapshot(row.get("snapshot_raw"))
    for c in ("canonical_url", "destination_url"):
        value = snapshot.get(c)
        if isinstance(value, str) and value.strip():
            urls.add(value.strip())
    for c in SEED_HOST_COLUMNS:
        value = row.get(c)
        if isinstance(value, str) and value.strip():
            host = normalize_shop_host(value)
            if host:
                urls.add(f"https://{host}/")  # the cart permalink's host (shopify_cart_base_url)
    return sorted(urls)


def build_plan(*, batch_id: str, rules: Iterable[Dict[str, Any]], seeds: Iterable[Dict[str, Any]],
               present: Iterable[Tuple[Any, Any]], markets: Optional[Iterable[str]] = None,
               lists: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The plan document (pure). `rules` / `seeds`: row dicts as RULES_SQL / SEEDS_SQL return them; `present`:
    (market, domain) pairs already in the allowlist, in any status; `lists`: the private redirector lists (their sha
    is recorded in the plan, so apply can refuse other lists)."""
    lists = ABSENT_LISTS if lists is None else lists
    require(isinstance(batch_id, str) and _BATCH.fullmatch(batch_id), "batch id must be a lowercase slug")
    scope = set(markets if markets is not None else pricing_markets())
    have = {(str(m or "").strip().upper(), str(d or "").strip().lower().lstrip(".")) for m, d in present}
    counts: Dict[Tuple[str, str], Counter] = defaultdict(Counter)
    hosts: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    marks: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    skips: Counter = Counter()

    def consider(market_raw: Any, urls: List[str], source: str) -> None:
        """One source ROW: each distinct entry it yields is counted once; each URL that yields none is a skip."""
        key = market_key(market_raw)
        if key not in scope:
            skips[(_masked_key(key), SKIP_UNLISTED_MARKET)] += 1
            return
        if not urls:
            skips[(key, SKIP_NO_URL)] += 1
            return
        found = set()
        for url in urls:
            entry, host, reason = derive(url)
            if reason:
                skips[(key, reason)] += 1
                continue
            found.add(entry)
            hosts[(key, entry)].add(host)
            if host != entry:
                marks[(key, entry)].add("widened")
            if is_apex(entry):
                marks[(key, entry)].add("apex")
            marks[(key, entry)].update(redirector_marks(url, lists))
        for entry in found:
            counts[(key, entry)][source] += 1

    for row in rules:
        url = row.get("destination_url")
        consider(row.get("market"), [url.strip()] if isinstance(url, str) and url.strip() else [], "rules")
    for row in seeds:
        consider(row.get("market"), _seed_urls(row), "seeds")

    markets_per_entry = Counter(entry for _, entry in counts)
    entries = []
    for (key, entry), c in sorted(counts.items()):
        if (key, entry) in have:
            skips[(key, SKIP_ALREADY_PRESENT)] += 1
            continue
        mk = set(marks[(key, entry)])
        if markets_per_entry[entry] >= MULTI_MARKET_THRESHOLD:
            mk.add("multi_market")
        if any(m.startswith(("affiliate_", "shortener_", "query_", "fragment_", "path_")) for m in mk):
            mk.add("redirector_suspect")
        entries.append({"market": key, "domain": entry, "rules": c["rules"], "seeds": c["seeds"],
                        "hosts": len(hosts[(key, entry)]), "marks": sorted(mk)})
    require(len(entries) <= MAX_ENTRIES, f"plan has more than {MAX_ENTRIES} entries")
    plan = {
        "schema_version": SCHEMA_VERSION,
        "operation": OPERATION,
        "batch_id": batch_id,
        "status": STAGED_STATUS,
        "redirector_lists_sha256": lists.get("sha256"),
        "entries": entries,
        "skips": [{"market": m, "reason": r, "count": n} for (m, r), n in sorted(skips.items())],
    }
    plan["plan_sha256"] = digest({k: v for k, v in plan.items() if k != "plan_sha256"})
    return plan


def verify_plan(plan: Any) -> Dict[str, Any]:
    require(isinstance(plan, dict) and plan.get("schema_version") == SCHEMA_VERSION and plan.get("operation") == OPERATION,
            "not an allowlist populate plan")
    require(plan.get("status") == STAGED_STATUS, "a populate plan only stages rows")
    lists_sha = plan.get("redirector_lists_sha256", "missing")
    require(lists_sha is None or (isinstance(lists_sha, str) and re.fullmatch(r"[0-9a-f]{64}", lists_sha)),
            "plan does not name its redirector lists")
    require(isinstance(plan.get("batch_id"), str) and _BATCH.fullmatch(plan["batch_id"]), "bad batch id")
    require(plan.get("plan_sha256") == digest({k: v for k, v in plan.items() if k != "plan_sha256"}),
            "plan document does not match its own sha")
    seen = set()
    scope = set(pricing_markets())
    for e in plan.get("entries") or []:
        require(isinstance(e, dict) and isinstance(e.get("market"), str) and isinstance(e.get("domain"), str), "bad entry")
        require(e["market"] in scope, "entry market is not a pricing market")
        require(derive("https://" + e["domain"] + "/")[0] == e["domain"], "entry domain is not a normalised entry")
        require((e["market"], e["domain"]) not in seen, "duplicate entry")
        seen.add((e["market"], e["domain"]))
    require(len(seen) <= MAX_ENTRIES, "too many entries")
    return plan


def plan_lines(plan: Dict[str, Any]) -> List[str]:
    """The dry-run report Peng reviews: per market counts, every entry with its marks, and the skips (counts only)."""
    entries = plan["entries"]
    lines = [f"{LINE_PREFIX}plan batch={plan['batch_id']} markets={len({e['market'] for e in entries})} "
             f"domains={len(entries)} status={plan['status']} plan_sha256={plan['plan_sha256']}"]
    by_market: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for e in entries:
        by_market[e["market"]].append(e)
    for market in sorted(by_market):
        rows = by_market[market]
        lines.append(f"{LINE_PREFIX}market={market} domains={len(rows)} from_rules={sum(1 for r in rows if r['rules'])} "
                     f"from_seeds={sum(1 for r in rows if r['seeds'])} widened={sum(1 for r in rows if 'widened' in r['marks'])} "
                     f"redirector_suspect={sum(1 for r in rows if 'redirector_suspect' in r['marks'])}")
        for r in rows:
            lines.append(f"{LINE_PREFIX}domain market={market} domain={r['domain']} rules={r['rules']} seeds={r['seeds']} "
                         f"hosts={r['hosts']} marks={','.join(r['marks']) or '-'}")
    for s in plan["skips"]:
        lines.append(f"{LINE_PREFIX}skip market={s['market']} reason={s['reason']} count={s['count']}")
    return lines


def doc_lines(kind: str, value: Any) -> List[str]:
    """`OLP <kind> <i>/<n> <sha256> <base64 chunk>` lines: one document split under the per-entry log limit."""
    raw = compact(value).encode()
    text = base64.b64encode(raw).decode()
    chunks = [text[i:i + DOC_CHUNK_BYTES] for i in range(0, len(text), DOC_CHUNK_BYTES)] or [""]
    whole = sha256(raw).hexdigest()
    return [f"{LINE_PREFIX}{kind} {i + 1}/{len(chunks)} {whole} {c}" for i, c in enumerate(chunks)]


def parse_doc_lines(text: str, kind: str) -> Any:
    """Reassemble a document from its `OLP <kind>` lines (in any order), checking the count and the whole-doc sha."""
    parts: Dict[int, str] = {}
    total, whole = None, None
    marker = f"{LINE_PREFIX}{kind} "
    for line in text.splitlines():
        if not line.startswith(marker):
            continue
        index, sha, chunk = line[len(marker):].split(" ", 2)
        i, n = (int(x) for x in index.split("/"))
        require(total in (None, n) and whole in (None, sha), f"{kind} lines from different documents")
        total, whole = n, sha
        parts[i] = chunk.strip()
    require(total is not None and sorted(parts) == list(range(1, total + 1)), f"{kind} lines are incomplete")
    raw = base64.b64decode("".join(parts[i] for i in range(1, total + 1)), validate=True)
    require(sha256(raw).hexdigest() == whole, f"{kind} does not match its sha")
    return json.loads(raw)


def _is_postgres(db) -> bool:
    return str(getattr(getattr(db, "url", None), "dialect", "")).startswith("postgres")


async def _read_only(db, *, require_postgres: bool = True) -> None:
    if not _is_postgres(db):
        require(not require_postgres, "the plan runs on Postgres only")
        return
    await db.execute("SET TRANSACTION READ ONLY")
    await db.execute("SET LOCAL statement_timeout = '5s'")
    await db.execute("SET LOCAL lock_timeout = '1s'")


async def _write_guard(db, *, require_postgres: bool = True) -> None:
    """Postgres only: bounded waits, the table locked against concurrent writers, and its unique index present."""
    if not _is_postgres(db):
        require(not require_postgres, "apply and rollback run on Postgres only")
        return
    await db.execute("SET LOCAL statement_timeout = '30s'")
    await db.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    # The lock first: it blocks every other writer AND a DROP INDEX, so the index checked below stays.
    try:
        await db.execute("LOCK TABLE outbound_link_allowed_domains IN SHARE ROW EXCLUSIVE MODE")
    except Exception as error:
        raise Refused(f"the allowlist table lock was not acquired within {LOCK_TIMEOUT} ({type(error).__name__}); "
                      "nothing written") from error
    found = await db.fetch_one(UNIQUE_INDEX_SQL)
    require(found is not None and int(dict(found)["n"]) >= 1,
            "no valid, non-partial unique index on exactly (market, domain) for this table; nothing written")


async def read_plan(db, *, batch_id: str, require_postgres: bool = True,
                    timing: Optional[Dict[str, Any]] = None, lists: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Read the sources inside one READ ONLY transaction and build the plan. Never writes. `timing`, when given, is
    filled with the read and build times and the row counts (printed as the `OLP timing` line, never in the plan)."""
    import time

    started = time.monotonic()
    async with db.transaction():
        await _read_only(db, require_postgres=require_postgres)
        rules = [dict(r) for r in await db.fetch_all(RULES_SQL)]
        seeds = [dict(r) for r in await db.fetch_all(SEEDS_SQL)]
        present = [(dict(r)["market"], dict(r)["domain"]) for r in await db.fetch_all(PRESENT_SQL)]
    read_done = time.monotonic()
    plan = build_plan(batch_id=batch_id, rules=rules, seeds=seeds, present=present, lists=lists)
    if timing is not None:
        timing.update({"read_ms": int((read_done - started) * 1000), "build_ms": int((time.monotonic() - read_done) * 1000),
                       "rules": len(rules), "seeds": len(seeds), "present": len(present)})
    return plan


def timing_line(timing: Dict[str, Any]) -> str:
    return (f"{LINE_PREFIX}timing ms={timing['read_ms'] + timing['build_ms']} read_ms={timing['read_ms']} "
            f"build_ms={timing['build_ms']} rules={timing['rules']} seeds={timing['seeds']} present={timing['present']}")


def notes_for(batch_id: str) -> str:
    return f"reviewed allowlist populate {batch_id} (staged)"


def _receipt(batch_id: str, rows: List[Tuple[int, str, str]], plan_sha: Optional[str]) -> Dict[str, Any]:
    rows = sorted(rows)
    per_market = Counter(m for _, m, _ in rows)
    receipt = {"operation": OPERATION, "batch_id": batch_id, "created_by": CREATED_BY_PREFIX + batch_id,
               "notes": notes_for(batch_id), "plan_sha256": plan_sha, "inserted": len(rows),
               "per_market": dict(sorted(per_market.items())), "rows": [list(r) for r in rows]}
    receipt["receipt_sha256"] = digest({k: v for k, v in receipt.items() if k != "receipt_sha256"})
    return receipt


def receipt_line(receipt: Dict[str, Any]) -> str:
    per_market = ",".join(f"{m}:{n}" for m, n in receipt["per_market"].items()) or "-"
    return (f"{LINE_PREFIX}receipt batch={receipt['batch_id']} inserted={receipt['inserted']} per_market={per_market} "
            f"receipt_sha256={receipt['receipt_sha256']}")


async def apply_plan(db, plan: Dict[str, Any], *, lists_sha256: Optional[str], require_postgres: bool = True) -> Dict[str, Any]:
    """Insert the plan's rows (status disabled, tagged with the batch) in ONE transaction; never update or delete.
    `lists_sha256`: the redirector lists loaded for this run; they must be the ones the plan was built with."""
    plan = verify_plan(plan)
    require(plan["redirector_lists_sha256"] == lists_sha256,
            "the redirector lists differ from the plan's; re-plan (nothing written)", "lists_sha_mismatch")
    batch_id, created_by = plan["batch_id"], CREATED_BY_PREFIX + plan["batch_id"]
    wanted = [(e["market"], e["domain"]) for e in plan["entries"]]
    inserted: List[Tuple[int, str, str]] = []
    async with db.transaction():
        await _write_guard(db, require_postgres=require_postgres)
        present = {(str(dict(r)["market"]).strip().upper(), str(dict(r)["domain"]).strip().lower())
                   for r in await db.fetch_all(PRESENT_SQL)}
        clash = [p for p in wanted if p in present]
        require(not clash, f"{len(clash)} planned pair(s) are already present; re-plan (nothing written)")
        tagged = await db.fetch_one("SELECT count(*) AS n FROM outbound_link_allowed_domains WHERE created_by = :c",
                                    {"c": created_by})
        require(int(dict(tagged)["n"]) == 0, "this batch id already has rows; a batch is applied once")
        for market, domain in wanted:
            row = await db.fetch_one(
                "INSERT INTO outbound_link_allowed_domains (market, domain, status, notes, created_by, created_at, updated_at) "
                "VALUES (:market, :domain, :status, :notes, :created_by, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) RETURNING id",
                {"market": market, "domain": domain, "status": STAGED_STATUS, "notes": notes_for(batch_id),
                 "created_by": created_by},
            )
            require(row is not None, "insert returned no id")
            inserted.append((int(dict(row)["id"]), market, domain))
        require(len(inserted) == len(wanted), "inserted count differs from the plan")
    return _receipt(batch_id, inserted, plan["plan_sha256"])


def verify_receipt(receipt: Any) -> Dict[str, Any]:
    require(isinstance(receipt, dict) and receipt.get("operation") == OPERATION, "not an allowlist populate receipt")
    require(isinstance(receipt.get("batch_id"), str) and _BATCH.fullmatch(receipt["batch_id"]), "bad batch id")
    require(receipt.get("created_by") == CREATED_BY_PREFIX + receipt["batch_id"], "receipt tag mismatch")
    require(receipt.get("notes") == notes_for(receipt["batch_id"]), "receipt notes mismatch")
    require(receipt.get("receipt_sha256") == digest({k: v for k, v in receipt.items() if k != "receipt_sha256"}),
            "receipt does not match its own sha")
    require(receipt.get("inserted") == len(receipt.get("rows") or []), "receipt count mismatch")
    return receipt


async def rollback_batch(db, receipt: Dict[str, Any], *, require_postgres: bool = True) -> Dict[str, Any]:
    """Delete exactly the receipt's rows of that batch, in ONE transaction, only while every one of them is still the
    staged row it inserted (status disabled, its notes). A row someone enforced or edited stops the rollback."""
    receipt = verify_receipt(receipt)
    created_by, notes = receipt["created_by"], receipt["notes"]
    expected = sorted(tuple(r) for r in receipt["rows"])
    async with db.transaction():
        await _write_guard(db, require_postgres=require_postgres)
        found = [dict(r) for r in await db.fetch_all(
            "SELECT id, market, domain, status, notes FROM outbound_link_allowed_domains WHERE created_by = :c",
            {"c": created_by})]
        current = sorted((int(r["id"]), r["market"], r["domain"]) for r in found)
        require(current == expected, "the batch's rows no longer match the receipt; nothing deleted")
        changed = [r for r in found if r["status"] != STAGED_STATUS or r["notes"] != notes]
        require(not changed, f"{len(changed)} batch row(s) are no longer staged (enforced or edited); nothing deleted")
        deleted = 0
        for row_id, _, _ in expected:
            gone = await db.fetch_one(
                "DELETE FROM outbound_link_allowed_domains WHERE id = :id AND created_by = :c AND status = :s RETURNING id",
                {"id": row_id, "c": created_by, "s": STAGED_STATUS})
            deleted += 1 if gone is not None else 0
        require(deleted == len(expected), "deleted count differs from the receipt")
    return {"operation": OPERATION, "batch_id": receipt["batch_id"], "deleted": deleted,
            "receipt_sha256": receipt["receipt_sha256"]}
