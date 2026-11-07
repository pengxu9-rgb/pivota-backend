"""Record the codes the shadow capture found on a store page (services/deal_code_capture.py) in
`observed_store_offers`, as UNVERIFIED `merchant_published` rows (ADR-027 D2; coupon discovery design §3.2 A2).

STORAGE, NOT TESTING. A row here is evidence that the store printed the code on its own page, nothing more: it is
never verified, emitted or applied from this module. Verification (a cart on the store's own checkout) is a
separate step with its own flags and approvals; serving reads only verified rows.

WHAT IS WRITTEN, per captured code:
  - seller_id   the page's host, lower-cased, "www." stripped (the same host the gateway's verify route takes);
  - market      the refresh partition's market ONLY when the page's own currency is that market's currency
                (US/USD, SG/SGD). Otherwise none: a row with no market evidence is never verified or served
                (D6 "Market" — never defaulted);
  - code        as the store printed it (Shopify matches codes case-insensitively; the deal_key upper-cases it);
  - kind/value  from the terms the bar stated next to the code: percent, amount (with the page's currency when
                the symbol agrees with it), free_shipping. A code with no stated kind is NOT written (the table
                requires one; guessing would be a false fact) and is counted `skipped_no_kind`;
  - min_subtotal, new_customer_only / once_per_customer / segment_limited from the bar's hints;
                eligibility_known stays FALSE (hints are a reading of prose, not a store's rule);
  - source_url  the page; terms_text the bar snippet (the store's own published text).
One row per (seller, market, code): the identity key is `observed_store_offer_key`. A later sighting refreshes only
`observed_at`, `source_url` and, while retention would keep it, `terms_text`; kind, value and restriction flags are set
once at insert, and verifications, outcomes and `created_at` are never touched.

WHAT IS NOT WRITTEN (review of be#2732):
  - a code read by the loose bare-"code" form (`after_code`): it stays in the shadow counts until its precision is
    measured in prod (`skipped_loose_form`);
  - any code from a page whose request was redirected to ANOTHER host: the codes are that host's, not the requested
    seller's (`skipped_host_changed`);
  - a seller the opt-out denylist covers (`read_denyset`, read once per run: an id as a host, its www. form and its
    subdomains; or the catalog hosts of an id as a merchant id; ADR-027 Q20) (`skipped_optout`). Where the denylist
    is uncertain (a merchant id spanning many registrable domains, hosts known only from canonical URLs) it blocks
    writes anyway. If that check cannot be read, nothing is written (`skipped_optout_unreadable`): fail closed;
  - a digit token that names something else (`not_a_code`: units, durations, times, model numbers, masked numbers,
    "example" codes) (`skipped_not_a_code`);
  - past the per-run budget: at most MAX_WRITES_PER_HOST_PER_RUN rows per host and MAX_WRITES_PER_RUN in all
    (`skipped_host_cap`, `skipped_run_cap`);
  - over a row another source owns for the same identity (`skipped_other_source`).
A minimum spend is kept only when its currency symbol agrees with the page's currency. Concurrent writers of the same
identity resolve to one row (an insert that loses the race becomes an update).

The code itself is never logged. Every failure is swallowed and counted (`errors`): recording can never cost a page
refresh.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, Optional, Set, Tuple
from urllib.parse import urlparse

from sqlalchemy import and_, func, select

from db.catalog import catalog_products
from db.database import database
from db.deal_lane import deal_lane_optout, observed_store_offer_key, observed_store_offer_verifications, observed_store_offers
from services import deal_code_capture
from services.brand_claim_service import _PUBLIC_SUFFIXES, _is_registrable_base

logger = logging.getLogger(__name__)

# The served markets and their currencies (PIVOTA_SERVING_PRICING_REGIONS = US,SG). A page whose own currency is not
# its partition's is not evidence for that market.
_MARKET_CURRENCY = {"US": "USD", "SG": "SGD"}
# A stated amount's symbol must agree with the page's currency, or the amount has no currency.
_SYMBOL_CURRENCIES = {"$": {"USD", "SGD", "AUD", "CAD", "NZD", "HKD"}, "£": {"GBP"}, "€": {"EUR"}}
_KINDS = {"percent", "amount", "free_shipping"}
# Digit tokens that are not codes (review of be#2732, P3-K): ordinals, durations and times ("1ST", "48HRS", "3PM",
# "2-DAY"), multipliers and rates ("3X", "1PER"), masked numbers ("XXXX-1234") and sun-protection labels ("SPF50").
# Kept on purpose, because stores do use them as codes and the verifier is the check: "50OFF", "20OFF", "4EVER",
# "COVID19", "W10".
_NOT_A_CODE_RE = re.compile(
    r"^(?:\d+(?:ST|ND|RD|TH|H|HR|HRS|AM|PM|X|PER)|\d+-?DAYS?|SPF\d+|[A-Z]\d[A-Z]"
    r"|\d+-?(?:ML|L|OZ|G|KG|LBS?|PCS?|PK|PACK|MINS?|HOURS?|WEEKS?|MONTHS?|YRS?|YEARS?|FT|IN|CM|MM|P|K|GB|TB|W|V|MAH)"
    r"|\d+-IN-\d+|\d+(?:AM|PM)-\d+(?:AM|PM)|KN\d+|N\d{2}"
    # review of be#2735 delta 3, P3-R3: dimensions and drive types ("4X4"), sizes ("3XL", "2XS"), "100PCT" (only that:
    # "10PCT" and "20PCT" can be codes, review of be#2742),
    # "360-DEGREE", ratings ("5STAR") and season labels ("2026SS", "2025FW")
    r"|\d+X\d+|\d+X{1,3}[SL]|100PCT|\d+-?DEG(?:REE)?S?|\d+-?STARS?|\d{4}(?:SS|FW|AW))$|X{3,}", re.IGNORECASE)
# The phrased forms only; the bare-"code" form is shadow-only until measured.
_WRITTEN_FORMS = {"use_code", "labelled", "with_code"}
# PROVENANCE: every row this writer creates has a deal_id with this prefix, and no other writer uses it. Retention
# (services/deal_code_retention.py) deletes only such rows, and this writer refreshes only such rows, so a
# merchant_published row entered by hand, from a connected store or by any other path is never touched by either.
CRAWL_DEAL_ID_PREFIX = "dlc_"


def normalize_host(value: Optional[str]) -> str:
    """A host as an opt-out, a catalog source_domain or a page names it, in one form (review of be#2732, P3-I): trimmed,
    lower-case, a URL reduced to its host, no port, no trailing dot, no leading "*." or "www.". "" when there is none."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "://" in text or "/" in text:
        text = (urlparse(text if "://" in text else "//" + text).hostname or "")
    elif ":" in text:
        text = text.split(":", 1)[0]
    if text and not text.isascii():
        # One spelling for an internationalised host (review of be#2735 delta 3, P3-R3), BEFORE the strips below so a
        # full-width "ｗｗｗ." or a trailing "。" is stripped too (review of be#2742, P3-5).
        text = _ascii_host(text)
    text = text.strip().rstrip(".")
    while text.startswith(("*.", "www.")):  # "*.host", "www.host" and "www.*.host" all name the host (be#2735)
        text = text[2:] if text.startswith("*.") else text[4:]
    return text


def _ascii_host(text: str) -> str:
    """The punycode form a URL carries (UTS-46 nontransitional, as browsers map it: "straße" stays "xn--strae-oqa").
    A label that cannot be encoded is kept as it is (it still matches the same spelling elsewhere). Never raises."""
    try:
        import idna  # a dependency of requests and httpx

        mapped = idna.uts46_remap(text, std3_rules=False, transitional=False)
        encode = lambda label: idna.alabel(label).decode("ascii")  # noqa: E731
    except Exception:  # noqa: BLE001 - no idna package, or a string UTS-46 cannot map: the stdlib codec per label
        mapped = text.replace("\u3002", ".").replace("\uff0e", ".").replace("\uff61", ".")
        encode = lambda label: label.encode("idna").decode("ascii")  # noqa: E731
    labels = []
    for label in mapped.split("."):
        if label and not label.isascii():
            try:
                label = encode(label)
            except Exception:  # noqa: BLE001 - an invalid label stays unicode
                pass
        labels.append(label)
    return ".".join(labels).lower()


def seller_id_for(url: str) -> Optional[str]:
    return normalize_host(urlparse(url or "").hostname or "") or None


def market_evidence(market: Optional[str], page_currency: Optional[str]) -> Optional[str]:
    m = str(market or "").strip().upper()
    expected = _MARKET_CURRENCY.get(m)
    return m if expected and str(page_currency or "").strip().upper() == expected else None


def _decimal(value: Optional[str]) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() and d >= 0 else None


def row_values(captured: "deal_code_capture.CapturedCode", *, url: str, market: Optional[str],
               page_currency: Optional[str]) -> Optional[dict]:
    """The observation columns for one captured code, or None when it cannot be written (no stated kind, no seller,
    or a value that is not a number). Pure."""
    seller = seller_id_for(url)
    if not seller or not captured.code or captured.kind not in _KINDS:
        return None
    currency = str(page_currency or "").strip().upper() or None
    value = None
    row_currency = None
    if captured.kind in ("percent", "amount"):
        value = _decimal(captured.value)
        if value is None:
            return None
        if captured.kind == "percent" and not (0 < value <= 100):
            return None
        if captured.kind == "amount":
            # An amount stated in another currency than the page's is not a fact about this page (P3-A): no row.
            if not (currency and currency in _SYMBOL_CURRENCIES.get(captured.currency_symbol or "", set())):
                return None
            row_currency = currency
    hints = set(captured.hints or [])
    min_subtotal = None
    if captured.min_subtotal is not None and currency and currency in _SYMBOL_CURRENCIES.get(captured.min_subtotal_symbol or "", set()):
        min_subtotal = _decimal(captured.min_subtotal)
        row_currency = currency
    market_ev = market_evidence(market, page_currency)
    return {
        "seller_id": seller,
        "market": market_ev,
        "code": captured.code,
        "kind": captured.kind,
        "value": value,
        "currency": row_currency,
        "min_subtotal": min_subtotal,
        "new_customer_only": "new_customer_only" in hints,
        "once_per_customer": "once_per_customer" in hints,
        "segment_limited": "segment_limited" in hints,
        "usage_limited": False,
        "eligibility_known": False,
        "source": "merchant_published",
        "source_url": url,
        "terms_text": (captured.snippet or None),
    }


def not_a_code(captured: "deal_code_capture.CapturedCode") -> bool:
    """True for a digit token that names something else, or one the page itself calls an example."""
    code = str(captured.code or "")
    if _NOT_A_CODE_RE.search(code):
        return True
    snippet = str(captured.snippet or "")
    at = snippet.upper().find(code.upper()) if code else -1
    if at >= 0 and re.search(r"(?:\bexample\b|\be\.g\.|\bsuch\s+as\b|\bsample\b|\bfor\s+instance\b)", snippet[max(0, at - 40):at], re.IGNORECASE):
        return True  # "Example: use code SAVE20" (P3-N5)
    if at >= 0:
        after = snippet[at + len(code): at + len(code) + 24].lower()
        if re.match(r"^\W{0,3}\(?\s*(?:e\.g\.|(?:for\s+)?example\b|for\s+instance\b|sample\b)", after):
            return True
    return False


def _same_seller(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a) and a == b


# Merchant ids that are not one seller (ADR-009: the legacy bucket of seed rows across many hosts, served by the agent
# API as those products' merchant id). An opt-out naming one never reaches the catalog leg (review of be#2735, P2-3); it
# is counted in the retention summary, since that opt-out row then denies nothing.
PLACEHOLDER_MERCHANT_IDS = frozenset({"external_seed"})
# A merchant id whose catalog hosts span more registrable domains than this is not one seller's (review of be#2735,
# P2-N1): FAIL CLOSED for writes (every host it names is blocked) and delete nothing through it (counted).
MAX_HOSTS_PER_OPTOUT_ID = 3
_IN_CHUNK = 500
# The canonical-URL fallback reads at most this many distinct HOSTS per merchant id (P3-N2; review of be#2735 delta 3,
# P3-R1(c): hosts, not URLs, so a store's own host is not crowded out by its marketplace listings or its other URLs).
CANONICAL_HOSTS_PER_ID = 200
# Where the database cannot take a host out of a URL in SQL (SQLite: tests and local runs), this many distinct URLs per
# id are read and reduced to hosts here.
CANONICAL_URLS_PER_ID_FALLBACK = 5000
# Marketplace and platform labels (as services/canonical_source_discovery._GENERIC_DOMAINS): a canonical URL on one of
# these names where the product is listed, not the seller's own host, so it never becomes a denied host.
MARKETPLACE_LABELS = frozenset({
    "shopify", "myshopify", "amazon", "ebay", "walmart", "target", "sephora", "ulta", "iherb", "yesstyle",
    "stylevana", "oliveyoung", "etsy", "lazada", "shopee", "coupang", "rakuten", "qoo10", "wixsite", "bigcartel",
    "squarespace",
})
# Shared hosts and registry suffixes beyond services/brand_claim_service._PUBLIC_SUFFIXES: a subdomain under one is
# another store, and the registrable domain of a host under one sits one label deeper (review of be#2735 delta 3,
# P3-R1(b), P3-R3).
EXTRA_SHARED_HOSTS = frozenset({
    "shop.app", "square.site", "mybigcommerce.com", "shopifypreview.com", "company.site",
    "github.io", "vercel.app", "herokuapp.com", "netlify.app",
    "com.vn", "com.ar",
})
# The denylist read is reused for this long within one run (P3-N6); a new run (deal_code_capture.reset_stats) rereads.
DENYLIST_CACHE_SECONDS = 600


def crawl_rows():
    """This writer's own rows: its source AND its deal_id prefix ("_" escaped: it is a LIKE wildcard)."""
    escaped = CRAWL_DEAL_ID_PREFIX.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")
    return and_(observed_store_offers.c.source == "merchant_published",
                observed_store_offers.c.deal_id.like(escaped + "%", escape="\\"))


def registrable(host: Optional[str]) -> str:
    """The registrable domain of a host, as services/seller_identity.etld1 computes it (a tenant of a shared platform
    is its own registrable name). "" when there is none."""
    h = normalize_host(host)
    if not h or "." not in h:
        return ""
    labels = h.split(".")
    suffix = ".".join(labels[-2:])
    if len(labels) >= 3 and (suffix in _PUBLIC_SUFFIXES or suffix in EXTRA_SHARED_HOSTS):
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def subdomain_anchor(host: str) -> bool:
    """True when the subdomains of `host` belong to the same seller: a registrable host, never a public suffix (co.uk,
    com.sg) or a shared platform (myshopify.com, wixsite.com, ...) (review of be#2735, P3-N3)."""
    return _is_registrable_base(host) and host not in EXTRA_SHARED_HOSTS


def is_marketplace(host: str) -> bool:
    reg = registrable(host)
    return not reg or not subdomain_anchor(reg) or reg.split(".")[0] in MARKETPLACE_LABELS


def _host_forms(value: str) -> Set[str]:
    host = normalize_host(value)
    return {host, "www." + host} if host and "." in host else set()


def covers(denied: Iterable[str], seller: Optional[str]) -> bool:
    """True when `seller` is a denied host, its www. form, or a subdomain of a denied host that anchors subdomains (an
    opted-out store's own subdomains; never through a public suffix or a shared platform host)."""
    denied = denied if isinstance(denied, (set, frozenset)) else set(denied)
    host = normalize_host(seller)
    if not host or not denied:
        return False
    if host in denied:
        return True
    parts = host.split(".")
    for k in range(1, len(parts) - 1):
        parent = ".".join(parts[k:])
        if parent in denied and subdomain_anchor(parent):
            return True
    return False


@dataclass(frozen=True)
class DenySet:
    """`delete`: confirmed hosts (bare and www.), through which retention deletes and the writer blocks. `block`: those
    plus hosts that only block writes (an over-broad merchant id's hosts; hosts known only from canonical URLs).
    `counts`: notes for the retention summary, never naming an id."""
    delete: frozenset
    block: frozenset
    counts: Dict[str, int]

    def blocks(self, seller: Optional[str]) -> bool:
        return covers(self.block, seller)


async def read_denyset() -> DenySet:
    """The opt-out denylist as hosts (ADR-027 Q20), read ONCE, uncorrelated (review of be#2735, P2-1; be#2732, P3-J).
      - An id with a dot is a host: trimmed, normalised (`normalize_host`), with its www. form.
      - An id without one is a merchant id (merchant ids carry no dot): its catalog hosts, by an indexed read of
        `merchant_id IN (id as entered, trimmed, lower-case)` (source_domain). For an id whose rows carry no
        source_domain, up to CANONICAL_HOSTS_PER_ID distinct canonical-URL hosts OF THAT ID are read; their hosts block
        writes only (a canonical URL may name a marketplace), and marketplace hosts are dropped (P3-N2).
      - The placeholder id never reaches the catalog (P2-3). An id whose hosts span more than MAX_HOSTS_PER_OPTOUT_ID
        registrable domains blocks writes on all of them and deletes nothing (P2-N1).
    Raises when it cannot be read: callers fail closed."""
    rows = await database.fetch_all(select(deal_lane_optout.c.seller_id))
    raw = {str(r["seller_id"] or "") for r in rows}
    ids = sorted({i.strip() for i in raw} - {""})
    counts: Dict[str, int] = {}
    delete: Set[str] = set()
    block: Set[str] = set()
    if not ids:
        return DenySet(frozenset(), frozenset(), counts)

    def bump(name: str) -> None:
        counts[name] = counts.get(name, 0) + 1

    merchants: Dict[str, Set[str]] = {}  # lower-case id -> the spellings to look up
    for i in ids:
        if "." in normalize_host(i):
            host = normalize_host(i)
            if host in EXTRA_SHARED_HOSTS or host in _PUBLIC_SUFFIXES:
                # A shared platform or registry suffix covers only that exact host, never the stores under it
                # (review of be#2742, P3-4): reported, as the opt-out probably meant one store.
                bump("optout_ids_shared_host")
            delete |= _host_forms(i)
        elif i.lower() in PLACEHOLDER_MERCHANT_IDS:
            bump("optout_ids_placeholder")
        else:
            merchants.setdefault(i.lower(), set()).update({i, i.lower()})
    merchants_raw = {r for r in raw if r.strip() and r.strip().lower() in merchants}  # padded as stored
    lookup = sorted(set().union(*merchants.values(), merchants_raw)) if merchants else []
    confirmed: Dict[str, Set[str]] = {}
    for k in range(0, len(lookup), _IN_CHUNK):
        found = await database.fetch_all(
            select(catalog_products.c.merchant_id, catalog_products.c.source_domain)
            .where(and_(catalog_products.c.merchant_id.in_(lookup[k:k + _IN_CHUNK]),
                        catalog_products.c.source_domain.isnot(None)))
            .distinct()
        )
        for r in found:
            host = normalize_host(r["source_domain"])
            if host and "." in host:
                confirmed.setdefault(str(r["merchant_id"]).strip().lower(), set()).add(host)
    for merchant, hosts in confirmed.items():
        regs = {registrable(h) or h for h in hosts}
        forms = set().union(*(_host_forms(h) for h in hosts))
        if len(regs) > MAX_HOSTS_PER_OPTOUT_ID:
            bump("optout_ids_too_broad")
            block |= forms
            continue
        delete |= forms  # 2-3 registrable domains is a normal store: no note (review of be#2735 delta 3)
    for merchant in sorted(set(merchants) - set(confirmed)):
        spellings = sorted(merchants[merchant] | {r for r in merchants_raw if r.strip().lower() == merchant})
        hosts = await _canonical_hosts(spellings)
        hosts = {h for h in hosts if h and "." in h and not is_marketplace(h)}
        if not hosts:
            bump("optout_ids_unmapped")
            continue
        bump("optout_ids_canonical_only")
        if len({registrable(h) for h in hosts}) > MAX_HOSTS_PER_OPTOUT_ID:
            bump("optout_ids_too_broad")
        block |= set().union(*(_host_forms(h) for h in hosts))
    return DenySet(frozenset(delete), frozenset(delete | block), counts)


def _is_sqlite() -> bool:
    return str(getattr(database, "url", "") or "").lower().startswith("sqlite")


# The host of an absolute URL, in SQL (Postgres: a two-argument substring is a POSIX regex with one capture group).
# Tolerant as the Python parse is (reviews of be#2742, P2-B and P2-B'): leading space, no scheme, "//host", userinfo up
# to the LAST "@" before the path, and space around the host. A scheme counts only before "//" (so "host:8080/p" reads
# "host", not the port), and the column loses tab, CR and LF first, as urlsplit does.
_URL_HOST_PATTERN = r"^\s*(?:[A-Za-z][A-Za-z0-9+.-]*://|//)?\s*(?:[^/?#]*@)?\s*([^/:?#\s]+)"
_URL_CONTROL_CHARS = "\t\n\r"


async def _canonical_hosts(spellings) -> Set[str]:
    """Up to CANONICAL_HOSTS_PER_ID distinct hosts of one merchant id's canonical URLs, the first by host order."""
    p = catalog_products
    where = and_(p.c.merchant_id.in_(spellings), p.c.canonical_url.isnot(None))
    if _is_sqlite():
        found = await database.fetch_all(
            select(p.c.canonical_url).where(where).distinct().order_by(p.c.canonical_url)
            .limit(CANONICAL_URLS_PER_ID_FALLBACK))
        hosts = sorted({normalize_host(r["canonical_url"]) for r in found} - {""})
        return set(hosts[:CANONICAL_HOSTS_PER_ID])
    url = func.translate(p.c.canonical_url, _URL_CONTROL_CHARS, "")
    host = func.lower(func.substring(url, _URL_HOST_PATTERN)).label("host")
    found = await database.fetch_all(
        select(host).where(where).distinct().order_by(host).limit(CANONICAL_HOSTS_PER_ID))
    return {normalize_host(r["host"]) for r in found if r["host"]} - {""}


async def denied_hosts() -> Tuple[Set[str], Dict[str, int]]:
    """The hosts retention may delete through, and the notes. (The writer blocks on `read_denyset().block`.)"""
    deny = await read_denyset()
    return set(deny.delete), dict(deny.counts)


_DENY_CACHE: Dict[str, Any] = {}


async def _denyset_for_run() -> DenySet:
    """The denylist, read once per refresh run (P3-N6). A failed read is not cached: the caller fails closed."""
    generation = deal_code_capture.run_generation()
    cached = _DENY_CACHE.get("entry")
    if cached and cached[0] == generation and time.monotonic() - cached[1] < DENYLIST_CACHE_SECONDS:
        return cached[2]
    deny = await read_denyset()
    _DENY_CACHE["entry"] = (generation, time.monotonic(), deny)
    return deny


async def _opted_out(seller: str) -> bool:
    """Raises when it cannot be read (the caller fails closed)."""
    return (await _denyset_for_run()).blocks(seller)


def _existing_row():
    o = observed_store_offers
    return select(o.c.deal_id, o.c.source, o.c.created_at, o.c.kind, o.c.value, o.c.currency)


def _same_terms(existing, values: dict) -> bool:
    """The sighting states the same discount the row holds (kind, value, currency)."""
    def num(x):
        d = _decimal(x)
        return None if d is None else d.normalize()

    return (existing["kind"] == values["kind"] and num(existing["value"]) == num(values["value"])
            and (existing["currency"] or None) == (values["currency"] or None))


def _aware(value) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _keeps_bar_text(deal_id: str, created_at, now: datetime) -> bool:
    """A re-sighting refreshes the bar text only while retention would keep it (review of be#2735, P3-3): not once the
    row has been checked, and not after TERMS_TEXT_DAYS from creation."""
    from services import deal_code_retention as retention  # imported here: retention imports this module

    created = _aware(created_at)
    if created is not None and created < now - timedelta(days=retention.TERMS_TEXT_DAYS):
        return False
    checked = await database.fetch_one(
        select(observed_store_offer_verifications.c.deal_id)
        .where(and_(observed_store_offer_verifications.c.deal_id == deal_id, retention.checked_verification()))
        .limit(1)
    )
    return checked is None


async def record(codes: Iterable["deal_code_capture.CapturedCode"], *, url: str, market: Optional[str],
                 page_currency: Optional[str], final_url: Optional[str] = None) -> None:
    """Write this page's captured codes. Never raises. `final_url` is where the fetch ended; a page served by another
    host than the one requested is not written."""
    try:
        codes = list(codes or [])
        if not codes:
            return
        seller = seller_id_for(url)
        if not seller:
            deal_code_capture.bump_write("skipped_no_seller", len(codes))
            return
        # Fail closed (P3-D): without where the fetch ended we cannot know whose page this was.
        if final_url is None or not _same_seller(seller_id_for(final_url), seller):
            deal_code_capture.bump_write("skipped_host_changed" if final_url is not None else "skipped_no_final_url", len(codes))
            return
        loose = [c for c in codes if c.form not in _WRITTEN_FORMS]
        if loose:
            deal_code_capture.bump_write("skipped_loose_form", len(loose))
        codes = [c for c in codes if c.form in _WRITTEN_FORMS]
        # A code with no digit stays in the shadow counts (review of be#2732, P2-A): all-caps prose after a code phrase
        # ("USE CODE THROUGH MIDNIGHT", "Coupon code: NEWSLETTER") reads as a letters-only token far more often than
        # stores print letters-only codes. Measured in prod before any letters-only code is written.
        no_digit = [c for c in codes if not any(ch.isdigit() for ch in c.code)]
        if no_digit:
            deal_code_capture.bump_write("skipped_no_digit", len(no_digit))
        codes = [c for c in codes if any(ch.isdigit() for ch in c.code)]
        not_codes = [c for c in codes if not_a_code(c)]
        if not_codes:
            deal_code_capture.bump_write("skipped_not_a_code", len(not_codes))
        codes = [c for c in codes if not not_a_code(c)]
        if not codes:
            return
        try:
            if await _opted_out(seller):
                deal_code_capture.bump_write("skipped_optout", len(codes))
                return
        except Exception as exc:  # noqa: BLE001 - an opt-out we cannot read means no write
            deal_code_capture.bump_write("skipped_optout_unreadable", len(codes))
            logger.warning("deal code capture write: opt-out check failed: %s", type(exc).__name__)
            return
        now = datetime.now(timezone.utc)
        for captured in codes:
            try:
                values = row_values(captured, url=url, market=market, page_currency=page_currency)
                if values is None:
                    deal_code_capture.bump_write("skipped_no_kind" if captured.kind not in _KINDS else "skipped_unreadable")
                    continue
                if values["market"] is None:
                    deal_code_capture.bump_write("no_market_evidence")
                key = observed_store_offer_key(
                    seller_id=values["seller_id"], market=values["market"], code=values["code"],
                    platform_rule_ref=None, program_id=None,
                )
                # A later sighting refreshes WHEN and WHERE it was seen (P3-F): kind and value are set once, at insert, so a
                # sighting can never overwrite what a verifier has since established. A restriction can be ADDED by a
                # later sighting, never cleared (be#2732 P3-F revised): a guest cart must not pass a first-order code.
                observation = {"source_url": values["source_url"]}
                for flag in ("new_customer_only", "once_per_customer", "segment_limited"):
                    if values[flag]:
                        observation[flag] = True
                existing = await database.fetch_one(
                    _existing_row()
                    .where(observed_store_offers.c.deal_key == key)
                )
                if existing is None:
                    # The per-run budget counts NEW rows only (P3-E); a repeat sighting refreshes without spending it.
                    cap = deal_code_capture.take_write_slot(seller)
                    if cap:
                        deal_code_capture.bump_write(cap)
                        continue
                    try:
                        await database.execute(
                            observed_store_offers.insert().values(
                                deal_id=CRAWL_DEAL_ID_PREFIX + uuid.uuid4().hex, deal_key=key, observed_at=now,
                                created_at=now, updated_at=now, **values,
                            )
                        )
                        deal_code_capture.bump_write("inserted")
                        continue
                    except Exception:  # noqa: BLE001 - the driver's own unique-violation class differs per dialect
                        # Another writer may have inserted the same identity first: then this sighting refreshes that
                        # row. If no row exists, the insert failed for another reason and that is re-raised.
                        existing = await database.fetch_one(
                            _existing_row()
                            .where(observed_store_offers.c.deal_key == key)
                        )
                        if existing is None:
                            raise
                        deal_code_capture.release_write_slot(seller)  # no new row: the budget is not spent (P3-E)
                if existing["source"] != "merchant_published" or not str(existing["deal_id"]).startswith(CRAWL_DEAL_ID_PREFIX):
                    # Another source, or a merchant_published row this writer did not create: never overwritten.
                    deal_code_capture.bump_write("skipped_other_source")
                    continue
                # The bar text follows the row's own discount: refreshed only by a sighting that states the same kind and
                # value, never emptied, and only while retention would keep it (be#2735 P3-3).
                if not _same_terms(existing, values):
                    deal_code_capture.bump_write("terms_differ")
                elif values["terms_text"] and await _keeps_bar_text(existing["deal_id"], existing["created_at"], now):
                    observation["terms_text"] = values["terms_text"]
                await database.execute(
                    observed_store_offers.update()
                    .where(and_(observed_store_offers.c.deal_key == key, crawl_rows()))
                    .values(**observation, observed_at=now, updated_at=now)
                )
                deal_code_capture.bump_write("updated")
            except Exception as exc:  # noqa: BLE001 - one code's failure never costs the others or the page
                deal_code_capture.bump_write("errors")
                logger.warning("deal code capture write failed: %s", type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 - recording never costs a page refresh
        deal_code_capture.bump_write("errors")
        logger.warning("deal code capture write failed: %s", type(exc).__name__)
