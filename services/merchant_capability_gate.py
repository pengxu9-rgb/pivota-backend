"""Capability-based checkout gate (Fix Plan A, option (ii)).

Lets a *protocol-capable* merchant — one that has a row in
``pcs_merchant_capabilities`` (minted by pivota-backend once a protocol/checkout
integration is verified) — create an order on the deferred-payment lane WITHOUT a
``merchant_psps`` row.

Everything here is fail-closed and DARK by default:

* ``settings.agent_checkout_capability_gate`` defaults to ``False`` (env
  ``AGENT_CHECKOUT_CAPABILITY_GATE``). With it off, ``capability_gate_permits_order_create``
  always returns ``False`` and the caller keeps its exact today's behavior (a
  missing PSP is still a hard 400).
* ``settings.agent_checkout_capability_gate_merchants`` (env
  ``AGENT_CHECKOUT_CAPABILITY_GATE_MERCHANTS``) is an optional canary allowlist:
  when NON-EMPTY, only a listed merchant can use the bypass. Empty = the boolean
  flag governs globally. The allowlist can only further restrict, never widen.

Scope guard: this gate ONLY relaxes the order-CREATE PSP requirement on the
deferred-payment path. It does NOT authorize a synchronous charge against a
non-PSP merchant — the actual settlement for such an order still flows through the
protocol/ACP lane (see the mapped charge-path seam in
docs/protocol_checkout_capability_canary_runbook.md).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from config.settings import settings
from db.database import database

logger = logging.getLogger(__name__)


async def merchant_is_protocol_capable(
    merchant_id: str, *, database_override: Optional[Any] = None
) -> bool:
    """True iff the merchant has a verified protocol/checkout capability row.

    Existence of a ``pcs_merchant_capabilities`` row (keyed by the non-null
    ``merchant_id`` PK) is the signal — pivota-backend only writes a row after a
    successful integration verify. Fails closed (returns ``False``) on any error.
    """
    mid = str(merchant_id or "").strip()
    if not mid:
        return False
    db_client = database_override or database
    try:
        row = await db_client.fetch_one(
            """
            SELECT 1
            FROM pcs_merchant_capabilities
            WHERE merchant_id = :merchant_id
            LIMIT 1
            """,
            {"merchant_id": mid},
        )
        return row is not None
    except Exception as exc:  # fail-closed: never let a lookup error open the gate
        logger.warning(
            "[CapabilityGate] pcs_merchant_capabilities lookup failed for %s: %s",
            mid,
            exc,
        )
        return False


def capability_gate_merchant_allowed(merchant_id: str) -> bool:
    """Flag + optional canary-allowlist check (no DB). Does NOT check capability."""
    if not settings.agent_checkout_capability_gate:
        return False
    allowlist = settings.agent_checkout_capability_gate_merchants
    if allowlist:
        return str(merchant_id or "").strip() in allowlist
    return True


async def capability_gate_permits_order_create(
    merchant_id: str, *, database_override: Optional[Any] = None
) -> bool:
    """Full gate decision for the order-create PSP bypass.

    Permits ONLY when ALL hold: (1) the flag is on, (2) the merchant passes the
    canary allowlist (if any), and (3) the merchant is protocol-capable. Any
    failure — including the flag being off — returns ``False`` (fail-closed), so
    with the flag off this is a guaranteed no-op.
    """
    if not capability_gate_merchant_allowed(merchant_id):
        return False
    return await merchant_is_protocol_capable(
        merchant_id, database_override=database_override
    )
