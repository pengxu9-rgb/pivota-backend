from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List

from services.shopify_graphql_client import shopify_admin_graphql

WEB_PIXEL_QUERY = """
query PivotaWebPixel {
  webPixel {
    id
    settings
  }
}
"""

WEB_PIXEL_CREATE_MUTATION = """
mutation PivotaWebPixelCreate($webPixel: WebPixelInput!) {
  webPixelCreate(webPixel: $webPixel) {
    webPixel {
      id
      settings
    }
    userErrors {
      field
      message
      code
    }
  }
}
"""

WEB_PIXEL_UPDATE_MUTATION = """
mutation PivotaWebPixelUpdate($id: ID!, $webPixel: WebPixelInput!) {
  webPixelUpdate(id: $id, webPixel: $webPixel) {
    webPixel {
      id
      settings
    }
    userErrors {
      field
      message
      code
    }
  }
}
"""


@dataclass
class ShopifyWebPixelProvisioningError(RuntimeError):
    code: str
    status_code: int = 502

    def __str__(self) -> str:
        return self.code


def _settings_keys(value: Any) -> List[str]:
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
    if not isinstance(parsed, dict):
        return []
    return sorted(str(key) for key in parsed.keys())


def _public_pixel(pixel: Any) -> Dict[str, Any]:
    pixel = pixel if isinstance(pixel, dict) else {}
    pixel_id = str(pixel.get("id") or "").strip() or None
    return {
        "configured": bool(pixel_id),
        "web_pixel_id": pixel_id,
        "settings_keys": _settings_keys(pixel.get("settings")),
    }


def _user_error_codes(payload: Any) -> List[str]:
    payload = payload if isinstance(payload, dict) else {}
    errors = payload.get("userErrors") or []
    if not isinstance(errors, list) or not errors:
        return []
    # Do not include Shopify messages here: a provider may echo invalid settings,
    # and the settings contain the collector token.
    return sorted(
        {
            str(error.get("code") or "SHOPIFY_USER_ERROR").strip()
            for error in errors
            if isinstance(error, dict)
        }
    )


def _raise_for_user_errors(payload: Any) -> None:
    codes = _user_error_codes(payload)
    if not codes:
        return
    suffix = ",".join(code for code in codes if code) or "SHOPIFY_USER_ERROR"
    raise ShopifyWebPixelProvisioningError(f"web_pixel_rejected:{suffix}")


async def get_shopify_web_pixel_status(
    *, shop_domain: str, access_token: str, api_version: str = "2025-10"
) -> Dict[str, Any]:
    data = await shopify_admin_graphql(
        shop_domain=shop_domain,
        access_token=access_token,
        query=WEB_PIXEL_QUERY,
        api_version=api_version,
        redact_errors=True,
    )
    return _public_pixel(data.get("webPixel"))


async def ensure_shopify_web_pixel(
    *,
    shop_domain: str,
    access_token: str,
    settings: Dict[str, str],
    api_version: str = "2025-10",
) -> Dict[str, Any]:
    current = await get_shopify_web_pixel_status(
        shop_domain=shop_domain,
        access_token=access_token,
        api_version=api_version,
    )

    async def update_pixel(web_pixel_id: str) -> Dict[str, Any]:
        data = await shopify_admin_graphql(
            shop_domain=shop_domain,
            access_token=access_token,
            query=WEB_PIXEL_UPDATE_MUTATION,
            variables={
                "id": web_pixel_id,
                "webPixel": {"settings": settings},
            },
            api_version=api_version,
            redact_errors=True,
        )
        return data.get("webPixelUpdate")

    if current["web_pixel_id"]:
        operation = "updated"
        payload = await update_pixel(current["web_pixel_id"])
    else:
        operation = "created"
        data = await shopify_admin_graphql(
            shop_domain=shop_domain,
            access_token=access_token,
            query=WEB_PIXEL_CREATE_MUTATION,
            variables={"webPixel": {"settings": settings}},
            api_version=api_version,
            redact_errors=True,
        )
        payload = data.get("webPixelCreate")
        if "TAKEN" in _user_error_codes(payload):
            # Another ensure may have created the app-owned singleton between
            # our read and create. Confirm it, then rotate to these settings.
            concurrent = await get_shopify_web_pixel_status(
                shop_domain=shop_domain,
                access_token=access_token,
                api_version=api_version,
            )
            if concurrent["web_pixel_id"]:
                operation = "updated"
                payload = await update_pixel(concurrent["web_pixel_id"])

    if not isinstance(payload, dict):
        raise ShopifyWebPixelProvisioningError("web_pixel_response_invalid")
    _raise_for_user_errors(payload)
    result = _public_pixel(payload.get("webPixel"))
    if not result["configured"]:
        raise ShopifyWebPixelProvisioningError("web_pixel_missing_after_write")
    return {"status": operation, **result}
