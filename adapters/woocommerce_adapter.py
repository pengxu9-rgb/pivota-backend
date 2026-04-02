from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse
import logging

import httpx

logger = logging.getLogger(__name__)


def normalize_woocommerce_store_url(raw_value: Optional[str]) -> str:
    raw = str(raw_value or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    return raw.rstrip("/")


class WooCommerceAdapter:
    """Lightweight adapter for WooCommerce connection validation."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        config = config or {}
        self.store_url = normalize_woocommerce_store_url(config.get("store_url"))
        self.consumer_key = str(config.get("consumer_key") or "").strip()
        self.consumer_secret = str(config.get("consumer_secret") or "").strip()

    def validate_config(self) -> Tuple[bool, Optional[str]]:
        if not self.store_url:
            return False, "WooCommerce Store URL is required"
        parsed = urlparse(self.store_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False, "WooCommerce Store URL must be a valid http(s) URL"
        if not self.consumer_key:
            return False, "WooCommerce Consumer Key is required"
        if not self.consumer_secret:
            return False, "WooCommerce Consumer Secret is required"
        return True, None

    async def test_connection(self) -> Dict[str, Any]:
        is_valid, error_msg = self.validate_config()
        if not is_valid:
            return {"success": False, "error": error_msg}

        url = f"{self.store_url}/wp-json/wc/v3/system_status"
        params = {
            "consumer_key": self.consumer_key,
            "consumer_secret": self.consumer_secret,
        }

        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                response = await client.get(url, params=params)

            if response.status_code != 200:
                return {
                    "success": False,
                    "error": f"HTTP {response.status_code}: {response.text[:200]}",
                }

            store_name = urlparse(self.store_url).netloc
            try:
                payload = response.json() or {}
                environment = payload.get("environment") if isinstance(payload, dict) else {}
                home_url = (
                    environment.get("home_url")
                    if isinstance(environment, dict)
                    else None
                )
                if isinstance(home_url, str) and home_url.strip():
                    store_name = urlparse(home_url).netloc or store_name
            except Exception:
                pass

            return {
                "success": True,
                "store_name": store_name,
            }
        except Exception as exc:
            logger.error("WooCommerce connection test failed: %s", exc)
            return {"success": False, "error": str(exc)}
