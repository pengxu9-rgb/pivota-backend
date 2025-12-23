from __future__ import annotations

import pytest

from mvp.ledger import InvalidTransition, LedgerStateMachine


def test_ledger_state_machine_happy_path():
    sm = LedgerStateMachine()
    assert sm.state == "draft"
    sm.step("checkout_attempted")
    assert sm.state == "checkout_attempted"
    sm.step("checkout_succeeded")
    assert sm.state == "paid"
    sm.step("order_placed")
    assert sm.state == "placed"
    sm.step("fulfilled")
    assert sm.state == "fulfilled"


def test_ledger_state_machine_rejects_invalid_transition():
    sm = LedgerStateMachine()
    with pytest.raises(InvalidTransition):
        sm.step("refund_requested")


def test_ledger_state_machine_try_step_is_safe():
    sm = LedgerStateMachine()
    assert sm.try_step("refund_requested") is None
    assert sm.state == "draft"

