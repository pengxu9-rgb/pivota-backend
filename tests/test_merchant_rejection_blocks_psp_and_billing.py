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

def _psp_gate_blocks(status, auto_approved):
    """The decision from routes/merchant_onboarding_routes.py's PSP setup gate,
    evaluated on the real source so this cannot drift from a re-typed copy."""
    import ast
    import re

    src = (BACKEND_ROOT / "routes" / "merchant_onboarding_routes.py").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r"is_auto_approved = merchant\.get\(\"auto_approved\", False\)\n"
        r"\s*if (.+?):\n\s*raise HTTPException",
        src,
        re.DOTALL,
    )
    assert match, "the PSP setup gate moved; this test must be re-pointed"
    condition = " ".join(match.group(1).split())
    return eval(  # noqa: S307 - evaluating this repo's own gate expression
        condition,
        {"merchant": {"status": status}, "is_auto_approved": auto_approved},
    )


@pytest.mark.parametrize(
    "status,auto_approved,blocked",
    [
        ("approved", True, False),            # the normal auto-approved merchant
        ("approved", False, False),           # approved by an admin after rejection
        ("rejected", False, True),            # rejected, auto_approved cleared
        ("rejected", True, True),             # rejected, stale flag — must STILL block
        ("pending_verification", False, True),
    ],
)
def test_the_psp_gate_blocks_a_rejected_merchant(status, auto_approved, blocked):
    """The `("rejected", True)` row is the whole point: it is the exact state the
    old code produced, and the state the gate used to wave through."""
    assert _psp_gate_blocks(status, auto_approved) is blocked


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


@pytest.mark.parametrize("status", sorted(SUPPRESSED_ONBOARDING_STATUSES))
def test_a_suppressed_merchant_stops_serving_even_with_an_active_store(status):
    """Public recall gates on catalog_merchants.status, which is derived from
    store connectivity — so a rejected merchant with a live store kept serving.
    The onboarding status is now an input to that derivation."""
    assert derive_merchant_status(["active"], onboarding_status=status) == "inactive"
    assert derive_merchant_status(["active", "connected"], onboarding_status=status) == "inactive"


@pytest.mark.parametrize("status", sorted(SUPPRESSED_ONBOARDING_STATUSES))
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
    match = re.search(
        r"merchant_status = str\(merchant\.get\(\"status\"\) or \"\"\)\.strip\(\)\.lower\(\)\n"
        r"\s*if (.+?):\n",
        src,
    )
    assert match
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


def test_both_order_entry_points_reach_the_same_handler():
    """The claim the single gate rests on. If agent_v2 ever stops delegating,
    it needs its own check and this is the test that says so."""
    src = (BACKEND_ROOT / "routes" / "agent_v2.py").read_text(encoding="utf-8")
    assert "agent_v1_create_order(" in src, (
        "agent_v2 no longer delegates to the order_routes handler; the order "
        "gate must be duplicated there or moved deeper"
    )
