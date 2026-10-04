"""Conservative query preparation for the canonical shopping catalog.

Retrieval text and price predicates have different jobs. Keep the caller's
original text, remove only budget clauses we can represent without guessing,
and normalize inflected category nouns using the existing taxonomy vocabulary.
No product, brand, or result-specific aliases belong here.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Any

from services.pdp_category_classifier import classify


_CURRENCY_ALIASES = {"$": "USD", "€": "EUR", "£": "GBP", "dollar": "USD", "dollars": "USD", "euro": "EUR", "euros": "EUR", "pound": "GBP", "pounds": "GBP"}
_CURRENCY_CODES = r"USD|EUR|GBP|CAD|AUD|NZD|JPY|CNY|HKD|SGD|CHF|INR|KRW"
_CURRENCY = rf"(?:[$€£]|(?:{_CURRENCY_CODES})(?=\s|\d)|dollars?\b|euros?\b|pounds?\b)"
_NAMED_CURRENCY = rf"(?:[$€£]|(?:{_CURRENCY_CODES})\b|dollars?\b|euros?\b|pounds?\b)"
_OPERATOR = r"(?:no|not) more than|(?:no|not) less than|less than|more than|at least|at most|up to|under|below|over|above|max(?:imum)?|min(?:imum)?|budget"
_NEGATION = r"do(?:es)?\s+not|don['’]t|doesn['’]t|aren['’]t|isn['’]t|not|never|without"
_MONEY_VERB = r"cost(?:s|ing)?|(?:be\s+)?priced|spend(?:ing)?|pay(?:ing)?|(?:be\s+)?charged"
_BUDGET = re.compile(
    rf"\b(?:(?P<negated_money>{_NEGATION})\s+(?:{_MONEY_VERB})\s+)?"
    rf"\b(?P<operator>{_OPERATOR})(?:\s+of)?\s*"
    rf"(?:(?P<before>{_CURRENCY})\s*)?(?P<amount>\d+(?:\.\d+)?|\.\d+)"
    rf"(?:\s*(?P<after>{_NAMED_CURRENCY}))?"
    r"(?!\w|[.,]\d)", re.IGNORECASE,
)
# A numeric money clause that is not representable must not survive as a
# descriptor: category-OR recall can otherwise return over-budget products.
# Bound the scan to a single short clause; don't mistake an ordinary model
# number or a physical measurement for a currency amount.
_UNPARSED_BUDGET = re.compile(
    rf"\b(?:{_OPERATOR}|price|priced|cost|spend|spending|afford|cheaper|dearer|expensive)"
    r"(?=\s|[-+\d.$€£:<>=])[^\n;!?]{0,64}?[-+]?(?:\d+(?:[.,]\d+)*|\.\d+)", re.I,
)
_UNPARSED_MONEY = re.compile(
    rf"(?:[$€£]|(?:{_CURRENCY_CODES})(?=\s|[-+\d.]))\s*[-+]?(?:\d|\.\d)"
    rf"|\d(?:[\d.,]*\d)?\s*{_NAMED_CURRENCY}", re.I,
)
_NONFINITE_AMOUNT = r"[-+]?(?:s?nan|inf(?:inity)?|∞)"
_NONFINITE_MONEY = re.compile(
    rf"\b(?:{_OPERATOR}|price|priced|cost|spend|spending)\b[^\n;!?]{{0,64}}?{_NONFINITE_AMOUNT}(?![a-z])"
    rf"|(?:[$€£]|(?:{_CURRENCY_CODES}))\s*{_NONFINITE_AMOUNT}(?![a-z])"
    rf"|{_NONFINITE_AMOUNT}\s*{_NAMED_CURRENCY}", re.I,
)
# A recognizable monetary clause with an unsupported textual amount is not
# a harmless search descriptor. This also covers unknown units/currency syntax
# without inventing a conversion or silently ignoring the money request.
_UNPARSED_TEXT_MONEY = re.compile(
    rf"\b(?:{_OPERATOR}|price|priced|cost|spend|spending|dearer|expensive)\b[^\n;!?]{{0,64}}?"
    rf"(?:[$€£]|(?:{_CURRENCY_CODES})(?=\b|[0-9])|dollars?\b|euros?\b|pounds?\b)", re.I,
)
_WORD_AMOUNT = r"zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion"
# Use the same bounded clause scan as numeric amounts. Unsupported modifiers
# and linking verbs do not make an amount harmless ("under about thirty NOK",
# "price should be thirty"). Currency vocabulary is intentionally irrelevant
# to refusal; typed product quantities are excluded below without conversion.
_UNPARSED_WORD_AMOUNT = re.compile(
    rf"\b(?:{_OPERATOR}|price|priced|cost|spend|spending|afford|cheaper|dearer|expensive)\b"
    rf"[^\n;!?]{{0,64}}?\b(?:{_WORD_AMOUNT})\b", re.I,
)
_MONEY_WORD = re.compile(r"\b(?:budget|price|priced|cost|spend|spending|afford|cheaper)\b", re.I)
_MEASUREMENT_SUFFIX = re.compile(
    r"^\s*(?:%|[- ]?\s*(?:percent|per\s+cent|percentage|mm|cm|km|m|inch(?:es)?|ft|feet|ml|lit(?:er|re)s?|l|mg|kg|g|fl\s*oz|oz|ounces?|lbs?|pounds?|gb|tb|mah|hz|watts?|w|stars?|years?)\b)", re.I,
)
_MEASUREMENT_PREFIX = re.compile(r"\b(?:spf|age|size|model|version|rating)\s*\d+(?:\.\d+)?$", re.I)
_UNHANDLED_NEGATION = re.compile(rf"\b(?:{_NEGATION}|no|avoid)\b", re.I)
_LEADING_REQUEST = re.compile(
    r"^(?:please\s+)?(?:find|show|recommend|suggest|search for|look for)\s+"
    r"(?:me\s+)?(?:(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+)?", re.I,
)


def _non_money_numeric_clause(match: re.Match, text: str) -> bool:
    clause = match.group(0)
    if _UNPARSED_MONEY.search(clause):
        return False
    typed = bool(_MEASUREMENT_SUFFIX.match(text[match.end():]) or _MEASUREMENT_PREFIX.search(clause))
    # "budget" can modify the product goal rather than introduce a price:
    # budget-friendly SPF50, budget iPhone15 cases, budget creams in 30ml.
    budget_modifier = bool(
        re.match(r"budget\s+(?!of\b|is\b)", clause, re.I)
        and (re.search(r"\bfriendly\b", clause, re.I) or classify(normalize_catalog_query(clause))
             or re.search(r"\b[a-z]{4,}\s*\d+(?:\.\d+)?$", clause, re.I))
    )
    money_words = {word.lower() for word in _MONEY_WORD.findall(clause)}
    if money_words and not (money_words == {"budget"} and budget_modifier):
        return False
    return typed or bool(budget_modifier and re.search(r"\b[a-z]{4,}\s*\d+(?:\.\d+)?$", clause, re.I))


def normalize_catalog_query(query: str) -> str:
    """Inflect only tokens whose singular is an existing product category.

    Already-recognized terms (headphones, shoes, glass, etc.) are untouched.
    Unknown words, model numbers, brands and qualifiers are never discarded.
    """
    text = _LEADING_REQUEST.sub("", str(query or "").strip()).strip(" .;,\t\n")

    def singular(match: re.Match) -> str:
        token = match.group(0)
        if classify(token):
            return token
        lower = token.lower()
        candidates = []
        if lower.endswith("ies"):
            candidates.append(token[:-3] + "y")
        if lower.endswith("es"):
            candidates.append(token[:-2])
        if lower.endswith("s") and not lower.endswith("ss"):
            candidates.append(token[:-1])
        return next((candidate for candidate in candidates if classify(candidate)), token)

    return " ".join(re.sub(r"\b[A-Za-z]+\b", singular, text).split())


def _money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() and amount >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def _currency(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return _CURRENCY_ALIASES.get(text.lower()) or (text.upper() if re.fullmatch(r"[A-Za-z]{3}", text) else None)


@dataclass(frozen=True)
class CanonicalSearchQuery:
    original_query: str
    retrieval_query: str
    currency: str | None
    price_min: Decimal | None
    price_max: Decimal | None
    min_exclusive: bool = False
    max_exclusive: bool = False
    error: str | None = None

    def allows_price(self, amount: Any, currency: Any) -> bool:
        if self.error:
            return False
        if self.price_min is None and self.price_max is None:
            return True
        value = _money(amount)
        # No inferred currency or FX, and no comparison of unknown money.
        if value is None or value <= 0 or _currency(currency) != self.currency:
            return False
        if self.price_min is not None and (value < self.price_min or (self.min_exclusive and value == self.price_min)):
            return False
        if self.price_max is not None and (value > self.price_max or (self.max_exclusive and value == self.price_max)):
            return False
        return True

    def metadata(self) -> dict:
        return {
            "original_query": self.original_query,
            "retrieval_query": self.retrieval_query,
            "budget_currency": self.currency,
            "price_min": str(self.price_min) if self.price_min is not None else None,
            "price_max": str(self.price_max) if self.price_max is not None else None,
            "min_exclusive": self.min_exclusive,
            "max_exclusive": self.max_exclusive,
            "error": self.error,
        }

    def unverified_constraints(self) -> list[str]:
        # Category recall is deliberately broad. Preserve every residual goal
        # term as unverified rather than representing category hits as proof of
        # a formula, size, rating, brand, or unknown qualifier.
        words = re.findall(r"[\w%'-]+", self.retrieval_query)
        scaffolding = {"a", "an", "the", "and", "or", "with", "for", "of", "that", "which", "is", "are"}
        residual = [word for word in words if word.lower() not in scaffolding and not classify(word)]
        return [self.retrieval_query] if residual else []


def prepare_canonical_search_query(query: str, *, price_min=None, price_max=None, currency=None) -> CanonicalSearchQuery:
    original = str(query or "").strip()
    minimum, maximum = _money(price_min), _money(price_max)
    budget_currency = _currency(currency)
    explicit_currency = budget_currency
    lower_exclusive = upper_exclusive = False
    error = None
    if (price_min is not None and minimum is None) or (price_max is not None and maximum is None):
        error = "invalid_budget_amount"
    currencies = {budget_currency} if budget_currency else set()
    accepted_spans: list[tuple[int, int]] = []

    def extract(match: re.Match) -> str:
        nonlocal minimum, maximum, lower_exclusive, upper_exclusive, error
        # An unsupported negation must not reverse the user's bound by letting
        # an inner comparator match on its own. Recognized full money clauses
        # begin at their negation; any remaining negation in the same clause is
        # ambiguous (e.g. "do not want products costing more than ...").
        prefix = original[:match.start()]
        for start, end in reversed(accepted_spans):
            prefix = prefix[:start] + " " * (end - start) + prefix[end:]
        clause_prefix = re.split(r"[.;,!?\n]", prefix)[-1]
        if _UNHANDLED_NEGATION.search(clause_prefix):
            return match.group(0)
        if not (match.group("before") or match.group("after")) and _MEASUREMENT_SUFFIX.match(original[match.end("amount"):]):
            return match.group(0)
        if not (match.group("before") or match.group("after") or explicit_currency):
            # One later USD clause cannot give a preceding concentration,
            # rating or other untyped number a monetary unit retroactively.
            error = error or "budget_currency_required"
            return match.group(0)
        if not (match.group("before") or match.group("after")):
            following_word = re.match(r"\s+([a-z]+)\b", original[match.end("amount"):], re.I)
            if following_word and following_word.group(1).lower() not in {"and", "or", "but", "with", "for", "please", "per"}:
                # An explicit context currency permits "under 30", not a
                # partial interpretation of "under 30 AED" as USD30.
                error = error or "unsupported_budget_clause"
                return match.group(0)
        amount = Decimal(match.group("amount"))
        currencies.update(c for c in (_currency(match.group("before")), _currency(match.group("after"))) if c)
        op = match.group("operator").lower()
        lower_bound = op in {"over", "above", "more than", "at least", "min", "minimum", "no less than", "not less than"}
        exclusive = op in {"over", "above", "more than", "under", "below", "less than"}
        if match.group("negated_money"):
            if op not in {"over", "above", "more than", "under", "below", "less than", "at least", "at most"}:
                error = error or "unsupported_budget_clause"
                return match.group(0)
            lower_bound, exclusive = not lower_bound, not exclusive
        if lower_bound:
            if minimum is None or amount > minimum:
                minimum, lower_exclusive = amount, exclusive
            elif amount == minimum:
                lower_exclusive = lower_exclusive or exclusive
        else:
            if maximum is None or amount < maximum:
                maximum, upper_exclusive = amount, exclusive
            elif amount == maximum:
                upper_exclusive = upper_exclusive or exclusive
        accepted_spans.append(match.span())
        return " "

    cleaned = _BUDGET.sub(extract, original)
    unsupported_numeric_clause = any(not _non_money_numeric_clause(match, cleaned) for match in _UNPARSED_BUDGET.finditer(cleaned))
    unsupported_word_clause = any(not _non_money_numeric_clause(match, cleaned) for match in _UNPARSED_WORD_AMOUNT.finditer(cleaned))
    if _NONFINITE_MONEY.search(cleaned):
        error = "invalid_budget_amount"
    elif unsupported_numeric_clause or unsupported_word_clause or _UNPARSED_MONEY.search(cleaned) or _UNPARSED_TEXT_MONEY.search(cleaned):
        error = error or "unsupported_budget_clause"
    if len(currencies) > 1:
        error = "conflicting_budget_currencies"
    elif currencies:
        budget_currency = next(iter(currencies))
    if minimum is not None or maximum is not None:
        if not budget_currency:
            error = error or "budget_currency_required"
        if minimum is not None and maximum is not None and (minimum > maximum or (minimum == maximum and (lower_exclusive or upper_exclusive))):
            error = error or "empty_budget_range"
    return CanonicalSearchQuery(original, normalize_catalog_query(cleaned), budget_currency, minimum, maximum, lower_exclusive, upper_exclusive, error)
