from __future__ import annotations

import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.chdir(REPO_ROOT)

from routes.accounts_orders_api import _extract_order_pricing_minor  # noqa: E402


def test_extract_order_pricing_minor_prefers_order_fields() -> None:
    result = _extract_order_pricing_minor(
        {
            "subtotal": "1.69",
            "discount_total": "0.16",
            "shipping_fee": "8.00",
            "tax": "0.00",
            "total": "9.53",
            "metadata": {},
        },
        [{"subtotal": "1.69", "quantity": 1, "unit_price": "1.69"}],
    )

    assert result == {
        "subtotal_minor": 169,
        "discount_total_minor": 16,
        "shipping_fee_minor": 800,
        "tax_minor": 0,
        "total_amount_minor": 953,
    }


def test_extract_order_pricing_minor_falls_back_to_pricing_quote_when_total_missing() -> None:
    result = _extract_order_pricing_minor(
        {
            "metadata": {
                "pricing_quote": {
                    "pricing": {
                        "subtotal": "1.69",
                        "discount_total": "0.16",
                        "shipping_fee": "8.00",
                        "tax": "0.00",
                    }
                }
            }
        },
        [{"subtotal": "1.69", "quantity": 1, "unit_price": "1.69"}],
    )

    assert result == {
        "subtotal_minor": 169,
        "discount_total_minor": 16,
        "shipping_fee_minor": 800,
        "tax_minor": 0,
        "total_amount_minor": 953,
    }
