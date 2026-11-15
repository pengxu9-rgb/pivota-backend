"""Local PostgreSQL regression for the scoped employee order API."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import databases
import httpx
import pytest
from fastapi import FastAPI

from db.sql_migrations import is_hand_applied_migration, split_statements
from routes import employee_shopify_orders
from services import employee_shopify_orders as order_service
from utils.auth import get_current_user

DSN = os.environ.get("DATABASE_URL", "").strip().replace("postgres://", "postgresql://", 1)
DB_NAME = DSN.rsplit("/", 1)[-1].split("?", 1)[0]
pytestmark = pytest.mark.skipif(
    not DSN.startswith("postgresql://") or not any(marker in DB_NAME for marker in ("_test", "test_", "dialect_check")),
    reason="requires a scratch PostgreSQL database",
)

MERCHANT = "fixture_shops"
PRIMARY = "fixture_primary"
SECONDARY = "fixture_secondary"
ORDER_ID = "18996744552814"
PRIMARY_DOMAIN = "fixture-primary.myshopify.com"
SECONDARY_DOMAIN = "fixture-secondary.myshopify.com"
ROSTER_PATH = "/employee/mcp/shopify-orders/merchants"
AVAILABILITY_PATH = "/employee/mcp/shopify-orders/availability"
ALLOWLIST = "EMPLOYEE_SHOPIFY_ORDER_SYNC_ALLOWED_STORES"


def _binding(store_id: str, domain: str, merchant_id: str = MERCHANT) -> dict:
    return {"merchant_id": merchant_id, "store_id": store_id, "domain": domain}


def _store_path(store_id: str, merchant: str = MERCHANT) -> str:
    return f"/employee/mcp/merchants/{merchant}/stores/{store_id}/orders"


def _provider_order(domain: str, *, updated_at: str, cursor: str | None) -> dict:
    def price(value):
        return {"shopMoney": {"amount": value, "currencyCode": "USD"}}

    product_id = "15087315648878" if cursor is None else "15087315157358"
    line_id = "777" if cursor is None else "778"
    return {
        "shop": {"myshopifyDomain": domain},
        "order": {
            "id": f"gid://shopify/Order/{ORDER_ID}",
            "name": "#1001",
            "test": True,
            "displayFinancialStatus": "PAID",
            "displayFulfillmentStatus": "UNFULFILLED",
            "createdAt": "2026-11-14T11:00:00Z",
            "updatedAt": updated_at,
            "currentSubtotalPriceSet": price("749.95"),
            "currentShippingPriceSet": price("15.00"),
            "currentTotalPriceSet": price("764.95"),
            "lineItems": {
                "nodes": [{
                    "id": f"gid://shopify/LineItem/{line_id}",
                    "product": {"id": f"gid://shopify/Product/{product_id}"},
                    "variant": {"id": "gid://shopify/ProductVariant/53885985718638"},
                    "title": "Primary line" if domain == PRIMARY_DOMAIN else "Demo 2 line",
                    "quantity": 1,
                }],
                "pageInfo": {"hasNextPage": cursor is None, "endCursor": "page-2" if cursor is None else None},
            },
        },
    }


@pytest.mark.asyncio
async def test_scoped_employee_orders(monkeypatch):
    schema = f"employee_shopify_orders_{uuid4().hex}"
    async with databases.Database(DSN, min_size=1, max_size=1) as admin:
        await admin.execute(f"CREATE SCHEMA {schema}")
        try:
            async with databases.Database(
                DSN, server_settings={"search_path": schema}, min_size=1, max_size=5,
            ) as db:
                monkeypatch.setattr(order_service, "database", db)
                await _exercise_employee_orders(db, monkeypatch)
        finally:
            await admin.execute(f"DROP SCHEMA {schema} CASCADE")


async def _exercise_employee_orders(database, monkeypatch):
    await database.execute("CREATE TABLE merchant_onboarding (merchant_id text PRIMARY KEY, business_name text, status text)")
    await database.execute(
        """
        CREATE TABLE merchant_stores (
          merchant_id text, store_id text PRIMARY KEY, name text, platform text, status text, domain text, api_key text
        )
        """
    )
    migration = Path(__file__).resolve().parents[1] / "db/migrations/286_employee_shopify_orders.sql.disabled"
    assert is_hand_applied_migration(str(migration))
    await database.execute("INSERT INTO merchant_onboarding VALUES ('fixture_shops', 'Fixture', 'active'), ('other', 'Other', 'active')")
    await database.execute_many(
        "INSERT INTO merchant_stores VALUES (:merchant_id, :store_id, :store_id, 'shopify', 'active', :domain, :token)",
        [
            {**_binding(PRIMARY, PRIMARY_DOMAIN), "token": f"fixture-token-for-{PRIMARY_DOMAIN}"},
            {**_binding(SECONDARY, SECONDARY_DOMAIN), "token": f"fixture-token-for-{SECONDARY_DOMAIN}"},
            {**_binding("invalid_test", "other.example.test", "other"), "token": "fixture-unavailable"},
        ],
    )

    role = "admin"
    permissions = []
    credential_calls = []
    upstream_calls = []
    provider_mode = None
    omit_order = False
    updated_at = "2026-11-14T11:01:00Z"

    async def employee():
        return {
            "role": role, "membership_type": "merchant" if role == "merchant" else "employee",
            "merchant_id": MERCHANT, "employee_id": "fixture_admin", "permissions": permissions,
        }

    async def credentials(store):
        credential_calls.append((store["store_id"], store["domain"]))
        return await order_service.resolve_store_order_credentials(store)

    async def shopify(request):
        query = json.loads(request.content)
        domain = request.url.host
        assert request.method == "POST" and request.url.path == "/admin/api/2026-10/graphql.json"
        assert request.headers["X-Shopify-Access-Token"] == f"fixture-token-for-{domain}"
        assert "mutation" not in query["query"] and "order(id: $id)" in query["query"]
        assert "customer" not in query["query"] and "shippingAddress" not in query["query"]
        assert query["variables"]["id"] == f"gid://shopify/Order/{ORDER_ID}"
        cursor = query["variables"]["after"]
        upstream_calls.append((domain, cursor))
        if provider_mode == "redirect":
            return httpx.Response(302, headers={"Location": "https://unapproved.example.org/"})
        if provider_mode == "denied":
            return httpx.Response(200, json={"errors": [{"extensions": {"code": "ACCESS_DENIED"}}]})
        model = _provider_order(domain, updated_at=updated_at, cursor=cursor)
        if provider_mode == "wrong_domain":
            model["shop"]["myshopifyDomain"] = PRIMARY_DOMAIN if domain == SECONDARY_DOMAIN else SECONDARY_DOMAIN
        if omit_order:
            model["order"] = None
        return httpx.Response(200, json={"data": model})

    app = FastAPI()
    app.include_router(employee_shopify_orders.router)
    app.dependency_overrides[get_current_user] = employee
    app.dependency_overrides[employee_shopify_orders.get_shopify_order_credential_resolver] = lambda: credentials
    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *args, **kwargs: async_client(*args, **kwargs, transport=httpx.MockTransport(shopify), trust_env=False),
    )

    primary, secondary = _store_path(PRIMARY), _store_path(SECONDARY)
    body = {"shopify_order_id": ORDER_ID, "expected_domain": SECONDARY_DOMAIN}
    primary_body = {**body, "expected_domain": PRIMARY_DOMAIN}
    async with async_client(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        monkeypatch.delenv("EMPLOYEE_SHOPIFY_ORDERS_ENABLED", raising=False)
        monkeypatch.delenv("EMPLOYEE_SHOPIFY_ORDER_SYNC_ENABLED", raising=False)
        monkeypatch.delenv(ALLOWLIST, raising=False)
        assert await database.fetch_val("SELECT to_regclass('employee_shopify_orders')::text") is None
        for path in (primary, f"{primary}/{ORDER_ID}", ROSTER_PATH, f"{ROSTER_PATH}/{MERCHANT}/stores"):
            assert (await client.get(path)).json() == {"detail": "SHOPIFY_ORDERS_DISABLED"}
        assert (await client.post(f"{secondary}/sync", json=body)).json() == {"detail": "SHOPIFY_ORDERS_DISABLED"}
        availability = await client.get(AVAILABILITY_PATH)
        assert availability.json() == {"enabled": False}
        assert availability.headers["Cache-Control"] == "private, no-store"
        role = "merchant"
        assert (await client.get(AVAILABILITY_PATH)).status_code == 403
        assert (await client.get(primary)).status_code == 403
        role = "admin"
        async with database.transaction():
            for statement in split_statements(migration.read_text()):
                await database.execute(statement)
        monkeypatch.setenv("EMPLOYEE_SHOPIFY_ORDERS_ENABLED", "1")
        assert (await client.get(AVAILABILITY_PATH)).json() == {"enabled": True}
        assert (await client.get(ROSTER_PATH)).json() == {"merchants": [{"merchant_id": MERCHANT, "business_name": "Fixture"}]}
        shops = (await client.get(f"{ROSTER_PATH}/{MERCHANT}/stores")).json()
        assert shops["merchant_id"] == MERCHANT
        assert [(row["store_id"], row["domain"]) for row in shops["stores"]] == [
            (PRIMARY, PRIMARY_DOMAIN), (SECONDARY, SECONDARY_DOMAIN),
        ]
        assert all("api_key" not in row for row in shops["stores"])
        assert (await client.get(secondary)).json()["can_sync"] is False
        assert (await client.post(f"{secondary}/sync", json=body)).json() == {"detail": "SHOPIFY_ORDER_SYNC_DISABLED"}
        monkeypatch.setenv("EMPLOYEE_SHOPIFY_ORDER_SYNC_ENABLED", "1")
        for rejected in (
            "[]", "*", "malformed",
            json.dumps([_binding(SECONDARY, PRIMARY_DOMAIN)]),
            json.dumps([_binding(SECONDARY, SECONDARY_DOMAIN, "other")]),
            json.dumps([_binding(SECONDARY, f"https://{SECONDARY_DOMAIN}")]),
            json.dumps([_binding(SECONDARY, SECONDARY_DOMAIN), {"merchant_id": MERCHANT}]),
            json.dumps([_binding(f"store_{number}", SECONDARY_DOMAIN) for number in range(10)]
                       + [_binding(SECONDARY, SECONDARY_DOMAIN)]),
        ):
            monkeypatch.setenv(ALLOWLIST, rejected)
            assert (await client.get(secondary)).json()["can_sync"] is False
            assert (await client.post(f"{secondary}/sync", json=body)).json() == {
                "detail": "SHOPIFY_ORDER_SYNC_STORE_UNAVAILABLE",
            }
        monkeypatch.setenv(ALLOWLIST, json.dumps([_binding(SECONDARY, SECONDARY_DOMAIN)]))
        role = "employee"
        assert (await client.get(secondary)).json()["can_sync"] is False
        assert (await client.post(f"{secondary}/sync", json=body)).status_code == 403
        role = "outsourced"
        assert (await client.get(secondary)).status_code == 403
        role = "employee"
        permissions = ["mcp.orders.sync"]
        assert (await client.get(secondary)).json()["can_sync"] is True
        assert (await client.get(primary)).json()["can_sync"] is False
        assert (await client.post(f"{primary}/sync", json=primary_body)).json() == {"detail": "SHOPIFY_ORDER_SYNC_STORE_UNAVAILABLE"}
        assert (await client.post(f"{secondary}/sync", json={**body, "shopify_order_id": 18996744552814})).status_code == 422
        assert (await client.post(f"{secondary}/sync", json={**body, "shopify_order_id": "#1001"})).status_code == 422
        assert (await client.post(f"{secondary}/sync", json={"shopify_order_id": ORDER_ID})).status_code == 422
        assert (await client.post(f"{secondary}/sync", json={**body, "expected_domain": f"https://{SECONDARY_DOMAIN}"})).status_code == 422
        assert (await client.get(f"{_store_path(SECONDARY, 'other')}/{ORDER_ID}")).status_code == 404
        assert not upstream_calls and not credential_calls

        # Both bindings are authorized, but the employee selected the previous domain.
        monkeypatch.setenv(ALLOWLIST, json.dumps([_binding(SECONDARY, SECONDARY_DOMAIN), _binding(SECONDARY, PRIMARY_DOMAIN)]))
        await database.execute(
            "UPDATE merchant_stores SET domain = :domain WHERE store_id = :store",
            {"domain": PRIMARY_DOMAIN, "store": SECONDARY},
        )
        changed = await client.post(f"{secondary}/sync", json=body)
        assert changed.status_code == 409 and changed.json() == {"detail": "SHOPIFY_STORE_CHANGED"}
        assert not upstream_calls and not credential_calls
        assert await database.fetch_val("SELECT count(*) FROM employee_shopify_orders") == 0
        await database.execute(
            "UPDATE merchant_stores SET domain = :domain WHERE store_id = :store",
            {"domain": SECONDARY_DOMAIN, "store": SECONDARY},
        )
        monkeypatch.setenv(ALLOWLIST, json.dumps([_binding(SECONDARY, SECONDARY_DOMAIN)]))

        for mode, status, detail in (
            ("redirect", 502, "SHOPIFY_ORDER_PROVIDER_UNAVAILABLE"),
            ("denied", 409, "SHOPIFY_ORDER_ACCESS_DENIED"),
            ("wrong_domain", 502, "SHOPIFY_ORDER_SOURCE_MISMATCH"),
        ):
            provider_mode = mode
            response = await client.post(f"{secondary}/sync", json=body)
            assert response.status_code == status and response.json() == {"detail": detail}
        provider_mode = None
        omit_order = True
        assert (await client.post(f"{secondary}/sync", json=body)).status_code == 404
        omit_order = False
        assert (await client.get(secondary)).json()["orders"] == []

        responses = await asyncio.gather(*(client.post(f"{secondary}/sync", json=body) for _ in range(2)))
        assert all(response.status_code == 200 for response in responses), [row.text for row in responses]
        response = responses[-1].json()
        assert response["domain"] == SECONDARY_DOMAIN
        order = response["order"]
        assert (order["name"], order["shopify_order_id"], order["test"]) == ("#1001", ORDER_ID, True)
        assert (order["currency"], order["subtotal"], order["shipping"], order["total"]) == ("USD", "749.95", "15.00", "764.95")
        assert (order["payment_status"], order["fulfillment_status"]) == ("PAID", "UNFULFILLED")
        assert [line["shopify_line_item_id"] for line in order["line_items"]] == ["777", "778"]
        assert order["line_items"][0]["product_id"] == "15087315648878"
        assert order["line_items"][0]["variant_id"] == "53885985718638"
        assert order["line_items"][0]["quantity"] == 1
        assert (await client.get(f"{secondary}/{ORDER_ID}")).json()["order"]["domain"] == SECONDARY_DOMAIN
        assert len((await client.get(secondary)).json()["orders"]) == 1
        assert (await client.get(f"{primary}/{ORDER_ID}")).status_code == 404
        assert (await client.get(primary)).json()["orders"] == []
        assert {domain for domain, _ in upstream_calls} == {SECONDARY_DOMAIN}
        assert set(credential_calls) == {(SECONDARY, SECONDARY_DOMAIN)}

        updated_at = "2026-11-14T11:00:30Z"
        stale = await client.post(f"{secondary}/sync", json=body)
        assert stale.status_code == 200 and stale.json()["order"]["shopify_updated_at"] == order["shopify_updated_at"]

        monkeypatch.setenv(ALLOWLIST, json.dumps([_binding(PRIMARY, PRIMARY_DOMAIN), _binding(SECONDARY, SECONDARY_DOMAIN)]))
        assert (await client.post(f"{primary}/sync", json=primary_body)).status_code == 200
        primary_order = (await client.get(f"{primary}/{ORDER_ID}")).json()["order"]
        assert primary_order["domain"] == PRIMARY_DOMAIN and primary_order["line_items"][0]["title"] == "Primary line"
        assert (await client.get(f"{secondary}/{ORDER_ID}")).json()["order"]["line_items"][0]["title"] == "Demo 2 line"
        assert await database.fetch_val("SELECT count(*) FROM employee_shopify_orders") == 2

        await database.execute(
            "UPDATE merchant_stores SET domain = 'reconfigured.myshopify.com' WHERE store_id = :store",
            {"store": SECONDARY},
        )
        assert (await client.get(f"{secondary}/{ORDER_ID}")).status_code == 404
        # Changing the store binding also hides old-domain results from its list.
        assert (await client.get(secondary)).json()["can_sync"] is False
        role = "employee"
        permissions = []
        assert (await client.get(secondary)).json()["orders"] == []
