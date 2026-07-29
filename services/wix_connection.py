"""Shared Wix connection validation and credential helpers."""

from __future__ import annotations

import json
import re
from typing import Any, Dict
from urllib.parse import parse_qs, unquote, urlparse

import httpx

WIX_PRODUCTS_QUERY_URL = "https://www.wixapis.com/stores-reader/v1/products/query"

# Wix keeps a product's category in COLLECTIONS, which are a separate resource
# from the product itself: `products/query` returns `collectionIds`, and the
# human-readable name lives only here. Same reader API, same credentials, so
# this costs one extra call per sync (not per product).
WIX_COLLECTIONS_QUERY_URL = "https://www.wixapis.com/stores-reader/v1/collections/query"

# Every Wix store has an implicit "All Products" collection that EVERY product
# belongs to. Mapping it into `product_type` would hand every row in the store
# the identical, meaningless category — which scores exactly as well as a real
# one while telling a buyer, a crawler, and the index nothing. That is the
# valid-but-wrong failure this codebase keeps re-learning, so it is excluded by
# id at the only place that reads collections.
WIX_ALL_PRODUCTS_COLLECTION_ID = "00000000-000000-000000-000000000001"
_WIX_SITE_ID_QUERY_KEYS = (
    "siteId",
    "site_id",
    "metaSiteId",
    "meta_site_id",
    "msid",
    "siteGuid",
)
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class WixConnectionValidationError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def coerce_wix_credential_blob(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                return dict(parsed) if isinstance(parsed, dict) else {}
            except Exception:
                return {}
    return {}


def _validate_wix_site_id(site_id: Any) -> str:
    site_id = str(site_id or "").strip()
    if not site_id:
        raise WixConnectionValidationError("WIX_SITE_ID_REQUIRED", "Wix Site ID is required")

    if any(ch.isspace() for ch in site_id):
        raise WixConnectionValidationError(
            "WIX_SITE_ID_INVALID",
            "Wix Site ID cannot contain whitespace.",
        )

    return site_id


def _extract_site_id_from_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    query_sources = [parsed.query]
    if parsed.fragment:
        fragment = parsed.fragment[1:] if parsed.fragment.startswith("?") else parsed.fragment
        if "?" in fragment:
            fragment = fragment.split("?", 1)[1]
        query_sources.append(fragment)

    for raw_query in query_sources:
        for key, values in parse_qs(raw_query, keep_blank_values=False).items():
            if key in _WIX_SITE_ID_QUERY_KEYS and values:
                return _validate_wix_site_id(values[0])

    segments = [unquote(segment).strip() for segment in parsed.path.split("/") if segment.strip()]
    lower_segments = [segment.lower() for segment in segments]

    for marker in ("dashboard", "site", "sites"):
        if marker in lower_segments:
            marker_index = lower_segments.index(marker)
            if marker_index + 1 < len(segments):
                return _validate_wix_site_id(segments[marker_index + 1])

    for segment in segments:
        if _UUID_RE.match(segment):
            return _validate_wix_site_id(segment)

    return ""


def normalize_wix_site_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise WixConnectionValidationError("WIX_SITE_ID_REQUIRED", "Wix Site ID is required")

    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        site_id = _extract_site_id_from_url(raw)
        if site_id:
            return site_id
        raise WixConnectionValidationError(
            "WIX_SITE_ID_NOT_FOUND_IN_URL",
            "Wix Site ID was not found in that URL. Paste the Wix URL that contains the Site ID, or paste the Site ID directly.",
        )

    return _validate_wix_site_id(raw)


def normalize_wix_api_key(value: Any) -> str:
    if isinstance(value, str):
        raw = value.strip()
        blob = coerce_wix_credential_blob(raw)
        if blob:
            return str(
                blob.get("access_token")
                or blob.get("wix_access_token")
                or blob.get("token")
                or blob.get("api_key")
                or ""
            ).strip()
        return raw
    blob = coerce_wix_credential_blob(value)
    return str(
        blob.get("access_token")
        or blob.get("wix_access_token")
        or blob.get("token")
        or blob.get("api_key")
        or ""
    ).strip()


def wix_authorization_header(api_key: str) -> str:
    token = str(api_key or "").strip()
    return token


def build_wix_catalog_headers(api_key: str, site_id: str) -> Dict[str, str]:
    return {
        "Authorization": wix_authorization_header(api_key),
        "wix-site-id": site_id,
        "Content-Type": "application/json",
    }


def build_wix_api_key_headers(api_key: str, site_id: str) -> Dict[str, str]:
    """Headers for Wix REST API-key calls.

    Wix API keys are sent as the raw Authorization value. They are not OAuth
    bearer tokens, so callers must not prepend "Bearer " to IST/JWS keys.
    """
    return {
        **build_wix_catalog_headers(api_key, site_id),
        "Accept": "application/json",
    }


def extract_wix_site_id(domain: Any, api_key: Any = None) -> str:
    blob = coerce_wix_credential_blob(api_key)
    site_id = str(blob.get("site_id") or blob.get("wix_site_id") or domain or "").strip()
    return normalize_wix_site_id(site_id)


async def validate_wix_catalog_access(site_id: Any, api_key: Any) -> Dict[str, Any]:
    normalized_site_id = normalize_wix_site_id(site_id)
    normalized_api_key = normalize_wix_api_key(api_key)
    if not normalized_api_key:
        raise WixConnectionValidationError("WIX_API_KEY_REQUIRED", "Wix API Key is required")

    payload = {"query": {"paging": {"limit": 1}}}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                WIX_PRODUCTS_QUERY_URL,
                json=payload,
                headers=build_wix_catalog_headers(normalized_api_key, normalized_site_id),
            )
    except httpx.RequestError as exc:
        raise WixConnectionValidationError(
            "WIX_API_UNREACHABLE",
            f"Could not reach Wix API while validating credentials: {exc}",
            status_code=502,
        ) from exc

    if response.status_code == 200:
        return {
            "site_id": normalized_site_id,
            "api_key": normalized_api_key,
            "status_code": response.status_code,
        }
    if response.status_code == 401:
        raise WixConnectionValidationError(
            "WIX_AUTH_FAILED",
            "Invalid Wix API Key or access token.",
        )
    if response.status_code == 403:
        raise WixConnectionValidationError(
            "WIX_PERMISSION_DENIED",
            "Wix credentials do not have permission to access this site.",
        )

    raise WixConnectionValidationError(
        "WIX_VALIDATION_FAILED",
        f"Wix API validation failed with status {response.status_code}: {response.text[:200]}",
    )
