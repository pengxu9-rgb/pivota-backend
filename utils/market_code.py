"""THE ONE NORMALISER for a buyer-market code on any path that keys a purchase decision.

A market that is unknown is UNKNOWN. It is never defaulted to "US", never truncated to two
letters, never guessed. This module is a leaf (stdlib only) so the fact store
(`db.merchant_purchasability`), the sweep, the ops route, the Reap rail, the Tier B allowlist and
the warm-handoff sink can all bind the SAME function object without importing each other — which
is what `tests/test_purchase_gate_market_not_defaulted.py` asserts by identity.

It used to live in `services.outbound_links_service` (which re-exports it, unchanged); the fact
store carried a second, different rule (`str(v or "").strip().upper()[:2]`, which read "USA" as
"US") until the two were made one.

NOT to be confused with `services.outbound_links_service.normalize_market`, which answers a
different question — "what market do we SERVE this click as" — and defaults to "US" on purpose.
A served market is catalog/serving state; it must never be read back as the buyer's market.
"""

from __future__ import annotations

import re
from typing import Any, Optional

__all__ = ["iso2_market", "MARKET_UNKNOWN"]

#: The reason literal a consumer reports when it was asked about a market it cannot key on
#: (absent, blank, or not ISO-2). Never a buyer identifier.
MARKET_UNKNOWN = "market_unknown"

_ISO2_MARKET_RE = re.compile(r"[A-Z]{2}")


def iso2_market(raw: Any) -> Optional[str]:
    """ISO-3166 alpha-2, upper-cased — or ``None``. The ONE normaliser for this vocabulary.

    ``"us"`` / ``"  sg  "`` -> ``"US"`` / ``"SG"``. ``"USA"``, ``""``, ``"U1"``, ``None``, a
    non-string -> ``None``: never truncated, never defaulted. Deliberately NOT
    `services.outbound_links_service.normalize_market`, which serves ``"USA"`` as ``"USA"`` and
    ``None`` as ``"US"`` — that function answers "what do we serve this click as", this one
    answers "is this a market code we can key a fact on".
    """
    if not isinstance(raw, str):
        return None
    candidate = raw.strip().upper()
    return candidate if _ISO2_MARKET_RE.fullmatch(candidate) else None
