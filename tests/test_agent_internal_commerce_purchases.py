"""routes/agent_internal_commerce_purchases.py: the AgenPas gateway's server-to-server purchase read.

On SQLite, over the REAL app. The public route's two auth dependencies are overridden only to build the
body the internal read must equal. No route here may reach the network.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Optional

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, database  # noqa: E402
from db.schema_guard import ensure_required_schema_light  # noqa: E402
from db.buyer_vault import hash_agent_user_ref  # noqa: E402
import db.a2a_rail_ledger as a2a_ledger  # noqa: E402
import db.agent_purchase_ledger as purchases  # noqa: E402
import services.payment_orchestration.purchase_view as purchase_view  # noqa: E402
import db.reap_agentic_ledger as reap_ledger  # noqa: E402
import routes.agent_internal_commerce_purchases as internal_routes  # noqa: E402
from routes.agent_auth import get_agent_context  # noqa: E402
from routes.agent_user_auth import AgentUserContext, get_agent_user_context  # noqa: E402

from main import app  # noqa: E402

_REAL_ASYNC_CLIENT = httpx.AsyncClient

pytestmark = pytest.mark.skipif(IS_POSTGRES, reason="the SQLite arm")

INTERNAL = "/agent/internal/commerce/purchases"
PUBLIC = "/agent/v2/commerce/purchases"
KEY = "internal_test_key_0123456789"
AGENT = "agent_agenpas_internal"
OTHER_AGENT = "agent_agenpas_other"
USER_REF = "user-pat"
EMAIL = "buyer@example.test"
STREET = "1 Main St"


@pytest.fixture(autouse=True)
async def _db():
    if not database.is_connected:
        await database.connect()
    for table in ("agent_purchases", "reap_agentic_purchases", "reap_agentic_enrollments", "a2a_rail_purchases",
                  "a2a_rail_buyer_refs"):
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    await ensure_required_schema_light()
    yield


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(purchases.AGENT_PURCHASE_LEDGER_ENABLED_ENV, "1")
    monkeypatch.setenv(internal_routes.AGENPAS_INTERNAL_READ_ENABLED_ENV, "1")
    monkeypatch.setenv(internal_routes.INTERNAL_KEY_ENV, KEY)
    monkeypatch.delenv("REAP_AGENTIC_ENABLED", raising=False)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    class _NetworkForbidden:
        def __init__(self, *a, **k):
            raise AssertionError("a read route reached the network")

    monkeypatch.setattr(httpx, "AsyncClient", _NetworkForbidden, raising=True)


class _Caller:
    agent_id = AGENT
    agent_user_ref: Optional[str] = USER_REF
    agent_name = "AgenPas internal read test"
    allowed_merchants = None
    session_id = "session_agenpas_internal"

    def can_access_merchant(self, merchant_id: str) -> bool:
        return True


async def _get(url, **kwargs):
    transport = httpx.ASGITransport(app=app)
    async with _REAL_ASYNC_CLIENT(transport=transport, base_url="http://test") as http:
        return await http.get(url, **kwargs)


async def _public(url):
    """The public route's answer for AGENT / USER_REF (its auth dependencies overridden)."""
    caller = _Caller()

    async def _agent():
        return caller

    app.dependency_overrides[get_agent_context] = _agent
    app.dependency_overrides[get_agent_user_context] = lambda: AgentUserContext(agent_user_ref=USER_REF)
    try:
        return await _get(url)
    finally:
        app.dependency_overrides.pop(get_agent_context, None)
        app.dependency_overrides.pop(get_agent_user_context, None)


def _error(resp) -> Optional[str]:
    """The route's reason, out of the app-wide error envelope (`detail.error`), as the public route's
    tests read it."""
    detail = resp.json().get("detail")
    return detail.get("error") if isinstance(detail, dict) else detail


def _envelope(resp):
    """The error body without its per-request metadata (timestamp, request id)."""
    return {k: v for k, v in resp.json().items() if k != "metadata"}


def _headers(key: Optional[str] = KEY, agent: Optional[str] = AGENT, **extra: str):
    headers = dict(extra)
    if key is not None:
        headers["X-Internal-Key"] = key
    if agent is not None:
        headers["X-Agenpas-Agent-Id"] = agent
    return headers


async def _reap(*, agent_id: str = AGENT, user_ref: str = USER_REF, state: str = "resolving",
                **columns: Any) -> str:
    purchase = await reap_ledger.create_purchase(
        buyer_ref="bref_pat", agent_id=agent_id, agent_user_ref_hash=hash_agent_user_ref(user_ref),
        merchant_domain="merchant.example", product_key="pk_1", product_name="Placeholder Serum", quantity=1,
        currency="USD", our_price_minor=4250, buyer_email=EMAIL,
        shipping_address={"addressLine1": STREET, "city": "Austin"},
    )
    updates = {"state": state, **columns}
    assignments = ", ".join(f"{k} = :{k}" for k in updates)
    await database.execute(f"UPDATE reap_agentic_purchases SET {assignments} WHERE id = :id",
                           {**updates, "id": purchase["id"]})
    await purchases.ensure_reap_parent(purchase["id"], basis="backfill")
    return purchase["id"]


def test_the_router_is_mounted_on_the_real_app():
    paths = {getattr(r, "path", None) for r in app.routes}
    assert f"{INTERNAL}/{{purchase_id}}" in paths


async def test_the_happy_path_body_equals_the_public_routes_body_for_the_same_row():
    rail_id = await _reap(state="completed", reap_order_id="ord_merchant_1", final_total_minor=4650)
    public = await _public(f"{PUBLIC}/{rail_id}")
    assert public.status_code == 200
    parent_id = public.json()["purchase_id"]
    for purchase_id in (rail_id, parent_id):
        internal = await _get(f"{INTERNAL}/{purchase_id}", headers=_headers())
        assert internal.status_code == 200, internal.text
        assert internal.json() == public.json()
    text = json.dumps(internal.json())
    for leaked in (EMAIL, STREET, hash_agent_user_ref(USER_REF), "agent_user_ref_hash"):
        assert leaked not in text


async def test_the_gateway_user_ref_is_ignored_for_scoping():
    rail_id = await _reap()
    answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers(**{"X-Agenpas-Agent-User-Ref": "usr_someone_else"}))
    assert answer.status_code == 200 and answer.json()["rail_purchase_id"] == rail_id


@pytest.mark.parametrize("key", [None, "", "wrong-key", KEY + "x", "k\u00e9y".encode("latin-1")])
async def test_a_wrong_or_missing_key_is_401(key):
    rail_id = await _reap()
    answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers(key=key))
    assert answer.status_code == 401 and _error(answer) == "unauthorized"
    assert rail_id not in answer.text


async def test_a_server_without_the_key_refuses_everyone(monkeypatch):
    rail_id = await _reap()
    monkeypatch.delenv(internal_routes.INTERNAL_KEY_ENV)
    for key in (KEY, "", None):
        answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers(key=key))
        assert answer.status_code == 503


async def test_with_the_dial_off_it_is_the_same_404_as_an_unknown_route(monkeypatch):
    rail_id = await _reap()
    monkeypatch.delenv(internal_routes.AGENPAS_INTERNAL_READ_ENABLED_ENV)
    unknown = await _get("/agent/internal/commerce/no-such-route/x", headers=_headers())
    for headers in (_headers(), _headers(key=None), _headers(key="wrong")):
        answer = await _get(f"{INTERNAL}/{rail_id}", headers=headers)
        assert (answer.status_code, _envelope(answer)) == (unknown.status_code, _envelope(unknown))
        assert answer.status_code == 404 and _error(answer) == "Not Found"


async def test_another_agents_purchase_is_not_found_like_no_purchase():
    mine = await _reap()
    theirs = await _reap(agent_id=OTHER_AGENT)
    missing = "rp_" + "0" * 24
    answers = [await _get(f"{INTERNAL}/{pid}", headers=_headers()) for pid in (theirs, missing)]
    assert [(a.status_code, _error(a)) for a in answers] == [(404, "purchase_not_found")] * 2
    assert _envelope(answers[0]) == _envelope(answers[1])
    # and the other way round
    other = await _get(f"{INTERNAL}/{mine}", headers=_headers(agent=OTHER_AGENT))
    assert (other.status_code, _error(other)) == (404, "purchase_not_found")


@pytest.mark.parametrize("purchase_id", ["rp_short", "pp_" + "g" * 24, "xx_" + "0" * 24, "rp_" + "0" * 23 + "%00"])
async def test_malformed_ids_are_not_found(purchase_id):
    answer = await _get(f"{INTERNAL}/{purchase_id}", headers=_headers())
    assert answer.status_code == 404


@pytest.mark.parametrize("agent", [None, "", "agent with spaces", "a" * 129])
async def test_the_agent_id_is_required_and_checked(agent):
    rail_id = await _reap()
    answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers(agent=agent))
    assert answer.status_code == 400 and _error(answer) == "agent_id_required"


async def test_a_dark_ledger_is_not_found(monkeypatch):
    rail_id = await _reap()
    monkeypatch.delenv(purchases.AGENT_PURCHASE_LEDGER_ENABLED_ENV)
    answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert (answer.status_code, _error(answer)) == (404, "purchase_not_found")


async def test_the_read_writes_nothing():
    rail_id = await _reap()
    await database.execute("DELETE FROM agent_purchases")  # a purchase whose parent is missing
    answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert answer.status_code == 404
    assert (await database.fetch_one("SELECT COUNT(*) AS n FROM agent_purchases"))["n"] == 0


async def _a2a(*, agent_id: str = AGENT, state: str = "paying") -> str:
    row = await a2a_ledger.create_purchase(
        agent_id=agent_id, agent_user_ref_hash=hash_agent_user_ref(USER_REF), merchant_domain="merchant.example",
        checkout_url="https://merchant.example/cart/40000000001:1", currency="USD", our_price_minor=4650,
        buyer_email=EMAIL, shipping_address={"addressLine1": STREET, "city": "Austin"})
    await database.execute(
        "UPDATE a2a_rail_purchases SET state = :s, quoted_total_minor = 6348, ceiling_source = 'allowance_v1', "
        "order_id = 'pvt-ap_x', checkout_task_id = 'ckout_internal1' WHERE id = :i", {"s": state, "i": row["id"]})
    await purchases.ensure_a2a_parent(row["id"], basis="backfill")
    return row["id"]


async def test_an_a2a_rail_purchase_reads_the_same_as_on_the_public_route():
    rail_id = await _a2a()
    public = await _public(f"{PUBLIC}/{rail_id}")
    assert public.status_code == 200, public.text
    internal = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert internal.status_code == 200 and internal.json() == public.json()
    body = internal.json()
    assert (body["rail"], body["state"], body["totals"]["ceiling_source"]) == ("a2a", "placing", "allowance_v1")
    text = json.dumps(body)
    for leaked in (EMAIL, STREET, "ckout_internal1", "pvt-ap_x", hash_agent_user_ref(USER_REF)):
        assert leaked not in text
    theirs = await _get(f"{INTERNAL}/{rail_id}", headers=_headers(agent=OTHER_AGENT))
    assert (theirs.status_code, _error(theirs)) == (404, "purchase_not_found")


async def test_a_view_or_ledger_fault_is_a_503_never_a_500(monkeypatch):
    rail_id = await _a2a()
    real_view = purchase_view.unified_purchase

    async def _broken_view(*a, **k):
        raise RuntimeError("view blew up")

    monkeypatch.setattr(purchase_view, "unified_purchase", _broken_view)
    answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert (answer.status_code, _error(answer)) == (503, "temporarily_unavailable")
    monkeypatch.setattr(purchase_view, "unified_purchase", real_view)

    async def _broken_ledger(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(purchases, "get_by_rail_id_for_agent", _broken_ledger)
    answer = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert (answer.status_code, _error(answer)) == (503, "temporarily_unavailable")
