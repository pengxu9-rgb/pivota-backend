from __future__ import annotations

import pytest

from services.strong_identifier import extract_strong_identifier, normalize_strong_identifier


@pytest.mark.parametrize(
    ("payload", "expected_value", "expected_kind"),
    [
        ({"gtin": "1234567890123"}, "1234567890123", "gtin"),
        ({"upc": "123456789012"}, "123456789012", "upc"),
        ({"gtin": "12345678"}, "12345678", "gtin"),
        ({"barcode": "0-12345-67890-5"}, "012345678905", "barcode"),
    ],
)
def test_extract_strong_identifier_normalizes_supported_digit_identifiers(
    payload: dict,
    expected_value: str,
    expected_kind: str,
) -> None:
    identifier = extract_strong_identifier(payload)

    assert identifier is not None
    assert identifier.value == expected_value
    assert identifier.kind == expected_kind


def test_extract_strong_identifier_uses_priority_order_across_payloads() -> None:
    identifier = extract_strong_identifier(
        {"barcode": "9999999999999", "mpn": "MPN-1"},
        {"upc": "123456789012"},
        {"gtin": "1234567890123"},
    )

    assert identifier is not None
    assert identifier.value == "1234567890123"
    assert identifier.kind == "gtin"


def test_extract_strong_identifier_reads_known_raw_payload_wrappers() -> None:
    identifier = extract_strong_identifier(
        {"platform_metadata": {"raw_wix_variant": {"gtin12": "123456789012"}}}
    )

    assert identifier is not None
    assert identifier.value == "123456789012"
    assert identifier.kind == "gtin"


@pytest.mark.parametrize("value", ["", None, "N/A", "0", "12345", "0000000000000"])
def test_normalize_strong_identifier_rejects_missing_and_garbage_digit_values(value) -> None:
    assert normalize_strong_identifier(value, "gtin") is None


def test_extract_strong_identifier_captures_mpn_as_last_fallback() -> None:
    identifier = extract_strong_identifier({"barcode": "N/A", "mpn": " MPN-ABC-123 "})

    assert identifier is not None
    assert identifier.value == "MPN-ABC-123"
    assert identifier.kind == "mpn"
