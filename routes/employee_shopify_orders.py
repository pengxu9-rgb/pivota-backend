"""Employee Shopify order availability, store selection, and scoped order readback."""

import logging
from typing import Annotated

from asyncpg import PostgresError
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, ConfigDict, StringConstraints

from services.employee_shopify_orders import (
    NATIVE_ORDER_ID_PATTERN,
    ShopifyOrderCredentialResolver,
    ShopifyOrderError,
    employee_shopify_orders_enabled,
    get_employee_shopify_order,
    list_employee_shopify_orders,
    list_shopify_order_merchants,
    list_shopify_order_stores,
    resolve_store_order_credentials,
    sync_employee_shopify_order,
)
from utils.auth import EMPLOYEE_STAFF_ROLES, get_current_employee, has_permission
from utils.tenant_scope import assert_merchant_scope, is_scope_staff

router = APIRouter(prefix="/employee/mcp", tags=["Employee Shopify Orders"])
logger = logging.getLogger(__name__)
_ORDER_PATH = "/merchants/{merchant_id}/stores/{store_id}/orders"
_SYNC_PERMISSION = "mcp.orders.sync"


class OrderSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shopify_order_id: Annotated[str, StringConstraints(strict=True, pattern=NATIVE_ORDER_ID_PATTERN)]


async def _employee_staff(
    actor: Annotated[dict, Depends(get_current_employee)], response: Response,
) -> dict:
    if not is_scope_staff(actor, EMPLOYEE_STAFF_ROLES):
        raise HTTPException(status_code=403, detail="EMPLOYEE_ACCESS_REQUIRED")
    response.headers["Cache-Control"] = "private, no-store"
    return actor


Employee = Annotated[dict, Depends(_employee_staff)]


async def _orders_employee(actor: Employee) -> dict:
    if not employee_shopify_orders_enabled():
        raise HTTPException(status_code=404, detail="SHOPIFY_ORDERS_DISABLED")
    return actor


OrdersEmployee = Annotated[dict, Depends(_orders_employee)]


def _employee_id(actor: dict) -> str:
    employee_id = actor.get("employee_id") or actor.get("employeeId")
    if not employee_id and not actor.get("membership_type"):
        employee_id = actor.get("user_id") or actor.get("sub")
    if not employee_id:
        raise HTTPException(status_code=403, detail="EMPLOYEE_ID_REQUIRED")
    return str(employee_id)


def get_shopify_order_credential_resolver() -> ShopifyOrderCredentialResolver:
    return resolve_store_order_credentials


async def _run(operation, *args) -> dict:
    try:
        return await operation(*args)
    except ShopifyOrderError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
    except (PostgresError, OSError) as exc:
        logger.warning("Employee Shopify Orders %s storage error (%s)", operation.__name__, type(exc).__name__)
        raise HTTPException(status_code=503, detail="SYNC_STORAGE_UNAVAILABLE") from exc


@router.get("/shopify-orders/availability")
async def shopify_orders_availability(actor: Employee) -> dict:
    return {"enabled": employee_shopify_orders_enabled()}


@router.get("/shopify-orders/merchants")
async def employee_shopify_order_merchants(actor: OrdersEmployee) -> dict:
    return await _run(list_shopify_order_merchants)


@router.get("/shopify-orders/merchants/{merchant_id}/stores")
async def employee_shopify_order_stores(
    actor: OrdersEmployee,
    merchant_id: str = Path(..., min_length=1, max_length=50),
) -> dict:
    assert_merchant_scope(actor, merchant_id, staff_roles=EMPLOYEE_STAFF_ROLES)
    return await _run(list_shopify_order_stores, merchant_id)


@router.get(_ORDER_PATH)
async def employee_shopify_orders(
    actor: OrdersEmployee,
    merchant_id: str = Path(..., min_length=1, max_length=50),
    store_id: str = Path(..., min_length=1, max_length=50),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
) -> dict:
    assert_merchant_scope(actor, merchant_id, staff_roles=EMPLOYEE_STAFF_ROLES)
    result = await _run(list_employee_shopify_orders, merchant_id, store_id, offset, limit)
    result["can_sync"] = result["can_sync"] and has_permission(actor, _SYNC_PERMISSION)
    return result


@router.post(_ORDER_PATH + "/sync")
async def employee_sync_shopify_order(
    body: OrderSyncRequest,
    actor: OrdersEmployee,
    credential_resolver: Annotated[ShopifyOrderCredentialResolver, Depends(get_shopify_order_credential_resolver)],
    merchant_id: str = Path(..., min_length=1, max_length=50),
    store_id: str = Path(..., min_length=1, max_length=50),
) -> dict:
    assert_merchant_scope(actor, merchant_id, staff_roles=EMPLOYEE_STAFF_ROLES)
    employee_id = _employee_id(actor)
    if not has_permission(actor, _SYNC_PERMISSION):
        raise HTTPException(status_code=403, detail="SHOPIFY_ORDER_SYNC_PERMISSION_REQUIRED")
    return await _run(sync_employee_shopify_order, merchant_id, store_id, body.shopify_order_id, employee_id, credential_resolver)


@router.get(_ORDER_PATH + "/{shopify_order_id}")
async def employee_shopify_order(
    actor: OrdersEmployee,
    merchant_id: str = Path(..., min_length=1, max_length=50),
    store_id: str = Path(..., min_length=1, max_length=50),
    shopify_order_id: str = Path(..., pattern=NATIVE_ORDER_ID_PATTERN),
) -> dict:
    assert_merchant_scope(actor, merchant_id, staff_roles=EMPLOYEE_STAFF_ROLES)
    return await _run(get_employee_shopify_order, merchant_id, store_id, shopify_order_id)
