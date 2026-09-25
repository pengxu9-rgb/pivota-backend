"""Rejecting an auto-approved merchant must actually stop them taking money.

Registration auto-approves everyone: routes/merchant_onboarding_routes.py:677
applies an unconditional "approval floor" over a failed auto-KYB, then :735
writes `approved`. That is the intended policy — merchants do not wait for an
employee. The employee's lever is REJECTION, afterwards.

That lever did nothing. `update_kyc_status` wrote `status="rejected"` and never
touched `auto_approved`, which registration had set True at :740, while the PSP
setup gate passed when EITHER status was approved OR auto_approved was set. So
every merchant it could apply to sailed through it.

Written to fail against the pre-fix code:
  * stop clearing auto_approved       -> the PSP tests admit a rejected merchant
  * drop the explicit `== "rejected"` -> same, once auto_approved is stale
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from db import merchant_onboarding as mo  # noqa: E402


class _FakeDB:
    """Records the UPDATE values the real code builds, without a database.

    Asserts on what `update_kyc_status` actually writes rather than on a
    re-typed copy of it — the defect was a column the function silently left
    alone, which only shows up in the emitted values.
    """

    def __init__(self):
        self.values = []

    async def execute(self, query):
        self.values.append(dict(query.compile().params))
        return 1


@pytest.fixture()
def db(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr(mo, "database", fake)
    return fake


async def test_rejecting_clears_auto_approved(db):
    """The one column that made rejection a no-op."""
    await mo.update_kyc_status("merch_1", "rejected", reason="fraudulent store")

    written = db.values[-1]
    assert written["status"] == "rejected"
    assert written["auto_approved"] is False, (
        "auto_approved survived the rejection; the PSP gate still passes"
    )
    assert written["rejection_reason"] == "fraudulent store"


async def test_approving_does_not_resurrect_auto_approved(db):
    """An admin approving is a manual decision, not an automatic one. The gate
    passes on status alone, so nothing needs auto_approved back."""
    await mo.update_kyc_status("merch_1", "approved")

    written = db.values[-1]
    assert written["status"] == "approved"
    assert "auto_approved" not in written or written["auto_approved"] is False


@pytest.mark.parametrize("status", ["rejected", "pending_verification", "deleted"])
async def test_any_non_approved_status_clears_it(db, status):
    """Whatever moves a merchant out of approved, the automatic-approval record
    is no longer true — including the arbitrary status the admin KYB route can
    set."""
    await mo.update_kyc_status("merch_1", status)
    assert db.values[-1]["auto_approved"] is False


# --- the gate itself --------------------------------------------------------

# The PSP gate is covered end-to-end by test_psp_setup_route_executed below,
# which runs the real handler. A source-introspection version lived here and
# was deleted rather than repaired: it could not see an early return inserted
# above the gate, a gate made unreachable, or the status set swapped for an
# empty one — all demonstrated survivors. Executing the route catches each.


def test_the_billing_gate_reads_status_alone():
    """Billing was already correct — it never consulted auto_approved, so a
    rejection stopped billing even while it did not stop the PSP. Pinned so the
    bypass is not copied into it later."""
    src = (BACKEND_ROOT / "routes" / "billing_routes.py").read_text(encoding="utf-8")
    assert 'if merchant_status != "approved":' in src
    assert "auto_approved" not in src


# --- serving: public search and agent recall --------------------------------

from services.store_lifecycle_service import (  # noqa: E402
    SUPPRESSED_ONBOARDING_STATUSES,
    derive_merchant_status,
)


# Literals, not sorted(SUPPRESSED_ONBOARDING_STATUSES). Parametrizing over the
# set under test means shrinking the set silently DELETES cases instead of
# failing them — dropping "rejected" left only [deleted] and both tests stayed
# green. The set's membership is asserted separately below.
@pytest.mark.parametrize("status", ["rejected", "deleted"])
def test_a_suppressed_merchant_stops_serving_even_with_an_active_store(status):
    """Public recall gates on catalog_merchants.status, which is derived from
    store connectivity — so a rejected merchant with a live store kept serving.
    The onboarding status is now an input to that derivation."""
    assert derive_merchant_status(["active"], onboarding_status=status) == "inactive"
    assert derive_merchant_status(["active", "connected"], onboarding_status=status) == "inactive"


@pytest.mark.parametrize("status", ["rejected", "deleted"])
def test_suppression_beats_the_no_store_rows_short_circuit(status):
    """The ordering that matters. `derive_merchant_status` returns None — "don't
    touch it" — for a merchant with no store rows, which is 471 of 483 rows in
    prod. A rejected merchant with no stores would have kept serving on that
    branch, so the suppression check runs BEFORE it."""
    assert derive_merchant_status([], onboarding_status=status) == "inactive"


def test_the_crawl_corpus_is_untouched():
    """The danger the derivation's own docstring warns about: a rule of "no
    active store => inactive" applied corpus-wide empties public search.
    Crawl-observed brands have no merchant_onboarding row at all, so they arrive
    with onboarding_status=None and the store rule decides exactly as before."""
    assert derive_merchant_status([], onboarding_status=None) is None
    assert derive_merchant_status([], onboarding_status="") is None
    assert derive_merchant_status([]) is None


@pytest.mark.parametrize("status", ["approved", "pending_verification", None])
def test_a_merchant_in_good_standing_still_serves(status):
    """The positive counterpart — suppression must key on the REJECTION, not on
    being checked at all."""
    assert derive_merchant_status(["active"], onboarding_status=status) == "active"
    assert derive_merchant_status(["inactive"], onboarding_status=status) == "inactive"


def test_approving_restores_eligibility_not_visibility():
    """Re-approval re-derives from the stores rather than forcing 'active': a
    merchant whose stores are all gone must not be resurrected into search."""
    assert derive_merchant_status(["active"], onboarding_status="approved") == "active"
    assert derive_merchant_status(["inactive"], onboarding_status="approved") == "inactive"
    assert derive_merchant_status([], onboarding_status="approved") is None


# --- orders -----------------------------------------------------------------

def _order_gate_blocks(status):
    """The decision from routes/order_routes.py's order-creation gate, read off
    the real source so it cannot drift from a re-typed copy."""
    import re

    src = (BACKEND_ROOT / "routes" / "order_routes.py").read_text(encoding="utf-8")
    assert 'merchant_status = str(merchant.get("status") or "").strip().lower()' in src, (
        "the order-creation merchant status gate moved; re-point this test"
    )
    # Anchored on the RAISE, not just the condition. Without that anchor the
    # helper passed when the gate was made inert — replacing
    # `raise HTTPException(403)` with a log line left all tests green, because
    # nothing here executes the route and the condition alone still evaluated
    # correctly.
    match = re.search(
        r"merchant_status = str\(merchant\.get\(\"status\"\) or \"\"\)\.strip\(\)\.lower\(\)\n"
        r"\s*if (.+?):\n"
        r"\s*raise HTTPException\(\n"
        r"\s*status_code=403,",
        src,
    )
    assert match, (
        "the order gate no longer reads 'if <cond>: raise HTTPException(403)'. "
        "It may have been made inert, moved, or reformatted — re-point this "
        "test deliberately rather than relaxing the pattern."
    )
    return eval(  # noqa: S307 - this repo's own gate expression
        match.group(1),
        {"merchant_status": status,
         "SUPPRESSED_ONBOARDING_STATUSES": SUPPRESSED_ONBOARDING_STATUSES},
    )


@pytest.mark.parametrize(
    "status,blocked",
    [("rejected", True), ("deleted", True), ("approved", False),
     ("pending_verification", False), ("", False)],
)
def test_order_creation_refuses_a_rejected_merchant(status, blocked):
    """routes/order_routes.py:3494 checked that the merchant EXISTED and nothing
    else, so a rejected merchant kept taking orders. Both entry points funnel
    here — routes/agent_v2.create_order_v2 builds a CreateOrderRequest and calls
    this handler — so one gate covers both."""
    assert _order_gate_blocks(status) is blocked


def test_the_order_gate_runs_before_the_order_is_persisted():
    """Position, not just presence.

    A gate that sits below the INSERT refuses an order that already exists.
    Moving it down keeps every condition test green, so the ordering is
    asserted directly."""
    src = (BACKEND_ROOT / "routes" / "order_routes.py").read_text(encoding="utf-8")
    gate = src.index('merchant_status = str(merchant.get("status") or "").strip().lower()')
    # The row is written by `create_order(order_data)`, not a raw INSERT — the
    # first version of this test looked for "INSERT INTO orders" and failed for
    # that reason, which is the right way round: a positional assertion that
    # cannot find its landmark must fail, not silently pass.
    marker = "await create_order(order_data)"
    assert marker in src, "the order persistence call moved; re-point this test"
    assert gate < src.index(marker), (
        "the order gate moved below the row it exists to prevent"
    )


def test_the_agent_surfaces_still_reach_this_handler():
    """The claim the single gate rests on — and the hop is INDIRECT.

    An earlier version of this asserted that agent_v2 called the order_routes
    handler directly. It does not: agent_v2 imports `agent_v1_create_order`
    from routes.agent_api, and agent_api calls order_routes. Asserting the
    wrong hop meant this test would have stayed green through a refactor that
    broke the real one.
    """
    agent_v2 = (BACKEND_ROOT / "routes" / "agent_v2.py").read_text(encoding="utf-8")
    assert "from routes.agent_api import" in agent_v2 or "agent_v1_create_order" in agent_v2

    agent_api = (BACKEND_ROOT / "routes" / "agent_api.py").read_text(encoding="utf-8")
    assert "create_new_order" in agent_api, (
        "routes/agent_api no longer reaches order_routes.create_new_order; the "
        "agent order surfaces now bypass the merchant status gate"
    )


# --- the doors an earlier version of this change did not close --------------

def test_the_other_psp_connect_route_is_gated_too():
    """`/merchant/integrations/psp/connect` in merchant_api_extensions had NO
    onboarding-status check of any kind. It takes merchant_id from the caller's
    own session, and a rejected merchant can still log in — so it connected a
    payment provider exactly as before, while a comment one file over claimed
    the other route was "the line that decides whether a rejected merchant can
    take money"."""
    src = (BACKEND_ROOT / "routes" / "merchant_api_extensions.py").read_text(
        encoding="utf-8"
    )
    handler = src[src.index('@router.post("/merchant/integrations/psp/connect")'):]
    handler = handler[: handler.index("@router.", 10)]
    assert "SUPPRESSED_ONBOARDING_STATUSES" in handler, (
        "the second PSP connect route lost its merchant-status gate"
    )
    assert "get_merchant_onboarding" in handler


def test_the_payment_canary_does_not_fabricate_an_approved_merchant():
    """merchant_dashboard_routes' order-backed canary built its own merchant
    dict with status="approved" hardcoded and handed it to the executor, which
    creates a REAL order row and runs a REAL PSP payment. It bypassed both the
    order gate and _load_canary_merchant's own approval check."""
    src = (BACKEND_ROOT / "routes" / "merchant_dashboard_routes.py").read_text(
        encoding="utf-8"
    )
    assert '"status": "approved",' not in src, (
        "a route is fabricating an approved merchant again"
    )
    assert "_load_canary_merchant" in src


def test_soft_delete_goes_through_update_kyc_status():
    """Soft delete wrote status="deleted" with a direct UPDATE, so
    `auto_approved` stayed True — and the PSP gate passes on that flag. The
    rejection path had exactly this bug; this was the same door one table
    over, and the deleted-status test above asserted a property the real flow
    did not have."""
    src = (BACKEND_ROOT / "db" / "merchant_onboarding.py").read_text(encoding="utf-8")
    fn = src[src.index("async def soft_delete_merchant_onboarding"):]
    fn = fn[: fn.index("\nasync def ", 10)] if "\nasync def " in fn[10:] else fn
    assert "update_kyc_status(merchant_id, \"deleted\")" in fn, (
        "soft delete no longer clears auto_approved"
    )
    assert 'values(status="deleted"' not in fn, "direct status write is back"


async def test_soft_delete_clears_auto_approved_end_to_end(db):
    """The property the previous test asserted in the abstract, exercised
    through the real function."""
    import db.merchant_onboarding as mod

    await mod.soft_delete_merchant_onboarding("merch_1")
    status_writes = [v for v in db.values if v.get("status") == "deleted"]
    assert status_writes, "soft delete wrote no status"
    assert status_writes[0]["auto_approved"] is False


# --- rejection must not be a one-way door -----------------------------------

def test_rejecting_a_store_less_merchant_is_reversible():
    """The asymmetry that made rejection permanent for most of the corpus.

    Suppression returns 'inactive' for a merchant with no store rows;
    re-approval returned None — "don't touch it" — leaving it dark forever. And
    nothing repairs that: reconcile_catalog_merchant_statuses drives off
    SELECT DISTINCT merchant_id FROM merchant_stores, so a zero-store merchant
    is invisible to the sweep. On prod that is 471 of 483 catalog rows.
    """
    assert derive_merchant_status([], onboarding_status="rejected") == "inactive"
    assert (
        derive_merchant_status(
            [], onboarding_status="approved", no_rows_means_active=True
        )
        == "active"
    ), "re-approval cannot bring a store-less merchant back"


def test_the_restore_flag_does_not_override_a_suppression():
    """Only the approve path passes it, but it must not be a master key."""
    assert (
        derive_merchant_status(
            [], onboarding_status="rejected", no_rows_means_active=True
        )
        == "inactive"
    )


def test_the_ordinary_sweep_still_does_not_touch_store_less_merchants():
    """The restore is caller-supplied evidence, exactly like
    no_rows_means_inactive. A sweep that passed it would resurrect the whole
    corpus into 'active'."""
    assert derive_merchant_status([], onboarding_status="approved") is None
    assert derive_merchant_status([], onboarding_status=None) is None


def test_the_approve_route_passes_the_restore_flag():
    src = (BACKEND_ROOT / "routes" / "merchant_onboarding_routes.py").read_text(
        encoding="utf-8"
    )
    assert "merchant_reinstated=True" in src, (
        "approval no longer restores a store-less merchant"
    )


def test_a_skipped_suppression_is_logged_as_an_error():
    """`sync_catalog_merchant_status` has several silent skip branches — kill
    switch, no catalog row, write_did_not_persist — and any of them leaves a
    rejected merchant serving while the API answers success. Not self-healing,
    so it must be loud."""
    src = (BACKEND_ROOT / "routes" / "merchant_onboarding_routes.py").read_text(
        encoding="utf-8"
    )
    assert 'suppression.get("skipped")' in src
    assert "logger.error(" in src


def test_the_canary_loader_refuses_a_rejected_merchant():
    """The check the fabricated dict skipped. _load_canary_merchant is the only
    thing standing between a rejected merchant and a real PSP charge on that
    route, so its own gate is pinned here."""
    src = (BACKEND_ROOT / "routes" / "payment_execution_routes.py").read_text(
        encoding="utf-8"
    )
    fn = src[src.index("async def _load_canary_merchant"):]
    fn = fn[: fn.index("\nasync def ", 10)]
    assert 'merchant.get("status") != "approved"' in fn
    assert "403" in fn or "HTTP_403_FORBIDDEN" in fn



def test_the_suppressed_set_has_exactly_the_expected_members():
    """Asserted directly, because the tests above no longer parametrize over it
    — and a set that quietly loses a member is the failure those tests could
    not see."""
    assert SUPPRESSED_ONBOARDING_STATUSES == frozenset({"rejected", "deleted"})


# --- end to end: the routes are actually executed ---------------------------
#
# Everything above pins pure functions and source-text expressions. That left
# every WIRING mutation alive: deleting the sync call from the reject route,
# making the gate unreachable, inserting an early return above it, importing an
# empty status set, or turning the 403 into a 200 all kept the suite green,
# because nothing here ran a route. These do.

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _onboarding_client(user):
    import routes.merchant_onboarding_routes as module

    app = FastAPI()
    app.include_router(module.router)

    async def fake_current_user():
        return user

    app.dependency_overrides[module.get_current_user] = fake_current_user
    return TestClient(app), module


@pytest.mark.parametrize(
    "status,auto_approved,expected",
    [
        ("approved", True, 200),
        ("rejected", True, 400),   # the exact state the old code waved through
        ("rejected", False, 400),
        ("deleted", True, 400),    # soft-deleted kept auto_approved=True
    ],
)
def test_psp_setup_route_executed(monkeypatch, status, auto_approved, expected):
    """The real handler, not its condition as a string."""
    user = {"role": "merchant", "merchant_id": "merch_a", "email": "m@x.com"}
    client, module = _onboarding_client(user)

    async def fake_get(merchant_id):
        return {
            "merchant_id": merchant_id,
            "status": status,
            "auto_approved": auto_approved,
            "psp_connected": False,
            "business_name": "M",
        }

    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get)

    async def fake_update(*a, **k):
        return True

    for name in ("update_psp_connection", "update_merchant_onboarding"):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, fake_update)

    resp = client.post(
        "/merchant/onboarding/psp/setup",
        json={"merchant_id": "merch_a", "psp_type": "stripe", "psp_key": "sk_test_x"},
    )
    if expected == 400:
        assert resp.status_code == 400, (
            f"status={status} auto_approved={auto_approved} reached PSP setup"
        )
        assert "approved" in str(resp.json()).lower()
    else:
        assert resp.status_code != 400 or "approved" not in str(resp.json()).lower()


def test_rejecting_calls_the_serving_suppression(monkeypatch):
    """Kills the survivor where the sync call is deleted from the reject route:
    the pure-function tests cannot see whether anyone calls it."""
    user = {"role": "admin", "email": "a@x.com", "sub": "admin"}
    client, module = _onboarding_client(user)
    app = client.app
    app.dependency_overrides[module.require_admin] = lambda: user

    called = {}

    async def fake_get(merchant_id):
        return {"merchant_id": merchant_id, "status": "approved",
                "business_name": "M", "auto_approved": True}

    async def fake_update(*a, **k):
        return True

    async def fake_sync(merchant_id, **kwargs):
        called["merchant_id"] = merchant_id
        called["kwargs"] = kwargs
        return {"changed": True, "skipped": None, "from": "active", "to": "inactive"}

    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get)
    monkeypatch.setattr(module, "update_kyc_status", fake_update)
    import services.store_lifecycle_service as sls

    monkeypatch.setattr(sls, "sync_catalog_merchant_status", fake_sync)

    resp = client.post("/merchant/onboarding/reject/merch_a?reason=fraud")
    assert resp.status_code == 200, resp.text
    assert called.get("merchant_id") == "merch_a", (
        "rejection did not re-derive serving; the merchant stays in public search"
    )
    assert resp.json()["serving_suppressed"]["to"] == "inactive"


def test_approving_calls_the_restore_with_the_reinstate_flag(monkeypatch):
    user = {"role": "admin", "email": "a@x.com", "sub": "admin"}
    client, module = _onboarding_client(user)
    client.app.dependency_overrides[module.require_admin] = lambda: user

    called = {}

    async def fake_get(merchant_id):
        return {"merchant_id": merchant_id, "status": "rejected", "business_name": "M"}

    async def fake_update(*a, **k):
        return True

    async def fake_sync(merchant_id, **kwargs):
        called.update(kwargs)
        return {"changed": True, "skipped": None}

    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get)
    monkeypatch.setattr(module, "update_kyc_status", fake_update)
    import services.store_lifecycle_service as sls

    monkeypatch.setattr(sls, "sync_catalog_merchant_status", fake_sync)

    resp = client.post("/merchant/onboarding/approve/merch_a")
    assert resp.status_code == 200, resp.text
    assert called.get("merchant_reinstated") is True, (
        "approval did not pass the restore flag; a store-less merchant stays dark"
    )


# --- the three survivors the wiring tests still missed ----------------------

@pytest.mark.parametrize("status", ["REJECTED", " Rejected ", "rejected\n", "DELETED"])
def test_suppression_is_case_and_whitespace_insensitive(status):
    """Nothing normalises what an admin can write. `/kyb` used to accept any
    string, and a status stored as "Rejected" would have suppressed nothing —
    dropping `.strip().lower()` was a live survivor."""
    assert derive_merchant_status(["active"], onboarding_status=status) == "inactive"


async def test_sync_actually_reads_the_onboarding_status(monkeypatch):
    """Kills the survivor where sync_catalog_merchant_status passes
    onboarding_status=None: every derivation test would still pass, because the
    pure function is fine — it just never receives the input."""
    import services.store_lifecycle_service as sls

    executed = []

    class _DB:
        async def fetch_all(self, *a, **k):
            return [{"status": "active"}]  # a live store: only rejection can override

        async def fetch_one(self, query, values=None):
            if "merchant_onboarding" in query:
                return {"status": "rejected"}
            return {"status": "inactive"} if executed else {"status": "active"}

        async def execute(self, query, values=None):
            executed.append(values)
            return 1

    monkeypatch.setattr(sls, "database", _DB())
    monkeypatch.setattr(sls, "reconciliation_enabled", lambda: True)

    out = await sls.sync_catalog_merchant_status("merch_a", reason="merchant_rejected")

    assert executed, "no UPDATE was issued; serving was never suppressed"
    assert executed[0]["new_status"] == "inactive"
    assert out["changed"] is True


def test_the_order_gate_uses_the_shared_status_set(monkeypatch):
    """Kills the survivor where order_routes defines its own empty
    SUPPRESSED_ONBOARDING_STATUSES: the source-introspection test injected the
    TEST's set into eval, so it could never see the module's binding change."""
    import routes.order_routes as order_module
    import services.store_lifecycle_service as sls

    assert order_module.SUPPRESSED_ONBOARDING_STATUSES is (
        sls.SUPPRESSED_ONBOARDING_STATUSES
    ), "order_routes is no longer using the shared status set"
    assert "rejected" in order_module.SUPPRESSED_ONBOARDING_STATUSES
