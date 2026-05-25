from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Tuple


NO_STRONG_IDENTIFIER = "no_strong_identifier"
MPN_CAPTURED_AS_BARCODE = "mpn_captured_as_barcode"

_DIGIT_IDENTIFIER_TYPES = {"gtin", "upc", "ean", "barcode"}
_VALID_DIGIT_LENGTHS = {8, 12, 13, 14}
_PLACEHOLDER_VALUES = {"n/a", "na", "none", "null", "unknown", "not available", "0", "-"}

_KEY_GROUPS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("gtin", ("gtin", "gtins", "gtin8", "gtin12", "gtin13", "gtin14", "gtin_8", "gtin_12", "gtin_13", "gtin_14")),
    ("upc", ("upc", "upcs", "upca", "upc_a", "upc12", "upc_12")),
    ("ean", ("ean", "eans", "ean8", "ean13", "ean_8", "ean_13")),
    ("barcode", ("barcode", "bar_code", "barcodes")),
    ("mpn", ("mpn", "manufacturer_part_number", "manufacturerPartNumber", "model_number", "modelNumber")),
)


@dataclass(frozen=True)
class StrongIdentifier:
    value: str
    kind: str


def _as_string(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text


def _normalize_digit_identifier(value: Any) -> str:
    digits = re.sub(r"\D+", "", _as_string(value))
    if len(digits) not in _VALID_DIGIT_LENGTHS:
        return ""
    if set(digits) == {"0"}:
        return ""
    return digits


def _normalize_mpn(value: Any) -> str:
    text = _as_string(value)
    if not text:
        return ""
    if text.lower() in _PLACEHOLDER_VALUES:
        return ""
    return text


def normalize_strong_identifier(value: Any, kind: str) -> Optional[StrongIdentifier]:
    normalized_kind = _as_string(kind).lower()
    if normalized_kind in _DIGIT_IDENTIFIER_TYPES:
        normalized = _normalize_digit_identifier(value)
    elif normalized_kind == "mpn":
        normalized = _normalize_mpn(value)
    else:
        normalized = ""
    if not normalized:
        return None
    return StrongIdentifier(value=normalized, kind=normalized_kind)


def _candidate_values(payload: Any, key_candidates: Iterable[str]) -> Iterable[Any]:
    if payload is None:
        return
    if isinstance(payload, Mapping):
        lowered = {str(key).lower(): value for key, value in payload.items()}
        for key in key_candidates:
            key_lower = key.lower()
            if key_lower in lowered:
                value = lowered[key_lower]
                if isinstance(value, (list, tuple, set)):
                    for item in value:
                        yield item
                else:
                    yield value
        for nested_key in (
            "raw",
            "snapshot",
            "variant",
            "product",
            "metadata",
            "platform_metadata",
            "catalog_meta",
            "catalogMeta",
            "commerce_facts_v1",
            "strong_identity",
            "raw_variant",
            "raw_product",
            "raw_wix_variant",
            "raw_wix_product",
        ):
            nested = lowered.get(nested_key.lower())
            if isinstance(nested, Mapping):
                yield from _candidate_values(nested, key_candidates)
    else:
        for key in key_candidates:
            if hasattr(payload, key):
                yield getattr(payload, key)


def extract_strong_identifier(*payloads: Any) -> Optional[StrongIdentifier]:
    for kind, keys in _KEY_GROUPS:
        for payload in payloads:
            for value in _candidate_values(payload, keys):
                identifier = normalize_strong_identifier(value, kind)
                if identifier is not None:
                    return identifier
    return None
