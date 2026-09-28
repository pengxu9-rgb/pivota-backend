"""Read a price a merchant page wrote as text, without guessing its decimal separator.

THE DEFECT THIS REPLACES. `services.external_offers_service._parse_price` kept digits and dots
and dropped everything else, commas included. So an EU page's "28,80" read as 2880 (100x) and
"1.234,56" read as 1.23456 (1/1000x). Those amounts went onto `external_product_seeds` and from
there to the seed search lanes.

THE RULE. A separator is decided by the text's own structure whenever the structure decides it,
and by the page's signals only when it does not:

  * "." and "," both present: the LAST one is the decimal separator ("1.234,56", "1,234.56").
  * one kind, more than once: it is the group separator ("1.234.567", "1,00,000").
  * one kind, once, NOT followed by exactly three digits: it is the decimal separator ("28,80",
    "28.8"). A group separator is always followed by exactly three digits, so there is no other
    reading.
  * one kind, once, followed by exactly three digits ("1,234", "2.400"): AMBIGUOUS. It is a
    group separator for a zero-decimal currency (JPY, KRW, ...), and otherwise only a
    `decimal_hint` decides it. With no hint, or with signals that disagree, it is REFUSED.

Spaces, NBSP, narrow NBSP, thin spaces and apostrophes are group separators only when three
digits follow ("2 400,00", "1'234.56"). A text holding two numbers ("28,80 EUR / 100 ml",
"$22.40 $28.00") is refused, never summed or concatenated.

A refusal is a `PriceRead` with `amount=None` and a `status` saying why, so callers can count it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

from utils.money import ZERO_DECIMAL_CURRENCIES

# Currencies with three minor units: a single separator followed by three digits can be a decimal
# separator only for these.
THREE_DECIMAL_CURRENCIES = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})

# Currencies whose prices are written with "." as the decimal separator wherever they are sold.
# EUR and CAD are absent on purpose: both are written either way (en-IE "€28.80" / de-DE
# "28,80 €", en-CA / fr-CA), so the currency alone says nothing about them.
DOT_DECIMAL_CURRENCIES = frozenset(
    {"USD", "GBP", "AUD", "NZD", "SGD", "HKD", "CNY", "TWD", "INR", "MYR", "THB", "PHP", "ILS", "MXN", "CHF"}
)
# Currencies whose prices are written with "," as the decimal separator.
COMMA_DECIMAL_CURRENCIES = frozenset(
    {"SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "RON", "BGN", "TRY", "RUB", "UAH", "BRL", "ARS", "COP"}
)

# Language subtags whose convention is a decimal comma. A region can override (de-CH, es-MX).
_COMMA_DECIMAL_LANGS = frozenset(
    {
        "de", "fr", "es", "it", "nl", "pt", "pl", "sv", "da", "fi", "nb", "nn", "no", "cs", "sk",
        "ru", "uk", "tr", "el", "hu", "ro", "bg", "hr", "sl", "sr", "lt", "lv", "et", "id", "vi",
        "ca", "eu", "gl", "is",
    }
)
_DOT_DECIMAL_LANGS = frozenset({"en", "ja", "zh", "ko", "th", "he", "ms", "hi", "tl", "fil", "ga"})
_DOT_DECIMAL_REGIONS = frozenset({"CH", "LI", "MX"})

# Shopify's money-format placeholders. Each names the separators the shop renders prices with.
# https://help.shopify.com/en/manual/international/pricing/currency-formatting
_SHOPIFY_AMOUNT_DECIMAL = {
    "amount": ".",
    "amount_no_decimals": ".",
    "amount_with_apostrophe_separator": ".",
    "amount_with_period_and_space_separator": ".",
    "amount_with_comma_separator": ",",
    "amount_no_decimals_with_comma_separator": ",",
    "amount_with_space_separator": ",",
    "amount_no_decimals_with_space_separator": ",",
}

# Currency glyphs in the price text that are themselves a separator signal. Each names currencies
# written with "." as the decimal separator or with no decimals at all (JPY/CNY, KRW, GBP), so in
# either case a "," is a group separator. "€" and "$" are absent: both are written either way
# ("$ 1.234,56" in AR/CL/CO).
_GLYPH_DECIMAL_HINTS = {"¥": ".", "￥": ".", "円": ".", "元": ".", "₩": ".", "￦": ".", "원": ".", "£": "."}

_SPACE_GROUP_RE = re.compile(r"(?<=\d)[    '’](?=\d{3}(?!\d))")
_NUMBER_RE = re.compile(r"[.,]?\d[\d.,]*")


@dataclass(frozen=True)
class PriceRead:
    """One read of one price text. `amount` is None exactly when `status` is not "parsed"."""

    amount: Optional[float]
    status: str


PARSED = "parsed"
# Refusals. Every one means "no price update", never "price 0".
EMPTY = "empty"
NO_DIGITS = "no_digits"
MULTIPLE_NUMBERS = "multiple_numbers"
NEGATIVE = "negative"
MALFORMED_GROUPING = "malformed_grouping"
AMBIGUOUS_SEPARATOR = "ambiguous_separator"
NOT_FINITE = "not_finite"


def decimal_hint_from_currency(currency: Optional[str]) -> Optional[str]:
    code = str(currency or "").strip().upper()
    if code in DOT_DECIMAL_CURRENCIES:
        return "."
    if code in COMMA_DECIMAL_CURRENCIES:
        return ","
    return None


def decimal_hint_from_locale(tag: Optional[str]) -> Optional[str]:
    """"de-DE" / "de_DE" / "fr" -> ","; "en-US" / "ja" / "de-CH" -> "."; unknown -> None."""
    parts = [p for p in re.split(r"[-_]", str(tag or "").strip()) if p]
    if not parts:
        return None
    lang = parts[0].lower()
    region = parts[1].upper() if len(parts) > 1 else ""
    if region in _DOT_DECIMAL_REGIONS and lang in _COMMA_DECIMAL_LANGS | _DOT_DECIMAL_LANGS:
        return "."
    if lang in _COMMA_DECIMAL_LANGS:
        return ","
    if lang in _DOT_DECIMAL_LANGS:
        return "."
    return None


def decimal_hint_from_money_format(money_format: Optional[str]) -> Optional[str]:
    """A Shopify money format ("€{{amount_with_comma_separator}}") -> its decimal separator."""
    tokens = {m.group(1).lower() for m in re.finditer(r"\{\{\s*(amount\w*)\s*\}\}", str(money_format or ""))}
    hints = {_SHOPIFY_AMOUNT_DECIMAL[t] for t in tokens if t in _SHOPIFY_AMOUNT_DECIMAL}
    return hints.pop() if len(hints) == 1 else None


def agreed_hint(hints: Iterable[Optional[str]]) -> Optional[str]:
    """The one decimal separator every present signal names, or None when none do or they
    disagree. Disagreement is not resolved by precedence: it is exactly the case to refuse."""
    present = {h for h in hints if h}
    return present.pop() if len(present) == 1 else None


def _valid_groups(groups: list[str]) -> bool:
    """Digit groups between group separators: 3-digit (1,234,567) or Indian (1,23,45,678)."""
    if not groups[0] or len(groups[0]) > 3 or len(groups[-1]) != 3:
        return False
    middle = groups[1:-1]
    return all(len(g) == 3 for g in middle) or all(len(g) == 2 for g in middle) and len(groups[0]) <= 2


def parse_crawled_price(
    raw: Any, *, currency: Optional[str] = None, decimal_hint: Optional[str] = None
) -> PriceRead:
    """The amount in `raw`, or a refusal saying why. See the module docstring for the rule.

    `raw` may be a JSON number (int/float), which is already unambiguous, or text.
    `currency` is the code the page pairs with this price, used only for its decimal places.
    `decimal_hint` is "." or "," from the page's own signals (`agreed_hint`), or None. A glyph
    in the text itself (`_GLYPH_DECIMAL_HINTS`) is one more signal and must agree with it.
    """
    if isinstance(raw, bool):
        return PriceRead(None, NO_DIGITS)
    if isinstance(raw, (int, float, Decimal)):
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            return PriceRead(None, NOT_FINITE)
        if value != value or value in (float("inf"), float("-inf")):
            return PriceRead(None, NOT_FINITE)
        if value < 0:
            return PriceRead(None, NEGATIVE)
        return PriceRead(value, PARSED)

    text = str(raw or "").strip()
    if not text:
        return PriceRead(None, EMPTY)
    glyph_hints = {h for g, h in _GLYPH_DECIMAL_HINTS.items() if g in text}
    if glyph_hints:
        decimal_hint = agreed_hint((decimal_hint, *glyph_hints)) if decimal_hint else agreed_hint(glyph_hints)
    text = _SPACE_GROUP_RE.sub("", text)
    tokens = list(_NUMBER_RE.finditer(text))
    if not tokens:
        return PriceRead(None, NO_DIGITS)
    if len(tokens) > 1:
        return PriceRead(None, MULTIPLE_NUMBERS)
    match = tokens[0]
    before = text[: match.start()].rstrip()
    if before.endswith(("-", "\u2212")):
        return PriceRead(None, NEGATIVE)
    token = match.group(0).rstrip(".,")
    if token[0] in ".,":
        token = "0" + token

    code = str(currency or "").strip().upper()
    dots, commas = token.count("."), token.count(",")
    if dots and commas:
        decimal_sep = "." if token.rfind(".") > token.rfind(",") else ","
    elif dots or commas:
        sep = "." if dots else ","
        tail = len(token) - token.rfind(sep) - 1
        if dots + commas > 1:
            decimal_sep = ""
        elif tail != 3:
            decimal_sep = sep
        elif code in ZERO_DECIMAL_CURRENCIES:
            decimal_sep = ""
        elif decimal_hint == sep:
            # The page says this separator is the decimal one, and three decimals is a price only
            # in a three-decimal currency. Anywhere else the hint and the text disagree.
            if code not in THREE_DECIMAL_CURRENCIES:
                return PriceRead(None, AMBIGUOUS_SEPARATOR)
            decimal_sep = sep
        elif decimal_hint in (".", ","):
            decimal_sep = ""
        else:
            return PriceRead(None, AMBIGUOUS_SEPARATOR)
    else:
        decimal_sep = ""

    if decimal_sep:
        integer, _, fraction = token.rpartition(decimal_sep)
        if not fraction.isdigit():
            return PriceRead(None, MALFORMED_GROUPING)
    else:
        integer, fraction = token, ""
    group_sep = {".": ",", ",": "."}.get(decimal_sep, "")
    if decimal_sep:
        if decimal_sep in integer:
            return PriceRead(None, MALFORMED_GROUPING)
        groups = integer.split(group_sep) if group_sep and group_sep in integer else [integer]
    else:
        seps = {c for c in integer if c in ".,"}
        if len(seps) > 1:
            return PriceRead(None, MALFORMED_GROUPING)
        groups = integer.split(seps.pop()) if seps else [integer]
    if len(groups) > 1 and not _valid_groups(groups):
        return PriceRead(None, MALFORMED_GROUPING)
    digits = "".join(groups)
    if not digits.isdigit():
        return PriceRead(None, MALFORMED_GROUPING)
    try:
        value = Decimal(f"{digits}.{fraction}" if fraction else digits)
    except InvalidOperation:
        return PriceRead(None, MALFORMED_GROUPING)
    return PriceRead(float(value), PARSED)


_CURRENCY_SYMBOLS = {"€": "EUR", "£": "GBP", "₩": "KRW", "￦": "KRW"}


def read_currency_code(raw: Any) -> Optional[str]:
    """A page's stated currency as an ISO code, or None when it states none we can read.

    "usd" / " EUR " -> the code; "€" / "£" / "₩" -> the one currency that symbol names. "$" and
    "¥" are refused: each names several currencies (USD/CAD/AUD/SGD..., JPY/CNY).
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if re.fullmatch(r"[A-Za-z]{3}", text):
        return text.upper()
    return _CURRENCY_SYMBOLS.get(text)
