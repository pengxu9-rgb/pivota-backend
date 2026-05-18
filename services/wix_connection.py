"""Shared Wix connection validation and credential helpers."""

from __future__ import annotations

import json
from typing import Any, Dict
from urllib.parse import urlparse

import httpx

WIX_PRODUCTS_QUERY_URL = "https://www.wixapis.com/stores-reader/v1/products/query"


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


def normalize_wix_site_id(value: Any) -> str:
    site_id = str(value or "").strip()
    if not site_id:
        raise WixConnectionValidationError("WIX_SITE_ID_REQUIRED", "Wix Site ID is required")

    parsed = urlparse(site_id)
    if parsed.scheme or parsed.netloc or "wixsite.com" in site_id.lower():
        raise WixConnectionValidationError(
            "WIX_SITE_ID_EXPECTED",
            "Wix Site ID is required; do not use the public wixsite.com store URL.",
        )

    if any(ch.isspace() for ch in site_id):
        raise WixConnectionValidationError(
            "WIX_SITE_ID_INVALID",
            "Wix Site ID cannot contain whitespace.",
        )

    return site_id


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
