"""Ownership behaviour of every merchant-facing route in merchant_store_connections.

WHY THIS FILE EXISTS. routes/merchant_store_connections.py had fifteen correct
ownership checks written five different ways -- `current_user["role"] ==
"merchant" and current_user.get("merchant_id") != target`, the same thing with
`str(... or "")` coercion, the same thing nested inside `if role == "merchant":`,
and one site with no role gate at all. `utils.auth.can_access_merchant` is the
system's single answer to "may this caller touch this merchant", and the
sibling modules (merchant_onboarding_shopify_verify_routes.py, and since the
cross-tenant fix wix_sync.py / employee_store_psp_fixes.py) all call it.

These tests were written and made green against the file BEFORE it was
unified -- they characterise the behaviour that already existed, so they cannot
be measuring the refactor's own presence. They are what makes the swap
checkable rather than merely plausible: the risk in unifying is not that the
new spelling refuses attackers, it is that it accidentally refuses STAFF (who
carry no merchant_id claim) or a merchant on their OWN record.

Three claims per route:
  * merchant_A asking for merchant_B is refused, with the OWNERSHIP message --
    not the role gate's "Not authorized". Distinguishing the two matters: a
    unification that accidentally dropped `merchant` from the role list would
    also 403, and would pass a status-only assertion while breaking every
    merchant.
  * merchant_A on their OWN merchant gets past the ownership gate.
  * staff (super_admin/admin/employee), who have no merchant_id claim at all,
    get past it cross-merchant.

Past the gate is asserted as "not the ownership refusal" for the table-driven
sweep -- downstream these routes hit Shopify/Wix/Woo and fail for their own
reasons, which is not this file's business. Four routes are additionally driven
to the handler body with a DB spy (see the bottom section) so the positive
claim has a load-bearing counterpart and is not only an absence.

Tokens are REAL signed JWTs; the `test-token` placeholder's pytest bypass
returns role=admin and would make every merchant claim here vacuous.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

MERCHANT_A = "merchant_A"
MERCHANT_B = "merchant_B"
STORE_B = "store_woo_bbbbbbbb"

_STAFF_ROLES = ("super_admin", "admin", "employee")


@pytest.fixture(scope="module")
def client() -> TestClient:
    import main

    return TestClient(main.app)


def _token(role: str, merchant_id: Optional[str] = None) -> str:
    from utils.auth import create_access_token

    claims: Dict[str, Any] = {
        "sub": f"u-{merchant_id or role}",
        "email": f"{merchant_id or role}@example.com",
        "role": role,
    }
    if merchant_id:
        claims["merchant_id"] = merchant_id
    return create_access_token(claims)


def _auth(role: str, merchant_id: Optional[str] = None) -> Dict[str, str]:
    return {"Authorization": f"Bearer {_token(role, merchant_id)}"}


# (id, method, path, builds the request for a given merchant_id, ownership message)
#
# Every POST body below is VALID for its pydantic model. An invalid one is
# rejected with 422 before the handler runs, which would make the refusal these
# tests assert come from the wrong place entirely.
_ROUTES = [
    (
        "shopify_oauth_start",
        "GET",
        "/integrations/shopify/oauth/start",
        lambda m: {"params": {"shop": "example-shop.myshopify.com", "merchant_id": m}},
        "Can only connect your own store",
    ),
    (
        "shopify_token_diagnostic",
        "GET",
        "/integrations/shopify/token/diagnostic",
        lambda m: {"params": {"merchant_id": m}},
        "Can only access your own merchant",
    ),
    (
        "wix_oauth_start_stub",
        "GET",
        "/integrations/wix/oauth/start",
        lambda m: {"params": {"merchant_id": m}},
        "Can only connect your own store",
    ),
    (
        "custom_connect",
        "POST",
        "/integrations/custom/connect",
        lambda m: {"json": {"merchant_id": m, "store_url": "https://shop.example"}},
        "Can only connect your own store",
    ),
    (
        "shopify_connect",
        "POST",
        "/integrations/shopify/connect",
        lambda m: {
            "json": {"merchant_id": m, "shop_domain": "example-shop.myshopify.com"}
        },
        "Can only connect your own store",
    ),
    (
        "shopify_verify",
        "POST",
        "/integrations/shopify/verify",
        lambda m: {
            "json": {"merchant_id": m, "callback_base_url": "https://api.example"}
        },
        "Can only verify your own store",
    ),
    (
        "shopify_webhook_events",
        "GET",
        "/integrations/shopify/webhooks/events",
        lambda m: {"params": {"merchant_id": m}},
        "Can only access your own merchant",
    ),
    (
        "shopify_products_sync",
        "POST",
        "/integrations/shopify/products/sync",
        lambda m: {"json": {"merchant_id": m}},
        "Can only sync your own store",
    ),
    (
        "wix_connect",
        "POST",
        "/integrations/wix/connect",
        lambda m: {
            "json": {"merchant_id": m, "site_id": "site-x", "api_key": "key-x"}
        },
        "Can only connect your own store",
    ),
    (
        "woocommerce_connect",
        "POST",
        "/integrations/woocommerce/connect",
        lambda m: {
            "json": {
                "merchant_id": m,
                "store_url": "https://woo.example",
                "consumer_key": "ck_x",
                "consumer_secret": "cs_x",
            }
        },
        "Can only connect your own store",
    ),
    (
        "bigcommerce_connect",
        "POST",
        "/integrations/bigcommerce/connect",
        lambda m: {
            "json": {"merchant_id": m, "store_hash": "hash-x", "access_token": "tok-x"}
        },
        "Can only connect your own store",
    ),
    (
        "prestashop_connect",
        "POST",
        "/integrations/prestashop/connect",
        lambda m: {
            "json": {
                "merchant_id": m,
                "store_url": "https://presta.example",
                "api_key": "key-x",
            }
        },
        "Can only connect your own store",
    ),
    (
        "support_email_update",
        "POST",
        "/integrations/stores/support-email",
        lambda m: {"json": {"merchant_id": m, "support_email": "help@example.com"}},
        "Can only update your own store",
    ),
    (
        "support_email_get",
        "GET",
        "/integrations/stores/support-email",
        lambda m: {"params": {"merchant_id": m}},
        "Can only view your own store",
    ),
]

_ROUTE_IDS = [r[0] for r in _ROUTES]


def _call(client: TestClient, method: str, path: str, headers, **kw):
    return client.request(method, path, headers=headers, **kw)


def _ownership_refusal(resp, message: str) -> bool:
    """True when THIS response is the ownership check's own 403.

    Deliberately not `status_code == 403`: the role gate returns 403 too, and a
    refactor that dropped `merchant` from the role list would satisfy a
    status-only assertion while locking every merchant out of their own data.
    """
    return resp.status_code == 403 and message in resp.text


@pytest.mark.parametrize(
    "method,path,build,message",
    [r[1:] for r in _ROUTES],
    ids=_ROUTE_IDS,
)
def test_merchant_is_refused_another_merchants_id(client, method, path, build, message):
    resp = _call(client, method, path, _auth("merchant", MERCHANT_A), **build(MERCHANT_B))

    assert _ownership_refusal(resp, message), (
        f"cross-merchant call was not refused by the ownership check: "
        f"{resp.status_code} {resp.text[:300]}"
    )


@pytest.mark.parametrize(
    "method,path,build,message",
    [r[1:] for r in _ROUTES],
    ids=_ROUTE_IDS,
)
def test_merchant_reaches_their_own_merchant(client, method, path, build, message):
    """The direction a unification is most likely to break."""
    resp = _call(client, method, path, _auth("merchant", MERCHANT_A), **build(MERCHANT_A))

    assert not _ownership_refusal(resp, message), (
        f"merchant was refused their OWN merchant: {resp.status_code} {resp.text[:300]}"
    )


@pytest.mark.parametrize("role", _STAFF_ROLES)
@pytest.mark.parametrize(
    "method,path,build,message",
    [r[1:] for r in _ROUTES],
    ids=_ROUTE_IDS,
)
def test_staff_reach_any_merchant(client, method, path, build, message, role):
    """Staff tokens carry no merchant_id claim at all, so a check written as a
    bare equality against the caller's claim would refuse every one of them."""
    resp = _call(client, method, path, _auth(role), **build(MERCHANT_B))

    assert not _ownership_refusal(resp, message), (
        f"{role} was refused cross-merchant: {resp.status_code} {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# The store_id-keyed route: ownership comes off the fetched row, not the request.
# ---------------------------------------------------------------------------


class _StoreDatabaseSpy:
    """Stands in for routes.merchant_store_connections.database.

    Returns only the columns each query SELECTs -- a double that hands back
    every column regardless cannot see a projection bug, and an ownership check
    reading a column its query never selected refuses the owner.
    """

    def __init__(self, store_owner: str = MERCHANT_B) -> None:
        self.store_owner = store_owner
        self.queries: List[str] = []
        self.executes: List[str] = []

    @staticmethod
    def _project(row: Dict[str, Any], query: str) -> Dict[str, Any]:
        _, _, rest = query.partition("SELECT ")
        columns, _, _ = rest.partition(" FROM ")
        columns = columns.strip()
        if not columns or columns == "*":
            return dict(row)
        wanted = [c.strip().split(" as ")[0].strip() for c in columns.split(",")]
        return {k: v for k, v in row.items() if k in wanted}

    def _row(self) -> Dict[str, Any]:
        return {
            "store_id": STORE_B,
            "merchant_id": self.store_owner,
            "domain": "woo.example",
            "name": "Victim Woo Storefront",
            "platform": "woocommerce",
            "status": "active",
            "support_email": "victim@othercompany.example",
            "api_key": '{"consumer_key": "ck_victim", "consumer_secret": "cs_victim"}',
        }

    async def fetch_one(self, query: str, values: Any = None, *a: Any, **kw: Any):
        flat = " ".join(str(query).split())
        self.queries.append(flat)
        if "merchant_stores" in flat:
            return self._project(self._row(), flat)
        return None

    async def fetch_all(self, query: str, *a: Any, **kw: Any) -> List[Dict[str, Any]]:
        self.queries.append(" ".join(str(query).split()))
        return []

    async def execute(self, query: str, *a: Any, **kw: Any) -> None:
        self.executes.append(" ".join(str(query).split()))
        return None


@pytest.fixture
def store_db(monkeypatch) -> _StoreDatabaseSpy:
    from routes import merchant_store_connections as mod

    spy = _StoreDatabaseSpy()
    monkeypatch.setattr(mod, "database", spy)
    return spy


_ENSURE_PATH = f"/integrations/woocommerce/{STORE_B}/webhooks/ensure"


def test_merchant_cannot_manage_another_merchants_store_by_store_id(client, store_db):
    """`store_id` is caller-supplied and the row is selected on store_id alone,
    so the owning merchant is a property of the ROW. Same shape as the
    cross-tenant hazard fixed in wix_sync.py."""
    resp = client.post(_ENSURE_PATH, headers=_auth("merchant", MERCHANT_A))

    assert _ownership_refusal(resp, "Can only manage your own store"), (
        f"{resp.status_code} {resp.text[:300]}"
    )
    assert not store_db.executes, "refused request still wrote"


def test_merchant_reaches_the_handler_for_their_own_store(client, store_db):
    store_db.store_owner = MERCHANT_A

    resp = client.post(_ENSURE_PATH, headers=_auth("merchant", MERCHANT_A))

    assert not _ownership_refusal(resp, "Can only manage your own store"), resp.text


def test_staff_reach_the_handler_for_any_store(client, store_db):
    resp = client.post(_ENSURE_PATH, headers=_auth("employee"))

    assert not _ownership_refusal(resp, "Can only manage your own store"), resp.text


# ---------------------------------------------------------------------------
# Positive counterparts with teeth: the handler body actually ran.
#
# "not the ownership refusal" above proves the gate opened; these prove what is
# behind it still executes for the callers who are entitled to it. Without
# them, a change that opened the gate and then broke the route would pass.
# ---------------------------------------------------------------------------


def test_own_merchant_read_reaches_the_database(client, store_db):
    resp = client.get(
        "/integrations/shopify/webhooks/events",
        headers=_auth("merchant", MERCHANT_A),
        params={"merchant_id": MERCHANT_A},
    )

    assert resp.status_code == 200, resp.text
    assert store_db.queries, "handler body never queried"
    assert any(MERCHANT_A in q or "merchant_id" in q for q in store_db.queries)


def test_staff_read_reaches_the_database_cross_merchant(client, store_db):
    resp = client.get(
        "/integrations/shopify/webhooks/events",
        headers=_auth("admin"),
        params={"merchant_id": MERCHANT_B},
    )

    assert resp.status_code == 200, resp.text
    assert store_db.queries, "handler body never queried"


def test_own_merchant_support_email_write_reaches_the_database(client, store_db):
    resp = client.post(
        "/integrations/stores/support-email",
        headers=_auth("merchant", MERCHANT_A),
        json={"merchant_id": MERCHANT_A, "support_email": "help@example.com"},
    )

    assert resp.status_code == 200, resp.text
    assert store_db.executes, "handler body never wrote"


def test_staff_support_email_write_reaches_the_database_cross_merchant(client, store_db):
    resp = client.post(
        "/integrations/stores/support-email",
        headers=_auth("employee"),
        json={"merchant_id": MERCHANT_B, "support_email": "help@example.com"},
    )

    assert resp.status_code == 200, resp.text
    assert store_db.executes, "handler body never wrote"


# ---------------------------------------------------------------------------
# Roles the gate must keep out. A unification touches the line right next to
# the role check, so pin that it survived.
# ---------------------------------------------------------------------------


def test_wix_oauth_stub_now_refuses_roles_that_used_to_reach_its_501(client):
    """THE ONE DELIBERATE BEHAVIOUR CHANGE in the unification, pinned.

    GET /integrations/wix/oauth/start was the only site in the module with no
    role gate -- just the merchant comparison -- so an agent or buyer fell
    through to the body. The body is an unconditional 501 (the Wix app is not
    registered yet), so nothing was ever exposed; what changes is the status
    those callers see: 403 instead of 501. This test FAILS against the
    pre-unification file, which is the honest signal that this one line is a
    change and not a rename.

    Uses the shared module client deliberately. An earlier cut opened its own
    `with TestClient(main.app)`, which runs the app's full lifespan inside one
    test of this file -- startup migrations against the test sqlite DB, a
    background reconnect supervisor, and database.disconnect() on the way out.
    It restored the connection state it found, so nothing broke, but no other
    test here needs the lifespan and neither does this one.
    """
    for role in ("agent", "buyer"):
        resp = client.get(
            "/integrations/wix/oauth/start",
            headers=_auth(role),
            params={"merchant_id": MERCHANT_B},
        )
        assert resp.status_code == 403, f"{role}: {resp.status_code} {resp.text[:200]}"
        assert "Not authorized" in resp.text

    # Staff and the owning merchant still reach the stub itself.
    for headers, merchant in (
        (_auth("admin"), MERCHANT_B),
        (_auth("merchant", MERCHANT_A), MERCHANT_A),
    ):
        resp = client.get(
            "/integrations/wix/oauth/start",
            headers=headers,
            params={"merchant_id": merchant},
        )
        assert resp.status_code == 501, resp.text


@pytest.mark.parametrize("role", ("agent", "buyer", "outsourced"))
def test_non_merchant_non_staff_roles_are_refused(client, store_db, role):
    resp = client.post(
        "/integrations/stores/support-email",
        headers=_auth(role),
        json={"merchant_id": MERCHANT_B, "support_email": "help@example.com"},
    )

    assert resp.status_code == 403, resp.text
    assert not store_db.executes, f"refused {role} still wrote"
