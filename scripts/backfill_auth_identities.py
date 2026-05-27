#!/usr/bin/env python3
"""
Backfill canonical identities and scoped memberships from legacy auth tables.

Safe to rerun. It does not delete or mutate legacy auth rows.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from db.auth_identity import upsert_credential_hash, upsert_membership, ensure_identity
from db.database import database


def _row_to_dict(row: Any) -> Dict[str, Any]:
    try:
        return dict(row or {})
    except Exception:
        return {}


def _norm_email(value: Any) -> str:
    return str(value or "").strip().lower()


async def _backfill_users() -> int:
    rows = await database.fetch_all(
        """
        SELECT id, email, password_hash, full_name, role, active, merchant_id
        FROM users
        WHERE COALESCE(email, '') <> ''
        """
    )
    count = 0
    for raw in rows:
        row = _row_to_dict(raw)
        email = _norm_email(row.get("email"))
        if not email:
            continue
        identity = await ensure_identity(
            email=email,
            full_name=row.get("full_name"),
            status="active" if row.get("active") is not False else "inactive",
        )
        if row.get("password_hash"):
            await upsert_credential_hash(
                identity_id=identity["identity_id"],
                password_hash=row["password_hash"],
                source="users_backfill",
            )

        role = str(row.get("role") or "").strip().lower()
        merchant_id = row.get("merchant_id")
        if role == "merchant" and not merchant_id:
            merchant = await database.fetch_one(
                """
                SELECT merchant_id
                FROM merchant_onboarding
                WHERE LOWER(contact_email) = LOWER(:email)
                ORDER BY created_at DESC
                LIMIT 1
                """,
                {"email": email},
            )
            merchant_id = _row_to_dict(merchant).get("merchant_id")
        if role == "merchant" and merchant_id:
            await upsert_membership(
                email=email,
                membership_type="merchant",
                role="merchant",
                entity_id=str(merchant_id),
                status="active" if row.get("active") is not False else "inactive",
                full_name=row.get("full_name"),
                source="users_backfill",
            )
            count += 1
        elif role == "agent":
            try:
                agent = await database.fetch_one(
                    """
                    SELECT agent_id
                    FROM agents
                    WHERE LOWER(COALESCE(email, owner_email, '')) = LOWER(:email)
                    LIMIT 1
                    """,
                    {"email": email},
                )
            except Exception:
                agent = await database.fetch_one(
                    """
                    SELECT agent_id
                    FROM agents
                    WHERE LOWER(COALESCE(owner_email, '')) = LOWER(:email)
                    LIMIT 1
                    """,
                    {"email": email},
                )
            agent_id = _row_to_dict(agent).get("agent_id")
            if agent_id:
                await upsert_membership(
                    email=email,
                    membership_type="agent",
                    role="agent",
                    entity_id=str(agent_id),
                    status="active" if row.get("active") is not False else "inactive",
                    full_name=row.get("full_name"),
                    source="users_backfill",
                )
                count += 1
    return count


async def _backfill_employees() -> int:
    try:
        rows = await database.fetch_all(
            """
            SELECT employee_id, name, email, role, status, permissions
            FROM employees
            WHERE COALESCE(email, '') <> ''
            """
        )
    except Exception:
        rows = await database.fetch_all(
            """
            SELECT employee_id, name, email, role, status
            FROM employees
            WHERE COALESCE(email, '') <> ''
            """
        )
    count = 0
    for raw in rows:
        row = _row_to_dict(raw)
        email = _norm_email(row.get("email"))
        employee_id = str(row.get("employee_id") or "").strip()
        if not email or not employee_id:
            continue
        await upsert_membership(
            email=email,
            membership_type="employee",
            role=str(row.get("role") or "employee").strip().lower(),
            entity_id=employee_id,
            status=str(row.get("status") or "active").strip().lower(),
            permissions=row.get("permissions"),
            full_name=row.get("name"),
            source="employees_backfill",
        )
        count += 1
    return count


async def _backfill_shop_users() -> int:
    rows = await database.fetch_all(
        """
        SELECT su.id, su.email, su.email_normalized, su.primary_role, su.is_guest, sup.password_hash
        FROM shop_users su
        LEFT JOIN shop_user_passwords sup ON sup.user_id = su.id
        WHERE COALESCE(su.email_normalized, su.email, '') <> ''
        """
    )
    count = 0
    for raw in rows:
        row = _row_to_dict(raw)
        email = _norm_email(row.get("email_normalized") or row.get("email"))
        shop_user_id = str(row.get("id") or "").strip()
        if not email or not shop_user_id:
            continue
        await upsert_membership(
            email=email,
            membership_type="customer",
            role=str(row.get("primary_role") or "customer").strip().lower(),
            entity_id=shop_user_id,
            status="inactive" if row.get("is_guest") is True else "active",
            password_hash=row.get("password_hash"),
            credential_source="shop_user_passwords_backfill" if row.get("password_hash") else None,
            source="shop_users_backfill",
        )
        count += 1
    return count


async def main() -> None:
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        users = await _backfill_users()
        employees = await _backfill_employees()
        shop_users = await _backfill_shop_users()
        print(
            "auth identity backfill complete: "
            f"users_memberships={users} employee_memberships={employees} customer_memberships={shop_users}"
        )
    finally:
        await database.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
