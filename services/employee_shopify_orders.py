"""Read one Shopify order into an employee-only, source-scoped snapshot."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

import httpx

from db.database import database
from services.shopify_access_token_service import resolve_shopify_admin_access_token
from services.shopify_domain import normalize_myshopify_domain

NATIVE_ORDER_ID_PATTERN = r"^[1-9][0-9]{0,19}$"
ShopifyOrderCredentialResolver = Callable[[Mapping[str, Any]], Awaitable[str | Mapping[str, Any] | None]]

_ADMIN_API_VERSION = "2026-10"
_STORE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,49}")
_ALLOWLIST_ENV = "EMPLOYEE_SHOPIFY_ORDER_SYNC_ALLOWED_STORES"
_ALLOWLIST_ENTRY_LIMIT = 10
_PUBLIC_ORDER_FIELDS = (
    "domain", "shopify_order_id", "name", "test", "payment_status", "fulfillment_status",
    "currency", "subtotal", "shipping", "total", "line_items",
    "shopify_created_at", "shopify_updated_at", "synced_at",
)
_ORDER_QUERY = """
query EmployeeShopifyOrder($id: ID!, $after: String) {
  shop { myshopifyDomain }
  order(id: $id) {
    id name test displayFinancialStatus displayFulfillmentStatus createdAt updatedAt
    currentSubtotalPriceSet { shopMoney { amount currencyCode } }
    currentShippingPriceSet { shopMoney { amount currencyCode } }
    currentTotalPriceSet { shopMoney { amount currencyCode } }
    lineItems(first: 100, after: $after) {
      nodes { id title quantity product { id } variant { id } }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""
_ACTIVE_SHOPIFY_STORES = """
    FROM merchant_stores AS stores
    JOIN merchant_onboarding AS merchants ON merchants.merchant_id = stores.merchant_id
    WHERE lower(trim(stores.platform)) = 'shopify'
      AND lower(trim(stores.status)) IN ('active', 'connected')
      AND lower(COALESCE(merchants.status, '')) <> 'deleted'
"""
_EXACT_SHOPIFY_STORE = _ACTIVE_SHOPIFY_STORES + """
    AND stores.merchant_id = :merchant_id AND stores.store_id = :store_id
"""
_BOUND_SHOPIFY_STORE = _EXACT_SHOPIFY_STORE + " AND stores.domain = :bound_domain"
_SCOPE_WHERE = f"""
    snapshots.merchant_id = :merchant_id AND snapshots.store_id = :store_id
    AND snapshots.domain = :domain AND EXISTS (SELECT 1 {_BOUND_SHOPIFY_STORE})
"""


class ShopifyOrderError(Exception):
    def __init__(self, code: str, status_code: int):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def _enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def employee_shopify_orders_enabled() -> bool:
    return _enabled("EMPLOYEE_SHOPIFY_ORDERS_ENABLED")


@lru_cache(maxsize=8)
def _parse_sync_allowlist(raw: str) -> frozenset[tuple[str, str, str]]:
    if not raw or len(raw) > 4096:
        return frozenset()
    try:
        rows = json.loads(raw)
    except (ValueError, RecursionError):
        return frozenset()
    if not isinstance(rows, list) or not 1 <= len(rows) <= _ALLOWLIST_ENTRY_LIMIT:
        return frozenset()
    result = set()
    for row in rows:
        if not isinstance(row, dict) or row.keys() != {"merchant_id", "store_id", "domain"}:
            return frozenset()
        merchant_id, store_id, domain = row["merchant_id"], row["store_id"], row["domain"]
        if (
            not all(isinstance(item, str) and _STORE_ID.fullmatch(item) for item in (merchant_id, store_id))
            or not isinstance(domain, str) or len(domain) > 77 or normalize_myshopify_domain(domain) != domain
        ):
            return frozenset()
        result.add((merchant_id, store_id, domain))
    return frozenset(result) if len(result) == len(rows) else frozenset()


def _sync_allowed(merchant_id: str, store_id: str, domain: str) -> bool:
    return (merchant_id, store_id, domain) in _parse_sync_allowlist(os.getenv(_ALLOWLIST_ENV, ""))


async def resolve_store_order_credentials(store: Mapping[str, Any]) -> str | None:
    return await database.fetch_val(
        "SELECT stores.api_key " + _BOUND_SHOPIFY_STORE,
        {"merchant_id": store["merchant_id"], "store_id": store["store_id"], "bound_domain": store["domain"]},
    )


async def list_shopify_order_merchants() -> dict:
    rows = await database.fetch_all(
        "SELECT merchants.merchant_id, merchants.business_name, stores.domain " + _ACTIVE_SHOPIFY_STORES
        + " AND stores.domain IS NOT NULL ORDER BY merchants.business_name, merchants.merchant_id",
    )
    merchants = {}
    for row in rows:
        merchant_id = row["merchant_id"]
        if normalize_myshopify_domain(row["domain"]):
            merchants[merchant_id] = {"merchant_id": merchant_id, "business_name": row["business_name"] or merchant_id}
    return {"merchants": list(merchants.values())}


async def list_shopify_order_stores(merchant_id: str) -> dict:
    rows = await database.fetch_all(
        "SELECT stores.store_id, stores.name, stores.status, stores.domain " + _ACTIVE_SHOPIFY_STORES
        + " AND stores.merchant_id = :merchant_id ORDER BY stores.name, stores.store_id",
        {"merchant_id": merchant_id},
    )
    stores = []
    for row in rows:
        domain = normalize_myshopify_domain(row["domain"])
        if domain:
            stores.append({**dict(row), "merchant_id": merchant_id, "platform": "shopify", "domain": domain})
    return {"merchant_id": merchant_id, "stores": stores}


async def _shopify_store(merchant_id: str, store_id: str) -> tuple[dict, str]:
    row = await database.fetch_one(
        "SELECT stores.merchant_id, stores.store_id, stores.domain, "
        "COALESCE(trim(stores.api_key), '') <> '' AS api_key_present " + _EXACT_SHOPIFY_STORE,
        {"merchant_id": merchant_id, "store_id": store_id},
    )
    if row is None:
        raise ShopifyOrderError("STORE_NOT_FOUND", 404)
    domain = normalize_myshopify_domain(row["domain"])
    if not domain:
        raise ShopifyOrderError("SHOPIFY_DOMAIN_INVALID", 409)
    return dict(row), domain


def _native_id(value: str, kind: str) -> str:
    prefix = f"gid://shopify/{kind}/"
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ValueError("Invalid Shopify ID")
    native_id = value[len(prefix):]
    if not re.fullmatch(NATIVE_ORDER_ID_PATTERN, native_id):
        raise ValueError("Invalid Shopify ID")
    return native_id


def _line_item(node: dict) -> dict:
    if type(node["quantity"]) is not int or node["quantity"] < 0 or not isinstance(node["title"], str):
        raise ValueError("Invalid Shopify line item")
    product, variant = node["product"], node["variant"]
    return {
        "shopify_line_item_id": _native_id(node["id"], "LineItem"),
        "product_id": _native_id(product["id"], "Product") if product is not None else None,
        "variant_id": _native_id(variant["id"], "ProductVariant") if variant is not None else None,
        "title": node["title"],
        "quantity": node["quantity"],
    }


def _order_fields(order: dict) -> dict:
    subtotal = order["currentSubtotalPriceSet"]["shopMoney"]
    shipping = order["currentShippingPriceSet"]["shopMoney"]
    total = order["currentTotalPriceSet"]["shopMoney"]
    currency = total["currencyCode"]
    amounts = [Decimal(money["amount"]) for money in (subtotal, shipping, total)]
    if (
        not isinstance(order["name"], str)
        or type(order["test"]) is not bool
        or not isinstance(order["displayFulfillmentStatus"], str)
        or (order["displayFinancialStatus"] is not None and not isinstance(order["displayFinancialStatus"], str))
        or not re.fullmatch(r"[A-Z]{3}", currency)
        or any(not amount.is_finite() for amount in amounts)
        or any(money["currencyCode"] != currency for money in (subtotal, shipping))
    ):
        raise ValueError("Invalid Shopify order fields")

    created_at = datetime.fromisoformat(order["createdAt"].replace("Z", "+00:00"))
    updated_at = datetime.fromisoformat(order["updatedAt"].replace("Z", "+00:00"))
    if created_at.utcoffset() is None or updated_at.utcoffset() is None:
        raise ValueError("Invalid Shopify order timestamp")

    return {
        "name": order["name"],
        "test": order["test"],
        "payment_status": order["displayFinancialStatus"],
        "fulfillment_status": order["displayFulfillmentStatus"],
        "currency": currency,
        "subtotal": amounts[0],
        "shipping": amounts[1],
        "total": amounts[2],
        "shopify_created_at": created_at,
        "shopify_updated_at": updated_at,
    }


async def _shopify_order_page(
    client: httpx.AsyncClient, domain: str, token: str, gid: str, cursor: str | None,
) -> dict:
    response = await client.post(
        f"https://{domain}/admin/api/{_ADMIN_API_VERSION}/graphql.json",
        headers={"X-Shopify-Access-Token": token},
        json={"query": _ORDER_QUERY, "variables": {"id": gid, "after": cursor}},
    )
    if response.status_code in {401, 403}:
        raise ShopifyOrderError("SHOPIFY_ORDER_ACCESS_DENIED", 409)
    if response.status_code != 200:
        raise ShopifyOrderError("SHOPIFY_ORDER_PROVIDER_UNAVAILABLE", 502)
    payload = response.json()
    if payload.get("errors"):
        if any(error.get("extensions", {}).get("code") == "ACCESS_DENIED" for error in payload["errors"]):
            raise ShopifyOrderError("SHOPIFY_ORDER_ACCESS_DENIED", 409)
        raise ShopifyOrderError("SHOPIFY_ORDER_PROVIDER_UNAVAILABLE", 502)
    return payload["data"]


async def _fetch_shopify_order(domain: str, token: str, order_id: str) -> dict:
    gid = f"gid://shopify/Order/{order_id}"
    cursor = None
    cursors = set()
    lines_by_id = {}
    snapshot = None
    try:
        async with asyncio.timeout(35):
            async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
                for _ in range(20):
                    page = await _shopify_order_page(client, domain, token, gid, cursor)
                    if normalize_myshopify_domain(page["shop"]["myshopifyDomain"]) != domain:
                        raise ShopifyOrderError("SHOPIFY_ORDER_SOURCE_MISMATCH", 502)
                    order = page["order"]
                    if order is None:
                        raise ShopifyOrderError("SHOPIFY_ORDER_NOT_FOUND", 404)
                    if order["id"] != gid:
                        raise ShopifyOrderError("SHOPIFY_ORDER_ID_MISMATCH", 502)
                    if snapshot is None:
                        snapshot = _order_fields(order)
                    elif _order_fields(order) != snapshot:
                        raise ShopifyOrderError("SHOPIFY_ORDER_CHANGED_DURING_SYNC", 409)

                    connection = order["lineItems"]
                    for node in connection["nodes"]:
                        line = _line_item(node)
                        line_id = line["shopify_line_item_id"]
                        if line_id in lines_by_id:
                            raise ValueError("Duplicate Shopify line item")
                        lines_by_id[line_id] = line
                    if connection["pageInfo"]["hasNextPage"] is False:
                        return {**snapshot, "line_items": json.dumps(list(lines_by_id.values()))}
                    cursor = connection["pageInfo"]["endCursor"]
                    if not cursor or cursor in cursors:
                        raise ValueError("Invalid Shopify line item cursor")
                    cursors.add(cursor)
    except (TimeoutError, httpx.RequestError) as exc:
        raise ShopifyOrderError("SHOPIFY_ORDER_PROVIDER_UNAVAILABLE", 502) from exc
    except (AttributeError, KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise ShopifyOrderError("SHOPIFY_ORDER_PROVIDER_RESPONSE_INVALID", 502) from exc
    raise ShopifyOrderError("SHOPIFY_ORDER_LINE_ITEM_LIMIT", 422)


def _public_order(row: Mapping[str, Any]) -> dict:
    order = {field: row[field] for field in _PUBLIC_ORDER_FIELDS}
    for key in ("subtotal", "shipping", "total"):
        order[key] = format(order[key], "f")
    if isinstance(order["line_items"], str):
        order["line_items"] = json.loads(order["line_items"])
    return order


def _store_scope(store: Mapping[str, Any], domain: str) -> dict:
    return {"merchant_id": store["merchant_id"], "store_id": store["store_id"], "domain": domain}


async def _get_order(store: Mapping[str, Any], scope: dict, order_id: str) -> dict:
    row = await database.fetch_one(
        "SELECT snapshots.* FROM employee_shopify_orders AS snapshots WHERE " + _SCOPE_WHERE
        + " AND snapshots.shopify_order_id = :shopify_order_id",
        {**scope, "bound_domain": store["domain"], "shopify_order_id": order_id},
    )
    if row is None:
        raise ShopifyOrderError("SHOPIFY_ORDER_NOT_FOUND", 404)
    return _public_order(row)


async def get_employee_shopify_order(merchant_id: str, store_id: str, order_id: str) -> dict:
    store, domain = await _shopify_store(merchant_id, store_id)
    scope = _store_scope(store, domain)
    return {**scope, "order": await _get_order(store, scope, order_id)}


async def list_employee_shopify_orders(merchant_id: str, store_id: str, offset: int, limit: int) -> dict:
    store, domain = await _shopify_store(merchant_id, store_id)
    scope = _store_scope(store, domain)
    rows = await database.fetch_all(
        "SELECT snapshots.* FROM employee_shopify_orders AS snapshots WHERE " + _SCOPE_WHERE
        + " ORDER BY snapshots.synced_at DESC, snapshots.shopify_order_id DESC LIMIT :limit OFFSET :offset",
        {**scope, "bound_domain": store["domain"], "offset": offset, "limit": limit + 1},
    )
    return {
        **scope,
        "orders": [_public_order(row) for row in rows[:limit]],
        "next_offset": offset + limit if len(rows) > limit else None,
        "can_sync": (
            _enabled("EMPLOYEE_SHOPIFY_ORDER_SYNC_ENABLED")
            and store["api_key_present"] and _sync_allowed(merchant_id, store_id, domain)
        ),
    }


async def sync_employee_shopify_order(
    merchant_id: str,
    store_id: str,
    expected_domain: str,
    order_id: str,
    actor_employee_id: str,
    credential_resolver: ShopifyOrderCredentialResolver,
) -> dict:
    if not _enabled("EMPLOYEE_SHOPIFY_ORDER_SYNC_ENABLED"):
        raise ShopifyOrderError("SHOPIFY_ORDER_SYNC_DISABLED", 409)
    store, domain = await _shopify_store(merchant_id, store_id)
    if domain != expected_domain:
        raise ShopifyOrderError("SHOPIFY_STORE_CHANGED", 409)
    if not _sync_allowed(merchant_id, store_id, domain):
        raise ShopifyOrderError("SHOPIFY_ORDER_SYNC_STORE_UNAVAILABLE", 409)
    raw_credentials = await credential_resolver(store)
    token, _ = await resolve_shopify_admin_access_token(
        shop_domain=domain,
        api_key_raw=json.dumps(dict(raw_credentials)) if isinstance(raw_credentials, Mapping) else raw_credentials,
        store_id=store_id,
        persist_refresh=False,
    )
    if not token:
        raise ShopifyOrderError("SHOPIFY_ORDER_CREDENTIALS_MISSING", 409)
    snapshot = await _fetch_shopify_order(domain, token, order_id)
    scope = _store_scope(store, domain)
    row = await database.fetch_one(
        f"""
        WITH bound_store AS (
            SELECT 1 {_BOUND_SHOPIFY_STORE}
            FOR SHARE OF merchants, stores
        ), written AS (
            INSERT INTO employee_shopify_orders (
                merchant_id, store_id, domain, shopify_order_id, name, test, payment_status,
                fulfillment_status, currency, subtotal, shipping, total, line_items,
                shopify_created_at, shopify_updated_at, synced_by_employee_id
            )
            SELECT :merchant_id, :store_id, :domain, :shopify_order_id, :name, :test, :payment_status,
                :fulfillment_status, :currency, :subtotal, :shipping, :total, CAST(:line_items AS jsonb),
                :shopify_created_at, :shopify_updated_at, :actor_employee_id
            FROM bound_store
            ON CONFLICT (merchant_id, store_id, domain, shopify_order_id) DO UPDATE SET
                name = EXCLUDED.name, test = EXCLUDED.test, payment_status = EXCLUDED.payment_status,
                fulfillment_status = EXCLUDED.fulfillment_status, currency = EXCLUDED.currency,
                subtotal = EXCLUDED.subtotal, shipping = EXCLUDED.shipping, total = EXCLUDED.total,
                line_items = EXCLUDED.line_items, shopify_created_at = EXCLUDED.shopify_created_at,
                shopify_updated_at = EXCLUDED.shopify_updated_at, synced_at = now(),
                synced_by_employee_id = EXCLUDED.synced_by_employee_id
            WHERE employee_shopify_orders.shopify_updated_at <= EXCLUDED.shopify_updated_at
            RETURNING *
        )
        SELECT written.*, bound.store_bound
        FROM (SELECT EXISTS (SELECT 1 FROM bound_store) AS store_bound) AS bound
        LEFT JOIN written ON TRUE
        """,
        {**scope, **snapshot, "bound_domain": store["domain"], "shopify_order_id": order_id,
         "actor_employee_id": actor_employee_id},
    )
    persisted = dict(row)
    if not persisted.pop("store_bound"):
        raise ShopifyOrderError("SHOPIFY_STORE_CHANGED", 409)
    order = (
        _public_order(persisted) if persisted["shopify_order_id"] is not None
        else await _get_order(store, scope, order_id)
    )
    return {**scope, "order": order}
