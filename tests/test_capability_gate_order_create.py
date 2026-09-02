"""Capability-based checkout gate (Fix Plan A, option (ii)).

Behavior tests for:
  * services.merchant_capability_gate — the flag + allowlist + capability decision.
  * routes.order_routes._resolve_active_order_psp — the order-create PSP bypass:
      order-create ACCEPTED for a protocol-capable merchant with the flag on,
      REJECTED with the flag off, and REJECTED for a non-capable merchant regardless.
"""

from __future__ import annotations

import re

import pytest
from fastapi import HTTPException


class _FakeDB:
    """Minimal async DB stub: fetch_one returns a row iff the merchant is 'capable'."""

    def __init__(self, capable_merchant_ids):
        self._capable = set(capable_merchant_ids)

    async def fetch_one(self, _sql, params):
        return {"?column?": 1} if params.get("merchant_id") in self._capable else None


# --------------------------------------------------------------------------- #
# services.merchant_capability_gate
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_gate_off_never_permits(monkeypatch):
    import services.merchant_capability_gate as gate

    monkeypatch.setattr(gate.settings, "agent_checkout_capability_gate", False)
    monkeypatch.setattr(gate.settings, "agent_checkout_capability_gate_merchants", frozenset())

    # Even a fully protocol-capable merchant is refused while the flag is off.
    db = _FakeDB({"merch_cap"})
    assert await gate.capability_gate_permits_order_create("merch_cap", database_override=db) is False


@pytest.mark.asyncio
async def test_gate_on_permits_capable_merchant(monkeypatch):
    import services.merchant_capability_gate as gate

    monkeypatch.setattr(gate.settings, "agent_checkout_capability_gate", True)
    monkeypatch.setattr(gate.settings, "agent_checkout_capability_gate_merchants", frozenset())

    db = _FakeDB({"merch_cap"})
    assert await gate.capability_gate_permits_order_create("merch_cap", database_override=db) is True
    # A merchant with no capability row is still refused (fail-closed).
    assert await gate.capability_gate_permits_order_create("merch_none", database_override=db) is False


@pytest.mark.asyncio
async def test_gate_on_allowlist_scopes_to_pilot(monkeypatch):
    import services.merchant_capability_gate as gate

    monkeypatch.setattr(gate.settings, "agent_checkout_capability_gate", True)
    monkeypatch.setattr(
        gate.settings, "agent_checkout_capability_gate_merchants", frozenset({"merch_pilot"})
    )

    db = _FakeDB({"merch_pilot", "merch_other"})
    # On the allowlist AND capable → permitted.
    assert await gate.capability_gate_permits_order_create("merch_pilot", database_override=db) is True
    # Capable but NOT on the allowlist → refused (allowlist only restricts).
    assert await gate.capability_gate_permits_order_create("merch_other", database_override=db) is False


@pytest.mark.asyncio
async def test_capability_lookup_fails_closed_on_db_error(monkeypatch):
    import services.merchant_capability_gate as gate

    class _BoomDB:
        async def fetch_one(self, _sql, _params):
            raise RuntimeError("db down")

    monkeypatch.setattr(gate.settings, "agent_checkout_capability_gate", True)
    monkeypatch.setattr(gate.settings, "agent_checkout_capability_gate_merchants", frozenset())
    assert await gate.merchant_is_protocol_capable("merch_x", database_override=_BoomDB()) is False


# --------------------------------------------------------------------------- #
# routes.order_routes._resolve_active_order_psp  (the actual gate point)
# --------------------------------------------------------------------------- #

def _patch_no_psp(monkeypatch):
    import routes.order_routes as module

    async def fake_fetch_active_runtime_merchant_psp(*, merchant_id, provider=None, psp_id=None):
        return None  # merchant has NO merchant_psps row

    monkeypatch.setattr(module, "fetch_active_runtime_merchant_psp", fake_fetch_active_runtime_merchant_psp)
    return module


def _patch_gate(monkeypatch, module, permits: bool):
    async def fake_permits(merchant_id, *, database_override=None):
        return permits

    monkeypatch.setattr(module, "capability_gate_permits_order_create", fake_permits)


@pytest.mark.asyncio
async def test_order_create_rejected_when_gate_off(monkeypatch):
    module = _patch_no_psp(monkeypatch)
    _patch_gate(monkeypatch, module, permits=False)  # flag off / not permitted

    with pytest.raises(HTTPException) as exc:
        await module._resolve_active_order_psp("merch_x", None, defer_payment=True)
    assert exc.value.status_code == 400
    assert exc.value.detail == "No active PSP configuration found for this merchant"


@pytest.mark.asyncio
async def test_order_create_accepted_for_capable_merchant_when_gate_on(monkeypatch):
    module = _patch_no_psp(monkeypatch)
    _patch_gate(monkeypatch, module, permits=True)  # gate permits (capable + flag on)

    provider, psp_id = await module._resolve_active_order_psp(
        "merch_cap", None, defer_payment=True
    )
    assert provider == module.CAPABILITY_DEFERRED_PSP_PROVIDER == "protocol_deferred"
    # Both values land in `orders`, which enforces CHECK check_psp_id_format
    # (`^psp_[a-z0-9]+_[a-z0-9]{12}$`) and CHECK check_psp_used_valid_provider.
    # This assertion used to pin `"merch_cap:protocol_deferred"`, an id the CHECK
    # refuses outright — so the lane this gate exists to enable could not have
    # created a single order. Pinned to the RULE now, not to a literal, and
    # executed against a real Postgres in
    # tests/test_psp_used_valid_provider_postgres.py.
    assert re.fullmatch(r"psp_[a-z0-9]+_[a-z0-9]{12}", psp_id), psp_id
    assert psp_id.startswith("psp_deferred_")
    # Deterministic per merchant: a retry must not mint a second identity.
    assert psp_id == module._capability_deferred_psp_id("merch_cap")
    assert psp_id != module._capability_deferred_psp_id("merch_other")


@pytest.mark.asyncio
async def test_bypass_only_on_deferred_payment_path(monkeypatch):
    """A protocol-capable merchant on a NON-deferred (synchronous-charge) order is
    still rejected — the bypass never opens a charge without a PSP."""
    module = _patch_no_psp(monkeypatch)
    _patch_gate(monkeypatch, module, permits=True)

    with pytest.raises(HTTPException) as exc:
        await module._resolve_active_order_psp("merch_cap", None, defer_payment=False)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_non_capable_merchant_rejected_even_when_deferred(monkeypatch):
    module = _patch_no_psp(monkeypatch)
    _patch_gate(monkeypatch, module, permits=False)  # gate refuses (not capable)

    with pytest.raises(HTTPException) as exc:
        await module._resolve_active_order_psp("merch_none", None, defer_payment=True)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_existing_psp_row_is_unaffected_by_gate(monkeypatch):
    """When a real PSP row exists, the gate is irrelevant and the real PSP is returned."""
    import routes.order_routes as module

    async def fake_fetch(*, merchant_id, provider=None, psp_id=None):
        return {"provider": "stripe", "psp_id": "psp_live_1"}

    monkeypatch.setattr(module, "fetch_active_runtime_merchant_psp", fake_fetch)
    # Gate would permit, but it must never be consulted when a PSP exists.
    _patch_gate(monkeypatch, module, permits=True)

    provider, psp_id = await module._resolve_active_order_psp(
        "merch_cap", "stripe", defer_payment=True
    )
    assert (provider, psp_id) == ("stripe", "psp_live_1")
