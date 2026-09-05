"""Shared Wix connection validation and credential helpers."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List
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


# A Wix app instance id is a GUID. This class carries NEITHER SQL LIKE
# wildcard: `%` and `_` are both excluded, so a shape-checked instance id can
# be interpolated into a LIKE needle without escaping. `-` is kept because a
# GUID needs it. A value that cannot be an instance id is refused before it
# ever reaches the database.
WIX_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{7,63}$")


def is_wix_instance_id(value: Any) -> bool:
    """True when `value` can be a Wix app instance id (and carries no wildcard)."""
    return bool(WIX_INSTANCE_ID_RE.match(str(value or "").strip()))


def stored_wix_instance_id(api_key: Any) -> str:
    """The instance id inside a stored credential, or ``""`` for a bare key."""
    blob = coerce_wix_credential_blob(api_key)
    return str(blob.get("instance_id") or blob.get("wix_instance_id") or "").strip()


async def find_wix_stores_by_instance_id(database: Any, instance_id: str) -> List[Dict[str, Any]]:
    """EVERY active Wix store whose stored instance id is exactly `instance_id`.

    The instance id lives inside the credential JSON, which SQLite and Postgres
    cannot be asked to index the same way, so the `LIKE` only NARROWS the scan
    and the exact comparison happens in Python — a substring match is never
    allowed to resolve a store.

    ALL matches are returned, never the first: `merchant_stores` carries no
    uniqueness on the instance id, so both the receiver (which must refuse an
    ambiguous delivery) and the connect route (which must refuse a claim on
    another merchant's instance) need to see the whole set.
    """
    instance_id = str(instance_id or "").strip()
    if not is_wix_instance_id(instance_id):
        return []
    rows = await database.fetch_all(
        """
        SELECT store_id, merchant_id, domain, api_key
        FROM merchant_stores
        WHERE platform = 'wix'
          AND lower(COALESCE(status, 'active')) IN ('active', 'connected')
          AND api_key LIKE :needle
        """,
        {"needle": f"%{instance_id}%"},
    )
    matches: List[Dict[str, Any]] = []
    for row in rows or []:
        store = dict(row)
        if stored_wix_instance_id(store.get("api_key")) == instance_id:
            matches.append(store)
    return matches


# Token keys `normalize_wix_api_key` prefers over `api_key`. An API-key connect
# has just validated fresh auth material, so a stale OAuth token left in the
# blob must not go on shadowing it.
_WIX_SUPERSEDED_TOKEN_KEYS = ("access_token", "wix_access_token", "token")


def merge_wix_credential(
    existing: Any,
    *,
    api_key: str,
    site_id: str = "",
    instance_id: str = "",
) -> str:
    """What to persist in `merchant_stores.api_key` on an API-key (re)connect.

    A bare key stays a bare key — byte-identical to what every writer produced
    before the blob existed — unless an `instance_id` is being added. When the
    stored credential IS already a blob it is merged into rather than replaced,
    so a reconnect cannot erase the `instance_id` that `POST /webhooks/wix`
    resolves the store by (or the `site_id` a reader may hold).
    """
    api_key = str(api_key or "")
    blob = coerce_wix_credential_blob(existing)
    instance_id = str(instance_id or "").strip()
    if not blob and not instance_id:
        return api_key
    merged = dict(blob)
    for key in _WIX_SUPERSEDED_TOKEN_KEYS:
        merged.pop(key, None)
    merged["api_key"] = api_key
    site_id = str(site_id or "").strip()
    if site_id:
        merged["site_id"] = site_id
    if instance_id:
        merged["instance_id"] = instance_id
    return json.dumps(merged)


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
