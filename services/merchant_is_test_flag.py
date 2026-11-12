"""The staff-set "this merchant is a test merchant" fact: `merchant_onboarding.is_test` (migration 282).

One writer, `set_merchant_is_test`, used by POST /admin/merchants/{merchant_id}/is-test. It changes
the column and writes an `employee_audit_logs` row in ONE transaction, so the fact never changes
without a record of who changed it and why. Setting the value a merchant already has changes
nothing and writes no row.

Readers: services/test_merchant_policy, behind MERCHANT_IS_TEST_FILTER_ENABLED (default off).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db.database import database

logger = logging.getLogger(__name__)

AUDIT_TARGET_TYPE = "merchant"
ACTION_SET = "merchant.is_test.set"
ACTION_CLEAR = "merchant.is_test.clear"


# Conditional on the value changing: under READ COMMITTED a second writer of the same value blocks
# on the first's row lock, re-checks the WHERE once it commits, and matches nothing, so exactly one
# of them writes the audit row (pinned in tests/test_admin_merchant_is_test_postgres.py).
SET_IS_TEST_SQL = (
    "UPDATE merchant_onboarding SET is_test = :v "
    "WHERE merchant_id = :m AND is_test <> :v RETURNING merchant_id"
)


class MerchantNotFound(LookupError):
    pass


def _actor_id(actor: Dict[str, Any]) -> Optional[str]:
    # The same order services/reviews_service._audit uses for employee_audit_logs.
    value = actor.get("employee_id") or actor.get("user_id") or actor.get("sub")
    return str(value) if value else None


async def get_merchant_is_test(merchant_id: str) -> bool:
    """The stored value. Raises MerchantNotFound for an unknown merchant; a DB error (the column
    not there, say) propagates."""
    row = await database.fetch_one(
        "SELECT is_test FROM merchant_onboarding WHERE merchant_id = :m", {"m": merchant_id}
    )
    if row is None:
        raise MerchantNotFound(merchant_id)
    return bool(row["is_test"])


async def set_merchant_is_test(
    *, merchant_id: str, is_test: bool, reason: str, actor: Dict[str, Any]
) -> Dict[str, Any]:
    """Set or clear the fact. Returns {"merchant_id", "is_test", "changed"}.

    The UPDATE is conditional on the value changing, so of two concurrent writers of the same
    value exactly one changes the row and writes the audit row. Raises MerchantNotFound for an
    unknown merchant; any DB error (the column or the audit table missing) rolls both back."""
    from db.reviews_center import employee_audit_logs

    value = bool(is_test)
    async with database.transaction():
        changed = await database.fetch_one(SET_IS_TEST_SQL, {"v": value, "m": merchant_id})
        if changed is None:
            current = await get_merchant_is_test(merchant_id)  # MerchantNotFound for an unknown id
            return {"merchant_id": merchant_id, "is_test": current, "changed": False}
        await database.execute(
            employee_audit_logs.insert().values(
                actor_employee_id=_actor_id(actor),
                actor_email=actor.get("email"),
                action=ACTION_SET if value else ACTION_CLEAR,
                target_type=AUDIT_TARGET_TYPE,
                target_id=merchant_id,
                reason=reason,
                before_json={"is_test": not value},
                after_json={"is_test": value},
                created_at=datetime.now(timezone.utc),
            )
        )
    # WARNING, not INFO: prod drops INFO from plain module loggers, and this is an audit line.
    logger.warning(
        "merchant is_test %s: merchant=%s actor=%s",
        "set" if value else "cleared",
        merchant_id,
        _actor_id(actor),
    )
    try:
        from services.test_merchant_policy import invalidate_cache

        invalidate_cache()
    except Exception:  # noqa: BLE001 - the write is done; the cache expires on its own
        logger.warning("merchant is_test: policy cache invalidation failed", exc_info=True)
    return {"merchant_id": merchant_id, "is_test": value, "changed": True}


async def list_is_test_audit(merchant_id: str, *, limit: int = 20) -> List[Dict[str, Any]]:
    """The newest is_test audit rows for one merchant."""
    rows = await database.fetch_all(
        "SELECT action, reason, actor_employee_id, actor_email, created_at "
        "FROM employee_audit_logs "
        "WHERE target_type = :tt AND target_id = :m AND action IN (:a_set, :a_clear) "
        "ORDER BY created_at DESC, id DESC LIMIT :lim",
        {
            "tt": AUDIT_TARGET_TYPE,
            "m": merchant_id,
            "a_set": ACTION_SET,
            "a_clear": ACTION_CLEAR,
            "lim": int(limit),
        },
    )
    out: List[Dict[str, Any]] = []
    for r in rows:
        created = r["created_at"]
        out.append(
            {
                "action": r["action"],
                "reason": r["reason"],
                "actor_employee_id": r["actor_employee_id"],
                "actor_email": r["actor_email"],
                "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
            }
        )
    return out
