"""Currency-aware minor-unit conversion.

One shared implementation so the charge path (PSP amounts) and the display path
(session totals) can never round differently. Zero-decimal currencies follow
Stripe's documented list (https://stripe.com/docs/currencies#zero-decimal): for
those, the minor unit IS the major unit (JPY 300 charges as amount=300, not
30000 — a 100x overcharge if converted naively).
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Union

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


def from_minor_units(amount_minor: Any, currency: Any) -> Union[int, float]:
    """Inverse of `to_minor_units`: PSP minor units back to major units.

    The exact mirror matters wherever a minor-unit charge amount is written into
    a MAJOR-unit column (e.g. the `payments.amount` record): a naive
    `amount_cents / 100.0` records a JPY 300 charge as 3.0 — a 100x understated
    payment row for every zero-decimal currency.

    Zero-decimal currencies return an int (the minor unit IS the major unit);
    everything else returns a float with exactly 2 decimals via Decimal
    arithmetic (never binary-float division).
    """
    try:
        value = Decimal(str(amount_minor if amount_minor is not None else "0"))
    except Exception:
        return 0
    code = str(currency or "").strip().upper()
    if code in ZERO_DECIMAL_CURRENCIES:
        try:
            return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        except Exception:
            return 0
    try:
        return float((value / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except Exception:
        return 0.0
