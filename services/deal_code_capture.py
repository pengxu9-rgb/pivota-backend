"""Merchant-published discount codes in a store page we ALREADY fetched (ADR-027 D2 `merchant_published`),
in SHADOW: it counts and logs what it would collect, and writes nothing.

WHY HERE. A 2026-11-03 census of the text we store found almost no codes: we keep product descriptions,
not the announcement bars and promo banners where stores print them, and the curated feed strips code lines
on purpose. The page refresh (`external_offers_service.resolve_external_offer`) already downloads each
store page, so reading its promo regions costs no extra request to any store.

WHAT IT READS. Only the first `_BODY_BUDGET` characters from `<body` (bars sit at the top), and in them only
the text of elements that NAME a promo region: a whole class or id token (or the tag name) such as
announcement / announcement-bar, promo-bar, promo-banner, top-bar, marquee, ticker-bar, header-message,
site-banner, notification-bar, free-shipping-bar, or an aria-label "Announcement(s)". A bare "banner" is
not one (hero banners hold product copy), a token that only contains a name ("product-sticker",
"desktop-bar") is not one, and html / body / main / header are never a region root, so a body class like
`has-announcement-bar` cannot turn the whole page into one. Script, style, noscript, template and svg
content is skipped. Hidden text IS read: we parse markup, not the rendering, and a rotating bar keeps its
other slides hidden.

WHAT IS A CODE. An all-caps or digit token after "use/enter/apply code", or after "promo/discount/coupon/
voucher code:", with at least one letter (four, if it has no digit), not a stop word ("USE CODE BELOW"),
not followed by "shade"/"swatch"/"tone" (a shade name, "NC20 shade"), not a gift-card style token
(three or more dash groups, or "gift card" next to it). Its shape and the phrase that introduced it are
counted separately, so shade-like short tokens (NC20) stay visible in the numbers. Around it, the terms
the region states: percent or amount off, free shipping, a minimum spend, and eligibility hints (new
customers, sign-up, once per customer, exclusions); each stated term goes to the NEAREST code. Those are
metadata only (ADR-027 D2): a code is evidence of nothing until a checkout applies it.

BOUNDED. The parse is linear (open-tag counts, implicit closing of p/li/option/dt/dd/tr/td/th, depth cap
`_MAX_DEPTH`), a region keeps at most `_MAX_REGION_CHARS`, a page at most `_MAX_CODES_PER_PAGE` codes, and
a snippet at most 160 characters, so a page's log line stays far under Cloud Logging's 256 KB entry limit.

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
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# A promo region's name, matched against ONE whole class/id token or the tag name: it must start the token
# or follow "-"/"_", and end the token or precede "-"/"_" ("shopify-section-announcement-bar" yes,
# "product-sticker" / "desktop-bar" / "website-banner" no).
_REGION_TOKEN_RE = re.compile(
    r"(?:^|[-_])(?:announcements?(?:[-_]?(?:bar|banner|message|text))?|promo[-_]?(?:bar|banner|strip)|"
    r"top[-_]?bar|marquee|ticker[-_]?(?:bar|tape)|header[-_]?message|site[-_]?banner|notification[-_]?bar|"
    r"free[-_]?shipping[-_]?bar)(?=$|[-_])",
    re.IGNORECASE,
)
# aria-label is prose ("Top bar menu"), so only the announcement label itself counts.
_REGION_LABEL_RE = re.compile(r"^\s*announcements?(?:\s+bar)?\s*$", re.IGNORECASE)
_NEVER_REGION_ROOTS = frozenset({"html", "body", "main", "header"})
_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
                        "track", "wbr"})
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})
# Inline tags join their text without a space ("SAVE<strong>10</strong>" reads SAVE10); any other tag
# separates words.
_INLINE_TAGS = frozenset({"a", "abbr", "b", "bdi", "bdo", "cite", "code", "data", "dfn", "em", "font", "i",
                          "kbd", "mark", "q", "s", "samp", "small", "span", "strong", "sub", "sup", "time",
                          "u", "var"})
# A new one of these closes an open one of the same tag on top of the stack, as browsers do; without it
# a page of unclosed <p>/<li> grows the stack without bound.
_SIBLING_CLOSED_TAGS = frozenset({"p", "li", "option", "dt", "dd", "tr", "td", "th"})
_BODY_START_RE = re.compile(r"<body[\s>/]", re.IGNORECASE)
_BODY_BUDGET = 200_000
_MAX_DEPTH = 512
_MAX_REGION_CHARS = 4000
_MAX_CODES_PER_PAGE = 20
_SNIPPET_CHARS = 160

# The code is case-sensitive (an all-caps or digit token, as stores print them); the words before it are not.
_CODE_RES = (
    ("use_code", re.compile(
        r"\b(?i:use|enter|apply)\s+(?i:the\s+)?(?i:(?:promo|discount|coupon|voucher)\s+)?(?i:code)\s*:?\s*"
        r"[\"'“‘]?([A-Z0-9][A-Z0-9_-]{2,19})\b")),
    ("labelled", re.compile(
        r"\b(?i:promo|discount|coupon|voucher)\s*(?i:code)\s*[:：]\s*[\"'“‘]?([A-Z0-9][A-Z0-9_-]{2,19})\b")),
)
# All-caps prose after "use code" ("USE CODE BELOW FOR 10% OFF") is not a code.
_STOP_CODES = frozenset({
    "ABOVE", "AFTER", "AGAIN", "ALL", "ALSO", "AND", "ANY", "APPLIED", "APPLY", "AT", "AUTO", "AUTOMATIC",
    "AUTOMATICALLY", "BEFORE", "BELOW", "CART", "CHECKOUT", "CODE", "CODES", "COUPON", "DISCOUNT", "DURING",
    "ENDS", "ENTER", "EXPIRES", "EXTRA", "FOR", "FREE", "FROM", "HERE", "INSTORE", "NEEDED", "NONE", "NOW",
    "OFF", "ONLINE", "ONLY", "ORDER", "ORDERS", "PLUS", "PROMO", "REQUIRED", "SALE", "SHIPPING", "SITE",
    "SITEWIDE", "STORE", "THAT", "THE", "THIS", "TODAY", "TOMORROW", "TONIGHT", "UPON", "USE", "VALID",
    "VOUCHER", "WEEKEND", "WHEN", "WHILE", "WITH", "YOUR",
})
_SHADE_AFTER_RE = re.compile(r"\s*(?:shades?|swatch(?:es)?|tones?|colou?rs?|finish)\b", re.IGNORECASE)
_GIFT_CARD_RE = re.compile(r"(?i)\bgift\s*cards?\b")
_GIFT_CARD_SPAN = 40
_SHORT_ALNUM_RE = re.compile(r"[A-Z]{1,2}\d{1,3}")

_PERCENT_RE = re.compile(r"\b(\d{1,3})\s?%\s?(?i:off)\b")
_AMOUNT_RE = re.compile(r"([$£€])\s?(\d{1,4}(?:\.\d{2})?)\s?(?i:off)\b")
_FREE_SHIPPING_RE = re.compile(r"(?i)\bfree\s+(?:standard\s+)?(?:shipping|delivery)\b")
# No two adjacent whitespace runs, so no backtracking on long blank stretches.
_MIN_SPEND_RE = re.compile(
    r"(?i)\b(?:(?:orders?|purchases?)(?:\s+of)?(?:\s+(?:over|above|of)|\s*\+)?|spend(?:ing)?)\s*"
    r"([$£€])\s?(\d{1,5}(?:\.\d{2})?)"
)
_HINTS = (
    ("new_customer_only", re.compile(r"(?i)\b(?:first|1st)\s+(?:order|purchase)|new\s+customers?\b")),
    ("segment_limited", re.compile(r"(?i)\b(?:sign\s*up|subscribe|subscriber|newsletter|members?\b|join\s+our)")),
    ("once_per_customer", re.compile(r"(?i)\bonce\s+per\s+customer|one\s+(?:use|time)\s+(?:per|only)|single[- ]use")),
    ("has_exclusions", re.compile(r"(?i)\bexclud(?:es|ing)|not\s+valid\s+on|exclusions?\s+apply")),
)
_WINDOW = 90


class _TooDeep(Exception):
    pass


def _names_region(tag: str, attrs: List[Any]) -> bool:
    # The tag itself (a theme's custom element, e.g. <announcement-bar>), each class and id token, and an
    # aria-label naming it ("Announcement").
    tokens = [tag]
    label = None
    for k, v in attrs:
        if k in ("class", "id"):
            tokens.extend(str(v or "").split())
        elif k == "aria-label":
            label = str(v or "")
    if any(_REGION_TOKEN_RE.search(t) for t in tokens):
        return True
    return label is not None and bool(_REGION_LABEL_RE.match(label))


class _RegionParser(HTMLParser):
    """Collects the text of every element that names a promo region, nested elements included. Linear: open
    tags are counted per name, so a stray end tag costs O(1), and every pop pays for its own push."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.regions: List[str] = []
        self._stack: List[str] = []
        self._open: Dict[str, int] = {}
        self._region_depth: Optional[int] = None
        self._skip_depth: Optional[int] = None
        self._buffer: List[str] = []

    def _push(self, tag: str) -> None:
        if len(self._stack) >= _MAX_DEPTH:
            raise _TooDeep()
        self._stack.append(tag)
        self._open[tag] = self._open.get(tag, 0) + 1

    def _pop(self) -> str:
        depth = len(self._stack)
        closed = self._stack.pop()
        self._open[closed] -= 1
        if self._skip_depth is not None and depth <= self._skip_depth:
            self._skip_depth = None
        if self._region_depth is not None and depth <= self._region_depth:
            text = re.sub(r"\s+", " ", "".join(self._buffer)).strip()
            if text:
                self.regions.append(text[:_MAX_REGION_CHARS])
            self._region_depth = None
            self._buffer = []
        return closed

    def _separate(self, tag: str) -> None:
        if self._region_depth is not None and self._skip_depth is None and tag not in _INLINE_TAGS:
            self._buffer.append(" ")

    def handle_starttag(self, tag: str, attrs: List[Any]) -> None:
        self._separate(tag)
        if tag in _VOID_TAGS:
            return
        if tag in _SIBLING_CLOSED_TAGS and self._stack and self._stack[-1] == tag:
            self._pop()
        self._push(tag)
        depth = len(self._stack)
        if self._skip_depth is None and tag in _SKIP_TAGS:
            self._skip_depth = depth
        if (self._region_depth is None and self._skip_depth is None and tag not in _NEVER_REGION_ROOTS
                and _names_region(tag, attrs)):
            self._region_depth = depth
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS or not self._open.get(tag):
            return
        self._separate(tag)
        # Close up to the matching open tag (tolerates unclosed children, as browsers do).
        while self._stack:
            if self._pop() == tag:
                break

    def handle_data(self, data: str) -> None:
        if self._region_depth is not None and self._skip_depth is None:
            self._buffer.append(data)


@dataclass
class _Scan:
    regions: List[str]
    over_budget: bool = False
    too_deep: bool = False


def _scan(html: Any) -> _Scan:
    if not isinstance(html, str):
        return _Scan(regions=[])
    body = _BODY_START_RE.search(html)
    start = body.start() if body else 0
    parser = _RegionParser()
    too_deep = False
    try:
        parser.feed(html[start:start + _BODY_BUDGET])
        parser.close()
    except _TooDeep:
        too_deep = True
    return _Scan(regions=parser.regions, over_budget=len(html) - start > _BODY_BUDGET, too_deep=too_deep)


def promo_regions(html: Any) -> List[str]:
    return _scan(html).regions


@dataclass
class CapturedCode:
    code: str
    kind: Optional[str]  # percent | amount | free_shipping | None (not stated near the code)
    value: Optional[str]
    currency_symbol: Optional[str]
    min_subtotal: Optional[str]
    hints: List[str] = field(default_factory=list)
    snippet: str = ""
    shape: str = ""  # letters | short_alnum (shade-like, e.g. NC20) | alnum
    form: str = ""  # use_code ("use/enter/apply code X") | labelled ("promo code: X")


def _shape(code: str) -> str:
    if not any(ch.isdigit() for ch in code):
        return "letters"
    return "short_alnum" if _SHORT_ALNUM_RE.fullmatch(code) else "alnum"


def _is_code(code: str, text: str, start: int, end: int) -> bool:
    if len(code) < 3 or code.upper() in _STOP_CODES or not any(ch.isalpha() for ch in code):
        return False
    if _shape(code) == "letters" and len(code) < 4:
        return False
    if code.count("-") >= 2 or set(code) <= {"X"}:  # gift-card style groups, or an "XXXX" placeholder
        return False
    if _SHADE_AFTER_RE.match(text, end):
        return False
    return not _GIFT_CARD_RE.search(text[max(0, start - _GIFT_CARD_SPAN): end + _GIFT_CARD_SPAN])


def _nearest(spans: List[Tuple[int, int]], start: int, end: int) -> Optional[int]:
    best, best_distance = None, None
    for i, (cs, ce) in enumerate(spans):
        distance = 0 if (start < ce and cs < end) else (cs - end if end <= cs else start - ce)
        if distance <= _WINDOW and (best_distance is None or distance < best_distance):
            best, best_distance = i, distance
    return best


def codes_in_region(text: str) -> List[CapturedCode]:
    text = text or ""
    matches: List[Tuple[str, int, int, str]] = []
    seen = set()
    for form, pattern in _CODE_RES:
        for match in pattern.finditer(text):
            raw = match.group(1)
            code = raw.strip("-_")
            start = match.start(1) + (len(raw) - len(raw.lstrip("-_")))
            end = start + len(code)
            if code.upper() in seen or not _is_code(code, text, start, end):
                continue
            seen.add(code.upper())
            matches.append((code, start, end, form))
    matches.sort(key=lambda m: m[1])
    spans = [(s, e) for _, s, e, _ in matches]
    terms: List[Dict[str, Any]] = [{"percent": None, "amount": None, "free_shipping": False, "spend": None,
                                    "hints": []} for _ in matches]

    # Each stated term goes to the nearest code within the window, never to every code in it.
    def assign(pattern: "re.Pattern[str]", key: str) -> None:
        for occurrence in pattern.finditer(text):
            owner = _nearest(spans, occurrence.start(), occurrence.end())
            if owner is None:
                continue
            if key == "free_shipping":
                terms[owner][key] = True
            elif key.startswith("hint:"):
                if key[5:] not in terms[owner]["hints"]:
                    terms[owner]["hints"].append(key[5:])
            elif terms[owner][key] is None:
                terms[owner][key] = occurrence

    if matches:
        assign(_PERCENT_RE, "percent")
        assign(_AMOUNT_RE, "amount")
        assign(_FREE_SHIPPING_RE, "free_shipping")
        assign(_MIN_SPEND_RE, "spend")
        for name, pattern in _HINTS:
            assign(pattern, "hint:" + name)

    out: List[CapturedCode] = []
    for (code, start, end, form), t in zip(matches, terms):
        captured = CapturedCode(code=code, kind=None, value=None, currency_symbol=None, min_subtotal=None,
                                shape=_shape(code), form=form)
        percent, amount = t["percent"], t["amount"]
        if percent is not None and 1 <= int(percent.group(1)) <= 100:
            captured.kind, captured.value = "percent", percent.group(1)
        elif amount is not None:
            captured.kind, captured.value, captured.currency_symbol = "amount", amount.group(2), amount.group(1)
        elif t["free_shipping"]:
            captured.kind = "free_shipping"
        if t["spend"] is not None:
            captured.min_subtotal = t["spend"].group(2)
            captured.currency_symbol = captured.currency_symbol or t["spend"].group(1)
        captured.hints = [name for name, _ in _HINTS if name in t["hints"]]
        captured.snippet = re.sub(r"\s+", " ", text[max(0, start - 60): end + 90]).strip()[:_SNIPPET_CHARS]
        out.append(captured)
    return out


def _page_codes(scan: _Scan) -> Tuple[List[CapturedCode], bool]:
    """The page's distinct codes (first seen wins), at most `_MAX_CODES_PER_PAGE`; and whether it hit the cap."""
    out: Dict[str, CapturedCode] = {}
    for region in scan.regions:
        for captured in codes_in_region(region):
            if captured.code.upper() in out:
                continue
            if len(out) >= _MAX_CODES_PER_PAGE:
                return list(out.values()), True
            out[captured.code.upper()] = captured
    return list(out.values()), False


def capture_codes(html: Any) -> List[CapturedCode]:
    return _page_codes(_scan(html))[0]


# ---- shadow counters (one process = one job run; the job resets them when it starts) ----------------------

_LOCK = threading.Lock()
_STATS: Dict[str, Any] = {}
_HOST_CODES: set = set()
_HOSTS_WITH_CODES: set = set()


def reset_stats() -> None:
    with _LOCK:
        _STATS.clear()
        _STATS.update({
            "pages_scanned": 0, "pages_with_region": 0, "pages_with_code": 0, "codes_seen": 0,
            "pages_over_budget": 0, "pages_too_deep": 0, "pages_codes_capped": 0,
            "by_kind": {}, "by_hint": {}, "by_shape": {}, "by_form": {}, "by_market": {}, "errors": 0,
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


def capture_shadow(html: Any, *, url: str, market: str, page_currency: Optional[str]) -> None:
    """Count and log the codes in this page's promo regions. Never raises; writes nothing."""
    try:
        scan = _scan(html)
        codes, capped = _page_codes(scan)
        host = (urlparse(url).hostname or "").lower()
        host = host[4:] if host.startswith("www.") else host
        if codes:
            # Built and printed BEFORE the counters move, so a failed print leaves them untouched. ASCII
            # escapes keep a lone surrogate from failing the write.
            print("DEAL_CODE_CAPTURE_PAGE " + json.dumps({
                "host": host,
                "market": market,
                "page_currency": page_currency,
                "codes_capped": capped,
                "codes": [asdict(c) for c in codes],
            }, ensure_ascii=True, separators=(",", ":")), flush=True)
        with _LOCK:
            _STATS["pages_scanned"] += 1
            _STATS["pages_over_budget"] += int(scan.over_budget)
            _STATS["pages_too_deep"] += int(scan.too_deep)
            _STATS["pages_codes_capped"] += int(capped)
            if scan.regions:
                _STATS["pages_with_region"] += 1
            if codes:
                _STATS["pages_with_code"] += 1
                _bump(_STATS["by_market"], market)
                _HOSTS_WITH_CODES.add(host)
            for captured in codes:
                if (host, captured.code.upper()) not in _HOST_CODES:
                    _HOST_CODES.add((host, captured.code.upper()))
                    _STATS["codes_seen"] += 1
                    _bump(_STATS["by_kind"], captured.kind)
                    _bump(_STATS["by_shape"], captured.shape)
                    _bump(_STATS["by_form"], captured.form)
                    for hint in captured.hints or ["none"]:
                        _bump(_STATS["by_hint"], hint)
    except Exception as exc:  # noqa: BLE001 - shadow capture never costs a page refresh
        with _LOCK:
            _STATS["errors"] = _STATS.get("errors", 0) + 1
        logger.warning("deal code capture failed: %s", type(exc).__name__)
