"""Currency-aware minor-unit conversion.

One shared implementation so the charge path (PSP amounts) and the display path
(session totals) can never round differently. Zero-decimal currencies follow
Stripe's documented list (https://stripe.com/docs/currencies#zero-decimal): for
those, the minor unit IS the major unit (JPY 300 charges as amount=300, not
30000 — a 100x overcharge if converted naively).
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

# Stripe's documented zero-decimal currencies.
ZERO_DECIMAL_CURRENCIES = frozenset(
    {
        "BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA",
        "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
    }
)


def to_minor_units(amount: Any, currency: Any) -> int:
    """Convert a major-unit amount to PSP minor units for `currency`.

    Decimal/ROUND_HALF_UP throughout — never binary-float multiplication. A
    None/invalid amount resolves to 0 (callers guard for missing totals before
    charging).
    """
    try:
        value = Decimal(str(amount if amount is not None else "0"))
    except Exception:
        return 0
    code = str(currency or "").strip().upper()
    factor = 1 if code in ZERO_DECIMAL_CURRENCIES else 100
    try:
        return int((value * factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except Exception:
        return 0
