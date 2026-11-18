"""Reading the amounts a checkout's confirm card (and the setup answer's prose) shows. Pure: no I/O, no logs.

The A2A rail's checkout step confirms a merchant checkout only when this says the setup's answer shows no
amount it cannot verify against the approval ceiling (services/payment_orchestration/a2a_rail_worker.
_checkout_confirm). The ceiling is the approved quote before any discount (a2a_rail_worker.checkout_ceiling).
It is shared by the worker (cards, `confirm_card_refused`) and the transport (the answer's text parts,
`text_refused`, read inside a2a_rail so that no prose leaves it).

THE CARD: A STRICT ALLOWLIST THAT FAILS CLOSED. The token is minted for exactly the ceiling and the scheme
enforces that amount (a2a_rail_worker._start_token): that cap is the primary control. This scan is the
second one, and it does not try to understand arbitrary receipts. Two rules:

  RULE 1. A card with nothing MONEY-LIKE proceeds; the token cap decides. A value is money-like when it is
    (a) a JSON number leaf anywhere (int or float; a boolean is not a number; any other non-JSON leaf type
        counts as a number);
    (b) a string leaf, or a KEY, holding a currency or amount pattern: a currency symbol (any Unicode Sc
        character: $, €, ＄, ...) next to a digit; an ISO-4217 code (`_ISO_4217`, in upper case, or `usd`
        in any case) as a whole word next to a digit; or digits with a decimal point and exactly two
        fraction digits ("40.00", "1,234.56"). All after NFKC, with invisible format characters removed;
    (c) a string leaf (not empty) whose OWN key holds a money word (`_MONEY_KEY_WORDS`, a substring of the
        key folded to lower-case letters and digits: "shipping_cost", "TotalPrice", "feeAmount", but also
        "feedback" and "summary": fail closed). A list item's own key is the list's key. An OBJECT under a
        money word ("shipping" holding the buyer's address) is not itself money: its leaves are judged by
        (a)-(c). Booleans, None and empty strings are never money-like.
    A string that is JSON (or a Python literal) inside ("{...}", "[...]") is parsed and judged the same way.
    RESIDUAL, accepted because the token cap is primary: a bare digit string under a non-money key
    ({"x": "1500"}: cents as text?) is NOT money-like and proceeds. So do zip codes, phone numbers and a
    card's last four ("94303", "+15125550100", "4399"), which is why the line is drawn there.

  RULE 2. A money-like card proceeds only when it is EXACTLY one of three named shapes (`_SHAPES`):
      S1 flat total          {total, currency}
      S2 canonical receipt   {line_items: [{quantity, price}], subtotal, total, currency; shipping, tax optional}
      S3 Shopify order JSON  {line_items: [{quantity, price; compare_at_price optional}], subtotal_price,
                              total_price, currency; shipping_lines: [{price}], total_tax, total_discounts
                              optional} (the field names of Shopify's REST Order resource: the shipping as
                              `shipping_lines`, the discount as `total_discounts`; no other spelling)
    beside, at the top level, only the known non-money keys of the measured confirm card (`_KNOWN_KEYS`:
    cardBrand, cardLast4, merchant, as strings; in S1 and S3 also `shipping` as the buyer's address object,
    whose keys are the address keys our setup sends, `ADDRESS_KEYS`, with string values). In S2 `shipping`
    is the shipping amount, so the address object cannot ride along. No known non-money value may itself be
    money-like. Matching is exact:
      - keys are spelled exactly as above: any other key, an alias, a case or separator variant ("Total",
        "grand_total", "subtotalPrice") is an unknown key, and so is a quantity outside its line;
      - an amount is a JSON number with at most two decimals, read as DOLLARS (a cents value read as
        dollars only reads larger, so that reading never lets more through), or a decimal string with
        exactly two fraction digits ("40.00"); never "$40.00", "40", "1500", "-5.00", "40.0", a list or an
        object; compare_at_price may also be null;
      - a quantity is a JSON integer >= 1; a line is an object with exactly its shape's keys; a list of
        lines (or of shipping lines) is not empty;
      - `currency` is "USD" (or "usd").
    The maths, in cents, for every shape: every amount <= the ceiling; the total <= the ceiling; the total
    >= sum(quantity x price) + shipping + tax - discounts; discounts >= 0 and <= sum(lines); S2: subtotal ==
    sum(lines); S3: subtotal_price == sum(lines) - total_discounts (Shopify's documented subtotal is after
    discounts, before shipping and tax; as subtotal_price is never negative, this also holds the discounts
    to at most the lines). EQUALITY, not >=, for the subtotal: a subtotal larger than its
    lines means lines we were not shown, a smaller one means lines it does not count; either way we cannot
    tell what is charged, and the cost is a false cancel before anything is submitted. compare_at_price is
    display only: checked as an amount, never summed, never the total.
    Anything else is refused: an unknown key, a prose or "$"-marked money string, unexpected nesting, two
    totals, a quantity outside its line, another currency, an amount over the ceiling.

  A NEW SHAPE IS ADDED ONLY WITH A MEASURED CARD (the provider's real confirm card showing it, recorded
  with placeholder values in tests/test_a2a_rail_checkout.py) AND ITS OWN REVIEW. Never widen a shape, or
  the rule-1 definition, to make a card that was not measured proceed.

  Bounds: data deeper than CARD_SCAN_MAX_DEPTH or larger than CARD_SCAN_MAX_NODES is refused, never partly
  read. No positive ceiling: refused.

THE PROSE (`text_refused`): an amount-shaped number in the answer's text (next to a currency symbol, an
upper-case ISO code, `usd`, or within three words after a total word) must be USD, well formed, not
negative and within the ceiling.

Anything this cannot verify refuses: the purchase is then cancelled before its confirm, when nothing has
been submitted.
"""

from __future__ import annotations

import ast
import decimal
import json
import math
import re
import unicodedata
from typing import Any, FrozenSet, Mapping, Optional

#: The card's data is depth 0; a leaf at depth 8 is read, at 9 refused. 2000 values are read, 2001 refused.
CARD_SCAN_MAX_DEPTH = 8
CARD_SCAN_MAX_NODES = 2000
#: A string longer than this that looks like JSON / a literal is not parsed: refused.
CARD_SCAN_MAX_JSON_CHARS = 20000

# ── rule 1: money-like ──────────────────────────────────────────────────────────────────────────

#: (c) Money words, matched as a substring of a key folded to [a-z0-9] (so "TotalPrice", "total_price" and
#: "total-price" all hold "total"). Fail closed: "feedback" holds "fee", "summary" holds "sum".
_MONEY_KEY_WORDS: FrozenSet[str] = frozenset({
    "total", "price", "amount", "amt", "cost", "fee", "tax", "vat", "gst", "hst", "pst", "duty", "duties",
    "charge", "discount", "saving", "coupon", "promo", "rebate", "voucher", "refund", "credit", "debit",
    "deposit", "shipping", "freight", "postage", "delivery", "handling", "tip", "gratuity", "insurance",
    "warranty", "donation", "gift", "pay", "due", "balance", "sum", "money", "cash", "cent", "dollar", "usd",
    "value", "net", "gross", "rate", "bill", "fare", "toll", "levy",
})
#: (b) The ISO-4217 alphabetic codes (with the fund, precious-metal and testing codes).
_ISO_4217: FrozenSet[str] = frozenset("""
AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BOV BRL BSD BTN BWP BYN BZD CAD
CDF CHE CHF CHW CLF CLP CNY COP COU CRC CUC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS
GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT
LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD
OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SLL SOS SRD SSP STN SVC
SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD USN UYI UYU UYW UZS VED VES VND VUV WST XAF XAG XAU
XBA XBB XBC XBD XCD XCG XDR XOF XPD XPF XPT XSU XTS XUA XXX YER ZAR ZMW ZWG ZWL
""".split())
_CODES = "|".join(sorted(_ISO_4217)) + "|[Uu][Ss][Dd]"
#: A code as a whole word next to a digit ("USD 40", "40EUR", "usd40"). A currency symbol is read as "$".
_CODE_NEXT_TO_DIGIT_RE = re.compile(rf"(?<![^\W\d_])(?:{_CODES})\s*\d|\d\s*(?:{_CODES})(?![^\W\d_])")
_SYMBOL_NEXT_TO_DIGIT_RE = re.compile(r"\$\s*\d|\d\s*\$")
#: Digits, a decimal point, exactly two fraction digits ("40.00"; not "7.1.5", "1.234" or "7.10.2").
_TWO_DECIMALS_RE = re.compile(r"(?<![\d.])\d+\.\d{2}(?!\d|[.,]\d)")

# ── rule 2: the shapes ──────────────────────────────────────────────────────────────────────────

#: The address keys our setup sends (a2a_rail.CHECKOUT_SHIPPING_REQUIRED + CHECKOUT_SHIPPING_OPTIONAL; a
#: test pins the two equal). The measured confirm card echoes them under `shipping`.
ADDRESS_KEYS: FrozenSet[str] = frozenset({
    "email", "first_name", "last_name", "phone", "country", "address1", "address2", "city", "state", "zip_code",
})
#: The measured confirm card's own keys besides `shipping`, each a string.
_KNOWN_KEYS: FrozenSet[str] = frozenset({"cardBrand", "cardLast4", "merchant"})
_USD = frozenset({"USD", "usd"})
#: A decimal string amount: no sign, no symbol, no grouping, no leading zero, exactly two fraction digits.
_DECIMAL_STRING_RE = re.compile(r"^(?:0|[1-9][0-9]{0,8})\.[0-9]{2}\Z")
#: A JSON number above this many dollars is refused before it is read exactly.
_NUMBER_MAX = 10 ** 9


class _Refused(Exception):
    """Something on the card cannot be verified against the ceiling."""


def normalise(text: str) -> str:
    """NFKC, every invisible format character removed."""
    text = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf" and ch not in _EXTRA_INVISIBLE)


def _skeleton(text: str) -> str:
    """Lookalike letters mapped to Latin, accents dropped: what a key or word is meant to look like."""
    mapped = normalise(text).translate(_CONFUSABLES)
    return "".join(ch for ch in unicodedata.normalize("NFKD", mapped) if not unicodedata.combining(ch))


def _money_key(key: str) -> bool:
    """(c): the key, folded to lower-case letters and digits, holds a money word."""
    folded = re.sub(r"[^a-z0-9]+", "", _skeleton(key).lower())
    return any(word in folded for word in _MONEY_KEY_WORDS)


def _money_pattern(text: str) -> bool:
    """(b): a currency symbol or an ISO code next to a digit, or a two-decimal number."""
    text = "".join("$" if unicodedata.category(ch) == "Sc" else ch for ch in normalise(text))
    return bool(_SYMBOL_NEXT_TO_DIGIT_RE.search(text) or _CODE_NEXT_TO_DIGIT_RE.search(text)
                or _TWO_DECIMALS_RE.search(text))


def _parse_embedded(text: str) -> Optional[Any]:
    """JSON, or a Python literal, inside a string; None when it is neither."""
    for parse in (json.loads, ast.literal_eval):
        try:
            parsed = parse(text)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            continue
        if isinstance(parsed, (dict, list, tuple)):
            return parsed
    return None


class _Scan:
    """One card's rule-1 walk, bounded."""

    def __init__(self) -> None:
        self.nodes = 0

    def count(self, depth: int) -> None:
        self.nodes += 1
        if self.nodes > CARD_SCAN_MAX_NODES or depth > CARD_SCAN_MAX_DEPTH:
            raise _Refused("card too large to scan")

    def money_like(self, value: Any, depth: int = 0, key: Optional[str] = None) -> bool:
        """Rule 1 (module docstring). `key` is the value's own key (a list item's: the list's). Every value is
        visited (no early return), so the bounds hold whatever is found first."""
        self.count(depth)
        if isinstance(value, Mapping):
            found = False
            for child_key, child in value.items():
                name = str(child_key)
                found = self.money_like(child, depth + 1, name) | _money_pattern(name) | found
            return found
        if isinstance(value, (list, tuple)):
            found = False
            for child in value:
                found = self.money_like(child, depth + 1, key) | found
            return found
        if value is None or isinstance(value, bool):
            return False
        if isinstance(value, str):
            text = normalise(value).strip()
            if text[:1] in ("{", "["):
                if len(text) > CARD_SCAN_MAX_JSON_CHARS:
                    raise _Refused("embedded data too large to read")
                parsed = _parse_embedded(text)
                if parsed is not None:
                    return self.money_like(parsed, depth + 1, key)
            if not text:
                return False
            return _money_pattern(text) or (key is not None and _money_key(key))
        return True  # (a): a number, or any other non-JSON leaf


def money_like(data: Any) -> bool:
    """Rule 1: whether anything in `data` is money-like; raises `_Refused` past the bounds. Pure."""
    return _Scan().money_like(data)


def _amount(value: Any, ceiling: int, nullable: bool = False) -> int:
    """An amount in cents (module docstring, rule 2), at most the ceiling; raises `_Refused` otherwise."""
    if value is None and nullable:
        return 0
    if isinstance(value, bool):
        raise _Refused("a boolean amount")
    if isinstance(value, int):
        minor = value * 100
    elif isinstance(value, float):
        if not math.isfinite(value) or abs(value) > _NUMBER_MAX:
            raise _Refused("a number out of range")
        exact = decimal.Decimal(repr(value))
        if exact != exact.quantize(decimal.Decimal("0.01")):
            raise _Refused("more than two decimals")
        minor = int(exact * 100)
    elif isinstance(value, str) and _DECIMAL_STRING_RE.match(value):
        units, _, cents = value.partition(".")
        minor = int(units) * 100 + int(cents)
    else:
        raise _Refused("not a JSON number or a two-decimal string")
    if minor < 0:
        raise _Refused("a negative amount")
    if minor > ceiling:
        raise _Refused("an amount above the ceiling")
    return minor


def _quantity(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _Refused("a quantity that is not a whole number of at least 1")
    return value


def _lines(value: Any, optional: FrozenSet[str], ceiling: int) -> int:
    """sum(quantity x price) over a non-empty list of lines, each exactly {quantity, price} (+ `optional`,
    display-only amounts that are checked and never summed)."""
    if not isinstance(value, list) or not value:
        raise _Refused("line items that are not a non-empty list")
    total = 0
    for line in value:
        if not isinstance(line, Mapping) or not {"quantity", "price"} <= set(line) <= {"quantity", "price"} | optional:
            raise _Refused("a line that is not exactly its shape")
        for key in optional & set(line):
            _amount(line[key], ceiling, nullable=True)
        total += _quantity(line["quantity"]) * _amount(line["price"], ceiling)
    return total


def _shipping_lines(value: Any, ceiling: int) -> int:
    if not isinstance(value, list) or not value:
        raise _Refused("shipping lines that are not a non-empty list")
    total = 0
    for line in value:
        if not isinstance(line, Mapping) or set(line) != {"price"}:
            raise _Refused("a shipping line that is not exactly {price}")
        total += _amount(line["price"], ceiling)
    return total


def _known(card: Mapping[str, Any], address_shipping: bool) -> None:
    """The known non-money keys present: strings (`shipping`, where allowed: an object of address keys and
    strings), none of them money-like."""
    for key in _KNOWN_KEYS & set(card):
        if not isinstance(card[key], str) or _Scan().money_like(card[key], 1, key):
            raise _Refused("a known key that is not a plain string")
    if address_shipping and "shipping" in card:
        address = card["shipping"]
        if (not isinstance(address, Mapping) or not set(address) <= ADDRESS_KEYS
                or not all(isinstance(v, str) for v in address.values()) or _Scan().money_like(address, 1)):
            raise _Refused("a shipping value that is not the buyer's address")


def _usd(card: Mapping[str, Any]) -> None:
    if not isinstance(card.get("currency"), str) or card["currency"] not in _USD:
        raise _Refused("a currency other than USD")


def _s1(card: Mapping[str, Any], ceiling: int) -> None:
    """S1: {total, currency}."""
    _known(card, address_shipping=True)
    _usd(card)
    _amount(card["total"], ceiling)


def _s2(card: Mapping[str, Any], ceiling: int) -> None:
    """S2: {line_items: [{quantity, price}], subtotal, total, currency; shipping, tax optional}."""
    _known(card, address_shipping=False)
    _usd(card)
    lines = _lines(card["line_items"], frozenset(), ceiling)
    subtotal = _amount(card["subtotal"], ceiling)
    shipping = _amount(card["shipping"], ceiling) if "shipping" in card else 0
    tax = _amount(card["tax"], ceiling) if "tax" in card else 0
    total = _amount(card["total"], ceiling)
    if subtotal != lines:
        raise _Refused("a subtotal that is not its lines")
    if total < lines + shipping + tax:
        raise _Refused("a total that does not cover its parts")


def _s3(card: Mapping[str, Any], ceiling: int) -> None:
    """S3, Shopify's REST Order fields: {line_items: [{quantity, price, compare_at_price?}], subtotal_price,
    total_price, currency; shipping_lines: [{price}], total_tax, total_discounts optional}."""
    _known(card, address_shipping=True)
    _usd(card)
    lines = _lines(card["line_items"], frozenset({"compare_at_price"}), ceiling)
    subtotal = _amount(card["subtotal_price"], ceiling)
    shipping = _shipping_lines(card["shipping_lines"], ceiling) if "shipping_lines" in card else 0
    tax = _amount(card["total_tax"], ceiling) if "total_tax" in card else 0
    discounts = _amount(card["total_discounts"], ceiling) if "total_discounts" in card else 0
    total = _amount(card["total_price"], ceiling)
    # discounts <= sum(lines) follows: subtotal_price is an amount (never negative) equal to their difference.
    if subtotal != lines - discounts:
        raise _Refused("a subtotal that is not its lines less the discounts")
    if total < lines + shipping + tax - discounts:
        raise _Refused("a total that does not cover its parts")


#: (name, required keys, optional keys, check). A NEW SHAPE IS ADDED ONLY WITH A MEASURED CARD AND ITS OWN
#: REVIEW (module docstring).
_SHAPES = (
    ("S1", frozenset({"total", "currency"}), _KNOWN_KEYS | {"shipping"}, _s1),
    ("S2", frozenset({"line_items", "subtotal", "total", "currency"}), _KNOWN_KEYS | {"shipping", "tax"}, _s2),
    ("S3", frozenset({"line_items", "subtotal_price", "total_price", "currency"}),
     _KNOWN_KEYS | {"shipping", "shipping_lines", "total_tax", "total_discounts"}, _s3),
)


def matched_shape(data: Any, ceiling: int) -> Optional[str]:
    """The shape `data` is exactly, within `ceiling` (rule 2), or None. Pure."""
    if not isinstance(data, Mapping) or not all(isinstance(k, str) for k in data):
        return None
    keys = set(data)
    for name, required, optional, check in _SHAPES:
        if not required <= keys <= required | optional:
            continue
        try:
            check(data, ceiling)
        except (_Refused, RecursionError):
            continue
        return name
    return None


def confirm_card_refused(data: Any, ceiling: Any) -> bool:
    """True when the card must NOT be confirmed (module docstring). Pure."""
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling <= 0:
        return True
    try:
        if not money_like(data):
            return False  # rule 1: nothing money-like; the token cap decides
    except (_Refused, RecursionError):
        return True
    return matched_shape(data, ceiling) is None  # rule 2


# ── the prose (`text_refused`) ──────────────────────────────────────────────────────────────────

#: Words that make a nearby number in the answer's prose an amount.
_TOTAL_WORDS = frozenset({
    "total", "subtotal", "amount", "price", "due", "balance", "sum", "cost", "charge", "charged", "pay", "paid",
    "payable", "owe", "owed", "fee",
})
#: Currency words and codes (lower case). Next to an amount-shaped number any of them but `usd` is a
#: currency we do not read. Only an UPPER-CASE three-letter code makes a number amount-shaped (so "Euro 24
#: Shop", "Gel Pen 12 pack", "30 pound bag" are not amounts).
_CURRENCY_WORDS = frozenset({
    "eur", "gbp", "cad", "aud", "nzd", "jpy", "cny", "hkd", "sgd", "inr", "mxn", "chf", "sek", "nok", "dkk",
    "brl", "krw", "zar", "twd", "thb", "myr", "idr", "php", "pln", "czk", "huf", "ils", "aed", "sar", "try",
    "rub", "ars", "clp", "cop", "pen", "vnd", "euro", "euros", "pound", "pounds", "yen", "yuan", "rupee",
    "rupees", "rs", "peso", "pesos", "dollar", "dollars", "cent", "cents", "bucks", "usd",
})
#: Lookalike letters (Cyrillic, Greek) mapped to the Latin letter they imitate.
_CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "к": "k", "м": "m", "т": "t",
    "в": "b", "н": "h", "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "ɡ": "g", "ո": "n", "ս": "u",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T",
    "Х": "X", "У": "Y", "Ѕ": "S", "І": "I", "Ј": "J",
    "ο": "o", "α": "a", "ρ": "p", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "τ": "t", "υ": "u", "χ": "x",
    "Ο": "O", "Α": "A", "Ε": "E", "Κ": "K", "Μ": "M", "Ν": "N", "Ρ": "P", "Τ": "T", "Χ": "X", "Υ": "Y",
    "Ζ": "Z", "Β": "B", "Η": "H", "Ι": "I",
})
_EXTRA_INVISIBLE = frozenset({"͏", "᠎", "ᅟ", "ᅠ", "ㅤ"})

_NUMBER_TOKEN_RE = re.compile(r"[0-9][0-9.,]*")
_STRICT_AMOUNT_RE = re.compile(r"^(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,9})(?:\.[0-9]{2})?\Z")
_LEFT_WORD_RE = re.compile(r"([^\W\d_]+)[ .:]{0,2}\Z")
_RIGHT_WORD_RE = re.compile(r"^[ .:]{0,2}([^\W\d_]+)")
_LEFT_DOLLAR_RE = re.compile(r"(?:(?<![^\W\d_])|(?<=[Uu][Ss]))\$(\s*)\Z")
_LEFT_FOREIGN_DOLLAR_RE = re.compile(r"([^\W\d_]{1,3})\$\s*\Z")
_RIGHT_FOREIGN_DOLLAR_RE = re.compile(r"^\s*([^\W\d_]{1,3})\$")
_LEFT_NEGATIVE_RE = re.compile(r"(?:-|\(|−)\s?(?:[Uu][Ss])?\$?\s*(?:[Uu][Ss][Dd]\s?)?\Z")
_PROSE_WORD_RE = re.compile(r"[^\W\d_]+")
#: The prose before a number read for a total-word: this many characters, its last three words.
_PROSE_WINDOW_CHARS = 40
_PROSE_WINDOW_WORDS = 3


def _total_word_before(text: str, start: int) -> bool:
    words = _PROSE_WORD_RE.findall(text[max(0, start - _PROSE_WINDOW_CHARS):start])[-_PROSE_WINDOW_WORDS:]
    return any(_skeleton(w).lower() in _TOTAL_WORDS for w in words)


def _prose_check(raw: str, ceiling: int) -> None:
    """Raises `_Refused` for an amount-shaped number in prose that is not a USD amount within `ceiling`."""
    text = normalise(raw)
    if any(ch.isdigit() and not ch.isascii() for ch in text):
        raise _Refused("a non-ASCII digit")
    for match in _NUMBER_TOKEN_RE.finditer(text):
        token = match.group(0).rstrip(".,")
        start, end = match.start(), match.start() + len(token)
        left, right = text[:start], text[end:]
        near = text[max(0, start - 4):start] + right[:4]
        symbol = any(ch != "$" and unicodedata.category(ch) == "Sc" for ch in near)
        dollar = _LEFT_DOLLAR_RE.search(left)
        foreign_dollar = _LEFT_FOREIGN_DOLLAR_RE.search(left) or _RIGHT_FOREIGN_DOLLAR_RE.search(right)
        raw_words = [m.group(1) for m in (_LEFT_WORD_RE.search(left), _RIGHT_WORD_RE.search(right)) if m]
        words = [_skeleton(w).lower() for w in raw_words]
        marked = dollar is not None or "usd" in words
        amount_shaped = (
            symbol or marked or foreign_dollar is not None
            or any(_skeleton(w).isupper() and len(w) == 3 and _skeleton(w).lower() in _CURRENCY_WORDS
                   for w in raw_words)
            or any(w in _TOTAL_WORDS for w in words) or _total_word_before(text, start)
        )
        if not amount_shaped:
            continue
        if symbol:
            raise _Refused("another currency symbol")
        if foreign_dollar is not None and _skeleton(foreign_dollar.group(1)).lower() != "us":
            raise _Refused("another country's dollar")
        if foreign_dollar is not None and any(not ch.isascii() for ch in foreign_dollar.group(1)):
            raise _Refused("a lookalike currency mark")
        if any(not ch.isascii() for ch in "".join(raw_words)):
            raise _Refused("non-Latin letters next to an amount")
        if any(w in _CURRENCY_WORDS and w != "usd" for w in words):
            raise _Refused("another currency word")
        if not marked:
            raise _Refused("an amount with no USD mark")
        if dollar is not None and len(dollar.group(1)) > 1:
            raise _Refused("a loosely spaced amount")
        if right[:1] in ("e", "E") or not _STRICT_AMOUNT_RE.match(token):
            raise _Refused("a malformed amount")
        units, _, cents = token.partition(".")
        minor = int(units.replace(",", "")) * 100 + int(cents or 0)
        if _LEFT_NEGATIVE_RE.search(left) is not None:
            raise _Refused("a negative amount")
        if minor > ceiling:
            raise _Refused("an amount above the ceiling")


def text_refused(text: Any, ceiling: Any) -> bool:
    """True when prose (a text part of the setup answer) shows an amount-shaped number we cannot verify
    against `ceiling`: "$999.00", "Total 999.00", "You will pay 999.00", "€40" refuse; "card ending 3478"
    does not. Pure; the text is not kept."""
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling <= 0:
        return True
    if not isinstance(text, str):
        return False
    try:
        _prose_check(text, ceiling)
    except (_Refused, RecursionError):
        return True
    return False
