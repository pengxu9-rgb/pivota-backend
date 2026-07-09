"""P-T2.3.4 — pivota_acp_client._acp_items carries product_id + variant_id.

The pivota-acp real-capture path needs BOTH product_id and variant_id (a Pivota
quote requires them); the single ACP `id` isn't enough. They must ride the ACP
item so the session persists them to /complete.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")

from services.pivota_acp_client import _acp_items  # noqa: E402


def test_acp_items_carry_product_and_variant_ids():
    out = _acp_items([
        {"product_id": "10064558129449", "variant_id": "53012602618153", "sku": "WIN-1", "quantity": 2},
    ])
    assert out == [{
        "id": "WIN-1",  # id still prefers sku
        "quantity": 2,
        "product_id": "10064558129449",
        "variant_id": "53012602618153",
    }]


def test_acp_items_omit_absent_ids():
    # No product/variant → only id + quantity (unchanged behavior for those callers).
    out = _acp_items([{"sku": "S1", "quantity": 1}])
    assert out == [{"id": "S1", "quantity": 1}]
    assert "product_id" not in out[0] and "variant_id" not in out[0]


def test_acp_items_id_fallback_when_no_sku():
    out = _acp_items([{"product_id": "P1", "variant_id": "V1", "quantity": 1}])
    assert out[0]["id"] == "V1"  # falls to variant when sku absent
    assert out[0]["product_id"] == "P1" and out[0]["variant_id"] == "V1"
