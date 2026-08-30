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
