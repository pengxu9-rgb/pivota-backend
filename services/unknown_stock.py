"""Unknown stock stays unknown (flag OFFERS_UNKNOWN_STOCK_STAYS_UNKNOWN_ENABLED, default OFF).

One home for the flag read and the tri-state stock verdict, so every producer reads them the same way:
routes/agent_shop_gateway (offers.resolve catalog arm, the external-seed projection, prefetched seed
candidates) and services/beauty_external_ranking (the external-seed FILTER product the strict-ingredient
lane's in-stock-only check reads). It lives here, not in the gateway, because the gateway imports
beauty_external_ranking: the ranking module cannot import the gateway back.

Callers read the flag and change NOTHING when it is off.
"""

from __future__ import annotations

import os
from typing import Any, Optional, Tuple

from utils.availability_vocabulary import IN_STOCK, OUT_OF_STOCK, normalize_availability

UNKNOWN_STOCK_STAYS_UNKNOWN_FLAG = "OFFERS_UNKNOWN_STOCK_STAYS_UNKNOWN_ENABLED"


def unknown_stock_stays_unknown() -> bool:
    """Default OFF, read per call. On: an offer or seed whose availability is missing or not understood says
    in_stock None (unknown), never True, and carries no invented inventory count. Off: today's behaviour.

    Parsing: case-insensitive "true" after trimming whitespace ("TRUE", " true " are on); every other value,
    "1", "yes" and "on" included, is OFF. The gateway (PIVOTA-Agent) reads its copy of this flag the same way."""
    return os.getenv(UNKNOWN_STOCK_STAYS_UNKNOWN_FLAG, "").strip().lower() == "true"


def says_out_of_stock(value: Any) -> bool:
    """The catalog arm's vocabulary (services.offer_buyability.OFFER_UNAVAILABLE_AVAILABILITIES) OR the seed
    lane's normalizer (utils.availability_vocabulary: "sold out", "out of stock", schema.org OutOfStock...)."""
    from services.offer_buyability import availability_is_known_unavailable

    return availability_is_known_unavailable(value) or normalize_availability(value) == OUT_OF_STOCK


def stated_in_stock(availability: Any) -> Optional[bool]:
    """True only on an affirmative in-stock statement, False on ANY out-of-stock statement (the catalog arm's
    vocabulary or the seed lane's: "sold out" is sold out in every lane), None when it is missing or says neither."""
    if says_out_of_stock(availability):
        return False
    return True if normalize_availability(availability) == IN_STOCK else None


# The spellings the catalog arm's SQL ranks as "cannot sell" when the flag is on. The SQL compares
# lower(btrim(availability)) to a list, so it cannot run the normalizer; this enumerates the separator,
# case and schema.org-IRI forms of every out-of-stock token, KEEPS only those the normalizer itself calls
# out of stock (so the list can never contradict the in_stock flag), and adds the off-flag list
# (OFFER_UNAVAILABLE_AVAILABILITIES) so it is a superset of it. Free prose the normalizer catches by
# phrase ("Temporarily out of stock - notify me") is not enumerable: such a row is still flagged
# in_stock False, it just sorts with sellable rows before the LIMIT (ordering only; no live row is spelled
# so — prod catalog_offers carry only in_stock / out_of_stock / unknown, 2026-09-18).
_OUT_OF_STOCK_WORD_FORMS = (
    ("out", "of", "stock"),
    ("sold", "out"),
    ("unavailable",),
    ("not", "available"),
    ("not", "in", "stock"),
    ("no", "longer", "available"),
    ("no", "longer", "in", "stock"),
    ("temporarily", "out", "of", "stock"),
    ("oos",),
    ("reserved",),
    ("discontinued",),
)
_SCHEMA_ORG_OUT_OF_STOCK = ("OutOfStock", "SoldOut", "Discontinued", "Reserved")
_SCHEMA_ORG_HOSTS = ("https://schema.org/", "http://schema.org/", "https://www.schema.org/", "http://www.schema.org/")


def _sql_out_of_stock_spellings() -> Tuple[str, ...]:
    from services.offer_buyability import OFFER_UNAVAILABLE_AVAILABILITIES

    candidates = set(OFFER_UNAVAILABLE_AVAILABILITIES)
    for words in _OUT_OF_STOCK_WORD_FORMS:
        for sep in (" ", "_", "-", ""):
            candidates.add(sep.join(words))
    for host in _SCHEMA_ORG_HOSTS:
        for term in _SCHEMA_ORG_OUT_OF_STOCK:
            candidates.add((host + term).lower())
    return tuple(sorted(c for c in candidates if c in OFFER_UNAVAILABLE_AVAILABILITIES or stated_in_stock(c) is False))


SQL_OUT_OF_STOCK_SPELLINGS: Tuple[str, ...] = _sql_out_of_stock_spellings()
