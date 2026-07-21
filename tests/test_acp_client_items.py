"""P-T2.3.4 — pivota_acp_client._acp_items carries product_id + variant_id.

The pivota-acp real-capture path needs BOTH product_id and variant_id (a Pivota
quote requires them); the single ACP `id` isn't enough. They must ride the ACP
item so the session persists them to /complete.
"""

from __future__ import annotations

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


# --- P-T2.3.4 fix: fulfillment_address → ACP Address contract (line_one) ---
from services.pivota_acp_client import _acp_address  # noqa: E402


def test_acp_address_maps_address_line1_to_line_one():
    # pivota-acp's Address REQUIRES line_one; the Pivota checkout shape uses
    # address_line1, so it must be mapped or pivota-acp 422s on the session.
    out = _acp_address({
        "name": "ACP Canary", "address_line1": "1 Test St", "address_line2": "Apt 2",
        "city": "San Francisco", "state": "CA", "country": "US", "postal_code": "94102",
    })
    assert out == {
        "line_one": "1 Test St", "line_two": "Apt 2", "name": "ACP Canary",
        "city": "San Francisco", "state": "CA", "country": "US", "postal_code": "94102",
    }


def test_acp_address_passthrough_when_already_acp_shaped():
    out = _acp_address({"line_one": "5 Main", "city": "NYC", "country": "US"})
    assert out["line_one"] == "5 Main" and "line_two" not in out


def test_acp_address_none_without_street_line():
    assert _acp_address({"city": "NYC"}) is None
    assert _acp_address(None) is None


# --- P-T2.3.4 fix: buyer → ACP Buyer contract (first_name/last_name required) ---
from services.pivota_acp_client import _acp_buyer  # noqa: E402


def test_acp_buyer_omitted_when_only_email():
    # pivota-acp Buyer REQUIRES first_name+last_name; an email-only buyer must be
    # omitted (session buyer is optional) rather than 422 the whole session.
    assert _acp_buyer({"email": "a@b.com"}) is None
    assert _acp_buyer(None) is None


def test_acp_buyer_maps_first_last_and_email():
    out = _acp_buyer({"first_name": "Ada", "last_name": "Lovelace", "email": "ada@x.com"})
    assert out == {"first_name": "Ada", "last_name": "Lovelace", "email": "ada@x.com"}


def test_acp_buyer_splits_single_name():
    out = _acp_buyer({"name": "Grace Hopper"})
    assert out == {"first_name": "Grace", "last_name": "Hopper"}
