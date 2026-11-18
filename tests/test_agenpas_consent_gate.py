"""The AgenPas consent gate (AGENPAS_CONSENT_GATE) on every surface that hands out a rail's hosted URL.

On SQLite, over the REAL app. Only the agent auth dependencies are overridden. No route here may reach the
network. The same fixture purchases (both rails, every state that matters) are read through:

  door      GET /agent/v2/commerce/purchases/{id} and the list
  reap      GET /agent/v2/commerce/reap/purchases/{id} and the list (create / resume / recover answer with
            the same `_owner_view` / `_owner_public_body`)
  internal  GET /agent/internal/commerce/purchases/{id} (the AgenPas read)
  events    the purchase.* webhook payloads services/payment_orchestration/purchase_events.detect writes

GOLDENS. tests/golden/agenpas_consent_gate_off.json holds every surface's bytes as origin/main
(2eee95cf8) answered them, RECORDED BEFORE THIS CHANGE TOUCHED ANY SOURCE (AGENPAS_GOLDEN_RECORD=1
writes it). With every new flag off the branch must answer the same bytes. Only random ids are replaced by
stable names before the comparison; nothing else is normalised. Re-record only on a commit whose old bodies
you can show, never to make a red run green.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, database  # noqa: E402
from db.schema_guard import ensure_required_schema_light  # noqa: E402
from db.buyer_vault import hash_agent_user_ref  # noqa: E402
import db.a2a_rail_ledger as a2a_ledger  # noqa: E402
import db.agent_purchase_consents as consents_db  # noqa: E402
import db.agent_purchase_ledger as purchases  # noqa: E402
import db.reap_agentic_ledger as reap_ledger  # noqa: E402
import routes.agent_internal_commerce_purchases as internal_routes  # noqa: E402
from routes.agent_auth import get_agent_context  # noqa: E402
from routes.agent_user_auth import AgentUserContext, get_agent_user_context  # noqa: E402
from services.payment_orchestration import purchase_consent, purchase_events  # noqa: E402

from main import app  # noqa: E402

_REAL_ASYNC_CLIENT = httpx.AsyncClient

pytestmark = pytest.mark.skipif(IS_POSTGRES, reason="the SQLite arm; tests/test_agenpas_consent_gate_postgres.py is Postgres")

_REPO = Path(__file__).resolve().parents[1]
GOLDEN_OFF = _REPO / "tests/golden/agenpas_consent_gate_off.json"
_MOUNTED_AT_IMPORT = any(getattr(r, "path", "").startswith("/agent/internal/commerce/purchases") for r in app.routes)
_WRITE_MOUNTED_AT_IMPORT = any(getattr(r, "path", "").endswith("/consents") for r in app.routes)

DOOR = "/agent/v2/commerce/purchases"
REAP = "/agent/v2/commerce/reap/purchases"
INTERNAL = "/agent/internal/commerce/purchases"
READ_KEY = "agenpas_read_test_key_0123456789abcdef"
AGENT = "agent_consent_gate"
USER_REF = "user-consent-gate"
OWNER = hash_agent_user_ref(USER_REF)
EMAIL = "buyer@example.test"
STREET = "1 Placeholder Way"
A2A_HOST = "pay.rail.example"
FIXED = "2026-11-01 00:00:00"
FUTURE = "2999-01-01 00:00:00"

#: Every hosted URL a fixture purchase carries. A gated body must contain none of them, anywhere.
URLS = {
    "R_enroll": "https://pay.prava.space/enroll/golden1",
    "R_approve": "https://pay.prava.space/approve/golden1",
    "A_bind": f"https://{A2A_HOST}/bind/golden1",
    "A_approve": f"https://{A2A_HOST}/approve/golden1",
    "A_3ds": f"https://{A2A_HOST}/3ds/golden1",
}


@pytest.fixture(autouse=True)
async def _db():
    if not database.is_connected:
        await database.connect()
    for table in ("agent_purchase_events", "agent_purchases", "reap_agentic_purchases", "reap_agentic_enrollments",
                  "a2a_rail_purchases", "a2a_rail_buyer_refs", "agent_purchase_consents"):
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    await ensure_required_schema_light()
    yield


@pytest.fixture(autouse=True)
def _mounted(request):
    """The internal read as an app built with its dial ON registers it (see its own tests). Tests named
    `..._dial_off_at_build_...` see main.app exactly as it was built (dials off in CI)."""
    if "_dial_off_at_build_" in request.node.name or (_MOUNTED_AT_IMPORT and _WRITE_MOUNTED_AT_IMPORT):
        yield
        return
    before = list(app.router.routes)
    app.include_router(internal_routes.router)
    # The consent write as an app built with ITS dial on registers it; its own dial still answers per request.
    app.include_router(internal_routes.consent_router)
    added = [r for r in app.router.routes if r not in before]
    app.router.routes[:] = added + before
    app.openapi_schema = None
    try:
        yield
    finally:
        app.router.routes[:] = before
        app.openapi_schema = None


#: Every new flag this change adds. Off (unset) unless a test sets one.
NEW_FLAGS = (
    "AGENPAS_CONSENT_GATE", "AGENPAS_CONSENT_WRITE_ENABLED", "AGENPAS_TERMS_URL", "AGENPAS_TERMS_VERSION",
    "AGENPAS_PRIVACY_URL", "AGENPAS_PRIVACY_VERSION", "AGENPAS_CONSENT_OFFER_SECRET",
    "AGENPAS_CONSENT_MIN_DWELL_SECONDS", "AGENPAS_INTERNAL_CONSENT_KEY",
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in NEW_FLAGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(purchases.AGENT_PURCHASE_LEDGER_ENABLED_ENV, "1")
    monkeypatch.setenv(internal_routes.AGENPAS_INTERNAL_READ_ENABLED_ENV, "1")
    monkeypatch.setenv(internal_routes.INTERNAL_KEY_ENV, READ_KEY)
    monkeypatch.setenv(purchase_events.AGENT_PURCHASE_WEBHOOKS_ENABLED_ENV, "1")
    # The Reap rail live (fake, test-only values) so its list answers; nothing here calls it.
    monkeypatch.setenv("REAP_AGENTIC_ENABLED", "1")
    monkeypatch.setenv("REAP_API_BASE_URL", "https://sandbox.api.reap.global")
    monkeypatch.setenv("REAP_API_KEY", "sk_test_key")
    monkeypatch.setenv("A2A_RAIL_HOSTED_HOST_SHA256", hashlib.sha256(A2A_HOST.encode()).hexdigest())


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    class _NetworkForbidden:
        def __init__(self, *a, **k):
            raise AssertionError("a read reached the network")

    monkeypatch.setattr(httpx, "AsyncClient", _NetworkForbidden, raising=True)


class _Caller:
    agent_id = AGENT
    agent_user_ref: Optional[str] = USER_REF
    agent_name = "Consent gate test"
    allowed_merchants = None
    session_id = "session_consent_gate"

    def can_access_merchant(self, merchant_id: str) -> bool:
        return True


async def _request(method: str, url: str, *, agent: bool = True, **kwargs) -> httpx.Response:
    if agent:
        async def _agent():
            return _Caller()

        app.dependency_overrides[get_agent_context] = _agent
        app.dependency_overrides[get_agent_user_context] = lambda: AgentUserContext(agent_user_ref=USER_REF)
    try:
        transport = httpx.ASGITransport(app=app)
        async with _REAL_ASYNC_CLIENT(transport=transport, base_url="http://test") as http:
            return await http.request(method, url, **kwargs)
    finally:
        app.dependency_overrides.pop(get_agent_context, None)
        app.dependency_overrides.pop(get_agent_user_context, None)


async def _internal(purchase_id: str) -> httpx.Response:
    return await _request("GET", f"{INTERNAL}/{purchase_id}", agent=False,
                          headers={"X-Internal-Key": READ_KEY, "X-Agenpas-Agent-Id": AGENT})


async def _fix_timestamps(table: str, row_id: str, keep: tuple) -> None:
    """Every timestamp a fixture row holds, set to one fixed instant, so the bodies are the same bytes on
    every run. `state_entered_at` stays live (the events job's lookback reads it); `keep` are set by hand."""
    columns = [r["name"] for r in await database.fetch_all(f"SELECT name FROM pragma_table_info('{table}')")]
    row = dict(await database.fetch_one(f"SELECT * FROM {table} WHERE id = :i", {"i": row_id}))
    fixed = [c for c in columns if c.endswith("_at") and row.get(c) is not None
             and c not in keep and c not in ("state_entered_at", "next_poll_at")]
    if fixed:
        assignments = ", ".join(f"{c} = :fixed" for c in fixed)
        await database.execute(f"UPDATE {table} SET {assignments} WHERE id = :i", {"i": row_id, "fixed": FIXED})


async def _reap(name: str, state: str, **columns: Any) -> str:
    purchase = await reap_ledger.create_purchase(
        buyer_ref="bref_consent_gate", agent_id=AGENT, agent_user_ref_hash=OWNER,
        merchant_domain="merchant.example", product_key="pk_1", product_name="Placeholder Serum", quantity=1,
        currency="USD", our_price_minor=4250, buyer_email=EMAIL,
        shipping_address={"addressLine1": STREET, "city": "Placeholder City"},
    )
    rp = purchase["id"]
    updates = {"state": state, **columns}
    assignments = ", ".join(f"{k} = :{k}" for k in updates)
    await database.execute(f"UPDATE reap_agentic_purchases SET {assignments} WHERE id = :id", {**updates, "id": rp})
    await _fix_timestamps("reap_agentic_purchases", rp, keep=tuple(k for k in columns if k.endswith("_at")))
    return rp


async def _a2a(state: str, **columns: Any) -> str:
    row = await a2a_ledger.create_purchase(
        agent_id=AGENT, agent_user_ref_hash=OWNER, merchant_domain="merchant.example",
        checkout_url="https://merchant.example/cart/40000000001:1", currency="USD", our_price_minor=4650,
        buyer_email=EMAIL, shipping_address={"addressLine1": STREET, "city": "Placeholder City"})
    updates = {"state": state, **columns}
    assignments = ", ".join(f"{k} = :{k}" for k in updates)
    await database.execute(f"UPDATE a2a_rail_purchases SET {assignments} WHERE id = :id", {**updates, "id": row["id"]})
    await _fix_timestamps("a2a_rail_purchases", row["id"], keep=tuple(k for k in columns if k.endswith("_at")))
    return row["id"]


async def _fixtures() -> Dict[str, str]:
    """name -> rail purchase id. Every state of both rails a hosted URL can ride on, and their neighbours."""
    ids: Dict[str, str] = {}
    ids["R_resolving"] = await _reap("R_resolving", "resolving")
    ids["R_enroll"] = await _reap("R_enroll", "needs_enrollment")
    await database.execute(
        "INSERT INTO reap_agentic_enrollments (id, buyer_ref, agent_id, status, hosted_url, hosted_url_expires_at, "
        "created_at, updated_at) VALUES ('re_consent_gate_1', 'bref_consent_gate', :a, 'pending', :u, :f, :c, :c)",
        {"a": AGENT, "u": URLS["R_enroll"], "f": FUTURE, "c": FIXED})
    await database.execute(
        "UPDATE reap_agentic_purchases SET enrollment_id = 're_consent_gate_1', hosted_url = :u, "
        "hosted_url_expires_at = :f WHERE id = :i", {"u": URLS["R_enroll"], "f": FUTURE, "i": ids["R_enroll"]})
    ids["R_quoting"] = await _reap("R_quoting", "quoting", quoted_total_minor=4650)
    ids["R_approve"] = await _reap("R_approve", "awaiting_approval", hosted_url=URLS["R_approve"],
                                   hosted_url_expires_at=FUTURE, reap_quote_expires_at=FUTURE, quoted_total_minor=4650)
    ids["R_processing"] = await _reap("R_processing", "processing", quoted_total_minor=4650)
    ids["R_completed"] = await _reap("R_completed", "completed", reap_order_id="ord_merchant_1",
                                     final_total_minor=4650)
    ids["A_bind"] = await _a2a("needs_payment_method", buyer_action_kind="card_binding",
                               buyer_action_url=URLS["A_bind"], buyer_action_expires_at=FUTURE)
    ids["A_quoting"] = await _a2a("quoting")
    ids["A_approve"] = await _a2a("awaiting_authorization", quoted_total_minor=6348, ceiling_source="allowance_v1",
                                  buyer_action_kind="approval", buyer_action_url=URLS["A_approve"],
                                  buyer_action_expires_at=FUTURE)
    ids["A_3ds"] = await _a2a("paying", quoted_total_minor=6348, buyer_action_kind="authenticate",
                              buyer_action_url=URLS["A_3ds"], buyer_action_expires_at=FUTURE)
    ids["A_completed"] = await _a2a("completed", quoted_total_minor=6348, final_total_minor=6348,
                                    merchant_order_id="mord_1")
    for name, rail_id in ids.items():
        if rail_id.startswith("rp_"):
            await purchases.ensure_reap_parent(rail_id, basis="backfill")
        else:
            await purchases.ensure_a2a_parent(rail_id, basis="backfill")
    # Distinct, fixed creation instants: the lists are newest first.
    for n, rail_id in enumerate(ids.values()):
        stamp = f"2026-11-01 00:{n:02d}:00"
        await database.execute("UPDATE agent_purchases SET created_at = :c WHERE rail_purchase_id = :i",
                               {"c": stamp, "i": rail_id})
        table = "reap_agentic_purchases" if rail_id.startswith("rp_") else "a2a_rail_purchases"
        await database.execute(f"UPDATE {table} SET created_at = :c WHERE id = :i", {"c": stamp, "i": rail_id})
    return ids


async def _names(ids: Dict[str, str]) -> Dict[str, str]:
    """Random id -> stable name, for both the rail id and its parent's pp_ id."""
    names = {rail_id: f"<{name}>" for name, rail_id in ids.items()}
    for row in await database.fetch_all("SELECT id, rail_purchase_id FROM agent_purchases"):
        names[row["id"]] = names[row["rail_purchase_id"]].replace("<", "<pp:")
    return names


def _stable(text: str, names: Dict[str, str]) -> str:
    for raw, name in sorted(names.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(raw, name)
    return text


async def _surfaces(ids: Dict[str, str]) -> Dict[str, str]:
    """surface name -> "<status> <body bytes>", ids replaced by their stable names."""
    out: Dict[str, str] = {}
    for name, rail_id in ids.items():
        door = await _request("GET", f"{DOOR}/{rail_id}")
        out[f"door:{name}"] = f"{door.status_code} {door.text}"
        internal = await _internal(rail_id)
        out[f"internal:{name}"] = f"{internal.status_code} {internal.text}"
        if rail_id.startswith("rp_"):
            reap = await _request("GET", f"{REAP}/{rail_id}")
            out[f"reap:{name}"] = f"{reap.status_code} {reap.text}"
    door_list = await _request("GET", f"{DOOR}?limit=50")
    out["door:list"] = f"{door_list.status_code} {door_list.text}"
    reap_list = await _request("GET", f"{REAP}?limit=50")
    out["reap:list"] = f"{reap_list.status_code} {reap_list.text}"
    await purchase_events.detect()
    rows = await database.fetch_all(
        "SELECT e.rail_state, e.event_type, e.status, e.skip_reason, e.payload, p.rail_purchase_id "
        "FROM agent_purchase_events e JOIN agent_purchases p ON p.id = e.purchase_id ORDER BY p.rail_purchase_id")
    by_id = {rail_id: name for name, rail_id in ids.items()}
    for row in rows:
        name = by_id[row["rail_purchase_id"]]
        out[f"event:{name}"] = (f"{row['event_type']} {row['status']} {row['skip_reason']} {row['rail_state']} "
                                f"{row['payload']}")
    names = await _names(ids)
    return {key: _stable(value, names) for key, value in sorted(out.items())}


async def _record_or_load() -> Dict[str, str]:
    if os.getenv("AGENPAS_GOLDEN_RECORD") == "1":
        surfaces = await _surfaces(await _fixtures())
        GOLDEN_OFF.write_text(json.dumps(surfaces, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        pytest.skip("golden recorded")
    return json.loads(GOLDEN_OFF.read_text(encoding="utf-8"))


# ── flags off: byte-identical to main ────────────────────────────────────────────────────────


async def test_with_every_new_flag_off_every_surface_answers_mains_bytes():
    golden = await _record_or_load()
    surfaces = await _surfaces(await _fixtures())
    assert sorted(surfaces) == sorted(golden)
    for key in golden:
        assert surfaces[key] == golden[key], key


async def test_the_golden_covers_every_surface_and_every_hosted_url():
    golden = json.loads(GOLDEN_OFF.read_text(encoding="utf-8"))
    kinds = {key.split(":", 1)[0] for key in golden}
    assert kinds == {"door", "internal", "reap", "event"}
    text = json.dumps(golden)
    for url in URLS.values():
        assert url in text, url


def test_the_internal_routers_openapi_with_the_write_dial_off_is_mains(monkeypatch):
    """tests/golden/agenpas_internal_read_openapi.json is main's schema for an app with the read mounted."""
    from fastapi import FastAPI

    monkeypatch.delenv(purchase_consent.WRITE_ENABLED_ENV, raising=False)
    fresh = FastAPI()
    assert internal_routes.mount(fresh) is True
    golden = json.loads((_REPO / "tests/golden/agenpas_internal_read_openapi.json").read_text("utf-8"))
    assert fresh.openapi() == golden
    assert not any(getattr(r, "path", "").endswith("/consents") for r in fresh.routes)


def test_mount_registers_the_consent_write_only_with_its_dial_on(monkeypatch):
    from fastapi import FastAPI

    monkeypatch.delenv(internal_routes.AGENPAS_INTERNAL_READ_ENABLED_ENV)
    monkeypatch.delenv(purchase_consent.WRITE_ENABLED_ENV, raising=False)
    off = FastAPI()
    assert internal_routes.mount(off) is False
    assert not any(INTERNAL in getattr(r, "path", "") for r in off.routes)
    monkeypatch.setenv(purchase_consent.WRITE_ENABLED_ENV, "1")
    on = FastAPI()
    assert internal_routes.mount(on) is True
    assert [(r.path, sorted(r.methods)) for r in on.routes if INTERNAL in getattr(r, "path", "")] == [
        (f"{INTERNAL}/{{purchase_id}}/consents", ["POST"])]


@pytest.mark.skipif(_WRITE_MOUNTED_AT_IMPORT, reason="main.app was built with AGENPAS_CONSENT_WRITE_ENABLED on")
def test_main_app_with_the_write_dial_off_at_build_has_no_consent_route_or_schema():
    assert not [r for r in app.routes if getattr(r, "path", "").endswith("/consents")]
    app.openapi_schema = None
    assert not [p for p in app.openapi()["paths"] if p.endswith("/consents")]


# ── gate on ──────────────────────────────────────────────────────────────────────────────────

WRITE_KEY = "agenpas_consent_write_test_key_0123456789ab"
TERMS_URL = "https://legal.example/terms"
PRIVACY_URL = "https://legal.example/privacy"
TERMS_V = "terms-2026-11"
PRIVACY_V = "privacy-2026-11"
SECRET = "offer_secret_for_tests_only_0123456789abcdef"
#: The fixtures whose hosted page the gate withholds, and the step each asks for.
GATED = {"R_enroll": "card_binding", "R_approve": "approval", "A_bind": "card_binding", "A_approve": "approval"}
#: The approval consent may also be taken while the rail is pricing.
LOCKING = {"R_quoting": "approval", "A_quoting": "approval"}


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setenv(purchase_consent.GATE_ENV, "1")
    monkeypatch.setenv(purchase_consent.WRITE_ENABLED_ENV, "1")
    monkeypatch.setenv(purchase_consent.TERMS_URL_ENV, TERMS_URL)
    monkeypatch.setenv(purchase_consent.TERMS_VERSION_ENV, TERMS_V)
    monkeypatch.setenv(purchase_consent.PRIVACY_URL_ENV, PRIVACY_URL)
    monkeypatch.setenv(purchase_consent.PRIVACY_VERSION_ENV, PRIVACY_V)
    monkeypatch.setenv(purchase_consent.OFFER_SECRET_ENV, SECRET)
    monkeypatch.setenv(internal_routes.CONSENT_KEY_ENV, WRITE_KEY)
    return monkeypatch


def _gated_urls() -> List[str]:
    return [url for key, url in URLS.items() if key != "A_3ds"]


async def _consent(ids, name: str, step: Optional[str] = None, epoch: Optional[int] = None) -> None:
    rail_id = ids[name]
    rail = "reap" if rail_id.startswith("rp_") else "a2a"
    step = step or GATED.get(name) or LOCKING[name]
    await consents_db.record(
        rail=rail, rail_purchase_id=rail_id, agent_id=AGENT, step=step,
        approval_epoch=purchase_consent.approval_epoch(rail, step) if epoch is None else epoch,
        terms_version=TERMS_V, privacy_version=PRIVACY_V, terms_url=TERMS_URL, privacy_url=PRIVACY_URL,
        offer_issued_at=datetime.now(timezone.utc))


def _without_consent_block(text: str) -> str:
    status, _, body = text.partition(" ")
    data = json.loads(body)
    data.pop("consent", None)
    return f"{status} {json.dumps(data, separators=(',', ':'), ensure_ascii=False)}"


def _body(text: str) -> Dict[str, Any]:
    return json.loads(text.partition(" ")[2])


async def test_gate_on_and_unconsented_no_card_or_approval_url_appears_on_any_surface(gate):
    golden = json.loads(GOLDEN_OFF.read_text(encoding="utf-8"))
    ids = await _fixtures()
    surfaces = await _surfaces(ids)
    assert sorted(surfaces) == sorted(golden)  # every event still fires (action_required included)
    everything = json.dumps(surfaces)
    for url in _gated_urls():
        assert url not in everything, url
        # not even its path
        assert url.split("/", 3)[3] not in everything, url
    for name, step in GATED.items():
        door = _body(surfaces[f"door:{name}"])
        golden_door = _body(golden[f"door:{name}"])
        assert door["next_action"] == {"kind": step, "expires_at": golden_door["next_action"]["expires_at"],
                                       "consent_required": True}
        assert door["detail"]["buyer_step"] == door["next_action"]
        assert "hosted_url" not in door["detail"]
        # the deadline stays
        assert door["detail"]["hosted_url_expires_at"] == golden_door["detail"]["hosted_url_expires_at"]
        event = surfaces[f"event:{name}"]
        assert event.startswith("purchase.action_required pending None ")
        assert json.loads(event.split(" ", 4)[4])["next_action"]["consent_required"] is True
        internal = _body(surfaces[f"internal:{name}"])
        assert {k: v for k, v in internal.items() if k != "consent"} == door
        assert internal["consent"]["step"] == step and internal["consent"]["satisfied"] is False
        assert internal["consent"]["available"] is True and set(internal["consent"]["offer"]) == {"id", "issued_at"}
        if name.startswith("R_"):
            assert _body(surfaces[f"reap:{name}"]) == door["detail"]
    for name, step in LOCKING.items():
        internal = _body(surfaces[f"internal:{name}"])
        assert internal["consent"]["step"] == step and "offer" in internal["consent"]
        assert _without_consent_block(surfaces[f"internal:{name}"]) == _without_consent_block(golden[f"internal:{name}"])
    # Everything that carries no card / approval page is main's body: authenticate (3DS) is NEVER gated.
    untouched = [k for k in golden if k.split(":", 1)[1] not in GATED and not k.endswith(":list")
                 and not (k.startswith("internal:") and k.split(":", 1)[1] in LOCKING)]
    assert "door:A_3ds" in untouched and "internal:A_3ds" in untouched and "event:A_bind" not in untouched
    for key in untouched:
        assert surfaces[key] == golden[key], key
    assert URLS["A_3ds"] in surfaces["door:A_3ds"]


async def test_consented_every_surface_is_todays_body(gate):
    golden = json.loads(GOLDEN_OFF.read_text(encoding="utf-8"))
    ids = await _fixtures()
    for name in list(GATED) + list(LOCKING):
        if name in LOCKING:
            continue
        await _consent(ids, name)
    surfaces = await _surfaces(ids)
    for key in golden:
        if key.startswith("internal:"):
            assert _without_consent_block(surfaces[key]) == _without_consent_block(golden[key]), key
        else:
            assert surfaces[key] == golden[key], key
    for name in GATED:
        assert _body(surfaces[f"internal:{name}"])["consent"]["satisfied"] is True
        assert "offer" not in _body(surfaces[f"internal:{name}"])["consent"]


async def test_an_approval_consent_from_another_epoch_or_the_card_step_does_not_open_the_approval(gate):
    ids = await _fixtures()
    await _consent(ids, "A_approve", epoch=2)
    await _consent(ids, "A_approve", step="card_binding")
    await _consent(ids, "R_approve", epoch=0)
    for name in ("A_approve", "R_approve"):
        body = (await _request("GET", f"{DOOR}/{ids[name]}")).json()
        assert body["next_action"]["consent_required"] is True and URLS[name] not in json.dumps(body)
    await _consent(ids, "A_approve")
    body = (await _request("GET", f"{DOOR}/{ids['A_approve']}")).json()
    assert body["next_action"]["url"] == URLS["A_approve"]


async def test_another_agents_consent_does_not_open_the_page(gate):
    ids = await _fixtures()
    await consents_db.record(
        rail="reap", rail_purchase_id=ids["R_approve"], agent_id="agent_someone_else", step="approval",
        approval_epoch=1, terms_version=TERMS_V, privacy_version=PRIVACY_V, terms_url=TERMS_URL,
        privacy_url=PRIVACY_URL, offer_issued_at=datetime.now(timezone.utc))
    body = (await _request("GET", f"{DOOR}/{ids['R_approve']}")).json()
    assert body["next_action"]["consent_required"] is True


async def test_a_consent_read_that_fails_withholds_every_page(gate, monkeypatch):
    ids = await _fixtures()
    for name in GATED:
        await _consent(ids, name)

    async def _broken(*a, **k):
        raise RuntimeError("consent table unreadable")

    monkeypatch.setattr(consents_db, "consents_for", _broken)
    surfaces = await _surfaces(ids)
    everything = json.dumps(surfaces)
    for url in _gated_urls():
        assert url not in everything
    for name in GATED:
        consent = _body(surfaces[f"internal:{name}"])["consent"]
        assert consent["available"] is False and consent["satisfied"] is False and "offer" not in consent


@pytest.mark.parametrize("missing", [purchase_consent.TERMS_URL_ENV, purchase_consent.PRIVACY_VERSION_ENV,
                                     purchase_consent.OFFER_SECRET_ENV])
async def test_gate_on_without_its_configuration_fails_closed(gate, missing):
    gate.delenv(missing)
    ids = await _fixtures()
    for name in GATED:
        body = (await _internal(ids[name])).json()
        assert "url" not in body["next_action"] and body["consent"]["available"] is False
        assert "offer" not in body["consent"]


@pytest.mark.parametrize("value", ["short", "http://legal.example/terms", "https://user@legal.example/terms"])
async def test_a_malformed_legal_url_or_short_secret_is_unconfigured(gate, value):
    if value == "short":
        gate.setenv(purchase_consent.OFFER_SECRET_ENV, "x" * 31)
    else:
        gate.setenv(purchase_consent.TERMS_URL_ENV, value)
    assert purchase_consent.config() is None


async def test_gate_on_with_the_write_off_offers_nothing(gate):
    gate.delenv(purchase_consent.WRITE_ENABLED_ENV)
    ids = await _fixtures()
    body = (await _internal(ids["R_approve"])).json()
    assert body["consent"]["available"] is False and "offer" not in body["consent"]
    assert "url" not in body["next_action"]


async def test_states_without_a_consent_step_carry_no_consent_block(gate):
    ids = await _fixtures()
    for name in ("R_resolving", "R_processing", "R_completed", "A_3ds", "A_completed"):
        assert "consent" not in (await _internal(ids[name])).json(), name


async def test_each_list_route_reads_the_consents_once_per_page(gate, monkeypatch):
    ids = await _fixtures()
    await _consent(ids, "R_approve")
    calls = []
    real = consents_db.consents_for

    async def _spy(agent_id, rail_purchase_ids):
        calls.append(sorted(rail_purchase_ids))
        return await real(agent_id, rail_purchase_ids)

    monkeypatch.setattr(consents_db, "consents_for", _spy)
    door = await _request("GET", f"{DOOR}?limit=50")
    assert door.status_code == 200 and len(calls) == 1 and calls[0] == sorted(ids.values())
    text = door.text
    assert URLS["R_approve"] in text and URLS["R_enroll"] not in text and URLS["A_bind"] not in text
    calls.clear()
    reap = await _request("GET", f"{REAP}?limit=50")
    assert reap.status_code == 200 and len(calls) == 1
    assert calls[0] == sorted(v for v in ids.values() if v.startswith("rp_"))
    assert URLS["R_approve"] in reap.text and URLS["R_enroll"] not in reap.text


async def test_with_the_gate_off_nothing_reads_the_consent_table(monkeypatch):
    ids = await _fixtures()

    async def _forbidden(*a, **k):
        raise AssertionError("the consent table was read with the gate off")

    monkeypatch.setattr(consents_db, "consents_for", _forbidden)
    await _surfaces(ids)


# ── the consent write ────────────────────────────────────────────────────────────────────────


def _offer(rail_id: str, step: str, *, age: float = 10, epoch: Optional[int] = None) -> Dict[str, str]:
    rail = "reap" if rail_id.startswith("rp_") else "a2a"
    issued = purchase_consent.issued_at_text(datetime.now(timezone.utc) - timedelta(seconds=age))
    cfg = purchase_consent.config()
    return {
        "step": step,
        "offer_id": purchase_consent.offer_id(cfg, rail=rail, rail_purchase_id=rail_id, step=step,
                                              epoch=purchase_consent.approval_epoch(rail, step) if epoch is None else epoch,
                                              issued_at=issued),
        "issued_at": issued, "terms_version": TERMS_V, "privacy_version": PRIVACY_V,
    }


async def _post(purchase_id: str, body: Any, *, key: Optional[str] = WRITE_KEY, agent: str = AGENT,
                raw: Optional[bytes] = None) -> httpx.Response:
    headers = {"X-Agenpas-Agent-Id": agent, "Content-Type": "application/json"}
    if key is not None:
        headers["X-Internal-Key"] = key
    content = raw if raw is not None else json.dumps(body).encode()
    return await _request("POST", f"{INTERNAL}/{purchase_id}/consents", agent=False, headers=headers, content=content)


async def _rows() -> List[Dict[str, Any]]:
    return [dict(r) for r in await database.fetch_all("SELECT * FROM agent_purchase_consents")]


async def test_the_offer_the_read_shows_records_a_consent_once_and_opens_the_page(gate):
    gate.setenv(purchase_consent.MIN_DWELL_ENV, "0")
    ids = await _fixtures()
    for name, step in GATED.items():
        rail_id = ids[name]
        consent = (await _internal(rail_id)).json()["consent"]
        body = {"step": step, "offer_id": consent["offer"]["id"], "issued_at": consent["offer"]["issued_at"],
                "terms_version": consent["terms_version"], "privacy_version": consent["privacy_version"]}
        first = await _post(rail_id, body)
        assert first.status_code == 201, first.text
        assert first.json()["created"] is True and first.json()["consent"]["step"] == step
        assert first.json()["consent"]["evidence"] == "link_holder_on_pivota_page"
        again = await _post(rail_id, body)
        assert again.status_code == 200 and again.json()["created"] is False
        assert again.json()["consent"] == first.json()["consent"]
        door = (await _request("GET", f"{DOOR}/{rail_id}")).json()
        assert door["next_action"]["url"] == URLS[name]
        assert (await _internal(rail_id)).json()["consent"]["satisfied"] is True
    rows = await _rows()
    assert len(rows) == len(GATED)
    by_rail_id = {r["rail_purchase_id"]: r for r in rows}
    parents = {r["rail_purchase_id"]: r["id"] for r in await database.fetch_all("SELECT * FROM agent_purchases")}
    for name, step in GATED.items():
        row = by_rail_id[ids[name]]
        assert (row["step"], row["agent_id"], row["terms_url"], row["privacy_url"]) == (step, AGENT, TERMS_URL, PRIVACY_URL)
        assert row["purchase_id"] == parents[ids[name]]
        assert row["approval_epoch"] == (0 if step == "card_binding" else 1)
    # The card step's row names the rail's card-side ref; the approval's names none.
    assert by_rail_id[ids["R_enroll"]]["rail_buyer_ref"] == "re_consent_gate_1"
    member = (await database.fetch_one("SELECT member_id FROM a2a_rail_purchases WHERE id = :i", {"i": ids["A_bind"]}))[0]
    assert by_rail_id[ids["A_bind"]]["rail_buyer_ref"] == member
    assert by_rail_id[ids["R_approve"]]["rail_buyer_ref"] is None
    # No PII in any row.
    text = json.dumps([{k: str(v) for k, v in r.items()} for r in rows])
    for leaked in (EMAIL, STREET, OWNER):
        assert leaked not in text


async def test_a_parent_id_is_normalised_to_the_rail_id(gate):
    ids = await _fixtures()
    parent = (await database.fetch_one("SELECT id FROM agent_purchases WHERE rail_purchase_id = :i",
                                       {"i": ids["R_approve"]}))["id"]
    answer = await _post(parent, _offer(ids["R_approve"], "approval"))
    assert answer.status_code == 201, answer.text
    [row] = await _rows()
    assert (row["rail"], row["rail_purchase_id"], row["purchase_id"]) == ("reap", ids["R_approve"], parent)


async def test_the_write_does_not_need_the_parent_and_heals_it(gate):
    ids = await _fixtures()
    await database.execute("DELETE FROM agent_purchases WHERE rail_purchase_id = :i", {"i": ids["A_approve"]})
    answer = await _post(ids["A_approve"], _offer(ids["A_approve"], "approval"))
    assert answer.status_code == 201, answer.text
    parent = await database.fetch_one("SELECT id FROM agent_purchases WHERE rail_purchase_id = :i", {"i": ids["A_approve"]})
    [row] = await _rows()
    assert parent is not None and row["purchase_id"] == parent["id"]


async def test_the_approval_consent_may_be_taken_while_the_rail_is_pricing(gate):
    ids = await _fixtures()
    for name in LOCKING:
        answer = await _post(ids[name], _offer(ids[name], "approval"))
        assert answer.status_code == 201, (name, answer.text)
    # ... and it holds once the rail moves on to the approval page (the same epoch).
    await database.execute(
        "UPDATE a2a_rail_purchases SET state = 'awaiting_authorization', buyer_action_kind = 'approval', "
        "buyer_action_url = :u, buyer_action_expires_at = :f WHERE id = :i",
        {"u": URLS["A_approve"], "f": FUTURE, "i": ids["A_quoting"]})
    body = (await _request("GET", f"{DOOR}/{ids['A_quoting']}")).json()
    assert body["next_action"]["url"] == URLS["A_approve"]


async def _refused(purchase_id, body, status, error, **kwargs):
    answer = await _post(purchase_id, body, **kwargs)
    assert answer.status_code == status, answer.text
    payload = answer.json()
    reason = payload.get("error") if isinstance(payload.get("error"), str) else (payload.get("detail") or {}).get("error")
    assert reason == error, payload
    assert await _rows() == []
    return answer


async def test_refusals_write_nothing(gate):
    ids = await _fixtures()
    approve = ids["R_approve"]
    good = _offer(approve, "approval")
    # wrong step for the state
    await _refused(approve, _offer(approve, "card_binding"), 409, "consent_stale")
    await _refused(ids["R_resolving"], _offer(ids["R_resolving"], "card_binding"), 409, "consent_stale")
    # a final purchase
    await _refused(ids["R_completed"], _offer(ids["R_completed"], "approval"), 409, "consent_stale")
    await _refused(ids["A_completed"], _offer(ids["A_completed"], "approval"), 409, "consent_stale")
    # another agent's purchase is the same 404 as none
    await _refused(approve, good, 404, "purchase_not_found", agent="agent_someone_else")
    await _refused("rp_" + "0" * 24, _offer("rp_" + "0" * 24, "approval"), 404, "purchase_not_found")
    # a stale version
    await _refused(approve, {**good, "terms_version": "terms-2026-10"}, 409, "consent_stale")
    await _refused(approve, {**good, "privacy_version": "privacy-2026-10"}, 409, "consent_stale")
    # a bad HMAC: forged, for another purchase, another step's, another epoch's, a shifted issued_at
    await _refused(approve, {**good, "offer_id": "0" * 64}, 400, "consent_offer_invalid")
    await _refused(approve, {**_offer(ids["A_approve"], "approval"), "offer_id": _offer(ids["A_approve"], "approval")["offer_id"]},
                   400, "consent_offer_invalid")
    await _refused(approve, _offer(approve, "approval", epoch=2), 400, "consent_offer_invalid")
    shifted = dict(good)
    shifted["issued_at"] = purchase_consent.issued_at_text(
        purchase_consent.parse_issued_at(good["issued_at"]) - timedelta(seconds=60))
    await _refused(approve, shifted, 400, "consent_offer_invalid")
    # dwell too short, and an offer too old
    await _refused(approve, _offer(approve, "approval", age=1), 409, "consent_too_soon")
    await _refused(approve, _offer(approve, "approval", age=-30), 409, "consent_too_soon")
    await _refused(approve, _offer(approve, "approval", age=1801), 409, "consent_stale")
    # malformed bodies
    for bad in ({**good, "extra": "x"}, {k: v for k, v in good.items() if k != "step"}, {**good, "step": "pay"},
                {**good, "offer_id": "ZZ"}, {**good, "issued_at": "2026-11-18 00:00:00"}, {**good, "step": 1},
                {**good, "terms_version": "has space"}, [good], "x"):
        await _refused(approve, bad, 400, "invalid_consent")
    await _refused(approve, None, 400, "invalid_consent", raw=b"{not json")
    await _refused(approve, None, 400, "invalid_consent", raw=json.dumps({**good, "pad": "y" * 1100}).encode())


async def test_missing_configuration_or_secret_refuses_the_write(gate):
    ids = await _fixtures()
    good = _offer(ids["R_approve"], "approval")
    for name in (purchase_consent.OFFER_SECRET_ENV, purchase_consent.TERMS_URL_ENV):
        value = os.environ[name]
        gate.delenv(name)
        await _refused(ids["R_approve"], good, 503, "consent_unavailable")
        gate.setenv(name, value)


async def test_the_write_has_its_own_key_and_never_takes_the_read_key(gate):
    ids = await _fixtures()
    good = _offer(ids["R_approve"], "approval")
    for key in (None, "", "wrong", READ_KEY, WRITE_KEY + "x"):
        await _refused(ids["R_approve"], good, 401, "unauthorized", key=key)
    gate.setenv(internal_routes.CONSENT_KEY_ENV, "k" * 31)
    await _refused(ids["R_approve"], good, 503, "internal_key_not_configured", key="k" * 31)
    gate.setenv(internal_routes.CONSENT_KEY_ENV, READ_KEY)  # a write key that is the read key is no write key
    await _refused(ids["R_approve"], good, 503, "internal_key_not_configured", key=READ_KEY)


async def test_with_the_write_dial_off_the_write_is_the_unknown_route_404(gate):
    ids = await _fixtures()
    gate.delenv(purchase_consent.WRITE_ENABLED_ENV)
    answer = await _post(ids["R_approve"], _offer(ids["R_approve"], "approval"))
    unknown = await _request("POST", "/agent/internal/commerce/no-such-route/x", agent=False)
    assert answer.status_code == unknown.status_code == 404
    assert {k: v for k, v in answer.json().items() if k != "metadata"} == {
        k: v for k, v in unknown.json().items() if k != "metadata"}
    assert await _rows() == []


@pytest.mark.parametrize("agent", ["", "agent with spaces", "a" * 129])
async def test_the_write_needs_the_agent_id(gate, agent):
    ids = await _fixtures()
    await _refused(ids["R_approve"], _offer(ids["R_approve"], "approval"), 400, "agent_id_required", agent=agent)


# ── invariants ───────────────────────────────────────────────────────────────────────────────


def _source_files():
    for path in _REPO.rglob("*"):
        if path.suffix not in (".py", ".sql") or any(part in (".git", ".venv", "node_modules") for part in path.parts):
            continue
        yield path


def test_nothing_updates_or_deletes_a_consent():
    """INSERT-ONLY: the only statement that removes a consent is the down migration's DROP TABLE."""
    pattern = re.compile(r"(UPDATE|DELETE\s+FROM|TRUNCATE(\s+TABLE)?)\s+agent_purchase_consents\b", re.I)
    hits = []
    for path in _source_files():
        rel = path.relative_to(_REPO).as_posix()
        if rel.startswith("tests/"):
            continue
        text = path.read_text("utf-8", errors="replace")
        if pattern.search(text):
            hits.append(rel)
    assert hits == []
    statements = [v for k, v in vars(consents_db).items() if k.isupper() and isinstance(v, str) and k.endswith("_SQL")
                  or k.endswith("_SQL_SQLITE")]
    assert statements and all(not re.search(r"\b(UPDATE|DELETE)\b", sql, re.I) for sql in statements)
    down = (_REPO / "db/migrations/down/289_agent_purchase_consents_down.sql").read_text("utf-8")
    assert "DROP TABLE IF EXISTS agent_purchase_consents" in down


def _may_be_quoting(tree: ast.AST, expr: ast.AST) -> bool:
    """A non-literal `to_state` is safe only as `NAME[...]` / `NAME[...][i]` over a dict literal bound to NAME
    whose values are all literals, none of them "quoting" (e.g. the worker's `ended = {...}[status]`)."""
    while isinstance(expr, ast.Subscript):
        expr = expr.value
    if not isinstance(expr, ast.Name):
        return True
    bound = [n.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == expr.id for t in n.targets)]
    if not bound:
        return True
    for value in bound:
        while isinstance(value, ast.Subscript):
            value = value.value
        if not isinstance(value, ast.Dict):
            return True
        leaves = [n for v in value.values for n in ast.walk(v) if not isinstance(n, (ast.Tuple, ast.List, ast.Load))]
        if not all(isinstance(n, ast.Constant) for n in leaves) or "quoting" in [n.value for n in leaves]:
            return True
    return False


def test_no_writer_of_the_a2a_re_quote_edge_exists():
    """THE APPROVAL EPOCH RESTS ON THIS (services/payment_orchestration/purchase_consent.approval_epoch).
    The A2A ledger permits `awaiting_authorization -> quoting`; nothing writes it, so every A2A purchase
    enters `quoting` once and its approval epoch is 1. A writer appearing makes that false: an approval
    consent would then carry over to a re-quoted price. Add a quoting-entry counter to a2a_rail_purchases,
    return it from approval_epoch, then change this test."""
    offenders = []
    for path in _source_files():
        rel = path.relative_to(_REPO).as_posix()
        if path.suffix != ".py" or rel.startswith("tests/"):
            continue
        text = path.read_text("utf-8", errors="replace")
        if "a2a_rail" not in text or "quoting" not in text:
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            kwargs = {k.arg: k.value for k in node.keywords if k.arg}
            to_state = kwargs.get("to_state")
            if not (isinstance(to_state, ast.Constant) and to_state.value == "quoting"):
                if (to_state is not None and not isinstance(to_state, ast.Constant) and rel != "db/a2a_rail_ledger.py"
                        and _may_be_quoting(tree, to_state)):
                    offenders.append(f"{rel}:{node.lineno} (to_state not a literal)")
                continue
            sources = kwargs.get("from_states")
            if not isinstance(sources, (ast.List, ast.Tuple)) or not all(isinstance(e, ast.Constant) for e in sources.elts):
                offenders.append(f"{rel}:{node.lineno} (from_states not a literal list)")
            elif "awaiting_authorization" in [e.value for e in sources.elts]:
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == []
    assert "quoting" in a2a_ledger.ALLOWED_TRANSITIONS["awaiting_authorization"]  # the edge this guards


async def test_pii_scrubs_and_expiry_sweeps_leave_the_consents(gate):
    ids = await _fixtures()
    for name in GATED:
        await _consent(ids, name)
    before = sorted((await _rows()), key=lambda r: r["id"])
    # Make every purchase old enough for each sweep to act on it.
    for table in ("reap_agentic_purchases", "a2a_rail_purchases"):
        await database.execute(f"UPDATE {table} SET created_at = '2020-01-01 00:00:00', "
                               f"state_entered_at = '2020-01-01 00:00:00', updated_at = '2020-01-01 00:00:00'")
    moved = await reap_ledger.expire_overdue_purchases(max_age_seconds=60, enrollment_grace_seconds=0)
    scrubbed = await reap_ledger.scrub_reconciling_purchase_pii(max_age_seconds=60)
    purged = await a2a_ledger.purge_stale_contact(max_age_seconds=3600)
    expired = await a2a_ledger.expire_overdue_purchases(max_age_seconds=60, max_lifetime_seconds=60)
    assert moved or scrubbed
    assert purged or expired
    assert sorted((await _rows()), key=lambda r: r["id"]) == before


def test_the_migration_holds_the_modules_ddl():
    migration = " ".join((_REPO / "db/migrations/289_agent_purchase_consents.sql").read_text("utf-8").split())
    for statement in (consents_db._CREATE_TABLE_PG, consents_db._CREATE_UNIQUE_INDEX):
        assert " ".join(statement.split()) in migration


def test_the_checks_are_the_python_vocabularies():
    ddl = consents_db._CREATE_TABLE_PG
    def values(name):
        match = re.search(rf"CONSTRAINT {name} CHECK \(\w+ IN \(([^)]*)\)\)", ddl)
        return tuple(v.strip().strip("'") for v in match.group(1).split(","))
    assert values("ck_agent_purchase_consents_step") == consents_db.STEPS
    assert values("ck_agent_purchase_consents_evidence") == consents_db.EVIDENCE
    assert values("ck_agent_purchase_consents_rail") == consents_db.RAILS


def test_the_offer_hmac_binds_every_field():
    cfg = purchase_consent.ConsentConfig(TERMS_URL, TERMS_V, PRIVACY_URL, PRIVACY_V, SECRET.encode())
    base = dict(rail="reap", rail_purchase_id="rp_" + "1" * 24, step="approval", epoch=1, issued_at="2026-11-18T00:00:00Z")
    first = purchase_consent.offer_id(cfg, **base)
    variants = [dict(base, rail="a2a"), dict(base, rail_purchase_id="rp_" + "2" * 24), dict(base, step="card_binding"),
                dict(base, epoch=2), dict(base, issued_at="2026-11-18T00:00:01Z")]
    seen = {first}
    for v in variants:
        seen.add(purchase_consent.offer_id(cfg, **v))
    for other in (purchase_consent.ConsentConfig(TERMS_URL + "x", TERMS_V, PRIVACY_URL, PRIVACY_V, SECRET.encode()),
                  purchase_consent.ConsentConfig(TERMS_URL, TERMS_V + "x", PRIVACY_URL, PRIVACY_V, SECRET.encode()),
                  purchase_consent.ConsentConfig(TERMS_URL, TERMS_V, PRIVACY_URL + "x", PRIVACY_V, SECRET.encode()),
                  purchase_consent.ConsentConfig(TERMS_URL, TERMS_V, PRIVACY_URL, PRIVACY_V + "x", SECRET.encode()),
                  purchase_consent.ConsentConfig(TERMS_URL, TERMS_V, PRIVACY_URL, PRIVACY_V, SECRET.encode() + b"x")):
        seen.add(purchase_consent.offer_id(other, **base))
    assert len(seen) == 1 + len(variants) + 5
    assert SECRET not in repr(cfg)


@pytest.mark.parametrize("raw,expected", [("", 3), ("0", 0), ("10", 10), ("-1", 3), ("abc", 3), ("99999", 3)])
def test_the_dwell_setting(monkeypatch, raw, expected):
    monkeypatch.setenv(purchase_consent.MIN_DWELL_ENV, raw)
    assert purchase_consent.min_dwell_seconds() == expected


async def test_the_db_layer_records_once_and_reads_by_page():
    common = dict(agent_id=AGENT, terms_version=TERMS_V, privacy_version=PRIVACY_V, terms_url=TERMS_URL,
                  privacy_url=PRIVACY_URL, offer_issued_at=datetime.now(timezone.utc))
    first, created = await consents_db.record(rail="reap", rail_purchase_id="rp_" + "1" * 24, step="approval",
                                              approval_epoch=1, **common)
    again, created_again = await consents_db.record(rail="reap", rail_purchase_id="rp_" + "1" * 24, step="approval",
                                                    approval_epoch=1, **common)
    assert created is True and created_again is False and again["id"] == first["id"]
    assert first["accepted_at"].tzinfo is not None and first["offer_issued_at"].tzinfo is not None
    await consents_db.record(rail="a2a", rail_purchase_id="ap_" + "1" * 24, step="card_binding", approval_epoch=0, **common)
    got = await consents_db.consents_for(AGENT, ["rp_" + "1" * 24, "ap_" + "1" * 24, "rp_" + "9" * 24])
    assert got == {("reap", "rp_" + "1" * 24, "approval", 1), ("a2a", "ap_" + "1" * 24, "card_binding", 0)}
    assert await consents_db.consents_for("agent_other", ["rp_" + "1" * 24]) == frozenset()
    assert await consents_db.consents_for(AGENT, []) == frozenset()
    with pytest.raises(ValueError):
        await consents_db.record(rail="reap", rail_purchase_id="rp_x", step="pay", approval_epoch=1, **common)


def test_the_prepare_gate_collects_the_postgres_statements():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_gate", _REPO / "tests/test_repo_sql_prepare_postgres.py")
    prepare_gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare_gate)
    collected = [" ".join(str(sql).split()) for _where, sql in prepare_gate.collect_statements()]
    for statement in (consents_db._INSERT_SQL, consents_db._SELECT_ONE_SQL, consents_db._SELECT_FOR_PURCHASES_SQL,
                      consents_db._SELECT_REAP_FOR_AGENT_SQL, consents_db._SELECT_A2A_FOR_AGENT_SQL,
                      consents_db._CREATE_TABLE_PG):
        assert " ".join(statement.split()) in collected, statement.strip().splitlines()[0]
