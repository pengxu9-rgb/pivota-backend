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
  - a seller on the opt-out denylist (`denied_hosts`: an id as a host or its www. form, or the catalog hosts of an id
    as a merchant id; ADR-027 Q20) (`skipped_optout`). If that check cannot be read, nothing is written
    (`skipped_optout_unreadable`): fail closed;
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
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, Optional, Set, Tuple
from urllib.parse import urlparse

from sqlalchemy import and_, select

from db.catalog import catalog_products
from db.database import database
from db.deal_lane import deal_lane_optout, observed_store_offer_key, observed_store_offer_verifications, observed_store_offers
from services import deal_code_capture

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
    r"^(?:\d+(?:ST|ND|RD|TH|H|HR|HRS|AM|PM|X|PER)|\d+-?DAYS?|SPF\d+|[A-Z]\d[A-Z])$|X{3,}", re.IGNORECASE)
# The phrased forms only; the bare-"code" form is shadow-only until measured.
_WRITTEN_FORMS = {"use_code", "labelled", "with_code"}
# PROVENANCE: every row this writer creates has a deal_id with this prefix, and no other writer uses it. Retention
# (services/deal_code_retention.py) deletes only such rows, and this writer refreshes only such rows, so a
# merchant_published row entered by hand, from a connected store or by any other path is never touched by either.
CRAWL_DEAL_ID_PREFIX = "dlc_"


def normalize_host(value: Optional[str]) -> str:
    """A host as an opt-out, a catalog source_domain or a page names it, in one form (review of be#2732, P3-I): trimmed,
    lower-case, a URL reduced to its host, no port, no trailing dot, no leading "www.". "" when there is none."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "://" in text or "/" in text:
        text = (urlparse(text if "://" in text else "//" + text).hostname or "")
    elif ":" in text:
        text = text.split(":", 1)[0]
    text = text.strip().rstrip(".")
    if text.startswith("www."):
        text = text[4:]
    return text


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
    if at >= 0:
        after = snippet[at + len(code): at + len(code) + 24].lower()
        if re.match(r"^\W{0,3}\(?\s*(?:e\.g\.|(?:for\s+)?example\b|sample\b)", after):
            return True
    return False


def _same_seller(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a) and a == b


# Merchant ids that are not one seller (ADR-009: the legacy bucket of seed rows across many hosts, served by the agent
# API as those products' merchant id). An opt-out naming one never reaches the catalog leg (review of be#2735, P2-3).
PLACEHOLDER_MERCHANT_IDS = frozenset({"external_seed"})
# An opt-out naming a merchant id the catalog ties to more hosts than this is not one seller's: its catalog leg is
# ignored (counted), and only a host-level opt-out stays exact. An ADR-009 observed id is one brand on one domain.
MAX_HOSTS_PER_OPTOUT_ID = 3
_IN_CHUNK = 500
_CANONICAL_URL_READ_LIMIT = 2000


def crawl_rows():
    """This writer's own rows: its source AND its deal_id prefix ("_" escaped: it is a LIKE wildcard)."""
    escaped = CRAWL_DEAL_ID_PREFIX.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")
    return and_(observed_store_offers.c.source == "merchant_published",
                observed_store_offers.c.deal_id.like(escaped + "%", escape="\\"))


# Hosts shared by many sellers: an opt-out of one covers that host only, never its subdomains (each a different store).
SHARED_PLATFORM_HOSTS = frozenset({"myshopify.com", "shopify.com", "shop.app"})


def _host_forms(value: str) -> Set[str]:
    host = normalize_host(value)
    return {host, "www." + host} if host else set()


def covers(denied: Set[str], seller: Optional[str]) -> bool:
    """True when `seller` is a denied host, its www. form, or a subdomain of a denied host (an opted-out store's own
    subdomains, review of be#2732, P3-I) — never through a shared platform host."""
    host = normalize_host(seller)
    if not host or not denied:
        return False
    if host in denied:
        return True
    parts = host.split(".")
    for k in range(1, len(parts) - 1):
        parent = ".".join(parts[k:])
        if parent in denied and parent not in SHARED_PLATFORM_HOSTS:
            return True
    return False


async def denied_hosts() -> Tuple[Set[str], Dict[str, int]]:
    """Every host the opt-out denylist covers (ADR-027 Q20), normalised (`normalize_host`), each in its bare and www.
    form: an id read as a host, and the catalog hosts of an id read as a merchant id (source_domain, or the canonical
    URL's host for rows with none). Read ONCE, uncorrelated (review of be#2735, P2-1; be#2732, P3-J): one read of the
    small opt-out table, then indexed reads of catalog_products by `merchant_id IN (id as entered, trimmed, lower-case)`.
    Placeholder ids and ids spanning more than MAX_HOSTS_PER_OPTOUT_ID hosts never reach the catalog leg (P2-3).
    Returns (hosts, counts); counts never name an id. Raises when it cannot be read: callers fail closed."""
    rows = await database.fetch_all(select(deal_lane_optout.c.seller_id))
    raw = {str(r["seller_id"] or "") for r in rows}
    ids = sorted({i.strip() for i in raw} - {""})
    counts: Dict[str, int] = {}
    hosts: Set[str] = set()
    if not ids:
        return hosts, counts

    def bump(name: str) -> None:
        counts[name] = counts.get(name, 0) + 1

    for i in ids:
        hosts |= _host_forms(i)
    lookup = set()
    for i in ids:
        if i.lower() in PLACEHOLDER_MERCHANT_IDS:
            bump("optout_ids_placeholder")
            continue
        lookup |= {i, i.lower()}
    lookup |= {r for r in raw if r.strip() and r.strip().lower() not in PLACEHOLDER_MERCHANT_IDS}  # padded as stored
    domains: Dict[str, Set[str]] = {}
    ordered = sorted(lookup)
    for k in range(0, len(ordered), _IN_CHUNK):
        found = await database.fetch_all(
            select(catalog_products.c.merchant_id, catalog_products.c.source_domain)
            .where(and_(catalog_products.c.merchant_id.in_(ordered[k:k + _IN_CHUNK]),
                        catalog_products.c.source_domain.isnot(None)))
            .distinct()
        )
        for r in found:
            host = normalize_host(r["source_domain"])
            if host:
                domains.setdefault(str(r["merchant_id"]).strip().lower(), set()).add(host)
    # An id whose catalog rows carry no source_domain: the host of their canonical URLs (bounded read).
    unmapped = sorted({i for i in lookup if i.strip().lower() not in domains})
    if unmapped:
        found = await database.fetch_all(
            select(catalog_products.c.merchant_id, catalog_products.c.canonical_url)
            .where(and_(catalog_products.c.merchant_id.in_(unmapped[:_IN_CHUNK]),
                        catalog_products.c.canonical_url.isnot(None)))
            .limit(_CANONICAL_URL_READ_LIMIT)
        )
        for r in found:
            host = normalize_host(r["canonical_url"])
            if host:
                domains.setdefault(str(r["merchant_id"]).strip().lower(), set()).add(host)
    for merchant, merchant_hosts in domains.items():
        if merchant in PLACEHOLDER_MERCHANT_IDS:
            continue
        if len(merchant_hosts) > MAX_HOSTS_PER_OPTOUT_ID:
            bump("optout_ids_too_broad")
            continue
        if len(merchant_hosts) > 1:
            bump("optout_ids_multi_host")
        for h in merchant_hosts:
            hosts |= _host_forms(h)
    return hosts, counts


async def _opted_out(seller: str) -> bool:
    """Raises when it cannot be read (the caller fails closed)."""
    hosts, _ = await denied_hosts()
    return covers(hosts, seller)


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
