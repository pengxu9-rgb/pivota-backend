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
NO_RELATIONSHIP_WITH_AGENT = "No orders between this merchant and this agent"


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


def _owns_agent(
    current_user: Optional[Dict[str, Any]],
    target: str,
    owner_email: Optional[str],
) -> bool:
    if not target or not isinstance(current_user, dict):
        return False
    if current_user.get("role") != "agent":
        return False
    claimed = _claim(current_user, "agent_id")
    if claimed:
        # A token that names its agent is that agent and no other; the email
        # route below is only for an agent user whose token carries no claim.
        return claimed == target
    wanted = str(owner_email or "").strip().lower()
    have = _claim(current_user, "email").strip().lower()
    return bool(wanted) and have == wanted


def assert_agent_scope(
    current_user: Optional[Dict[str, Any]],
    agent_id: Any,
    *,
    staff_roles: Collection[str] = ADMIN_ROLES,
    owner_email: Optional[str] = None,
) -> None:
    """403 unless the caller is staff or the agent that owns `agent_id`.

    Same rule routes/agent_api.py applies to API-key callers
    (`str(order.get("agent_id")) != str(context.agent_id)`), for JWT callers:
    an `agent` token whose `agent_id` claim equals the path id, or -- for the
    agent's own user, whose token carries NO agent_id claim -- whose `email`
    equals the agent record's `owner_email` (pass it in, or use
    `assert_agent_scope_or_owner`, which loads it). A token that carries an
    agent_id claim is judged on that claim alone.

    Deliberately NOT accepted: `email == agent_id` and `user_id == agent_id`.
    Neither is an ownership relation (see routes/agent_management.py
    `_is_own_agent_record`), and the second compares a users-table id with an
    agent id.
    """
    target = _target(agent_id)
    if target and is_scope_staff(current_user, staff_roles):
        return
    if _owns_agent(current_user, target, owner_email):
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
    """`assert_agent_scope`, loading `agents.owner_email` only when the claim check fails.

    Must be awaited; the route sweep test checks that it is.
    """
    target = _target(agent_id)
    if target and (
        is_scope_staff(current_user, staff_roles) or _owns_agent(current_user, target, None)
    ):
        return
    owner_email: Optional[str] = None
    if (
        target
        and isinstance(current_user, dict)
        and current_user.get("role") == "agent"
        and not _claim(current_user, "agent_id")
        and _claim(current_user, "email").strip()
    ):
        try:
            row = await database.fetch_one(
                "SELECT owner_email FROM agents WHERE agent_id = :agent_id",
                {"agent_id": target},
            )
        except Exception:  # noqa: BLE001 - a failed lookup proves nothing: refuse below
            logger.warning("agent owner lookup failed; refusing", exc_info=True)
            row = None
        if row is not None:
            owner_email = dict(row).get("owner_email")
    assert_agent_scope(
        current_user, target, staff_roles=staff_roles, owner_email=owner_email
    )


async def merchant_has_agent_relationship(database: Any, merchant_id: str, agent_id: str) -> bool:
    """True when the agent has placed at least one (non-deleted) order with the merchant.

    Orders, not payouts: `POST /merchants/{merchant_id}/payouts/bulk` lets the
    merchant write `agent_payouts` rows for any agent_id it likes, so a payout
    row is something the merchant can forge. An order's agent_id is stamped by
    the agent's own authenticated call.
    """
    row = await database.fetch_one(
        """
        SELECT 1 AS present
        FROM orders
        WHERE merchant_id = :merchant_id
          AND agent_id = :agent_id
          AND (is_deleted IS NULL OR is_deleted = FALSE)
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
