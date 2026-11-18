"""Reading the amounts a checkout's confirm card (and the setup answer's prose) shows. Pure: no I/O, no logs.

The A2A rail's checkout step confirms a merchant checkout only after this says no amount on the setup's
answer exceeds the approval ceiling, or could exceed it unread (services/payment_orchestration/
a2a_rail_worker._checkout_confirm). It is shared by the worker (cards) and the transport (the answer's
text parts, read inside a2a_rail so that no prose leaves it), so both read with ONE set of rules.

THE RULES, in the order they apply:

  1. Normalise: NFKC (full-width "$９９９" reads "$999", a no-break space reads " "), every invisible
     format character removed (Unicode category Cf: U+200B, U+2060-U+2064, U+FEFF, ...; and U+034F).
  2. Classify each KEY: lookalike letters (Cyrillic, Greek) are mapped to Latin and accents dropped, then
       contact   address / zip / postal / phone / email, and the other non-money spellings (names, street,
                 city, unit, company, instructions, card display, method, ids, dates): a number here is
                 the buyer's own data, never money, unless a currency SYMBOL is right next to it;
       money     total / amount / price / cost / charge / fee / tax / shipping* / gst / tip / freight ...:
                 every number here must be a USD amount read with certainty;
       plain     anything else (a merchant name, an item name, a note): a number is an amount only when a
                 currency symbol, an upper-case ISO code or a total-word stands next to it.
     Inside `shipping` / `payment` / `delivery` (money), a key that is not contact inherits money.
  3. Read each AMOUNT-SHAPED number (rule 2 decides which): it must be a strict USD amount ("$1,234.50",
     "US$ 9", "USD 9", "9.00 USD", case-insensitive USD), at most the ceiling, not negative, not in an
     exponent, at most one space after the mark, with no other currency (symbol, another country's
     dollar, ISO code, currency word) and no non-ASCII letter next to it (a lookalike "СА$" is unknown).
     A money-ish string with no digits but a currency or number word ("nine hundred dollars") is unread.
  4. SUMS: per object, the non-grand-total amounts (subtotal + shipping + tax + fees + every `total_*`
     part) and quantity x unit price; per list, its items. Each at most the ceiling. A quantity is read
     from an int, a whole float or a digit string under any *qty* / *quantity* key; a unit price from
     price / unitPrice / unitCost / each, as a string or a money object ({"amount": ..} / {"value": ..}).
     A grand total (total, grandTotal, totalPrice, orderTotal, amountDue, totalDue, totalAmount) is
     checked alone and is not added to its siblings' sum (a summary beside the line items is not counted
     twice). JSON (or a Python-literal) inside a string is parsed and counts toward its parent.
  5. Bounds: data deeper than CARD_SCAN_MAX_DEPTH or larger than CARD_SCAN_MAX_NODES is refused, never
     partly read. No positive ceiling: refused.

Booleans and None are never amounts. Anything this cannot read with certainty refuses: the purchase is then
cancelled before its confirm, when nothing has been submitted.
"""

from __future__ import annotations

import ast
import json
import re
import unicodedata
from typing import Any, List, Mapping, Optional, Tuple

#: The card's data is depth 0; a leaf at depth 8 is read, at 9 refused. 2000 values are read, 2001 refused.
CARD_SCAN_MAX_DEPTH = 8
CARD_SCAN_MAX_NODES = 2000
#: A string longer than this that looks like JSON / a literal is not parsed: refused.
CARD_SCAN_MAX_JSON_CHARS = 20000

#: Strong non-money spellings: they win over a money spelling ("shippingAddress" is an address,
#: "shippingMethod" a method, "itemName" a name): the buyer's contact and the descriptors around it.
_CONTACT_STRONG = (
    "address", "zip", "postal", "postcode", "phone", "email", "method", "speed", "carrier", "service",
    "instruction", "name", "company", "merchant", "label", "description", "title", "note", "currency", "code",
)
#: DISCOUNT spellings (after the strong contact check, so a `couponCode` is a code, not an amount). A
#: discount only ever LOWERS what is charged, so its amount is never added to a sum (that could only make a
#: card look dearer) and never subtracted from one either (conservative: a sum is checked as if no discount
#: applied, so ignoring a discount can never let an over-ceiling charge through). A negative discount
#: ("-$5.00", -500) is therefore allowed, and so is one above the ceiling. It must still be READABLE: an
#: unmarked string, a malformed amount or another currency under a discount key refuses like anywhere else.
_DISCOUNT_PARTS = ("discount", "saving", "promo", "coupon", "rebate", "voucher")
#: A key with a discount word is a PURE discount only when it carries none of these too: `totalAfterDiscount`,
#: `discountedTotal`, `couponFee`, `promoShipping`, `netOfDiscount` are what is CHARGED, not what is taken off,
#: so they are money (read strictly, summed as a component, a grand total when they are a total). When in
#: doubt, money: cancelling on doubt is safe, ignoring on doubt is not.
_NOT_A_PURE_DISCOUNT = (
    "total", "price", "amountdue", "fee", "charge", "cost", "shipping", "tax", "net", "after", "with", "final",
    "pay", "subtotal", "due",
)
#: Under a discount object, the children that are ITS amount (anything else there is money: doubt is money).
_DISCOUNT_AMOUNT_KEYS = frozenset({"amount", "value", "amt"})
#: The sum OF the discounts, spelled with "total": still a pure discount.
_DISCOUNT_TOTAL_KEYS = frozenset({"totaldiscounts", "discounttotal", "totaldiscount", "discountstotal"})
#: Money spellings, matched as parts of the canonical key ...
_MONEY_PARTS = (
    "total", "amount", "price", "cost", "charge", "balance", "vat", "duty", "money", "shipping",
    "freight", "postage", "handling", "gratuity",
)
#: ... at its end ...
_MONEY_SUFFIXES = ("due", "fee", "fees", "tax", "taxes", "gst", "hst", "pst", "tip", "tips")
#: ... or exactly.
_MONEY_EXACT = frozenset({"sum", "payment", "delivery", "each", "rate", "shippingrate"})
#: The other non-money spellings (after the money check): a number under them is the buyer's own data.
_CONTACT_PARTS = (
    "street", "city", "state", "country", "region", "province", "line1", "line2", "apt", "apartment",
    "suite", "house", "last4", "brand", "card", "sku", "date", "time",
)
_CONTACT_EXACT = frozenset({"unit", "floor", "building"})
_CONTACT_SUFFIXES = ("id",)
#: Grand-total spellings: checked alone, not added to their siblings.
_GRAND_TOTAL_KEYS = frozenset({
    "total", "grandtotal", "totalprice", "ordertotal", "amountdue", "totaldue", "totalamount",
    "grandtotalamount", "totalpayable", "amounttotal",
})
#: Unit-price spellings (for quantity x unit price).
_UNIT_PRICE_KEYS = frozenset({"price", "unitprice", "unitcost", "cost", "each", "priceeach", "unitamount", "itemprice"})
#: Words that make a nearby number in a plain field an amount.
_TOTAL_WORDS = frozenset({"total", "subtotal", "amount", "price", "due", "balance", "sum", "cost", "charge"})
#: Currency words and codes (lower case). Next to an amount-shaped number any of them but `usd` is a
#: currency we do not read. In a PLAIN field only an UPPER-CASE three-letter code counts (so "Euro 24 Shop",
#: "Gel Pen 12 pack", "30 pound bag" are not amounts).
_CURRENCY_WORDS = frozenset({
    "eur", "gbp", "cad", "aud", "nzd", "jpy", "cny", "hkd", "sgd", "inr", "mxn", "chf", "sek", "nok", "dkk",
    "brl", "krw", "zar", "twd", "thb", "myr", "idr", "php", "pln", "czk", "huf", "ils", "aed", "sar", "try",
    "rub", "ars", "clp", "cop", "pen", "vnd", "euro", "euros", "pound", "pounds", "yen", "yuan", "rupee",
    "rupees", "rs", "peso", "pesos", "dollar", "dollars", "cent", "cents", "bucks", "usd",
})
_NUMBER_WORDS = frozenset({
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
    "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred", "thousand",
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


def canon_key(name: str) -> str:
    return "".join(ch for ch in _skeleton(name).lower() if ch.isascii() and ch.isalnum())


#: How a key's value is read.
CONTACT, MONEY, PLAIN, DISCOUNT = "contact", "money", "plain", "discount"


def is_pure_discount(canon: str) -> bool:
    """A key (canonical) that names only an amount TAKEN OFF: a discount word and no money word, or the sum of
    the discounts (`total_discounts`)."""
    if canon in _DISCOUNT_TOTAL_KEYS:
        return True
    return any(part in canon for part in _DISCOUNT_PARTS) and not any(word in canon for word in _NOT_A_PURE_DISCOUNT)


def _is_grand_total_key(canon: str) -> bool:
    """A grand total: an exact spelling, or a total qualified as what is finally charged (`totalAfterDiscount`,
    `discountedTotal`, `totalWithCoupon`, `finalTotal`). Never a subtotal, never the sum of the discounts."""
    if canon in _GRAND_TOTAL_KEYS:
        return True
    if "total" not in canon or "subtotal" in canon or canon in _DISCOUNT_TOTAL_KEYS:
        return False
    return any(word in canon for word in ("after", "with", "final", "discounted", "grand", "payable", "net"))


def _is_unit_price_key(canon: str) -> bool:
    return (canon in _UNIT_PRICE_KEYS or "unitprice" in canon or "unitcost" in canon
            or ("price" in canon and "total" not in canon))


def key_kind(name: str, parent: str, sibling_currency: bool = False) -> str:
    """CONTACT, MONEY or PLAIN for the value under `name`, inside a parent read as `parent`."""
    canon = canon_key(name)
    if any(part in canon for part in _CONTACT_STRONG):
        return CONTACT
    if is_pure_discount(canon):
        return DISCOUNT
    if any(part in canon for part in _DISCOUNT_PARTS):
        return MONEY  # a discount word beside a money word: what is charged (`totalAfterDiscount`)
    if parent == DISCOUNT and canon in _DISCOUNT_AMOUNT_KEYS:
        return DISCOUNT  # `{"discount": {"amount": "-$5.00"}}`: the amount of a discount
    if (canon in _MONEY_EXACT or any(part in canon for part in _MONEY_PARTS)
            or canon.endswith(_MONEY_SUFFIXES)):
        return MONEY
    if (canon in _CONTACT_EXACT or any(part in canon for part in _CONTACT_PARTS)
            or canon.endswith(_CONTACT_SUFFIXES)):
        return CONTACT
    if canon == "value" and sibling_currency:
        return MONEY
    # Under money, or under a discount (anything there but its amount is what is charged, or unknown): money.
    return MONEY if parent in (MONEY, DISCOUNT) else PLAIN


def _quantity(value: Any) -> Optional[int]:
    """A quantity read from an int, a whole float or a digit string; None when absent. A quantity that is
    not a whole number refuses (we would not know what is ordered)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise _Refused("a fractional quantity")
    if isinstance(value, str):
        text = normalise(value).strip()
        if text.isdigit() and text.isascii():
            return int(text)
        if text and any(ch.isdigit() for ch in text):
            raise _Refused("a quantity we cannot read")
    return None


def _is_quantity_key(canon: str) -> bool:
    return "qty" in canon or "quantity" in canon or canon in ("count", "units")


def string_amounts(raw: str, ceiling: int, kind: str) -> List[int]:
    """The USD amounts `raw` shows, each read with certainty and within `ceiling`; raises `_Refused` for an
    amount-shaped number that is not one (see the module docstring, rules 2 and 3)."""
    text = normalise(raw)
    if any(ch.isdigit() and not ch.isascii() for ch in text):
        raise _Refused("a non-ASCII digit")
    amounts: List[int] = []
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
        usd_word = "usd" in words
        marked = dollar is not None or usd_word
        if kind == CONTACT:
            amount_shaped = symbol or marked
        elif kind == DISCOUNT and right[:1] == "%":
            amount_shaped = False  # "10%" off: a rate, and a discount only lowers the charge
        elif kind in (MONEY, DISCOUNT):
            amount_shaped = True
        else:
            amount_shaped = (
                symbol or marked or foreign_dollar is not None
                or any(_skeleton(w).isupper() and len(w) == 3 and _skeleton(w).lower() in _CURRENCY_WORDS
                       for w in raw_words)
                or any(w in _TOTAL_WORDS for w in words)
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
        if kind == DISCOUNT:
            units_, _, cents_ = token.partition(".")
            if int(units_.replace(",", "")) * 100 + int(cents_ or 0) > ceiling:
                # A real discount is never larger than what the buyer approved: an amount that big under a
                # discount key is more likely a mislabelled total. Cancel.
                raise _Refused("a discount larger than the ceiling")
            continue  # readable USD and within the ceiling; it only lowers the charge; never summed
        if _LEFT_NEGATIVE_RE.search(left):
            raise _Refused("a negative amount")
        units, _, cents = token.partition(".")
        minor = int(units.replace(",", "")) * 100 + int(cents or 0)
        if minor > ceiling:
            raise _Refused("an amount above the ceiling")
        amounts.append(minor)
    if kind in (MONEY, DISCOUNT) and not any(ch.isdigit() for ch in text):
        # "nine hundred ninety-nine dollars": an amount we cannot read. A currency name alone ("USD") is not.
        words = {_skeleton(w).lower() for w in re.findall(r"[^\W\d_]+", text)}
        if words & _NUMBER_WORDS:
            raise _Refused("an amount written in words")
    return amounts


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


def confirm_card_refused(data: Any, ceiling: Any) -> bool:
    """True when the card must NOT be confirmed (module docstring). Pure."""
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling <= 0:
        return True
    nodes = 0

    def count(depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > CARD_SCAN_MAX_NODES or depth > CARD_SCAN_MAX_DEPTH:
            raise _Refused("card too large to scan")

    def scan_string(raw: str, depth: int, kind: str) -> Tuple[int, bool]:
        """(what the string adds to a sum, whether that is a grand total). Embedded data is walked."""
        text = normalise(raw).strip()
        if text[:1] in ("{", "["):
            if len(text) > CARD_SCAN_MAX_JSON_CHARS:
                raise _Refused("embedded data too large to read")
            parsed = _parse_embedded(text)
            if parsed is not None:
                return walk(parsed, depth + 1, kind)
        return sum(string_amounts(raw, ceiling, kind)), False

    def walk(value: Any, depth: int, kind: str) -> Tuple[int, bool]:
        """Scan `value`; return (what it adds to its parent's sum, whether that came from a grand total)."""
        count(depth)
        if isinstance(value, Mapping):
            sibling_currency = any(canon_key(str(k)) == "currency" for k in value)
            totals: List[int] = []
            components: List[int] = []
            units: List[int] = []
            quantity: Optional[int] = None
            for key, item in value.items():
                name = normalise(str(key))
                string_amounts(name, ceiling, PLAIN if kind != CONTACT else CONTACT)  # an amount in a key
                canon = canon_key(name)
                child = key_kind(name, kind, sibling_currency)
                if _is_quantity_key(canon) and not isinstance(item, (Mapping, list, tuple)):
                    count(depth + 1)
                    found = _quantity(item)
                    if found is not None:
                        quantity = found
                    continue
                if isinstance(item, str):
                    count(depth + 1)
                    contribution, is_total = scan_string(item, depth + 1, child)
                else:
                    contribution, is_total = walk(item, depth + 1, child)
                if not contribution:
                    continue
                if _is_grand_total_key(canon) or is_total:
                    totals.append(contribution)
                else:
                    components.append(contribution)
                if _is_unit_price_key(canon):
                    units.append(contribution)
            if sum(components) > ceiling:
                raise _Refused("parts add up past the ceiling")
            if quantity is not None and quantity < 0:
                raise _Refused("a negative quantity")
            if quantity and units and quantity * max(units) > ceiling:
                raise _Refused("quantity x unit price past the ceiling")
            if totals:
                return max(totals), True
            if quantity and units:
                return quantity * max(units), False
            return sum(components), False
        if isinstance(value, (list, tuple)):
            line_items = 0
            for item in value:
                if isinstance(item, str):
                    count(depth + 1)
                    contribution, _ = scan_string(item, depth + 1, kind)
                else:
                    contribution, _ = walk(item, depth + 1, kind)
                line_items += contribution
            if line_items > ceiling:
                raise _Refused("line items add up past the ceiling")
            return line_items, False
        if isinstance(value, str):
            return scan_string(value, depth, kind)
        if value is None or isinstance(value, bool):
            return 0, False
        if isinstance(value, (int, float)) and kind == MONEY:
            raise _Refused("a bare number under a money key: units unknown")
        if isinstance(value, (int, float)) and kind == DISCOUNT:
            # A bare number under a discount key: an integer is read in cents (-500 is $5.00), a fraction in
            # dollars (-5.5 is $5.50). Larger than the ceiling either way: more likely a mislabelled total.
            magnitude = abs(value) if isinstance(value, int) else abs(value) * 100
            if magnitude > ceiling:
                raise _Refused("a discount larger than the ceiling")
        return 0, False  # a discount within the ceiling only lowers the charge: not summed

    try:
        walk(data if isinstance(data, Mapping) else {"card": data}, 0, PLAIN)
    except (_Refused, RecursionError):
        return True
    return False


def text_refused(text: Any, ceiling: Any) -> bool:
    """True when prose (a text part of the setup answer) shows an amount-shaped number we cannot verify
    against `ceiling`. Read as a PLAIN field: "$999.00", "Total 999.00", "€40" refuse; "card ending 3478"
    does not. Pure; the text is not kept."""
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling <= 0:
        return True
    if not isinstance(text, str):
        return False
    try:
        string_amounts(text, ceiling, PLAIN)
    except (_Refused, RecursionError):
        return True
    return False
