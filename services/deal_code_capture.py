"""Merchant-published discount codes in a store page we ALREADY fetched (ADR-027 D2 `merchant_published`),
in SHADOW: it counts and logs what it would collect, and writes nothing.

WHY HERE. A 2026-11-03 census of the text we store found almost no codes: we keep product descriptions,
not the announcement bars and promo banners where stores print them, and the curated feed strips code lines
on purpose. The page refresh (`external_offers_service.resolve_external_offer`) already downloads each
store page, so reading its promo regions costs no extra request to any store.

WHAT IT READS. Only the first `_BODY_BUDGET` characters from where the body starts (bars sit at the top): the
first `<body>` tag (or, on a page without one, the first tag that cannot be in a head), found by parsing the
head as markup, so a `<body` or `</head>` inside a script is not it. In them, only the text of elements that
NAME a promo region: a whole class or id token (or the tag name) such as announcement / announcement-bar,
promo-bar, promo-banner, top-bar, marquee, ticker-bar, header-message, site-banner, notification-bar,
free-shipping-bar, or an aria-label "Announcement(s)". A bare "banner" is not one (hero banners hold product
copy), a token that only contains a name ("product-sticker", "desktop-bar") is not one, and html / body / main
/ header are never a region root, so a body class like `has-announcement-bar` cannot turn the whole page into
one. A page-state token is never a name: a has-/with-/is-/no-/show-/hide- prefix or a single-dash state suffix
(`announcement-bar-visible`, `top-bar-open`, `marquee-enabled`). A wrapper or modifier token
(`announcement-bar-container`, `announcement-area`, `announcement-bar--sticky`) is a FALLBACK region: its text
counts only when nothing inside it names a region, so a bar that names itself inside a wrapper is read alone,
and a bar named only by its wrapper or modifier class is still read. Codes from a fallback region are counted
apart (`by_region`). Script, style, noscript, template and svg content is skipped. Hidden text IS read: we
parse markup, not the rendering, and a rotating bar keeps its other slides hidden.

WHAT IS A CODE. An all-caps or digit token after "use/enter/apply code", after "promo/discount/coupon/
voucher code:", after "with/using code" (form `with_code`), or right after the word "code" ("Code: X", "code X";
form `after_code`), with at least one letter, not a stop word ("USE CODE BELOW", "USE CODE IN-STORE"), not
followed by "shade"/"swatch"/"tone" (a shade name, "NC20 shade"), not a gift-card number (three or more
dash-separated groups of four, 1234-5678-ABCD) or an XXXX placeholder. "Gift card" near a code is a hint,
not a rejection: "Excludes gift cards" is one of the commonest bar disclaimers. Its shape and the phrase
that introduced it are counted separately, so shade-like short tokens (NC20) and three-letter codes (VIP)
stay visible in the numbers. Around it, the terms the region states: percent or amount off, free
shipping, a minimum spend, and eligibility hints (new customers, sign-up, once per customer, exclusions,
gift cards); each stated term goes to the NEAREST code, and a percent outside 1-100 is not a term
("0% off then 20% off" reads 20). Those are metadata only (ADR-027 D2): a code is evidence of nothing
until a checkout applies it.

BOUNDED. The parse is linear (open-tag counts, implicit closing of p/li/option/dt/dd/tr/td/th, depth cap
`_MAX_DEPTH`), a region keeps at most `_MAX_REGION_CHARS`, a page at most `_MAX_CODES_PER_PAGE` codes, and
a snippet at most 160 characters, so a page's log line stays far under Cloud Logging's 256 KB entry limit.

SHADOW, behind DEAL_CODE_CAPTURE_SHADOW (default off; set on the refresh job alone). With it on, each
page with a code logs one `DEAL_CODE_CAPTURE_PAGE` JSON line and the counters below accumulate for the
job's summary. Nothing is written here: `capture_shadow` returns the page's codes, and the separate writer
(services/deal_code_capture_writer.py, its own flag DEAL_CODE_CAPTURE_WRITE) records them in
`observed_store_offers` as unverified merchant_published rows. Every failure is swallowed: capture can never
cost a page refresh.

DIAGNOSTICS, COUNTS ONLY (never region or code text): why a page with a promo region yields no code. Per
page: a named region vs only a fallback one; a region that mentions "code" in any case; a region where a
RELAXED pattern (any case, any phrasing: "with code save10", "Code: X") finds a token, and for each such
token the strict matcher did not capture, why (`by_miss`: stop word, lowercase with/without a digit, no
letter, rejected as a shade/gift card/placeholder, a strict code past the page cap, or wording the strict
phrases do not cover); a strict phrase anywhere in the HTML, scripts and section JSON included (a bar
rendered by script); a region-name class/id in a start tag at or past the last `_WINDOW_EDGE` chars of the
body window (cut or unread); and each region's length (`by_region_chars`). A diagnostics failure counts in
`diagnosis_errors` and leaves the page's capture as it was.
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
# A class/id token that states the PAGE's state ("has-announcement-bar", "announcement-bar-visible") is never a
# region name: it often sits on a wrapper around the whole page. A token that wraps or modifies the bar
# ("announcement-bar-container", "announcement-bar--sticky") is a fallback name: see _RegionParser. Only a TRAILING
# `--` modifier counts: Shopify's section ids carry `--<id>__` before the name
# ("shopify-section-sections--1__announcement-bar"). No regex for the modifier: `--[^_]*$` rescans to the end from
# every `--` and a long dash run makes it quadratic.
_STATE_PREFIX_RE = re.compile(r"(?:has|with|is|no|show|hide)[-_]", re.IGNORECASE)
_STATE_SUFFIX_RE = re.compile(
    r"[-_](?:visible|active|open|opened|enabled|closed|hidden|shown|dismissed)$", re.IGNORECASE
)
_WRAPPER_SUFFIX_RE = re.compile(r"[-_](?:container|wrapper|wrap|outer|holder|area)$", re.IGNORECASE)


def _token_kind(token: str) -> str:
    """state (never a name) | wrapper (a fallback name) | name."""
    if _STATE_PREFIX_RE.match(token):
        return "state"
    modifier = token.rfind("--")
    if modifier >= 0 and "_" not in token[modifier:]:
        return "wrapper"  # before the state suffix: "announcement-bar--visible" modifies the bar itself
    if _STATE_SUFFIX_RE.search(token):
        return "state"
    if _WRAPPER_SUFFIX_RE.search(token):
        return "wrapper"
    return "name"


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
# Tags a head may hold. On a page with no <body> tag, the first other start tag is where the body starts.
_HEAD_TAGS = frozenset({"html", "head", "title", "base", "link", "meta", "style", "script", "noscript", "template"})
_HEAD_SCAN_LIMIT = 1_000_000  # realistic heads are well under; 1 MB of bare "<" costs ~0.5 s
# Raised from 200_000 (2026-11-07): 10-20% of scanned pages had a named bar starting past the old window
# (`pages_named_region_past_budget`). The parse is linear; the head scan limit above is unchanged.
_BODY_BUDGET = 500_000
_MAX_DEPTH = 512
_MAX_REGION_CHARS = 4000
_MAX_CODES_PER_PAGE = 20
_BLOCK_MARK = "\ue000"  # a block tag's boundary inside region text (not whitespace), written out as a line break
_SNIPPET_CHARS = 160

# The code is case-sensitive (an all-caps or digit token, as stores print them); the words before it are not.
_CODE_RES = (
    ("use_code", re.compile(
        r"\b(?i:use|enter|apply)\s+(?i:the\s+)?(?i:(?:promo|discount|coupon|voucher)\s+)?(?i:code)[ \t]*:?[ \t]*"
        r"[\"'“‘]?([A-Z0-9][A-Z0-9_-]{2,19})\b")),
    ("labelled", re.compile(
        r"\b(?i:promo|discount|coupon|voucher)\s*(?i:code)[ \t]*[:：][ \t]*[\"'“‘]?([A-Z0-9][A-Z0-9_-]{2,19})\b")),
    # Widened 2026-11-07 from the shadow diagnostics (by_miss "wording": all-caps codes after "code" that the two
    # forms above miss — "with code SAVE10", "Code: WELCOME15", "Extra 20% off code SPRING20"). Listed AFTER them,
    # so a code they read keeps their form; each new form is counted apart in `by_form`.
    ("with_code", re.compile(
        r"\b(?i:with|using)\s+(?i:the\s+)?(?i:(?:promo|discount|coupon|voucher)\s+)?(?i:code)[ \t]*[:：]?[ \t]*"
        r"[\"'“‘]?([A-Z0-9][A-Z0-9_-]{2,19})\b")),
    ("after_code", re.compile(
        r"\b(?i:code)\b[ \t]{0,3}[:：]?[ \t]{0,3}[\"'“‘]?([A-Z0-9][A-Z0-9_-]{2,19})\b")),
)
# All-caps prose after "use code" ("USE CODE BELOW FOR 10% OFF", "PROMO CODE: TBA") is not a code.
# Compared with dashes removed, so IN-STORE is INSTORE and MON-FRI is MONFRI.
_STOP_CODES = frozenset({
    "ABOVE", "ADD", "AFTER", "AGAIN", "ALL", "ALSO", "ALWAYS", "AND", "ANY", "APP", "APPLIED", "APPLY", "ARE", "ASAP",
    "ASK", "AT", "ATCHECKOUT", "AUTO", "AUTOMATIC", "AUTOMATICALLY", "BAG", "BEFORE", "BELOW", "BEST", "BIG", "BOX",
    "BUT", "CAN", "CART", "CHECKOUT", "CODE", "CODES", "COUPON", "DAY", "DISCOUNT", "DURING", "EACH", "EMAIL",
    "EMAILED", "ENDS", "ENTER", "ENTIRE", "ETC", "EVERY", "EVERYWHERE", "EXP", "EXPIRES", "EXTRA", "FAQ", "FIELD",
    "FIRST", "FOR", "FREE", "FROM", "GET", "HAS", "HERE", "HOT", "HOW", "INSIDE", "INSTEAD", "INSTORE", "ITEM",
    "ITEMS", "ITS", "LATER", "LINK", "LISTED", "MANUALLY", "MAX", "MAY", "MEMBERS", "MIN", "MONFRI", "NEEDED", "NEW",
    "NEXT", "NONE", "NOT", "NOW", "OFF", "ONCE", "ONE", "ONLINE", "ONLY", "ORDER", "ORDERS", "OUR", "OUT", "OWN",
    "PER", "PLUS", "PROMO", "PROVIDED", "PURCHASE", "RECEIVED", "REQUIRED", "SALE", "SAT", "SEE", "SELECT",
    "SELECTED", "SENT", "SHIPPING", "SHOWN", "SITE", "SITEWIDE", "SOON", "SPF", "STORE", "STOREWIDE", "TBA", "TBD",
    "TCS", "THAT", "THE", "THEN", "THIS", "THRU", "TIL", "TODAY", "TOMORROW", "TONIGHT", "TOO", "TOP", "TWICE", "TWO",
    "UNTIL", "UPON", "UPS", "USE", "VALID", "VIA", "VOUCHER", "WEB", "WEEKEND", "WHEN", "WHILE", "WHY", "WITH", "YET",
    "YOU", "YOUR", "WILL", "CANNOT", "WORKS", "WORK", "EXPIRED", "SOLD",
})
# Only for the loose bare-"code" form (`after_code`): words a bare "code X" names that are not codes there
# ("Dress code BLACK"). The phrased forms keep reading them ("use code RED" stays a code).
_AFTER_CODE_STOP = frozenset({
    "RED", "BLACK", "WHITE", "BLUE", "GREEN", "PINK", "NUDE", "ROSE", "GOLD", "SILVER", "BROWN", "GREY", "GRAY",
    "BEIGE", "IVORY", "CORAL", "PEACH", "PURPLE", "ORANGE", "YELLOW", "NAVY", "CLEAR", "MATTE", "JOIN", "STOP",
    "HELP", "YES", "NO",
})
# A noun right before a bare "code" that makes it some other code: product/item/style/shade/SKU/referral/postal/
# zip/dress/country/QR/gift-card/text-message/area/access/verification/security/tracking/reference/order/model/
# part/colour/bar codes.
_NON_PROMO_CODE_NOUN_RE = re.compile(
    r"(?i)\b(?:product|item|style|shade|sku|referral|refer|postal|post|zip|dress|country|qr|gift(?:\s*card)?|"
    r"text|sms|area|access|verification|security|tracking|reference|order|model|part|colou?r|bar|discount\s+card)"
    r"[\s:_-]{0,3}$"
)
# A bare "code X" is read only with a promo context near it: a percent or amount, "off", "save", "sale",
# "discount", "promo", "coupon", "voucher", "deal", "offer", or free shipping/delivery/gift.
_PROMO_CONTEXT_RE = re.compile(
    r"(?i)\d{1,3}\s?%|[$£€]\s?\d|\boff\b|\bsave\b|\bsale\b|\bdiscount|\bpromo|\bcoupon|\bvoucher|\bdeals?\b|"
    r"\boffer|\bfree\s+(?:shipping|delivery|gift)"
)
_AFTER_CODE_CONTEXT_BEFORE = 80
_AFTER_CODE_CONTEXT_AFTER = 60
_SHADE_AFTER_RE = re.compile(r"\s{0,3}(?:shades?|swatch(?:es)?|tones?|colou?rs?|finish)\b", re.IGNORECASE)
_GIFT_CARD_NUMBER_RE = re.compile(r"[A-Z0-9]{4}(?:-[A-Z0-9]{4}){2,}")
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
    ("has_exclusions", re.compile(r"(?i)\bexclud(?:es|ed|ing)|not\s+valid\s+on|exclusions?\s+apply")),
    ("mentions_gift_card", re.compile(r"(?i)\bgift\s*cards?\b")),
)
_WINDOW = 90


class _TooDeep(Exception):
    pass


def _region_kind(tag: str, attrs: List[Any]) -> Optional[str]:
    """named | fallback | None. Named by the tag itself (a theme's custom element, e.g. <announcement-bar>), a
    class or id name token, or an aria-label naming it ("Announcement"); fallback when only a wrapper or
    modifier token names it."""
    named = bool(_REGION_TOKEN_RE.search(tag))
    wrapped = False
    label = None
    for k, v in attrs:
        if k in ("class", "id"):
            for token in str(v or "").split():
                if not _REGION_TOKEN_RE.search(token):
                    continue
                kind = _token_kind(token)
                named = named or kind == "name"
                wrapped = wrapped or kind == "wrapper"
        elif k == "aria-label":
            label = str(v or "")
    if named or (label is not None and _REGION_LABEL_RE.match(label)):
        return "named"
    return "fallback" if wrapped else None


class _RegionParser(HTMLParser):
    """Collects the text of every element that names a promo region, nested elements included, and of every
    fallback region with no named region inside it. Linear: open tags are counted per name, so a stray end
    tag costs O(1), and every pop pays for its own push."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.regions: List[str] = []
        self.fallback_regions: List[str] = []
        self._stack: List[str] = []
        self._open: Dict[str, int] = {}
        self._region_depth: Optional[int] = None
        self._skip_depth: Optional[int] = None
        self._buffer: List[str] = []
        self._fallback_depth: Optional[int] = None
        self._fallback_buffer: List[str] = []
        self._fallback_has_named = False

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
            _flush(self._buffer, self.regions)
            self._region_depth = None
            self._buffer = []
        if self._fallback_depth is not None and depth <= self._fallback_depth:
            if not self._fallback_has_named:
                _flush(self._fallback_buffer, self.fallback_regions)
            self._fallback_depth = None
            self._fallback_buffer = []
        return closed

    def _separate(self, tag: str) -> None:
        if self._skip_depth is None and tag not in _INLINE_TAGS:
            if self._region_depth is not None:
                self._buffer.append(_BLOCK_MARK)
            if self._fallback_depth is not None:
                self._fallback_buffer.append(_BLOCK_MARK)

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
        if self._region_depth is not None or self._skip_depth is not None or tag in _NEVER_REGION_ROOTS:
            return
        kind = _region_kind(tag, attrs)
        if kind == "named":
            self._region_depth = depth
            self._buffer = []
            self._fallback_has_named = True
        elif kind == "fallback" and self._fallback_depth is None:
            self._fallback_depth = depth
            self._fallback_buffer = []
            self._fallback_has_named = False

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS or not self._open.get(tag):
            return
        self._separate(tag)
        # Close up to the matching open tag (tolerates unclosed children, as browsers do).
        while self._stack:
            if self._pop() == tag:
                break

    def handle_data(self, data: str) -> None:
        if self._skip_depth is None:
            if self._region_depth is not None:
                self._buffer.append(data)
            if self._fallback_depth is not None:
                self._fallback_buffer.append(data)


def _flush(buffer: List[str], into: List[str]) -> None:
    # A block boundary stays a line break (review of be#2735, P3-N5): a marquee item ending in "USE CODE" must not
    # join the next item's first word. Whitespace inside a text node collapses to one space as before.
    text = re.sub(r"\s+", " ", "".join(buffer))
    text = re.sub(r" ?\ue000[\ue000 ]*", "\n", text).strip(" \n\ue000")
    if text:
        into.append(text[:_MAX_REGION_CHARS])


class _BodyFound(Exception):
    pass


class _BodyLocator(HTMLParser):
    """Stops at the first <body> start tag. Script and style are parsed as raw text, so a `<body` or `</head>`
    inside one is not seen. A stray tag in the head (a tracking pixel's <img>, a custom element) does not end
    the search: the first tag a head cannot hold, outside noscript/template, is only remembered as where the
    body starts on a page with no <body> tag."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)  # False makes a run of "&" cost ~0.5 s per MB
        self._hidden = 0
        self.implicit_start: Optional[Tuple[int, int]] = None

    def handle_starttag(self, tag: str, attrs: List[Any]) -> None:
        if tag == "body":
            raise _BodyFound()
        if tag in ("noscript", "template"):
            self._hidden += 1
        elif not self._hidden and tag not in _HEAD_TAGS and self.implicit_start is None:
            self.implicit_start = self.getpos()

    def handle_endtag(self, tag: str) -> None:
        if tag in ("noscript", "template") and self._hidden:
            self._hidden -= 1


def _offset(html: str, position: Tuple[int, int]) -> int:
    line, column = position
    offset = 0
    for _ in range(line - 1):
        offset = html.index("\n", offset) + 1
    return offset + column


def _body_start(html: str) -> int:
    locator = _BodyLocator()
    try:
        locator.feed(html[:_HEAD_SCAN_LIMIT])  # no close(): it only flushes what is left as text, never a tag
    except _BodyFound:
        return _offset(html, locator.getpos())
    if locator.implicit_start is not None:
        return _offset(html, locator.implicit_start)
    # Nothing but head up to the limit: the body, if any, starts after it.
    return _HEAD_SCAN_LIMIT if len(html) > _HEAD_SCAN_LIMIT else 0


@dataclass
class _Scan:
    regions: List[str]
    fallback_regions: List[str] = field(default_factory=list)
    over_budget: bool = False
    too_deep: bool = False
    window_end: int = 0


def _scan(html: Any) -> _Scan:
    if not isinstance(html, str):
        return _Scan(regions=[])
    start = _body_start(html)
    parser = _RegionParser()
    too_deep = False
    try:
        parser.feed(html[start:start + _BODY_BUDGET])
        parser.close()
    except _TooDeep:
        too_deep = True
    return _Scan(regions=parser.regions, fallback_regions=parser.fallback_regions,
                 over_budget=len(html) - start > _BODY_BUDGET, too_deep=too_deep, window_end=start + _BODY_BUDGET)


def promo_regions(html: Any) -> List[str]:
    scan = _scan(html)
    return scan.regions + scan.fallback_regions


@dataclass
class CapturedCode:
    code: str
    kind: Optional[str]  # percent | amount | free_shipping | None (not stated near the code)
    value: Optional[str]
    currency_symbol: Optional[str]
    min_subtotal: Optional[str]
    hints: List[str] = field(default_factory=list)
    snippet: str = ""
    shape: str = ""  # short_letters (VIP, USA) | letters | short_alnum (shade-like, e.g. NC20) | alnum
    form: str = ""  # use_code ("use/enter/apply code X") | labelled ("promo code: X") | with_code | after_code
    region: str = "named"  # named | fallback (only a wrapper or modifier class named it)
    min_subtotal_symbol: Optional[str] = None  # the currency symbol the minimum spend was stated in


def _shape(code: str) -> str:
    if not any(ch.isdigit() for ch in code):
        return "short_letters" if len(code) <= 3 else "letters"
    return "short_alnum" if _SHORT_ALNUM_RE.fullmatch(code) else "alnum"


def _is_code(code: str, text: str, start: int, end: int) -> bool:
    if len(code) < 3 or code.upper().replace("-", "") in _STOP_CODES or not any(ch.isalpha() for ch in code):
        return False
    if _GIFT_CARD_NUMBER_RE.fullmatch(code) or set(code.replace("-", "").replace("_", "")) <= {"X"}:  # 1234-5678-ABCD, or an "XXXX(-XXXX)" placeholder
        return False
    return not _SHADE_AFTER_RE.match(text, end)


def _nearest(spans: List[Tuple[int, int]], start: int, end: int) -> Optional[int]:
    best, best_distance = None, None
    for i, (cs, ce) in enumerate(spans):
        distance = 0 if (start < ce and cs < end) else (cs - end if end <= cs else start - ce)
        if distance <= _WINDOW and (best_distance is None or distance < best_distance):
            best, best_distance = i, distance
    return best


def _after_code_ok(text: str, phrase_start: int, start: int, end: int, code: str) -> bool:
    if not code[0].isalpha() or _SHORT_ALNUM_RE.fullmatch(code) or code.upper() in _AFTER_CODE_STOP:
        return False
    if _NON_PROMO_CODE_NOUN_RE.search(text[max(0, phrase_start - 40):phrase_start]):
        return False
    window = text[max(0, phrase_start - _AFTER_CODE_CONTEXT_BEFORE):phrase_start] + " " + text[end:end + _AFTER_CODE_CONTEXT_AFTER]
    return bool(_PROMO_CONTEXT_RE.search(window))


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
            # A bare "code X" is the loosest form (review of be#2732: on its own it read product, style, postal,
            # referral and item codes). It is read only when ALL hold: the token starts with a letter and is not a
            # shade-like short token ("code: NC20", "2N1"); no non-promo noun comes right before "code"; the token is
            # not one of the words a bare "code" names ("Dress code BLACK"); and a promo context is near. The phrased
            # forms keep their wider reading ("use code VIP2" stays a code).
            if form == "after_code" and not _after_code_ok(text, match.start(), start, end, code):
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
            if key == "percent" and not 1 <= int(occurrence.group(1)) <= 100:
                continue  # "0% off" or "150% off" is not a term; a later valid one still is
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
        if percent is not None:
            captured.kind, captured.value = "percent", percent.group(1)
        elif amount is not None:
            captured.kind, captured.value, captured.currency_symbol = "amount", amount.group(2), amount.group(1)
        elif t["free_shipping"]:
            captured.kind = "free_shipping"
        if t["spend"] is not None:
            captured.min_subtotal = t["spend"].group(2)
            captured.min_subtotal_symbol = t["spend"].group(1)
            captured.currency_symbol = captured.currency_symbol or t["spend"].group(1)
        captured.hints = [name for name, _ in _HINTS if name in t["hints"]]
        captured.snippet = re.sub(r"\s+", " ", text[max(0, start - 60): end + 90]).strip()[:_SNIPPET_CHARS]
        out.append(captured)
    return out


def _page_codes(scan: _Scan) -> Tuple[List[CapturedCode], bool]:
    """The page's distinct codes (first seen wins, named regions first), at most `_MAX_CODES_PER_PAGE`; and
    whether it hit the cap."""
    out: Dict[str, CapturedCode] = {}
    regions = [(text, "named") for text in scan.regions] + [(text, "fallback") for text in scan.fallback_regions]
    for text, region in regions:
        for captured in codes_in_region(text):
            if captured.code.upper() in out:
                continue
            if len(out) >= _MAX_CODES_PER_PAGE:
                return list(out.values()), True
            captured.region = region
            out[captured.code.upper()] = captured
    return list(out.values()), False


def capture_codes(html: Any) -> List[CapturedCode]:
    return _page_codes(_scan(html))[0]


# ---- shadow diagnostics: counts only, never text ----------------------------------------------------------

_CODE_WORD_RE = re.compile(r"(?i)\bcodes?\b")
# A code-like token after the word "code" in any case and any phrasing. Region text has single spaces.
_RELAXED_CODE_RE = re.compile(r"(?i)\bcode\b\s{0,3}[:：]?\s{0,3}[\"'“‘]?([a-z0-9][a-z0-9_-]{2,19})\b")
# The strict matcher's phrases, any case, anywhere in the HTML. Gaps up to 40 whitespace chars cover a
# phrase wrapped across indented source lines; bounded, so a blank run costs at most 40 steps per start.
_CODE_PHRASE_RE = re.compile(
    r"(?i)\b(?:(?:use|enter|apply)\s{1,40}(?:the\s{1,40})?(?:(?:promo|discount|coupon|voucher)\s{1,40})?code\b"
    r"|(?:promo|discount|coupon|voucher)\s{0,40}code\s{0,40}[:：])"
)
# A start tag, and a class/id attribute inside it after whitespace or a quote: not `el.id = "..."` in a
# script, `[class="..."]` in a stylesheet, `data-section-id="..."`, or text.
_TAG_RE = re.compile(r"<[a-zA-Z][^<>]{0,2000}>")
_ATTR_RE = re.compile(r"""(?i)(?<=[\s"'/])(?:class|id)\s{0,40}=\s{0,40}["']([^"'<>]{1,300})["']""")
_DIAG_SCAN_LIMIT = 2_000_000
# A bar tag this close to the window's end has its text cut (or none): counted as past the window.
_WINDOW_EDGE = 300
_REGION_CHAR_BUCKETS = ((40, "lt40"), (200, "40_199"), (1000, "200_999"))


@dataclass
class _Diagnosis:
    region_mentions_code: bool = False
    relaxed_match: bool = False
    code_phrase_in_html: bool = False
    named_region_past_budget: bool = False
    failed: bool = False
    misses: List[str] = field(default_factory=list)
    region_chars: List[str] = field(default_factory=list)


def _miss_reason(token: str, text: str, start: int, end: int) -> str:
    if token.upper().replace("-", "") in _STOP_CODES:
        return "stop"
    if not any(ch.isalpha() for ch in token):
        return "no_letter"
    if token != token.upper():
        return "lower_with_digit" if any(ch.isdigit() for ch in token) else "lower_letters"
    if not _is_code(token, text, start, end):
        return "rejected"
    return "wording"


def _named_region_tag_past(html: str, start: int, end: int) -> bool:
    for tag in _TAG_RE.finditer(html, start, end):
        for attr in _ATTR_RE.finditer(tag.group(0)):
            if any(_REGION_TOKEN_RE.search(t) and _token_kind(t) == "name" for t in attr.group(1).split()):
                return True
    return False


def _region_chars(text: str) -> str:
    for limit, name in _REGION_CHAR_BUCKETS:
        if len(text) < limit:
            return name
    return "1000_plus"


def _diagnose(html: str, scan: _Scan, captured: List[CapturedCode], capped: bool = False) -> _Diagnosis:
    diagnosis = _Diagnosis()
    texts = scan.regions + scan.fallback_regions
    taken = {c.code.upper() for c in captured}
    # On a capped page, strict codes past the cap are not misses: they get their own reason (captured ones
    # are skipped before it is asked).
    past_cap = {c.code.upper() for text in texts for c in codes_in_region(text)} if capped else set()
    seen = set()
    for text in texts:
        diagnosis.region_chars.append(_region_chars(text))
        diagnosis.region_mentions_code = diagnosis.region_mentions_code or bool(_CODE_WORD_RE.search(text))
        for match in _RELAXED_CODE_RE.finditer(text):
            diagnosis.relaxed_match = True
            raw = match.group(1)
            token = raw.strip("-_")
            start = match.start(1) + (len(raw) - len(raw.lstrip("-_")))
            if token.upper() in taken or token.upper() in seen:
                continue
            seen.add(token.upper())
            reason = "past_cap" if token.upper() in past_cap else _miss_reason(token, text, start, start + len(token))
            diagnosis.misses.append(reason)
    diagnosis.code_phrase_in_html = bool(_CODE_PHRASE_RE.search(html, 0, _DIAG_SCAN_LIMIT))
    if scan.over_budget:
        diagnosis.named_region_past_budget = _named_region_tag_past(
            html, max(0, scan.window_end - _WINDOW_EDGE), scan.window_end + _DIAG_SCAN_LIMIT)
    return diagnosis


# ---- shadow counters (one process = one job run; the job resets them when it starts) ----------------------

_LOCK = threading.Lock()
_STATS: Dict[str, Any] = {}
_HOST_CODES: set = set()
_HOSTS_WITH_CODES: set = set()


_RUN_GENERATION = [0]


def run_generation() -> int:
    """Bumped by every reset_stats() (once per refresh run): the writer caches its opt-out read per generation."""
    return _RUN_GENERATION[0]


def reset_stats() -> None:
    with _LOCK:
        _RUN_GENERATION[0] += 1
        _STATS.clear()
        _STATS.update({
            "pages_scanned": 0, "pages_with_region": 0, "pages_with_code": 0, "codes_seen": 0,
            "pages_over_budget": 0, "pages_too_deep": 0, "pages_codes_capped": 0,
            "by_kind": {}, "by_hint": {}, "by_shape": {}, "by_form": {}, "by_region": {}, "by_market": {},
            "errors": 0,
            # Diagnostics (counts only): see the module docstring.
            "pages_with_named_region": 0, "pages_with_fallback_region_only": 0, "pages_region_mentions_code": 0,
            "pages_relaxed_code_match": 0, "pages_code_phrase_in_html": 0, "pages_named_region_past_budget": 0,
            "by_miss": {}, "by_region_chars": {}, "diagnosis_errors": 0,
            # The writer's counts (DEAL_CODE_CAPTURE_WRITE): inserted, updated, skipped_*, errors.
            "writes": {},
        })
        _HOST_CODES.clear()
        _HOSTS_WITH_CODES.clear()
        _WRITE_HOSTS.clear()


# The writer's per-run budget (services/deal_code_capture_writer.py): rows written per host and in all, reset with the
# counters when the job starts.
_WRITE_HOSTS: Dict[str, int] = {}
MAX_WRITES_PER_RUN = 1000
MAX_WRITES_PER_HOST_PER_RUN = 20


def take_write_slot(host: str) -> Optional[str]:
    """None when one more row may be written for this host in this run; otherwise why not (counted by the caller)."""
    with _LOCK:
        if sum(_WRITE_HOSTS.values()) >= MAX_WRITES_PER_RUN:
            return "skipped_run_cap"
        if _WRITE_HOSTS.get(host, 0) >= MAX_WRITES_PER_HOST_PER_RUN:
            return "skipped_host_cap"
        _WRITE_HOSTS[host] = _WRITE_HOSTS.get(host, 0) + 1
        return None


def release_write_slot(host: str) -> None:
    """Give back a slot taken for an insert that wrote no new row (it lost a race and became an update)."""
    with _LOCK:
        if _WRITE_HOSTS.get(host, 0) > 0:
            _WRITE_HOSTS[host] -= 1


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


def capture_shadow(html: Any, *, url: str, market: str, page_currency: Optional[str]) -> List[CapturedCode]:
    """Count and log the codes in this page's promo regions, and return them (empty on any failure). Never
    raises; writes nothing."""
    codes: List[CapturedCode] = []
    try:
        scan = _scan(html)
        codes, capped = _page_codes(scan)
        diagnosis = _Diagnosis()
        if isinstance(html, str):
            try:  # a diagnostics failure never costs the page's capture
                diagnosis = _diagnose(html, scan, codes, capped)
            except Exception as exc:  # noqa: BLE001
                diagnosis = _Diagnosis(failed=True)
                logger.warning("deal code capture diagnosis failed: %s", type(exc).__name__)
        host = (urlparse(url).hostname or "").lower()
        host = host[4:] if host.startswith("www.") else host
        if codes:
            # Built and printed BEFORE the counters move, so a failed print leaves them untouched. NEVER the code or
            # the bar text (review of be#2732): only the code's shape, form, region, stated kind and hints.
            print("DEAL_CODE_CAPTURE_PAGE " + json.dumps({
                "host": host,
                "market": market,
                "page_currency": page_currency,
                "codes_capped": capped,
                # No hash either (review of be#2732, P3-C): an unsalted hash of a short code is recovered by a
                # dictionary in milliseconds. Repeat sightings are counted by the summary's distinct_host_codes.
                "codes": [{
                    "kind": c.kind, "shape": c.shape, "form": c.form, "region": c.region, "hints": c.hints,
                } for c in codes],
            }, ensure_ascii=True, separators=(",", ":")), flush=True)
        with _LOCK:
            _STATS["pages_scanned"] += 1
            _STATS["pages_over_budget"] += int(scan.over_budget)
            _STATS["pages_too_deep"] += int(scan.too_deep)
            _STATS["pages_codes_capped"] += int(capped)
            if scan.regions or scan.fallback_regions:
                _STATS["pages_with_region"] += 1
            _STATS["pages_with_named_region"] += int(bool(scan.regions))
            _STATS["pages_with_fallback_region_only"] += int(not scan.regions and bool(scan.fallback_regions))
            _STATS["pages_region_mentions_code"] += int(diagnosis.region_mentions_code)
            _STATS["pages_relaxed_code_match"] += int(diagnosis.relaxed_match)
            _STATS["pages_code_phrase_in_html"] += int(diagnosis.code_phrase_in_html)
            _STATS["pages_named_region_past_budget"] += int(diagnosis.named_region_past_budget)
            _STATS["diagnosis_errors"] += int(diagnosis.failed)
            for reason in diagnosis.misses:
                _bump(_STATS["by_miss"], reason)
            for bucket in diagnosis.region_chars:
                _bump(_STATS["by_region_chars"], bucket)
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
                    _bump(_STATS["by_region"], captured.region)
                    for hint in captured.hints or ["none"]:
                        _bump(_STATS["by_hint"], hint)
    except Exception as exc:  # noqa: BLE001 - shadow capture never costs a page refresh
        with _LOCK:
            _STATS["errors"] = _STATS.get("errors", 0) + 1
        logger.warning("deal code capture failed: %s", type(exc).__name__)
        return []
    return codes


def bump_write(key: str, n: int = 1) -> None:
    """The writer's counters (services/deal_code_capture_writer.py), in the same summary: `writes`."""
    with _LOCK:
        writes = _STATS.setdefault("writes", {})
        writes[key] = writes.get(key, 0) + n
