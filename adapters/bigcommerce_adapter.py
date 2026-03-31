from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
import logging
import re

import httpx

logger = logging.getLogger(__name__)

_STORE_HASH_RE = re.compile(r"^[a-z0-9-]+$")


def normalize_bigcommerce_store_hash(raw_value: Optional[str]) -> str:
    raw = str(raw_value or "").strip().lower()
    if not raw:
        return ""
    if raw.endswith(".mybigcommerce.com"):
        raw = raw.split(".", 1)[0]
    return raw


def build_bigcommerce_domain(store_hash: Optional[str]) -> str:
    normalized = normalize_bigcommerce_store_hash(store_hash)
    if not normalized:
        return ""
    return f"{normalized}.mybigcommerce.com"


def build_bigcommerce_headers(access_token: str, client_id: Optional[str] = None) -> Dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Auth-Token": str(access_token or "").strip(),
    }
    client_id_value = str(client_id or "").strip()
    if client_id_value:
        headers["X-Auth-Client"] = client_id_value
    return headers


class BigCommerceAdapter:
    """Lightweight adapter for BigCommerce connection validation."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        config = config or {}
        self.store_hash = normalize_bigcommerce_store_hash(config.get("store_hash"))
        self.access_token = str(config.get("access_token") or "").strip()
        self.client_id = str(config.get("client_id") or "").strip()

    def validate_config(self) -> Tuple[bool, Optional[str]]:
        if not self.store_hash:
            return False, "BigCommerce Store Hash is required"
        if not _STORE_HASH_RE.match(self.store_hash):
            return False, "BigCommerce Store Hash format is invalid"
        if not self.access_token:
            return False, "BigCommerce Access Token is required"
        return True, None

    async def test_connection(self) -> Dict[str, Any]:
        is_valid, error_msg = self.validate_config()
        if not is_valid:
            return {"success": False, "error": error_msg}

        url = f"https://api.bigcommerce.com/stores/{self.store_hash}/v2/store"
        headers = build_bigcommerce_headers(self.access_token, self.client_id)

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(url, headers=headers)

            if response.status_code != 200:
                return {
                    "success": False,
                    "error": f"HTTP {response.status_code}: {response.text[:200]}",
                }

            store_name = build_bigcommerce_domain(self.store_hash)
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    store_name = (
                        str(payload.get("name") or "").strip()
                        or str(payload.get("domain") or "").strip()
                        or store_name
                    )
            except Exception:
                pass

            return {
                "success": True,
                "store_name": store_name,
            }
        except Exception as exc:
            logger.error("BigCommerce connection test failed: %s", exc)
            return {"success": False, "error": str(exc)}
