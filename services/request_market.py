"""THE MARKET OF A REQUEST THAT NAMES NONE (decision D-10, 2026-11-08).

A request that names no market is answered AS US, and the answer says so: the answer carries
``market_assumed`` (``market_assumed_label()``). A request that names a market is passed through
exactly as it arrived -- a valid one keeps its answer byte for byte, and an invalid one is refused or
ignored exactly as before.

"Names no market" means absent, ``None`` or a blank string, nothing else. A market is never inferred
from a currency, a language or a buyer address.

The gateway (PIVOTA-Agent ``src/services/requestMarket.js``) holds the same rule, the same member and
the same text for the MCP, UCP and public doors; this module is the backend's copy for the doors only
the backend serves (``POST /agent/v2/products/search``).
"""

from __future__ import annotations

from typing import Any, Dict

ASSUMED_MARKET = "US"

MARKET_ASSUMED_MESSAGE = (
    "No market was named in this request, so it was answered for a US buyer: prices, currency, stock and "
    "checkout links are for the US. Send `market` (ISO 3166-1 alpha-2) to choose another."
)


def market_is_unnamed(raw: Any) -> bool:
    """True when ``raw`` names no market at all (absent, None, or a blank string)."""
    return raw is None or (isinstance(raw, str) and raw.strip() == "")


def market_assumed_label() -> Dict[str, str]:
    """The member an answer carries when its market was assumed. A fresh dict per call."""
    return {"code": ASSUMED_MARKET, "message": MARKET_ASSUMED_MESSAGE}
