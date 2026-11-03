"""Merchant-published discount codes in a store page we ALREADY fetched (ADR-027 D2 `merchant_published`),
in SHADOW: it counts and logs what it would collect, and writes nothing.

WHY HERE. A 2026-11-03 census of the text we store found almost no codes: we keep product descriptions,
not the announcement bars and promo banners where stores print them, and the curated feed strips code lines
on purpose. The page refresh (`external_offers_service.resolve_external_offer`) already downloads each
store page, so reading its promo regions costs no extra request to any store.

WHAT IT READS. Only text inside elements whose class or id names a promo region (announcement bar, promo
bar, top bar, marquee, ticker, header message, banner). Product copy is never read, so a shade or product
code in a description cannot become a deal. A code is an all-caps or digit token after "use/enter/apply
code", or after "promo/discount/coupon/voucher code:". Around it, the terms the region states: percent or
amount off, free shipping, a minimum spend, and eligibility hints (new customers, sign-up, once per
customer). Those are metadata only (ADR-027 D2): a code is evidence of nothing until a checkout applies it.

SHADOW ONLY, behind DEAL_CODE_CAPTURE_SHADOW (default off; set on the refresh job alone). With it on, each
page with a code logs one `DEAL_CODE_CAPTURE_PAGE` JSON line and the counters below accumulate for the
job's summary. Nothing is written to `observed_store_offers` here; writing waits for the shadow numbers.
Every failure is swallowed: capture can never cost a page refresh.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# A promo region's class or id. Shopify's announcement-bar sections and the common theme spellings.
_REGION_RE = re.compile(
    r"announcement|promo[-_ ]?bar|promo[-_ ]?banner|top[-_ ]?bar|marquee|ticker|header[-_ ]?message|"
    r"site[-_ ]?banner|notification[-_ ]?bar|free[-_ ]?shipping[-_ ]?bar",
    re.IGNORECASE,
)
_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
                        "track", "wbr"})
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})
_MAX_REGION_CHARS = 4000

# The code is case-sensitive (an all-caps or digit token, as stores print them); the words before it are not.
_CODE_RES = (
    re.compile(r"\b(?i:use|enter|apply)\s+(?i:the\s+)?(?i:(?:promo|discount|coupon|voucher)\s+)?(?i:code)\s*:?\s*"
               r"[\"'“‘]?([A-Z0-9][A-Z0-9_-]{2,19})\b"),
    re.compile(r"\b(?i:promo|discount|coupon|voucher)\s*(?i:code)\s*[:：]\s*[\"'“‘]?([A-Z0-9][A-Z0-9_-]{2,19})\b"),
)
_STOP_CODES = frozenset({"CODE", "PROMO", "COUPON", "DISCOUNT", "FREE", "SALE", "OFF", "THE", "AND", "AT", "NOW"})

_PERCENT_RE = re.compile(r"(\d{1,2})\s?%\s?(?i:off)")
_AMOUNT_RE = re.compile(r"([$£€])\s?(\d{1,4}(?:\.\d{2})?)\s?(?i:off)")
_FREE_SHIPPING_RE = re.compile(r"(?i)\bfree\s+(?:standard\s+)?(?:shipping|delivery)\b")
_MIN_SPEND_RE = re.compile(
    r"(?i)\b(?:(?:orders?|purchases?)\s+(?:of\s+)?(?:over|above|of|\+)?|spend(?:ing)?)\s*"
    r"([$£€])\s?(\d{1,5}(?:\.\d{2})?)"
)
_HINTS = (
    ("new_customer_only", re.compile(r"(?i)\b(?:first|1st)\s+(?:order|purchase)|new\s+customers?\b")),
    ("segment_limited", re.compile(r"(?i)\b(?:sign\s*up|subscribe|subscriber|newsletter|members?\b|join\s+our)")),
    ("once_per_customer", re.compile(r"(?i)\bonce\s+per\s+customer|one\s+(?:use|time)\s+(?:per|only)|single[- ]use")),
    ("has_exclusions", re.compile(r"(?i)\bexclud(?:es|ing)|not\s+valid\s+on|exclusions?\s+apply")),
)
_WINDOW = 90


class _RegionParser(HTMLParser):
    """Collects the text of every element whose class or id names a promo region, nested elements included."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.regions: List[str] = []
        self._stack: List[str] = []
        self._region_depth: Optional[int] = None
        self._skip_depth: Optional[int] = None
        self._buffer: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Any]) -> None:
        if tag in _VOID_TAGS:
            return
        self._stack.append(tag)
        depth = len(self._stack)
        if self._skip_depth is None and tag in _SKIP_TAGS:
            self._skip_depth = depth
        if self._region_depth is None:
            # The tag itself (a theme's custom element, e.g. <announcement-bar>), its class and id, and
            # an aria-label naming it ("Announcement").
            names = " ".join([tag] + [str(v or "") for k, v in attrs if k in ("class", "id", "aria-label")])
            if _REGION_RE.search(names):
                self._region_depth = depth
                self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS or tag not in self._stack:
            return
        # Close up to the matching open tag (tolerates unclosed children, as browsers do).
        while self._stack:
            depth = len(self._stack)
            closed = self._stack.pop()
            if self._skip_depth is not None and depth <= self._skip_depth:
                self._skip_depth = None
            if self._region_depth is not None and depth <= self._region_depth:
                text = re.sub(r"\s+", " ", " ".join(self._buffer)).strip()
                if text:
                    self.regions.append(text[:_MAX_REGION_CHARS])
                self._region_depth = None
                self._buffer = []
            if closed == tag:
                break

    def handle_data(self, data: str) -> None:
        if self._region_depth is not None and self._skip_depth is None:
            self._buffer.append(data)


def promo_regions(html: str) -> List[str]:
    parser = _RegionParser()
    parser.feed(html or "")
    parser.close()
    return parser.regions


@dataclass
class CapturedCode:
    code: str
    kind: Optional[str]  # percent | amount | free_shipping | None (not stated near the code)
    value: Optional[str]
    currency_symbol: Optional[str]
    min_subtotal: Optional[str]
    hints: List[str] = field(default_factory=list)
    snippet: str = ""


def _terms_near(text: str, start: int, end: int) -> Dict[str, Any]:
    window = text[max(0, start - _WINDOW): end + _WINDOW]
    terms: Dict[str, Any] = {"kind": None, "value": None, "currency_symbol": None, "min_subtotal": None}
    percent = _PERCENT_RE.search(window)
    amount = _AMOUNT_RE.search(window)
    if percent:
        terms.update(kind="percent", value=percent.group(1))
    elif amount:
        terms.update(kind="amount", value=amount.group(2), currency_symbol=amount.group(1))
    elif _FREE_SHIPPING_RE.search(window):
        terms.update(kind="free_shipping")
    spend = _MIN_SPEND_RE.search(window)
    if spend:
        terms["min_subtotal"] = spend.group(2)
        terms["currency_symbol"] = terms["currency_symbol"] or spend.group(1)
    terms["hints"] = [name for name, pattern in _HINTS if pattern.search(window)]
    terms["snippet"] = re.sub(r"\s+", " ", window).strip()[:160]
    return terms


def codes_in_region(text: str) -> List[CapturedCode]:
    found: Dict[str, CapturedCode] = {}
    for pattern in _CODE_RES:
        for match in pattern.finditer(text or ""):
            code = match.group(1).strip("-_")
            if code in _STOP_CODES or code.isdigit() or len(code) < 3 or code.upper() in found:
                continue
            terms = _terms_near(text, match.start(), match.end())
            found[code.upper()] = CapturedCode(code=code, **terms)
    return list(found.values())


def capture_codes(html: str) -> List[CapturedCode]:
    out: Dict[str, CapturedCode] = {}
    for region in promo_regions(html):
        for captured in codes_in_region(region):
            out.setdefault(captured.code.upper(), captured)
    return list(out.values())


# ---- shadow counters (one process = one job run) ------------------------------------------------------

_LOCK = threading.Lock()
_STATS: Dict[str, Any] = {}
_HOST_CODES: set = set()
_HOSTS_WITH_CODES: set = set()


def reset_stats() -> None:
    with _LOCK:
        _STATS.clear()
        _STATS.update({
            "pages_scanned": 0, "pages_with_region": 0, "pages_with_code": 0, "codes_seen": 0,
            "by_kind": {}, "by_hint": {}, "by_market": {}, "errors": 0,
        })
        _HOST_CODES.clear()
        _HOSTS_WITH_CODES.clear()


reset_stats()


def stats() -> Dict[str, Any]:
    with _LOCK:
        out = json.loads(json.dumps(_STATS))
        out["distinct_host_codes"] = len(_HOST_CODES)
        out["hosts_with_codes"] = len(_HOSTS_WITH_CODES)
        return out


def _bump(bucket: Dict[str, int], key: Optional[str]) -> None:
    name = key or "unstated"
    bucket[name] = bucket.get(name, 0) + 1


def capture_shadow(html: str, *, url: str, market: str, page_currency: Optional[str]) -> None:
    """Count and log the codes in this page's promo regions. Never raises; writes nothing."""
    try:
        regions = promo_regions(html)
        codes: Dict[str, CapturedCode] = {}
        for region in regions:
            for captured in codes_in_region(region):
                codes.setdefault(captured.code.upper(), captured)
        host = (urlparse(url).hostname or "").lower()
        host = host[4:] if host.startswith("www.") else host
        with _LOCK:
            _STATS["pages_scanned"] += 1
            if regions:
                _STATS["pages_with_region"] += 1
            if codes:
                _STATS["pages_with_code"] += 1
                _bump(_STATS["by_market"], market)
                _HOSTS_WITH_CODES.add(host)
            for captured in codes.values():
                if (host, captured.code.upper()) not in _HOST_CODES:
                    _HOST_CODES.add((host, captured.code.upper()))
                    _STATS["codes_seen"] += 1
                    _bump(_STATS["by_kind"], captured.kind)
                    for hint in captured.hints or ["none"]:
                        _bump(_STATS["by_hint"], hint)
        if codes:
            print("DEAL_CODE_CAPTURE_PAGE " + json.dumps({
                "host": host,
                "market": market,
                "page_currency": page_currency,
                "codes": [asdict(c) for c in codes.values()],
            }, ensure_ascii=False, separators=(",", ":")), flush=True)
    except Exception as exc:  # noqa: BLE001 - shadow capture never costs a page refresh
        with _LOCK:
            _STATS["errors"] = _STATS.get("errors", 0) + 1
        logger.warning("deal code capture failed: %s", type(exc).__name__)
