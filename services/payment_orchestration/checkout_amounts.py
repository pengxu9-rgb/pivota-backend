"""Reading the amounts a checkout's confirm card (and the setup answer's prose) shows. Pure: no I/O, no logs.

The A2A rail's checkout step confirms a merchant checkout only after this says no amount on the setup's
answer exceeds the approval ceiling, or could exceed it unread (services/payment_orchestration/
a2a_rail_worker._checkout_confirm). It is shared by the worker (cards) and the transport (the answer's
text parts, read inside a2a_rail so that no prose leaves it), so both read with ONE set of rules.

The ceiling is the approved quote BEFORE any discount (a2a_rail_worker.checkout_ceiling: our price x
quantity plus the tax and shipping allowances). So the parts of a card (subtotal, shipping, tax, fees) are
checked as charged before their discounts, and a discount never lowers what is checked.

THE RULES, in the order they apply:

  1. Normalise: NFKC (full-width "$９９９" reads "$999", a no-break space reads " "), every invisible
     format character removed (Unicode category Cf: U+200B, U+2060-U+2064, U+FEFF, ...; and U+034F).
  2. Split each KEY into words (`key_words`): lookalike letters (Cyrillic, Greek) mapped to Latin, accents
     dropped, then split on camelCase, snake_case, kebab-case, dots, spaces and digits, lower-cased; a
     lower-case run of known words ("grandtotal", "shippingfee") is split into them. Every test below is on
     WHOLE words, never on a substring: "internet" holds no "net", "payee" no "pay", "aftercare" no "after".
  3. Classify the key (`key_kind`), first match wins:
       contact   the exact code keys (couponCode, promoCode, discountCode); a key whose last word is an
                 address / contact word (shippingAddress, paymentMethod, taxId); any other key with a
                 contact word and no money word (merchant, label, service, carrier, currency, code, name):
                 a number here is the buyer's own data, never money, unless a currency SYMBOL is next to it;
       slot      an explicit discount slot: the amount / value of a pure discount, or the sum of the
                 discounts spelled exactly (total_discounts, discountTotal, totalSavings, totalCouponAmount):
                 a readable USD amount at most the ceiling (either sign), never summed;
       plain     a percentage or a rate (discountRate, taxRate, `percent` under a discount): not an amount;
       discount  a PURE discount: a discount word (discount, savings, promo, coupon, rebate, voucher), no
                 word before it that makes it what is charged (after, with, final, net, post, pay, due,
                 balance, of, ...) and no money or charge word after it. Read by the SIGN RULE: a negative
                 amount is a discount (allowed, never summed, any size); a positive one is money (summed,
                 at most the ceiling). A percentage ("10%") or a code ("SAVE10") is not an amount;
       money     any other key with a money word (total, amount, price, cost, charge, fee, tax, shipping,
                 gst, tip, freight, due, pay, ...) or with a discount word that names a charge
                 (totalAfterDiscount, couponFee, promoShipping): every number must be USD read with
                 certainty, not negative, at most the ceiling;
       plain     anything else: a number is an amount only when a currency symbol, an upper-case ISO code
                 or a total-word (in the three words before it) stands next to it.
     Inside money, a key that is nothing else inherits money; inside a discount, it follows the sign rule.
  4. A BARE number (int or float) under a money, discount or slot key is unreadable (units unknown).
  5. SUMS, per object: its grand total (ONLY the exact spellings total, grandTotal, totalAmount, amountDue,
     orderTotal, totalPrice, totalDue, balanceDue, in any case or separator) and its components (every other
     amount: subtotal, shipping, tax, fees, `total_*` and `*Total` parts). The object's charge is
     max(grand total, sum of components), and each must be at most the ceiling; a grand total is not added
     to its siblings (a summary beside the line items is not counted twice). Per list, its items add up.
     Quantity x unit price: a quantity is read from an int, a whole float, a digit string, a one-item list
     or an object's number under any qty / quantity / count / units / pcs key (the largest wins); a unit
     price from any price / cost / each / amount key that is not a total. JSON (or a Python literal) inside
     a string is parsed and counts toward its parent.
  6. Bounds: data deeper than CARD_SCAN_MAX_DEPTH or larger than CARD_SCAN_MAX_NODES is refused, never
     partly read. No positive ceiling: refused.

Booleans and None are never amounts. Anything this cannot read with certainty refuses: the purchase is then
cancelled before its confirm, when nothing has been submitted.
"""

from __future__ import annotations

import ast
import json
import re
import unicodedata
from typing import Any, FrozenSet, List, Mapping, Optional, Sequence, Tuple

#: The card's data is depth 0; a leaf at depth 8 is read, at 9 refused. 2000 values are read, 2001 refused.
CARD_SCAN_MAX_DEPTH = 8
CARD_SCAN_MAX_NODES = 2000
#: A string longer than this that looks like JSON / a literal is not parsed: refused.
CARD_SCAN_MAX_JSON_CHARS = 20000

# ── the vocabulary (whole words, lower case) ────────────────────────────────────────────────────

#: Money words: a key with one of them anywhere is money (unless rule 3 says otherwise).
_MONEY_WORDS = frozenset({
    "total", "totals", "subtotal", "subtotals", "amount", "amounts", "amt", "price", "prices", "pricing", "cost",
    "costs", "charge", "charges", "charged", "surcharge", "surcharges", "balance", "vat", "duty", "duties",
    "money", "shipping", "freight", "postage", "handling", "gratuity", "fee", "fees", "tax", "taxes", "gst",
    "hst", "pst", "tip", "tips", "due", "pay", "payable", "cents",
})
#: Money only as the WHOLE key ("delivery" is a charge, "deliveryWindow" is not).
_MONEY_EXACT = frozenset({"sum", "payment", "payments", "delivery", "each", "rate", "rates"})
#: The discount words.
_DISCOUNT_WORDS = frozenset({
    "discount", "discounts", "saving", "savings", "promo", "promos", "promotion", "promotions", "coupon",
    "coupons", "rebate", "rebates", "voucher", "vouchers",
})
#: Words that, anywhere in a key with a discount word, make it what is CHARGED (totalAfterDiscount,
#: total_with_coupon, netOfDiscount, youPayWithSavings, amountPostDiscount): money, never a discount.
_CHARGE_QUALIFIERS = frozenset({
    "after", "with", "final", "net", "post", "pay", "payable", "due", "balance", "charged", "of", "sum", "grand",
    "less", "minus", "incl", "including", "without", "adjusted", "remaining",
})
#: After the discount word, these keep a key a pure discount (discountAmount, couponValue).
_DISCOUNT_AMOUNT_WORDS = frozenset({"amount", "amounts", "amt", "value", "values"})
#: The children of a pure discount (or of a slot) that ARE its amount: a slot.
_SLOT_CHILD_KEYS = frozenset({"amount", "value", "amt"})
#: Exact code keys: a code, never an amount, whatever it holds ("SAVE10").
_CODE_KEYS = frozenset({"couponcode", "promocode", "discountcode"})
#: Percentages and rates: never an amount (a currency SYMBOL next to a number there is still read).
_PERCENT_WORDS = frozenset({"percent", "percents", "percentage", "percentages", "pct", "ratio"})
_RATE_OF = frozenset({"tax", "vat", "exchange", "conversion", "interest"}) | _DISCOUNT_WORDS
#: Contact words: with no money word, a key with one of them is contact (the buyer's data, descriptors).
_CONTACT_WORDS = frozenset({
    "address", "addresses", "zip", "zipcode", "postal", "postcode", "phone", "telephone", "mobile", "email",
    "method", "speed", "instruction", "instructions", "name", "street", "city", "state", "country", "region",
    "province", "apt", "apartment", "suite", "house", "floor", "building", "unit", "date", "time", "id", "ids",
    "ref", "reference", "number", "type", "status", "last", "first", "line", "carrier", "service", "company",
    "merchant", "label", "description", "title", "note", "notes", "currency", "code", "card", "brand", "sku",
    "barcode", "url", "link", "image",
})
#: With a money word in the key, only a key whose LAST word is one of these is contact (shippingAddress,
#: paymentMethod, dueDate, taxId). `serviceFee`, `merchantFee`, `labelPrice`, `totalWithCode` are money.
_CONTACT_HEADS = frozenset({
    "address", "addresses", "zip", "zipcode", "postal", "postcode", "phone", "telephone", "mobile", "email",
    "method", "speed", "instruction", "instructions", "name", "street", "city", "state", "country", "region",
    "province", "apt", "apartment", "suite", "house", "floor", "building", "date", "time", "id", "ids", "ref",
    "reference", "number", "type", "status",
})
#: A last word that names a part of the word before it ("zip_code", "address_line", "house_number").
_HEAD_SUFFIXES = frozenset({"code", "line", "number"})
#: THE grand totals (joined words). Every other total-ish key is a component.
_GRAND_TOTAL_KEYS = frozenset({
    "total", "grandtotal", "totalamount", "amountdue", "ordertotal", "totalprice", "totaldue", "balancedue",
})
_TOTALISH = frozenset({"total", "totals", "subtotal", "subtotals", "sum"})
_UNIT_PRICE_WORDS = frozenset({"price", "prices", "cost", "costs", "each", "amount", "amt", "rate"})
_QUANTITY_WORDS = frozenset({"qty", "quantity", "quantities", "count", "units", "pcs", "pieces", "piece"})
#: Other words a lower-case run may be split into (with every word above).
_STRUCTURE_WORDS = frozenset({
    "order", "orders", "item", "items", "line", "lines", "product", "products", "merchandise", "cart",
    "shipment", "discounted", "applied", "unit", "info", "details", "summary", "grand", "code", "per",
})
#: Words that make a nearby number in a plain field (or the answer's prose) an amount.
_TOTAL_WORDS = frozenset({
    "total", "subtotal", "amount", "price", "due", "balance", "sum", "cost", "charge", "charged", "pay", "paid",
    "payable", "owe", "owed", "fee",
})
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
_VOCABULARY = (_MONEY_WORDS | _MONEY_EXACT | _DISCOUNT_WORDS | _CHARGE_QUALIFIERS | _DISCOUNT_AMOUNT_WORDS
               | _PERCENT_WORDS | _RATE_OF | _CONTACT_WORDS | _QUANTITY_WORDS | _STRUCTURE_WORDS)
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
_PERCENT_AFTER_RE = re.compile(r"^\s?%")
_EXPONENT_AFTER_RE = re.compile(r"^[eE][+-]?[0-9]")
_EXPONENT_BEFORE_RE = re.compile(r"[0-9][eE][+-]?\Z")
_PROSE_WORD_RE = re.compile(r"[^\W\d_]+")
#: The prose before a number read for a total-word: this many characters, its last three words.
_PROSE_WINDOW_CHARS = 40
_PROSE_WINDOW_WORDS = 3


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


def _split_run(word: str) -> List[str]:
    """A lower-case run split into known words when it is made of them only ("grandtotal" -> grand, total);
    otherwise the run itself. Fewest words wins."""
    if word in _VOCABULARY or not word.isascii() or not word.isalpha() or len(word) > 64:
        return [word]
    best: List[Optional[List[str]]] = [None] * (len(word) + 1)
    best[0] = []
    for end in range(1, len(word) + 1):
        for start in range(max(0, end - 14), end - 1):
            head = best[start]
            if head is not None and word[start:end] in _VOCABULARY:
                if best[end] is None or len(head) + 1 < len(best[end]):
                    best[end] = head + [word[start:end]]
    return best[-1] or [word]


def key_words(name: str) -> Tuple[str, ...]:
    """A key as lower-case whole words: camelCase, snake_case, kebab-case, dots, spaces and digits split."""
    text = _skeleton(name)
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
    text = re.sub(r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])", " ", text)
    words: List[str] = []
    for raw in re.split(r"[\W_]+", text):
        if raw:
            words.extend(_split_run(raw.lower()))
    return tuple(words)


def canon_key(name: str) -> str:
    """The key's words joined: "grand_total", "GrandTotal" and "grand-total" are all "grandtotal"."""
    return "".join(key_words(name))


def _words(name_or_words: Any) -> Tuple[str, ...]:
    return tuple(name_or_words) if isinstance(name_or_words, (tuple, list)) else key_words(str(name_or_words))


#: How a key's value is read.
CONTACT, MONEY, PLAIN, DISCOUNT, SLOT, DISCOUNT_INNER = (
    "contact", "money", "plain", "discount", "slot", "discount-inner")
#: The kinds read by the discount rules (a code or a percentage is not an amount there).
_DISCOUNT_KINDS = frozenset({DISCOUNT, SLOT, DISCOUNT_INNER})


def _has_money_word(words: Sequence[str]) -> bool:
    return any(w in _MONEY_WORDS for w in words) or "".join(words) in _MONEY_EXACT


def is_pure_discount(name_or_words: Any) -> bool:
    """A key that names only an amount TAKEN OFF (rule 3): a discount word, no charge qualifier anywhere,
    and after the last discount word only its amount (discountAmount) or another plain word (discountNetwork),
    never a money word (promoShipping, couponFee). A target before it is fine (shippingDiscount, taxDiscount)."""
    words = _words(name_or_words)
    marks = [i for i, w in enumerate(words) if w in _DISCOUNT_WORDS]
    if not marks or any(w in _CHARGE_QUALIFIERS for w in words):
        return False
    after = words[marks[-1] + 1:]
    return not any(w in _MONEY_WORDS and w not in _DISCOUNT_AMOUNT_WORDS for w in after)


def _is_discount_slot_key(words: Sequence[str]) -> bool:
    """The sum of the discounts, spelled exactly: a discount word, `total`, and at most its amount
    (total_discounts, discountTotal, totalSavings, totalDiscountAmount, discount_amount_total)."""
    rest = [w for w in words if w not in _DISCOUNT_WORDS]
    return (len(words) <= 4 and any(w in _DISCOUNT_WORDS for w in words) and rest.count("total") == 1
            and all(w == "total" or w in _DISCOUNT_AMOUNT_WORDS for w in rest))


def _is_grand_total_key(name_or_words: Any) -> bool:
    """One of the exact grand-total spellings (rule 5)."""
    return "".join(_words(name_or_words)) in _GRAND_TOTAL_KEYS


def _is_unit_price_key(words: Sequence[str]) -> bool:
    return any(w in _UNIT_PRICE_WORDS for w in words) and not any(w in _TOTALISH for w in words)


def _is_quantity_key(words: Sequence[str]) -> bool:
    return any(w in _QUANTITY_WORDS for w in words) and not _has_money_word(words)


def _contact_head(words: Sequence[str]) -> bool:
    """The key's last word (digits dropped) is an address / contact word: "shippingAddress1", "zip_code"."""
    core = [w for w in words if not w.isdigit()]
    if not core:
        return False
    if core[-1] in _HEAD_SUFFIXES and len(core) > 1 and core[-2] in _CONTACT_HEADS:
        return True
    return core[-1] in _CONTACT_HEADS


def key_kind(name: str, parent: str, sibling_currency: bool = False) -> str:
    """CONTACT, SLOT, PLAIN, DISCOUNT, MONEY or DISCOUNT_INNER for the value under `name`, inside a parent
    read as `parent` (rule 3, whole words)."""
    words = key_words(name)
    joined = "".join(words)
    present = set(words)
    if joined in _CODE_KEYS:
        return CONTACT
    if parent in (DISCOUNT, SLOT) and joined in _SLOT_CHILD_KEYS:
        return SLOT  # `{"discount": {"amount": "-$5.00"}}`: the amount of a discount
    if _is_discount_slot_key(words):
        return SLOT
    if present & _PERCENT_WORDS or ("rate" in present and (present & _RATE_OF or parent in _DISCOUNT_KINDS)):
        return PLAIN  # a percentage or a rate: not an amount
    if is_pure_discount(words):
        return DISCOUNT
    if _has_money_word(words):
        return CONTACT if _contact_head(words) else MONEY
    if present & _DISCOUNT_WORDS:
        return MONEY  # a discount word with a charge qualifier: what is charged (netOfDiscount)
    if present & _CONTACT_WORDS:
        return CONTACT
    if joined == "value" and sibling_currency:
        return MONEY
    if parent == MONEY:
        return MONEY
    if parent in _DISCOUNT_KINDS:
        return DISCOUNT_INNER  # under a discount, anything else follows the sign rule
    return PLAIN


def _quantity(value: Any, depth: int = 0) -> Optional[int]:
    """A quantity read from an int, a whole float, a digit string, a list of them or an object holding
    them (the largest); None when absent. A quantity that is not a whole number refuses."""
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
    if depth >= 2:
        raise _Refused("a quantity nested too deep")
    items = value.values() if isinstance(value, Mapping) else value if isinstance(value, (list, tuple)) else ()
    found = [q for q in (_quantity(item, depth + 1) for item in items) if q is not None]
    if any(q < 0 for q in found):
        raise _Refused("a negative quantity")
    return max(found) if found else None


def _glued_to_letters(text: str, start: int, end: int) -> bool:
    return (start > 0 and text[start - 1].isalpha()) or (end < len(text) and text[end].isalpha())


def _total_word_before(text: str, start: int) -> bool:
    words = _PROSE_WORD_RE.findall(text[max(0, start - _PROSE_WINDOW_CHARS):start])[-_PROSE_WINDOW_WORDS:]
    return any(_skeleton(w).lower() in _TOTAL_WORDS for w in words)


def string_amounts(raw: str, ceiling: int, kind: str) -> List[int]:
    """The USD amounts `raw` adds to a sum, each read with certainty and within `ceiling`; raises `_Refused`
    for an amount-shaped number that is not one (module docstring, rules 3 and 4)."""
    text = normalise(raw)
    if any(ch.isdigit() and not ch.isascii() for ch in text):
        raise _Refused("a non-ASCII digit")
    has_letters = any(ch.isalpha() for ch in text)
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
        currency_next = symbol or marked or foreign_dollar is not None or any(w in _CURRENCY_WORDS for w in words)
        if kind == CONTACT:
            amount_shaped = symbol or marked
        elif kind in _DISCOUNT_KINDS:
            # A percentage ("10%", "10 % off"), a code ("SAVE10", "2FOR1") or a count in prose ("Buy 2 get 1
            # free") is not an amount; a bare "5.00", "-5", or anything next to a currency mark is.
            if _EXPONENT_AFTER_RE.match(right) or _EXPONENT_BEFORE_RE.search(left):
                amount_shaped = True  # "1e3": an exponent, refused below as malformed
            elif _PERCENT_AFTER_RE.match(right):
                amount_shaped = False
            elif _glued_to_letters(text, start, end):
                amount_shaped = currency_next
            elif has_letters and "." not in token:
                amount_shaped = currency_next
            else:
                amount_shaped = True
        elif kind == MONEY:
            amount_shaped = True
        else:
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
        negative = _LEFT_NEGATIVE_RE.search(left) is not None
        if kind == SLOT:
            # The amount of a discount, or their sum: never summed, either sign; never larger than what the
            # buyer approved (that is more likely a mislabelled total).
            if minor > ceiling:
                raise _Refused("a discount larger than the ceiling")
            continue
        if negative and kind in (DISCOUNT, DISCOUNT_INNER):
            continue  # THE SIGN RULE: taken off; it only lowers the charge, so it is never summed
        if negative:
            raise _Refused("a negative amount")
        if minor > ceiling:
            raise _Refused("an amount above the ceiling")
        amounts.append(minor)  # under a discount key too: a positive amount there is money
    if (kind == MONEY or kind in _DISCOUNT_KINDS) and not any(ch.isdigit() for ch in text):
        # "nine hundred ninety-nine dollars": an amount we cannot read. A currency name alone ("USD") is not.
        found = {_skeleton(w).lower() for w in _PROSE_WORD_RE.findall(text)}
        if found & _NUMBER_WORDS:
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
            quantities: List[int] = []
            for key, item in value.items():
                name = normalise(str(key))
                string_amounts(name, ceiling, PLAIN if kind != CONTACT else CONTACT)  # an amount in a key
                words = key_words(name)
                if _is_quantity_key(words):
                    count(depth + 1)
                    found = _quantity(item)
                    if found is not None:
                        quantities.append(found)
                    continue
                child = key_kind(name, kind, sibling_currency)
                if isinstance(item, str):
                    count(depth + 1)
                    contribution, is_total = scan_string(item, depth + 1, child)
                else:
                    contribution, is_total = walk(item, depth + 1, child)
                if not contribution:
                    continue
                if _is_grand_total_key(words) or is_total:
                    totals.append(contribution)
                else:
                    components.append(contribution)
                if _is_unit_price_key(words):
                    units.append(contribution)
            if sum(components) > ceiling:
                raise _Refused("parts add up past the ceiling")
            quantity = max(quantities) if quantities else None
            if quantity is not None and quantity < 0:
                raise _Refused("a negative quantity")
            line = quantity * max(units) if quantity and units else 0
            if line > ceiling:
                raise _Refused("quantity x unit price past the ceiling")
            charged = max([sum(components), line, *totals])
            return charged, bool(totals)
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
        if isinstance(value, (int, float)) and (kind == MONEY or kind in _DISCOUNT_KINDS):
            raise _Refused("a bare number under a money or discount key: units unknown")
        return 0, False

    try:
        walk(data if isinstance(data, Mapping) else {"card": data}, 0, PLAIN)
    except (_Refused, RecursionError):
        return True
    return False


def text_refused(text: Any, ceiling: Any) -> bool:
    """True when prose (a text part of the setup answer) shows an amount-shaped number we cannot verify
    against `ceiling`. Read as a PLAIN field: "$999.00", "Total 999.00", "You will pay 999.00", "€40"
    refuse; "card ending 3478" does not. Pure; the text is not kept."""
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling <= 0:
        return True
    if not isinstance(text, str):
        return False
    try:
        string_amounts(text, ceiling, PLAIN)
    except (_Refused, RecursionError):
        return True
    return False
