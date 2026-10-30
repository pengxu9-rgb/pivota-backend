"""Conservative query preparation for the canonical shopping catalog.

Retrieval text and price predicates have different jobs. Keep the caller's
original text, remove only budget clauses we can represent without guessing,
and normalize inflected category nouns using the existing taxonomy vocabulary.
No product, brand, or result-specific aliases belong here.

A money clause we cannot represent never empties the search. It stays in the
retrieval text and the plan reports it as unverified, so callers can say the
budget was not enforced rather than showing a shopper zero products for
"cleansers between $10 and $20" or "eye cream for over 40s". Only a caller's
own invalid API bounds or a contradictory range return an empty result.
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
# "between $10 and $20", "$10-$20", "from 10 to 20 dollars". Inclusive at both
# ends. A bare "10-20" with neither a currency nor "between"/"from" is a size,
# shade or SPF range as often as a price, so it is left alone.
_RANGE = re.compile(
    r"(?:\b(?P<lead>between|from)\s+)?"
    rf"(?:(?P<b1>{_CURRENCY})\s*)?(?<![A-Za-z\d.,])(?P<low>\d+(?:\.\d+)?)(?:\s*(?P<a1>{_NAMED_CURRENCY}))?"
    r"\s*(?P<sep>-|–|—|\bto\b|\band\b)\s*"
    rf"(?:(?P<b2>{_CURRENCY})\s*)?(?P<high>\d+(?:\.\d+)?)(?:\s*(?P<a2>{_NAMED_CURRENCY}))?"
    r"(?!\w|[.,]\d)", re.IGNORECASE,
)
# A numeric money clause we could not represent is reported as unverified.
# Bound the scan to a single short clause; don't mistake an ordinary model
# number ("k18", "under30cm") or a physical measurement for a currency amount.
_UNPARSED_BUDGET = re.compile(
    rf"\b(?:{_OPERATOR}|price|priced|cost|spend|spending|afford|cheaper|dearer|expensive)"
    r"(?=\s|[-+\d.$€£:<>=])[^\n;!?]{0,64}?(?<![A-Za-z\d.,])[-+]?(?:\d+(?:[.,]\d+)*|\.\d+)", re.I,
)
# "under NOK30", "budget AED 50": a code we cannot price. The lookbehind above
# keeps "k18" a model number, so name this shape explicitly. Typed attributes
# ("under SPF 30") are excluded by _MEASUREMENT_PREFIX at the call site.
_UNKNOWN_CODE_MONEY = re.compile(
    rf"\b(?:{_OPERATOR}|price|priced|cost|costing|spend|spending)\s+(?:of\s+)?[A-Za-z]{{3}}\s*\d+(?:\.\d+)?(?![\w.])", re.I,
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
    # A money clause stayed in the text because we could not represent it.
    unparsed_budget: bool = False

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
            "unparsed_budget_clause": self.unparsed_budget,
        }

    def unverified_constraints(self) -> list[str]:
        # The shopper's own words name the budget we did not enforce.
        if self.unparsed_budget:
            return [self.original_query]
        # Category recall is deliberately broad. Preserve every residual goal
        # term as unverified rather than representing category hits as proof of
        # a formula, size, rating, brand, or unknown qualifier.
        words = re.findall(r"[\w%'-]+", self.retrieval_query)
        scaffolding = {"a", "an", "the", "and", "or", "with", "for", "of", "that", "which", "is", "are"}
        residual = [word for word in words if word.lower() not in scaffolding and not classify(word)]
        return [self.retrieval_query] if residual else []


def prepare_canonical_search_query(query: str, *, price_min=None, price_max=None, currency=None, market_currency=None) -> CanonicalSearchQuery:
    """Split a shopping query into retrieval text and enforceable money bounds.

    `currency` is the caller's explicit budget currency. `market_currency` is
    the serving market's own currency (US -> USD); it prices an untyped bound
    such as "under 30" the way the legacy parser did, but never overrides a
    currency the shopper actually wrote.
    """
    original = str(query or "").strip()
    minimum, maximum = _money(price_min), _money(price_max)
    explicit_currency = _currency(currency)
    context_currency = explicit_currency or _currency(market_currency)
    lower_exclusive = upper_exclusive = False
    error = None
    unparsed = False
    untyped_bound = False
    if (price_min is not None and minimum is None) or (price_max is not None and maximum is None):
        error = "invalid_budget_amount"
    currencies = {explicit_currency} if explicit_currency else set()
    accepted_spans: list[tuple[int, int]] = []

    def negated_before(text: str, start: int) -> bool:
        # An unsupported negation must not reverse the user's bound by letting
        # an inner comparator match on its own. Recognized full money clauses
        # begin at their negation; any remaining negation in the same clause is
        # ambiguous (e.g. "do not want products costing more than ...").
        prefix = text[:start]
        for span_start, span_end in reversed(accepted_spans):
            prefix = prefix[:span_start] + " " * (span_end - span_start) + prefix[span_end:]
        return bool(_UNHANDLED_NEGATION.search(re.split(r"[.;,!?\n]", prefix)[-1]))

    def tighten(amount: Decimal, lower_bound: bool, exclusive: bool) -> None:
        nonlocal minimum, maximum, lower_exclusive, upper_exclusive
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

    def extract_range(match: re.Match) -> str:
        nonlocal unparsed, untyped_bound
        markers = [match.group(name) for name in ("b1", "a1", "b2", "a2") if match.group(name)]
        sep, lead = match.group("sep").lower(), (match.group("lead") or "").lower()
        if (sep == "and" and lead != "between") or (not markers and not lead):
            return match.group(0)
        if _MEASUREMENT_SUFFIX.match(stage[match.end("high"):]) or (
            not match.group("a1") and _MEASUREMENT_SUFFIX.match(stage[match.end("low"):])
        ):
            return match.group(0)
        if negated_before(stage, match.start()):
            unparsed = True
            return match.group(0)
        if not markers:
            if not context_currency:
                unparsed = True
                return match.group(0)
            untyped_bound = True
        currencies.update(c for c in (_currency(m) for m in markers) if c)
        low, high = Decimal(match.group("low")), Decimal(match.group("high"))
        if low > high:
            low, high = high, low
        tighten(low, True, False)
        tighten(high, False, False)
        accepted_spans.append(match.span())
        return " " * len(match.group(0))

    def extract(match: re.Match) -> str:
        nonlocal unparsed, untyped_bound
        if negated_before(stage, match.start()):
            unparsed = True
            return match.group(0)
        typed = bool(match.group("before") or match.group("after"))
        if not typed and _MEASUREMENT_SUFFIX.match(stage[match.end("amount"):]):
            return match.group(0)
        op = match.group("operator").lower()
        lower_bound = op in {"over", "above", "more than", "at least", "min", "minimum", "no less than", "not less than"}
        exclusive = op in {"over", "above", "more than", "under", "below", "less than"}
        if match.group("negated_money"):
            if op not in {"over", "above", "more than", "under", "below", "less than", "at least", "at most"}:
                unparsed = True
                return match.group(0)
            lower_bound, exclusive = not lower_bound, not exclusive
        if not typed:
            # Without a written currency, "over 50" / "at least 40" is an age,
            # audience or strength ("women over 50", "skin over 40") as often
            # as a price. Only a written currency makes a floor a price.
            if lower_bound and not match.group("negated_money"):
                return match.group(0)
            following_word = re.match(r"\s+([a-z]+)\b", stage[match.end("amount"):], re.I)
            if not context_currency or (
                following_word and following_word.group(1).lower() not in {"and", "or", "but", "with", "for", "please", "per"}
            ):
                # "under 30 AED" must not become USD30; "under 18 kids" is not money.
                unparsed = True
                return match.group(0)
            untyped_bound = True
        currencies.update(c for c in (_currency(match.group("before")), _currency(match.group("after"))) if c)
        tighten(Decimal(match.group("amount")), lower_bound, exclusive)
        accepted_spans.append(match.span())
        return " " * len(match.group(0))

    stage = original
    stage = _RANGE.sub(extract_range, stage)
    stage = _BUDGET.sub(extract, stage)
    cleaned = stage
    if (
        _NONFINITE_MONEY.search(cleaned)
        or any(not _non_money_numeric_clause(match, cleaned) for match in _UNPARSED_BUDGET.finditer(cleaned))
        or any(not _non_money_numeric_clause(match, cleaned) for match in _UNPARSED_WORD_AMOUNT.finditer(cleaned))
        or _UNPARSED_MONEY.search(cleaned)
        or _UNPARSED_TEXT_MONEY.search(cleaned)
        or any(not _MEASUREMENT_PREFIX.search(match.group(0)) for match in _UNKNOWN_CODE_MONEY.finditer(cleaned))
    ):
        unparsed = True
    budget_currency = None
    if len(currencies) > 1 or (untyped_bound and currencies and context_currency not in currencies):
        # Two written currencies, or an untyped "under 30" beside "over 10 euros":
        # there is no single currency to compare in, so enforce nothing.
        budget_currency = None
    elif currencies:
        budget_currency = next(iter(currencies))
    elif minimum is not None or maximum is not None:
        budget_currency = context_currency
    if (minimum is not None or maximum is not None) and not budget_currency:
        if error is None:
            unparsed = True
        minimum = maximum = None
        lower_exclusive = upper_exclusive = False
    if minimum is not None and maximum is not None and (minimum > maximum or (minimum == maximum and (lower_exclusive or upper_exclusive))):
        error = error or "empty_budget_range"
    return CanonicalSearchQuery(
        original, normalize_catalog_query(cleaned), budget_currency, minimum, maximum,
        lower_exclusive, upper_exclusive, error, unparsed,
    )
