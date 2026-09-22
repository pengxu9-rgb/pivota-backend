"""Product and variant DETAIL for external-retailer sellers, served by the gateway.

WHY THIS EXISTS. `GET /agent/v1/products/merchants/{merchant_id}/product/{product_id}` and
`/variant/{variant_id}` read only this backend's per-merchant sources: the onboarding record, the
products cache, and a connected Shopify Admin API. An external retailer's products (observed
sellers, `merch_obs_*` -- e.g. jsmbeauty.sg) are in none of them, so a partner that finds such a
product through search and opens it gets a 404. Measured 2026-09-22 for the Meitu JSM gloss:
search returns it, detail and variant detail both 404.

The gateway already serves that detail: its PDP lane, `get_pdp_v2` -- the lane the public product
page renders from, and the one hosted UCP `get_product` is rerouted to for observed sellers (title,
brand, images, every shade with its SKU and live price). This module asks that lane, over the
gateway's HTTP `/agent/shop/v1/invoke`, with the caller's OWN credential, and reshapes the answer
into this endpoint's contract -- one detail implementation (ADR-021), not a second copy here.

⚠️ NOT `get_product_detail`. Over HTTP invoke, the gateway sends `get_product_detail` straight
back to THIS backend's Python invoke route, which has no observed-seller rows and 404s. The
observed-seller reroute to `get_pdp_v2` lives only in the gateway's in-process commerce kernel
(invokeCommerceKernelRawUpstream), which hosted UCP uses and HTTP invoke does not. Review of #2239
caught this; the first version's mocked gateway answered any POST, so its tests could not.

Scope: observed sellers only. Onboarded merchants keep their existing path untouched, and the
gateway's own detail for a non-observed seller would call back into this backend.

Flag: AGENT_PRODUCT_DETAIL_VIA_GATEWAY=on (default off). Read per call.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional, Tuple

import httpx

from services.agent_search_gateway_proxy import HOP_HEADER, gateway_headers

FLAG = "AGENT_PRODUCT_DETAIL_VIA_GATEWAY"
OBSERVED_SELLER_PREFIX = "merch_obs_"
INVOKE_PATH = "/agent/shop/v1/invoke"
TIMEOUT_SECONDS = 8.0

_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_SECONDS))
    return _client


def enabled_for(merchant_id: Optional[str], headers: Mapping[str, str], env: Mapping[str, str] = os.environ) -> Tuple[bool, str]:
    """Whether THIS detail request is forwarded, and why not when it is not."""
    if str(env.get(FLAG, "")).strip().lower() not in {"1", "true", "on", "yes"}:
        return False, "flag_off"
    if not str(merchant_id or "").strip().startswith(OBSERVED_SELLER_PREFIX):
        return False, "not_an_observed_seller"
    if any(key.lower() == HOP_HEADER.lower() for key in headers.keys()):
        return False, "already_proxied"
    return True, "enabled"


# The one table lookup: the caller's reference (a Pivota id, a product key, a content key, a
# source product id, or the name segment of an external key) -> the product's Pivota signature,
# scoped to THIS merchant and to unsuppressed rows (the same refusal every serving path makes).
# A Pivota id goes through it too: the gateway lane is merchant-blind, so an unchecked `sig_` would
# let `/merchants/A/product/<B's sig>` serve B's product labelled A.
# The external key is `ext:<name>::<hash>` (derive_product_key), so its name segment is matched by
# that exact shape, never by a free LIKE. For the Meitu gloss the name equals the Shopify handle;
# that is not guaranteed in general, and a miss is an honest 404.
_PRODUCT_SQL = """
SELECT DISTINCT cp.pivota_signature_id
FROM catalog_products cp
WHERE cp.merchant_id = :merchant_id
  AND cp.suppressed_at IS NULL
  AND COALESCE(cp.pivota_signature_id, '') <> ''
  AND (cp.pivota_signature_id = :ref
       OR cp.source_product_id = :ref
       OR cp.product_key = :ref
       OR cp.content_key = :ref
       OR cp.product_key LIKE :ext_ref)
LIMIT 2
"""

_VARIANT_SQL = """
SELECT DISTINCT cp.pivota_signature_id
FROM catalog_skus cs
JOIN catalog_products cp ON cp.product_key = cs.product_key
WHERE cs.merchant_id = :merchant_id
  AND (cs.source_variant_id = :variant_id OR cs.sku = :variant_id)
  AND cs.suppressed_at IS NULL
  AND cp.suppressed_at IS NULL
  AND COALESCE(cp.pivota_signature_id, '') <> ''
LIMIT 2
"""


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def resolve_signature(database: Any, merchant_id: str, *, product_ref: Optional[str] = None,
                            variant_id: Optional[str] = None) -> Tuple[Optional[str], str]:
    """(signature, reason). Exactly one product, or refuse -- never a guess between two."""
    if product_ref:
        ref = str(product_ref).strip()
        rows = await database.fetch_all(_PRODUCT_SQL, {
            "merchant_id": merchant_id, "ref": ref, "ext_ref": f"ext:{_like_escape(ref)}::%",
        })
    else:
        rows = await database.fetch_all(_VARIANT_SQL, {"merchant_id": merchant_id, "variant_id": str(variant_id or "").strip()})
    sigs = {str(dict(row)["pivota_signature_id"]) for row in (rows or [])}
    if len(sigs) == 1:
        return sigs.pop(), "resolved"
    return None, "ambiguous" if sigs else "not_found"


async def fetch_detail(*, base_url: str, merchant_id: str, signature: str, variant_id: Optional[str],
                       headers: Mapping[str, str]) -> Tuple[Optional[Dict[str, Any]], str, int]:
    """Ask the gateway's PDP lane once. (product in the `{product}` detail shape, reason,
    caller-facing status); never raises. `merchant_id`/`variant_id` are not sent: the lane is keyed
    by the Pivota id alone, and the merchant was already checked against it in resolve_signature."""
    try:
        response = await _get_client().post(
            f"{str(base_url).rstrip('/')}{INVOKE_PATH}",
            json={
                "operation": "get_pdp_v2",
                "payload": {"product_ref": {"product_id": signature}, "include": ["product_overview"]},
            },
            headers=gateway_headers(headers),
        )
    except httpx.TimeoutException:
        return None, "gateway_timeout", 504
    except httpx.RequestError:
        return None, "gateway_unavailable", 503
    try:
        body = response.json()
    except Exception:
        return None, "gateway_invalid_json", 502
    if response.status_code == 200 and isinstance(body, dict):
        product = normalize_pdp_v2(body)
        if product is not None:
            return product, "ok", 200
        return None, "gateway_no_canonical_product", 404
    error = body.get("error") if isinstance(body, dict) else None
    code = str((error or {}).get("code") or "") if isinstance(error, dict) else ""
    if code in {"NO_MERCHANT_OFFER", "PRODUCT_NOT_FOUND", "NOT_FOUND"} or response.status_code == 404:
        return None, f"gateway_{code.lower() or 'not_found'}", 404
    status = response.status_code
    return None, f"gateway_http_{status}", status if status in {400, 401, 403, 429, 503, 504} else 502


def normalize_pdp_v2(body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The gateway's own normalizePdpV2ToProductDetail (src/server.js), in Python: the canonical
    module's `pdp_payload.product`, with brand `{name}` and price `{current:{amount,currency}}`
    flattened. None when there is no canonical product."""
    modules = body.get("modules") if isinstance(body.get("modules"), list) else []
    canonical = next((m for m in modules if isinstance(m, dict) and m.get("type") == "canonical"), None)
    data = canonical.get("data") if isinstance(canonical, dict) and isinstance(canonical.get("data"), dict) else {}
    payload = data.get("pdp_payload") if isinstance(data.get("pdp_payload"), dict) else {}
    product = payload.get("product")
    if not isinstance(product, dict):
        return None
    out = dict(product)
    brand = product.get("brand")
    out["brand"] = (brand.get("name") if isinstance(brand, dict) else brand) or None
    price = product.get("price")
    current = price.get("current") if isinstance(price, dict) and isinstance(price.get("current"), dict) else None
    out["price"] = current.get("amount") if current else (price if isinstance(price, (int, float)) else None)
    out["currency"] = current.get("currency") if current else (product.get("currency") if isinstance(product.get("currency"), str) else None)
    return out


def _money(value: Any) -> Optional[float]:
    if isinstance(value, dict):
        current = value.get("current") if isinstance(value.get("current"), dict) else value
        value = current.get("amount")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _currency(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        current = value.get("current") if isinstance(value.get("current"), dict) else value
        return current.get("currency")
    return None


def to_backend_detail(product: Dict[str, Any], *, merchant_id: str) -> Dict[str, Any]:
    """The gateway's product in THIS endpoint's contract: `{status, product}` with the same keys the
    local path returns, plus the fields a referral product needs (where to buy it).

    Stock COUNTS are unknown for an external retailer, so `inventory_quantity` is null -- never a
    fabricated 0 that reads as "sold out" -- and `available` carries the retailer's own flag.
    """
    currency = product.get("currency")
    variants: List[Dict[str, Any]] = []
    option_values: Dict[str, List[str]] = {}
    for raw in product.get("variants") or []:
        if not isinstance(raw, dict):
            continue
        availability = raw.get("availability") if isinstance(raw.get("availability"), dict) else {}
        in_stock = availability.get("in_stock")
        variant_currency = _currency(raw.get("price")) or currency
        currency = currency or variant_currency
        variants.append({
            "variant_id": str(raw.get("variant_id") or ""),
            "title": raw.get("title") or "Default",
            "price": _money(raw.get("price")),
            "currency": variant_currency,
            "sku": raw.get("sku_id") or raw.get("sku"),
            "inventory_quantity": None,
            "available": bool(in_stock) if in_stock is not None else None,
            "options": raw.get("options") or [],
            "image_url": raw.get("image_url"),
        })
        for option in raw.get("options") or []:
            if isinstance(option, dict) and option.get("name") and option.get("value") is not None:
                values = option_values.setdefault(str(option["name"]), [])
                if option["value"] not in values:
                    values.append(option["value"])
    images = product.get("image_urls") or product.get("images") or ([product["image_url"]] if product.get("image_url") else [])
    return {
        "status": "success",
        "product": {
            "id": str(product.get("product_id") or ""),
            "merchant_id": merchant_id,
            "currency": currency,
            "title": product.get("title") or "",
            "description": product.get("description") or "",
            "vendor": product.get("brand"),
            "product_type": product.get("product_type") or product.get("category"),
            "variants": variants,
            "options": [{"name": name, "values": values} for name, values in option_values.items()],
            "images": list(images),
            "tags": [],
            "default_variant_id": product.get("default_variant_id"),
            "destination_url": product.get("destination_url") or product.get("external_redirect_url"),
            "canonical_url": product.get("canonical_url"),
            "category_path": product.get("category_path"),
            "pivota_signature_id": product.get("pivota_signature_id"),
            "source": product.get("source") or "external_seed",
            "served_by": "gateway",
        },
    }
