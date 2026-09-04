"""A role gate is not an ownership check.

THE DEFECT, live on prod through 6732e545e. Six routes gated on
`utils.auth.MERCHANT_OR_EMPLOYEE_STAFF_ROLES` / `AGENT_OR_EMPLOYEE_STAFF_ROLES`
-- constants that admit EVERY merchant / EVERY agent, not the owning one --
and then operated on a merchant_id / store_id / agent_id taken straight out of
the request:

  * POST /merchant/onboarding/setup-psp  (employee_store_psp_fixes) -- the
    worst one: merchant_A could overwrite merchant_B's PSP credentials
    (api_key/secret_key/account_id). The only check on the target was "does
    this merchant exist".
  * POST /integrations/wix/connect-sync  (wix_sync) -- repoint another
    merchant's Wix store at the caller's site_id + api_key.
  * POST /integrations/wix/test          (wix_sync) -- read another merchant's
    store name / site_id, exercising THEIR stored api_key against Wix.
  * POST /merchant/integrations/{wix,woocommerce,bigcommerce}/sync (wix_sync,
    via _sync_connected_platform_products) -- keyed on an arbitrary store_id.
  * GET  /merchant/integrations/sync-status (wix_sync) -- same store_id hazard.
  * GET  /agents/{agent_id}              (agent_management) -- carried a
    comment saying "No additional restrictions needed since sensitive data is
    filtered". Only api_key/api_key_hash are stripped; owner_email,
    webhook_url, allowed_merchants, metadata and quotas were not.

THE RULE: the role constant decides who may ATTEMPT the route.
`utils.auth.can_access_merchant` / `can_access_agent` decides WHOSE record they
get -- staff (super_admin/admin/employee) keep cross-tenant access, a
`merchant`/`agent` is confined to their own. This is the shape
routes/merchant_onboarding_shopify_verify_routes.py already uses.

Every test here signs a REAL JWT (never the `test-token` placeholder, whose
pytest bypass in utils.auth returns role=admin and would make every claim
vacuous) and asserts BOTH directions: the cross-tenant caller is refused
*before the sensitive action happens*, and the staff / own-record callers still
reach it. The spies are what make "before" checkable -- a 403 that arrives
after the credential was already written would pass a status-only assertion.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

MERCHANT_A = "merchant_A"
MERCHANT_B = "merchant_B"
AGENT_A = "agent_A"
AGENT_B = "agent_B"
STORE_B = "store_wix_bbbbbbbb"


@pytest.fixture(scope="module")
def client() -> TestClient:
    import main

    return TestClient(main.app)


def _merchant_token(merchant_id: str) -> str:
    from utils.auth import create_access_token

    return create_access_token(
        {
            "sub": f"u-{merchant_id}",
            "email": f"{merchant_id}@example.com",
            "role": "merchant",
            "merchant_id": merchant_id,
        }
    )


def _staff_token(role: str = "super_admin") -> str:
    from utils.auth import create_access_token

    return create_access_token(
        {"sub": f"u-{role}", "email": f"{role}@example.com", "role": role}
    )


def _agent_token(agent_id: str) -> str:
    from utils.auth import create_access_token

    return create_access_token(
        {
            "sub": f"u-{agent_id}",
            "email": f"{agent_id}@example.com",
            "role": "agent",
            "agent_id": agent_id,
        }
    )


def _auth(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. POST /merchant/onboarding/setup-psp -- cross-tenant PSP CREDENTIAL WRITE
# ---------------------------------------------------------------------------


class _PspDatabaseSpy:
    """Stands in for routes.employee_store_psp_fixes.database.

    The merchant-exists lookup answers YES (that lookup was the handler's only
    guard on the target, so a spy that answered None would make the defect
    untestable), and the existing-PSP lookup answers None so the handler takes
    its INSERT path.
    """

    def __init__(self) -> None:
        self.queries: List[str] = []

    async def fetch_one(self, query: str, values: Any = None, *a: Any, **kw: Any):
        self.queries.append(" ".join(str(query).split()))
        if "merchant_onboarding" in str(query):
            return {"merchant_id": (values or {}).get("merchant_id")}
        return None

    async def execute(self, query: str, *a: Any, **kw: Any) -> None:
        self.queries.append(" ".join(str(query).split()))
        return None


class _PersistSpy:
    """Stands in for the canonical PSP writer -- the sensitive action itself."""

    def __init__(self) -> None:
        self.writes: List[Dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Dict[str, Any]:
        self.writes.append(kwargs)
        return {"psp_id": kwargs.get("psp_id") or "psp_stub"}


@pytest.fixture
def psp_spies(monkeypatch) -> Tuple[_PspDatabaseSpy, _PersistSpy]:
    from routes import employee_store_psp_fixes as mod

    db = _PspDatabaseSpy()
    persist = _PersistSpy()
    monkeypatch.setattr(mod, "database", db)
    monkeypatch.setattr(mod, "persist_canonical_merchant_psp", persist)
    return db, persist


def _setup_psp(client: TestClient, token: str, merchant_id: str):
    return client.post(
        "/merchant/onboarding/setup-psp",
        headers=_auth(token),
        json={
            "merchant_id": merchant_id,
            "psp_type": "stripe",
            "api_key": "sk_test_attacker_key",
            "test_mode": True,
        },
    )


def test_merchant_cannot_overwrite_another_merchants_psp_credentials(client, psp_spies):
    """The headline defect. Kills: deleting the can_access_merchant call, or
    moving it after the write."""
    _db, persist = psp_spies

    resp = _setup_psp(client, _merchant_token(MERCHANT_A), MERCHANT_B)

    assert resp.status_code == 403, (
        f"merchant_A wrote a PSP row for merchant_B: {resp.status_code} {resp.text}"
    )
    # The status alone is not the claim: a 403 raised after persist ran would
    # still have overwritten merchant_B's payment credentials.
    assert not persist.writes, (
        f"refused request still wrote PSP credentials: {persist.writes}"
    )


def test_merchant_can_still_set_up_their_own_psp(client, psp_spies):
    """Positive counterpart. Kills a 'fix' that simply refuses every merchant --
    merchant self-service during onboarding is the reason `merchant` is in the
    role list at all."""
    _db, persist = psp_spies

    resp = _setup_psp(client, _merchant_token(MERCHANT_A), MERCHANT_A)

    assert resp.status_code == 200, resp.text
    assert [w["merchant_id"] for w in persist.writes] == [MERCHANT_A]


@pytest.mark.parametrize("role", ("super_admin", "admin", "employee"))
def test_staff_keep_cross_merchant_psp_setup(client, psp_spies, role):
    """Staff configuring a PSP on a merchant's behalf is the employee portal's
    whole job. Kills an over-tight fix that binds staff to a merchant_id claim
    they do not carry."""
    _db, persist = psp_spies

    resp = _setup_psp(client, _staff_token(role), MERCHANT_B)

    assert resp.status_code == 200, f"{role} was refused: {resp.text}"
    assert [w["merchant_id"] for w in persist.writes] == [MERCHANT_B]


# ---------------------------------------------------------------------------
# 2. routes/wix_sync.py -- four routes, one hazard
# ---------------------------------------------------------------------------


def _victim_store(merchant_id: str = MERCHANT_B) -> Dict[str, Any]:
    return {
        "store_id": STORE_B,
        "merchant_id": merchant_id,
        "name": "Victim Wix Storefront",
        "domain": "victim-site-id",
        "api_key": "wix_victim_api_key",
        "platform": "wix",
        "status": "active",
        "last_sync": None,
        "product_count": 7,
    }


def _project(row: Dict[str, Any], query: str) -> Dict[str, Any]:
    """Return only the columns the query actually SELECTs.

    A double that hands back every column no matter what was asked for cannot
    see a projection bug: the first cut of these tests did exactly that, and a
    mutant deleting `merchant_id` from the sync-status SELECT survived -- the
    very mistake this fix had to avoid, since an ownership check reading a
    column the query never returns 403s the owner. Modelling the projection is
    what makes test_merchant_can_still_poll_their_own_sync_status load-bearing.
    """
    select, _, rest = query.partition("SELECT ")
    columns, _, _ = rest.partition(" FROM ")
    columns = columns.strip()
    if not columns or columns == "*":
        return dict(row)
    wanted = [c.strip().split(" as ")[0].strip() for c in columns.split(",")]
    return {k: v for k, v in row.items() if k in wanted}


class _WixDatabaseSpy:
    """Stands in for routes.wix_sync.database.

    `store_owner` is what the row in merchant_stores actually belongs to; tests
    flip it to make the same request either cross-tenant or self-service.
    """

    def __init__(self, store_owner: str = MERCHANT_B) -> None:
        self.store_owner = store_owner
        self.queries: List[str] = []
        self.executes: List[str] = []

    async def fetch_one(self, query: str, values: Any = None, *a: Any, **kw: Any):
        flat = " ".join(str(query).split())
        self.queries.append(flat)
        if "merchant_onboarding" in flat:
            return {"merchant_id": (values or {}).get("merchant_id")}
        if "merchant_stores" in flat:
            return _project(_victim_store(self.store_owner), flat)
        return None

    async def execute(self, query: str, *a: Any, **kw: Any) -> None:
        self.executes.append(" ".join(str(query).split()))
        return None


class _WixValidationSpy:
    """Stands in for services.wix_connection.validate_wix_catalog_access.

    Records the api_key it was handed: on /integrations/wix/test the handler
    passes the STORED key, so a recorded victim key is proof the route
    exercised another merchant's credential.
    """

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str]] = []

    async def __call__(self, site_id: str, api_key: str) -> Dict[str, Any]:
        self.calls.append((site_id, api_key))
        return {"site_id": site_id, "api_key": api_key}


class _SyncProductsSpy:
    """Stands in for routes.product_sync.sync_products (imported inside the
    handler, so it must be patched on the defining module)."""

    class _Result:
        status = "success"
        message = "ok"
        products_synced = 3
        platform = "wix"
        sync_time = "2026-09-04T00:00:00Z"

    def __init__(self) -> None:
        self.calls: List[str] = []

    async def __call__(self, request: Any, background_tasks: Any, current_user: Any):
        self.calls.append(request.merchant_id)
        return self._Result()


@pytest.fixture
def wix_spies(monkeypatch):
    from routes import product_sync, wix_sync as mod

    db = _WixDatabaseSpy()
    validate = _WixValidationSpy()
    sync = _SyncProductsSpy()
    monkeypatch.setattr(mod, "database", db)
    monkeypatch.setattr(mod, "validate_wix_catalog_access", validate)
    monkeypatch.setattr(product_sync, "sync_products", sync)
    return db, validate, sync


def _connect_wix(client: TestClient, token: str, merchant_id: str):
    return client.post(
        "/integrations/wix/connect-sync",
        headers=_auth(token),
        params={
            "merchant_id": merchant_id,
            "api_key": "attacker_wix_key",
            "site_id": "attacker-site-id",
            "store_name": "Attacker Store",
        },
    )


def test_merchant_cannot_hijack_another_merchants_wix_connection(client, wix_spies):
    """Kills: deleting the ownership check on connect-sync. The refusal must
    land before any write -- the UPDATE branch replaces the victim's api_key."""
    db, validate, _sync = wix_spies

    resp = _connect_wix(client, _merchant_token(MERCHANT_A), MERCHANT_B)

    assert resp.status_code == 403, f"store hijack succeeded: {resp.text}"
    assert not db.executes, f"refused request still wrote merchant_stores: {db.executes}"
    assert not validate.calls, "refused request still reached the Wix API"


def test_merchant_can_still_connect_their_own_wix_store(client, wix_spies):
    db, _validate, _sync = wix_spies

    resp = _connect_wix(client, _merchant_token(MERCHANT_A), MERCHANT_A)

    assert resp.status_code == 200, resp.text
    assert db.executes, "own-store connect never wrote merchant_stores"


def test_staff_keep_cross_merchant_wix_connect(client, wix_spies):
    db, _validate, _sync = wix_spies

    resp = _connect_wix(client, _staff_token(), MERCHANT_B)

    assert resp.status_code == 200, resp.text
    assert db.executes


def test_merchant_cannot_test_another_merchants_wix_connection(client, wix_spies):
    """Leaked the victim's store name and site_id, and called Wix with the
    victim's stored api_key. Kills deletion of the ownership check."""
    _db, validate, _sync = wix_spies

    resp = client.post(
        "/integrations/wix/test",
        headers=_auth(_merchant_token(MERCHANT_A)),
        params={"merchant_id": MERCHANT_B},
    )

    assert resp.status_code == 403, resp.text
    assert "Victim Wix Storefront" not in resp.text
    assert not validate.calls, (
        f"refused request still spent the victim's credential: {validate.calls}"
    )


def test_merchant_can_still_test_their_own_wix_connection(client, wix_spies):
    _db, validate, _sync = wix_spies

    resp = client.post(
        "/integrations/wix/test",
        headers=_auth(_merchant_token(MERCHANT_A)),
        params={"merchant_id": MERCHANT_A},
    )

    assert resp.status_code == 200, resp.text
    assert validate.calls, "own-store test never reached the Wix API"


def test_staff_keep_cross_merchant_wix_test(client, wix_spies):
    _db, validate, _sync = wix_spies

    resp = client.post(
        "/integrations/wix/test",
        headers=_auth(_staff_token()),
        params={"merchant_id": MERCHANT_B},
    )

    assert resp.status_code == 200, resp.text
    assert validate.calls


@pytest.mark.parametrize("platform", ("wix", "woocommerce", "bigcommerce"))
def test_merchant_cannot_sync_another_merchants_store_by_store_id(
    client, wix_spies, platform
):
    """All three sync POSTs funnel through _sync_connected_platform_products,
    so the hazard is shared; the parametrize proves the fix sits in the shared
    helper and not in one route."""
    _db, _validate, sync = wix_spies

    resp = client.post(
        f"/merchant/integrations/{platform}/sync",
        headers=_auth(_merchant_token(MERCHANT_A)),
        params={"store_id": STORE_B, "wait": True},
    )

    assert resp.status_code == 403, resp.text
    assert "Victim Wix Storefront" not in resp.text
    assert not sync.calls, f"refused request still ran a sync: {sync.calls}"


def test_merchant_can_still_sync_their_own_store_by_store_id(client, wix_spies):
    db, _validate, sync = wix_spies
    db.store_owner = MERCHANT_A

    resp = client.post(
        "/merchant/integrations/wix/sync",
        headers=_auth(_merchant_token(MERCHANT_A)),
        params={"store_id": STORE_B, "wait": True},
    )

    assert resp.status_code == 200, resp.text
    assert sync.calls == [MERCHANT_A]


def test_staff_keep_cross_merchant_store_sync(client, wix_spies):
    _db, _validate, sync = wix_spies

    resp = client.post(
        "/merchant/integrations/wix/sync",
        headers=_auth(_staff_token()),
        params={"store_id": STORE_B, "wait": True},
    )

    assert resp.status_code == 200, resp.text
    assert sync.calls == [MERCHANT_B]


def test_merchant_cannot_poll_another_merchants_sync_status(client, wix_spies):
    """The poll response carries the victim's store name, status, last_sync and
    product_count."""
    resp = client.get(
        "/merchant/integrations/sync-status",
        headers=_auth(_merchant_token(MERCHANT_A)),
        params={"platform": "wix", "store_id": STORE_B},
    )

    assert resp.status_code == 403, resp.text
    assert "Victim Wix Storefront" not in resp.text


def test_merchant_can_still_poll_their_own_sync_status(client, wix_spies):
    """Kills the near-miss version of this fix: the store_id branch did not
    SELECT merchant_id, so an ownership check reading that column would 403
    every merchant -- including on their own store."""
    db, _validate, _sync = wix_spies
    db.store_owner = MERCHANT_A

    resp = client.get(
        "/merchant/integrations/sync-status",
        headers=_auth(_merchant_token(MERCHANT_A)),
        params={"platform": "wix", "store_id": STORE_B},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["store_id"] == STORE_B


def test_merchant_without_store_id_still_polls_via_their_own_merchant_claim(client, wix_spies):
    """The other branch of both wix routes: no store_id, so the row is looked
    up by the caller's OWN merchant_id claim. It was never the vulnerable path,
    and the ownership check must not have broken it."""
    db, _validate, _sync = wix_spies
    db.store_owner = MERCHANT_A

    resp = client.get(
        "/merchant/integrations/sync-status",
        headers=_auth(_merchant_token(MERCHANT_A)),
        params={"platform": "wix"},
    )

    assert resp.status_code == 200, resp.text
    assert "store_id = :store_id" not in " ".join(db.queries), (
        "the no-store_id branch should have keyed on merchant_id"
    )


def test_merchant_without_store_id_still_syncs_their_own_store(client, wix_spies):
    db, _validate, sync = wix_spies
    db.store_owner = MERCHANT_A

    resp = client.post(
        "/merchant/integrations/wix/sync",
        headers=_auth(_merchant_token(MERCHANT_A)),
        params={"wait": True},
    )

    assert resp.status_code == 200, resp.text
    assert sync.calls == [MERCHANT_A]


def test_staff_keep_cross_merchant_sync_status(client, wix_spies):
    resp = client.get(
        "/merchant/integrations/sync-status",
        headers=_auth(_staff_token()),
        params={"platform": "wix", "store_id": STORE_B},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["store_name"] == "Victim Wix Storefront"


# ---------------------------------------------------------------------------
# 3. GET /agents/{agent_id} -- cross-agent record read
# ---------------------------------------------------------------------------

_VICTIM_AGENT = {
    "agent_id": AGENT_B,
    "agent_name": "Victim Agent",
    "owner_email": "victim@othercompany.example",
    "webhook_url": "https://victim.example/hooks/pivota",
    "allowed_merchants": ["merchant_secret_1", "merchant_secret_2"],
    "metadata": {"contract": "victim-only"},
    "rate_limit": 100,
    "daily_quota": 10000,
}


class _GetAgentSpy:
    def __init__(self) -> None:
        self.calls: List[str] = []

    async def __call__(self, agent_id: str) -> Optional[Dict[str, Any]]:
        self.calls.append(agent_id)
        record = dict(_VICTIM_AGENT)
        record["agent_id"] = agent_id
        if agent_id == AGENT_A:
            record["owner_email"] = "agent_A@example.com"
        return record


@pytest.fixture
def agent_spy(monkeypatch) -> _GetAgentSpy:
    from routes import agent_management as mod

    spy = _GetAgentSpy()
    monkeypatch.setattr(mod, "get_agent", spy)
    return spy


def test_agent_cannot_read_another_agents_record(client, agent_spy):
    """Kills restoring "No additional restrictions needed since sensitive data
    is filtered". Redaction of api_key is not an ownership check: the fields
    asserted here are exactly what stayed in the response."""
    resp = client.get(f"/agents/{AGENT_B}", headers=_auth(_agent_token(AGENT_A)))

    assert resp.status_code == 403, f"agent_A read agent_B: {resp.text}"
    body = resp.text
    for leaked in (
        "victim@othercompany.example",
        "https://victim.example/hooks/pivota",
        "merchant_secret_1",
    ):
        assert leaked not in body, f"403 response still leaked {leaked}"


def test_agent_can_still_read_their_own_record(client, agent_spy):
    resp = client.get(f"/agents/{AGENT_A}", headers=_auth(_agent_token(AGENT_A)))

    assert resp.status_code == 200, resp.text
    assert resp.json()["agent"]["agent_id"] == AGENT_A


def test_agent_identified_only_by_user_id_still_reads_their_own_record(client, agent_spy):
    """Some agent tokens carry the identity as `user_id`, not `agent_id` -- the
    sibling guards in agent_management.py all honour that fallback. Kills a fix
    that uses can_access_agent alone and locks those agents out of their own
    record."""
    from utils.auth import create_access_token

    token = create_access_token(
        {
            "sub": AGENT_A,
            "email": "agent_A@example.com",
            "role": "agent",
            "user_id": AGENT_A,
        }
    )

    resp = client.get(f"/agents/{AGENT_A}", headers=_auth(token))

    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("role", ("super_admin", "admin", "employee"))
def test_staff_keep_cross_agent_reads(client, agent_spy, role):
    resp = client.get(f"/agents/{AGENT_B}", headers=_auth(_staff_token(role)))

    assert resp.status_code == 200, f"{role} was refused: {resp.text}"
    assert resp.json()["agent"]["agent_id"] == AGENT_B


def test_non_agent_non_staff_roles_are_still_refused(client, agent_spy):
    """The role gate must survive the ownership fix."""
    from utils.auth import create_access_token

    token = create_access_token(
        {"sub": "u-b", "email": "b@example.com", "role": "buyer"}
    )
    resp = client.get(f"/agents/{AGENT_B}", headers=_auth(token))

    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# 4. The pairing itself, pinned in source.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "rel", ("routes/wix_sync.py", "routes/employee_store_psp_fixes.py")
)
def test_merchant_role_gates_in_the_fixed_modules_are_paired_with_ownership(rel):
    """Every MERCHANT_OR_EMPLOYEE_STAFF_ROLES gate in these two modules must be
    matched by a can_access_merchant call.

    Not a general law -- a route that only ever uses the caller's OWN
    merchant_id claim needs no such call. It is a ratchet on the two modules
    this change fixed, where every gate does read an identifier out of the
    request. A new handler here that gates on role alone is the bug returning;
    if it genuinely takes no request-supplied merchant_id/store_id, move it or
    amend this list deliberately.
    """
    source = (_REPO_ROOT / rel).read_text()
    gates = source.count("MERCHANT_OR_EMPLOYEE_STAFF_ROLES:")
    checks = source.count("can_access_merchant(")

    assert checks >= gates, (
        f"{rel}: {gates} role gate(s) but only {checks} ownership check(s) -- a "
        "role gate says who may attempt the route, never whose row they get"
    )


def test_auth_constants_do_not_claim_to_be_ownership_checks():
    """The comments on both constants asserted a property the call sites did
    not have. Kills a revert to wording that reads as a guarantee."""
    source = (_REPO_ROOT / "utils" / "auth.py").read_text()
    head, _, _ = source.partition("AGENT_OR_EMPLOYEE_STAFF_ROLES = ")

    _, _, merchant_note = head.partition("MERCHANT_OR_EMPLOYEE_STAFF_ROLES = ")
    assert "can_access_merchant" in merchant_note or "can_access_merchant" in head, (
        "MERCHANT_OR_EMPLOYEE_STAFF_ROLES is documented without naming the "
        "ownership check its call sites must pair with"
    )
    assert "can_access_agent" in source.split("AGENT_OR_EMPLOYEE_STAFF_ROLES = ")[0], (
        "AGENT_OR_EMPLOYEE_STAFF_ROLES is documented without naming "
        "can_access_agent"
    )
