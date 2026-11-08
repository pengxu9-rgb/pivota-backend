"""The one ownership guard for routes that take a tenant id from the URL.

A role gate is not an ownership check. A route that admits "merchant" (or
"agent") and then reads `{merchant_id}` / `{agent_id}` out of the path hands
every merchant every other merchant's rows, because anyone can self-register
as a merchant or an agent. These helpers decide WHOSE record the caller gets:

* staff (by default `ADMIN_ROLES`; a route may pass a wider or narrower set)
  keep cross-tenant access, but only on an employee token -- a merchant or
  agent membership never counts as staff, whatever its role string says;
* a `merchant` passes `assert_merchant_scope` only for the merchant_id its
  token carries, and an `agent` passes `assert_agent_scope` only for its own
  agent_id;
* everyone else gets 403.

Call the guard FIRST in the handler, before any read or write that uses the
id. tests/test_tenant_scope_route_sweep.py enumerates every mounted route with
a `{merchant_id}` or `{agent_id}` path parameter and fails when one neither
calls these guards nor sits in its reviewed allowlist.
"""
from __future__ import annotations

import logging
from typing import Any, Collection, Dict, Optional

from fastapi import HTTPException, status

from utils.auth import ADMIN_ROLES

logger = logging.getLogger(__name__)

NOT_AUTHORIZED_FOR_MERCHANT = "Not authorized for this merchant"
NOT_AUTHORIZED_FOR_AGENT = "Not authorized for this agent"
NO_RELATIONSHIP_WITH_AGENT = "No paid orders between this merchant and this agent"


def _target(value: Any) -> str:
    return "" if value is None else str(value)


def _claim(current_user: Dict[str, Any], key: str) -> str:
    value = current_user.get(key)
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value)


def is_scope_staff(
    current_user: Optional[Dict[str, Any]],
    staff_roles: Collection[str] = ADMIN_ROLES,
) -> bool:
    """True when the caller is staff for the purpose of a cross-tenant read.

    `membership_type` is checked the way `utils.auth.get_current_employee`
    checks it: a token minted for a merchant or agent membership is never
    staff, even if its role string were ever one of the staff roles.
    """
    if not isinstance(current_user, dict):
        return False
    role = current_user.get("role")
    if not isinstance(role, str) or role not in staff_roles:
        return False
    membership_type = str(current_user.get("membership_type") or "").strip().lower()
    return membership_type in ("", "employee")


def assert_merchant_scope(
    current_user: Optional[Dict[str, Any]],
    merchant_id: Any,
    *,
    staff_roles: Collection[str] = ADMIN_ROLES,
) -> None:
    """403 unless the caller is staff or the merchant that owns `merchant_id`."""
    target = _target(merchant_id)
    if target and is_scope_staff(current_user, staff_roles):
        return
    if (
        target
        and isinstance(current_user, dict)
        and current_user.get("role") == "merchant"
        and _claim(current_user, "merchant_id") == target
    ):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=NOT_AUTHORIZED_FOR_MERCHANT,
    )


def _owns_agent(current_user: Optional[Dict[str, Any]], target: str) -> bool:
    if not target or not isinstance(current_user, dict):
        return False
    if current_user.get("role") != "agent":
        return False
    # A token that names its agent is that agent and no other. A token with no
    # agent_id claim names no agent: its email is not proof of ownership.
    claimed = _claim(current_user, "agent_id")
    return bool(claimed) and claimed == target


def assert_agent_scope(
    current_user: Optional[Dict[str, Any]],
    agent_id: Any,
    *,
    staff_roles: Collection[str] = ADMIN_ROLES,
) -> None:
    """403 unless the caller is staff or the agent that owns `agent_id`.

    Same rule routes/agent_api.py applies to API-key callers
    (`str(order.get("agent_id")) != str(context.agent_id)`), for JWT callers:
    an `agent` token whose `agent_id` claim equals the path id.

    Deliberately NOT accepted: `email == agent_id`, `user_id == agent_id`, and
    `email == agents.owner_email`. None is an ownership relation. The first
    two compare unrelated identifiers (see routes/agent_management.py
    `_is_own_agent_record`). The third trusts an address nobody proved:
    staff create agents whose owner_email has no users row, and a self-service
    signup at that address (routes/auth.py) used to inherit the agent. Agent
    portal tokens carry the agent_id claim (routes/agent_account.login_agent),
    so only a claim-less legacy token (/auth/signin) is refused by this; such
    tokens never passed these routes before the shared guard either.
    """
    target = _target(agent_id)
    if target and is_scope_staff(current_user, staff_roles):
        return
    if _owns_agent(current_user, target):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=NOT_AUTHORIZED_FOR_AGENT,
    )


async def assert_agent_scope_or_owner(
    database: Any,
    current_user: Optional[Dict[str, Any]],
    agent_id: Any,
    *,
    staff_roles: Collection[str] = ADMIN_ROLES,
) -> None:
    """`assert_agent_scope`, awaitable, for the routes that used to accept the owner email.

    It used to load `agents.owner_email` and admit a claim-less agent token whose
    email matched. That fallback is gone on every route, reads and mutations
    alike (keys, webhooks, bank details, payouts, identity issuers, protocols):
    see assert_agent_scope for why the email is not proof. `database` is
    accepted and unused so the call sites keep one shape. Must be awaited; the
    route sweep test checks that it is.
    """
    assert_agent_scope(current_user, agent_id, staff_roles=staff_roles)


# payment_status values that mean the buyer paid (the dashboard's paid set:
# routes/merchant_dashboard_routes.PAID_PAYMENT_STATUSES_SQL). Fully refunded is out.
RELATIONSHIP_PAID_PAYMENT_STATUSES = (
    "paid", "completed", "succeeded", "success", "settled", "partially_refunded",
)


async def merchant_has_agent_relationship(database: Any, merchant_id: str, agent_id: str) -> bool:
    """True when the agent has placed at least one non-deleted, PAID order with the merchant.

    Orders, not payouts: `POST /merchants/{merchant_id}/payouts/bulk` lets the
    merchant write `agent_payouts` rows for any agent_id it likes, so a payout
    row is something the merchant can forge. An order's agent_id is stamped by
    the agent's own authenticated call.

    Paid, not any order: a merchant can get an unpaid order cheaply by acting as a
    buyer on the agent's own surface and asking it to buy the merchant's product,
    and that alone used to unlock the agent's bank details. A paid order costs the
    buyer the order total.
    """
    statuses = ", ".join(f"'{s}'" for s in RELATIONSHIP_PAID_PAYMENT_STATUSES)
    row = await database.fetch_one(
        f"""
        SELECT 1 AS present
        FROM orders
        WHERE merchant_id = :merchant_id
          AND agent_id = :agent_id
          AND (is_deleted IS NULL OR is_deleted = FALSE)
          AND LOWER(COALESCE(payment_status, '')) IN ({statuses})
        LIMIT 1
        """,
        {"merchant_id": str(merchant_id), "agent_id": str(agent_id)},
    )
    return row is not None


async def assert_merchant_agent_relationship(
    database: Any,
    current_user: Optional[Dict[str, Any]],
    merchant_id: Any,
    agent_id: Any,
    *,
    staff_roles: Collection[str] = ADMIN_ROLES,
) -> None:
    """Merchant scope, then (for a non-staff caller) a real merchant-agent relationship.

    For routes keyed on BOTH a merchant and an agent where the caller is the
    merchant: owning the merchant is not enough to read an arbitrary agent's
    records. Staff skip the relationship check.
    """
    assert_merchant_scope(current_user, merchant_id, staff_roles=staff_roles)
    if is_scope_staff(current_user, staff_roles):
        return
    target_agent = _target(agent_id)
    if not target_agent or not await merchant_has_agent_relationship(
        database, _target(merchant_id), target_agent
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=NO_RELATIONSHIP_WITH_AGENT,
        )
