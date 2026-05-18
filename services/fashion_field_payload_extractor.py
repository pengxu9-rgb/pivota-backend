"""Read merchant-published fashion fields out of a StandardProduct's
platform_metadata.

Authoritative path for material / care / size_guide — when a merchant
sets the Shopify standard metafield (e.g. namespace='custom', key='material'),
or any equivalent surface that lands in platform_metadata, we want those
values flowing into catalog_products with source='merchant_payload'
confidence=1.0 (the highest trust tier).

This is the COMPANION to services/fashion_field_extractor.py (LLM v2):
  - payload extractor (this file) = merchant-authored, highest trust
  - LLM extractor                  = derived from descriptions, gated by
                                     0.6 confidence threshold downstream

If a merchant provides the metafield, the payload extractor wins; the
LLM is a fallback for legacy/unstructured rows.

Shopify metafield shape (per https://shopify.dev/api/admin-rest/2024-04/resources/metafield):
  {
    "namespace": "custom",
    "key": "material",
    "value": "100% organic cotton",
    "type": "single_line_text_field"
  }

We look at platform_metadata["metafields"] as a list of these dicts.
For size_guide we accept richer structures (json_string type returns a
JSON-serialized payload).

Note on Shopify adapter today: the existing shopify_real_adapter only
fetches /products.json which does NOT include metafields by default.
A follow-up needs to either (a) call /products/{id}/metafields.json per
product OR (b) move to GraphQL with metafields inline. THIS file handles
the consume side only — it's a no-op until the adapter is upgraded
OR a merchant-onboarding form writes metafields directly.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Known metafield (namespace, key) pairs that map to a target field.
# Tuples in priority order — earlier entries win when multiple match.
# Includes Shopify's "shopify" standard namespace (introduced 2024) plus
# common merchant conventions ('custom' / no-namespace).
_MATERIAL_KEYS: List[Tuple[Optional[str], str]] = [
    ("shopify", "material"),
    ("custom", "material"),
    ("custom", "fabric"),
    ("custom", "composition"),
    (None, "material"),
    (None, "fabric"),
]
_CARE_KEYS: List[Tuple[Optional[str], str]] = [
    ("shopify", "care_instructions"),
    ("custom", "care_instructions"),
    ("custom", "care"),
    ("custom", "washing_instructions"),
    (None, "care_instructions"),
    (None, "care"),
]
_SIZE_GUIDE_KEYS: List[Tuple[Optional[str], str]] = [
    ("shopify", "size_chart"),
    ("custom", "size_guide"),
    ("custom", "size_chart"),
    ("custom", "sizing"),
    (None, "size_guide"),
    (None, "size_chart"),
]


def _metafields_list(platform_metadata: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(platform_metadata, dict):
        return []
    raw = platform_metadata.get("metafields")
    if isinstance(raw, list):
        return [m for m in raw if isinstance(m, dict)]
    return []


def _toplevel_dict(platform_metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Legacy/admin-injection shape: bare top-level keys on platform_metadata
    like {"material": "100% cotton"}. Lower-trust than metafields but
    still merchant-authored if present."""
    return platform_metadata if isinstance(platform_metadata, dict) else {}


def _find_metafield_value(
    metafields: List[Dict[str, Any]],
    candidates: List[Tuple[Optional[str], str]],
) -> Optional[Any]:
    for namespace, key in candidates:
        for mf in metafields:
            mf_ns = mf.get("namespace")
            mf_key = mf.get("key")
            if mf_key != key:
                continue
            if namespace is not None and mf_ns != namespace:
                continue
            value = mf.get("value")
            if value is None or value == "":
                continue
            return value
    return None


def _find_toplevel_value(
    metadata: Dict[str, Any],
    candidates: List[Tuple[Optional[str], str]],
) -> Optional[Any]:
    # Bare top-level keys ignore namespace.
    for _ns, key in candidates:
        value = metadata.get(key)
        if value is None or value == "":
            continue
        return value
    return None


def extract_material_from_payload(platform_metadata: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return the merchant-published material string (or None)."""
    metafields = _metafields_list(platform_metadata)
    raw = _find_metafield_value(metafields, _MATERIAL_KEYS)
    if raw is None:
        raw = _find_toplevel_value(_toplevel_dict(platform_metadata), _MATERIAL_KEYS)
    if isinstance(raw, str):
        cleaned = raw.strip()
        return cleaned or None
    return None


def extract_care_from_payload(platform_metadata: Optional[Dict[str, Any]]) -> Optional[str]:
    metafields = _metafields_list(platform_metadata)
    raw = _find_metafield_value(metafields, _CARE_KEYS)
    if raw is None:
        raw = _find_toplevel_value(_toplevel_dict(platform_metadata), _CARE_KEYS)
    if isinstance(raw, str):
        cleaned = raw.strip()
        return cleaned or None
    return None


def extract_size_guide_from_payload(platform_metadata: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Returns the size_guide as a dict (jsonb-ready). Accepts either:
       - a json_string metafield (Shopify type='json_string') containing
         a serialized {columns, rows, ...} object
       - a plain string (wrapped into {raw: <string>} for the catalog column)
       - a dict (passed through)"""
    metafields = _metafields_list(platform_metadata)
    raw = _find_metafield_value(metafields, _SIZE_GUIDE_KEYS)
    if raw is None:
        raw = _find_toplevel_value(_toplevel_dict(platform_metadata), _SIZE_GUIDE_KEYS)
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None
        # Try JSON parse first (shopify json_string type)
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, json.JSONDecodeError):
            pass
        return {"raw": stripped}
    return None
