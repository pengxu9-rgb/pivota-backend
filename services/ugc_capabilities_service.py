from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select, text

from db.database import database
from db.orders import orders as orders_table
from db.reviews_center import buyer_review_user_subject, ugc_questions


@dataclass(frozen=True)
class UgcSubject:
    subject_type: str
    subject_id: str
    # Optional identifiers that help match purchases to the subject.
    product_id: Optional[str] = None
    product_group_id: Optional[str] = None


def _coerce_items(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return []
    if isinstance(value, list):
        return [it for it in value if isinstance(it, dict)]
    return []


async def _ensure_database_connected() -> None:
    if getattr(database, "is_connected", False):
        return
    try:
        await database.connect()
    except Exception:
        # Let callers fail with their own error path.
        return


async def ensure_ugc_tables_exist() -> None:
    """
    Best-effort schema guard for environments where migrations are not applied yet.
    Safe to call frequently.
    """
    await _ensure_database_connected()
    try:
        await database.fetch_one(text("SELECT 1 FROM buyer_review_user_subject LIMIT 1"))
    except Exception:
        await database.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS buyer_review_user_subject (
                  id BIGSERIAL PRIMARY KEY,
                  user_id TEXT NOT NULL,
                  subject_type VARCHAR(32) NOT NULL,
                  subject_id TEXT NOT NULL,
                  review_id BIGINT NOT NULL REFERENCES product_reviews(id) ON DELETE CASCADE,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  CONSTRAINT ux_buyer_review_user_subject UNIQUE(user_id, subject_type, subject_id)
                );
                CREATE INDEX IF NOT EXISTS idx_buyer_review_user_subject_user
                  ON buyer_review_user_subject(user_id);
                CREATE INDEX IF NOT EXISTS idx_buyer_review_user_subject_subject
                  ON buyer_review_user_subject(subject_type, subject_id);
                """
            )
        )

    try:
        await database.fetch_one(text("SELECT 1 FROM ugc_questions LIMIT 1"))
    except Exception:
        await database.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS ugc_questions (
                  id BIGSERIAL PRIMARY KEY,
                  user_id TEXT NOT NULL,
                  subject_type VARCHAR(32) NOT NULL,
                  subject_id TEXT NOT NULL,
                  question TEXT NOT NULL,
                  status VARCHAR(16) NOT NULL DEFAULT 'active',
                  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_ugc_questions_user_created
                  ON ugc_questions(user_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_ugc_questions_subject_created
                  ON ugc_questions(subject_type, subject_id, created_at DESC);
                """
            )
        )


async def get_product_group_member_product_ids(product_group_id: str) -> Set[str]:
    """
    Best-effort lookup. Returns member platform_product_id values for purchase matching.

    NOTE: this uses raw SQL because product_group_members isn't modeled in SQLAlchemy metadata.
    """
    pgid = str(product_group_id or "").strip()
    if not pgid:
        return set()

    await _ensure_database_connected()
    try:
        rows = await database.fetch_all(
            text(
                """
                SELECT platform_product_id
                FROM product_group_members
                WHERE product_group_id = :pgid
                  AND platform_product_id IS NOT NULL
                  AND platform_product_id != ''
                """
            ),
            {"pgid": pgid},
        )
    except Exception:
        return set()

    out: Set[str] = set()
    for r in rows:
        try:
            val = str(r["platform_product_id"] or "").strip()  # type: ignore[index]
        except Exception:
            val = str(getattr(r, "platform_product_id", "") or "").strip()
        if val:
            out.add(val)
    return out


def _is_paid_order(row: Dict[str, Any]) -> bool:
    payment_status = str(row.get("payment_status") or "").strip().lower()
    status = str(row.get("status") or "").strip().lower()
    if payment_status in {"paid", "succeeded"}:
        return True
    if row.get("paid_at") is not None:
        return True
    # Some legacy deployments may encode as status only.
    if status in {"paid", "delivered", "completed", "fulfilled"}:
        return True
    return False


def _iter_candidate_item_ids(item: Dict[str, Any]) -> Iterable[str]:
    candidates = [
        item.get("product_id"),
        item.get("productId"),
        item.get("platform_product_id"),
        item.get("platformProductId"),
        item.get("id"),
    ]
    for c in candidates:
        s = str(c or "").strip()
        if s:
            yield s


def _iter_candidate_group_ids(item: Dict[str, Any]) -> Iterable[str]:
    candidates = [
        item.get("product_group_id"),
        item.get("productGroupId"),
        item.get("group_id"),
        item.get("groupId"),
    ]
    for c in candidates:
        s = str(c or "").strip()
        if s:
            yield s


async def user_has_purchased_subject(*, email_normalized: str, subject: UgcSubject) -> bool:
    """
    Returns whether the accounts user (by email) has at least one paid/completed order
    containing a matching item for this subject.
    """
    await _ensure_database_connected()

    email_norm = str(email_normalized or "").strip().lower()
    if not email_norm:
        return False

    # Candidate product ids that can satisfy purchase. Start with explicit product_id (if any),
    # and optionally include group members.
    product_ids: Set[str] = set()
    if subject.product_id:
        product_ids.add(str(subject.product_id).strip())

    group_id = str(subject.product_group_id or "").strip() or (
        subject.subject_id if subject.subject_type == "product_group" else ""
    )
    if group_id:
        member_ids = await get_product_group_member_product_ids(group_id)
        product_ids.update(member_ids)

    # Always include subject_id as a fallback match.
    product_ids.add(str(subject.subject_id).strip())

    query = (
        select(orders_table)
        .where(
            and_(
                orders_table.c.is_deleted.is_(False),
                func.lower(orders_table.c.customer_email) == email_norm,
                or_(
                    func.lower(orders_table.c.payment_status).in_(["paid", "succeeded"]),
                    orders_table.c.paid_at.isnot(None),
                    func.lower(orders_table.c.status).in_(["paid", "delivered", "completed", "fulfilled"]),
                ),
            )
        )
        .order_by(orders_table.c.created_at.desc())
        .limit(200)
    )

    try:
        rows = await database.fetch_all(query)
    except Exception:
        return False

    for r in rows:
        row = dict(r)
        if not _is_paid_order(row):
            continue
        items = _coerce_items(row.get("items"))
        for it in items:
            if group_id:
                if any(gid == group_id for gid in _iter_candidate_group_ids(it)):
                    return True
            if product_ids:
                for pid in _iter_candidate_item_ids(it):
                    if pid in product_ids:
                        return True

    return False


async def has_user_reviewed_subject(*, user_id: str, subject_type: str, subject_id: str) -> bool:
    await ensure_ugc_tables_exist()
    uid = str(user_id or "").strip()
    st = str(subject_type or "").strip()
    sid = str(subject_id or "").strip()
    if not uid or not st or not sid:
        return False
    try:
        row = await database.fetch_one(
            buyer_review_user_subject.select().where(
                (buyer_review_user_subject.c.user_id == uid)
                & (buyer_review_user_subject.c.subject_type == st)
                & (buyer_review_user_subject.c.subject_id == sid)
            )
        )
        return bool(row)
    except Exception:
        return False


async def bind_user_review_subject(*, user_id: str, subject_type: str, subject_id: str, review_id: int) -> None:
    await ensure_ugc_tables_exist()
    uid = str(user_id or "").strip()
    st = str(subject_type or "").strip()
    sid = str(subject_id or "").strip()
    if not uid or not st or not sid:
        raise HTTPException(status_code=400, detail="INVALID_SUBJECT")
    try:
        await database.execute(
            buyer_review_user_subject.insert().values(
                user_id=uid,
                subject_type=st,
                subject_id=sid,
                review_id=int(review_id),
                created_at=datetime.now(timezone.utc),
            )
        )
    except Exception:
        # Unique constraint -> already reviewed.
        raise HTTPException(status_code=409, detail="ALREADY_REVIEWED")


async def is_question_rate_limited(*, user_id: str, subject_type: str, subject_id: str, window_seconds: int = 60) -> bool:
    await ensure_ugc_tables_exist()
    uid = str(user_id or "").strip()
    st = str(subject_type or "").strip()
    sid = str(subject_id or "").strip()
    if not uid or not st or not sid:
        return False

    query = (
        select(ugc_questions.c.created_at)
        .where(
            (ugc_questions.c.user_id == uid)
            & (ugc_questions.c.subject_type == st)
            & (ugc_questions.c.subject_id == sid)
            & (ugc_questions.c.status == "active")
        )
        .order_by(ugc_questions.c.created_at.desc())
        .limit(1)
    )
    try:
        row = await database.fetch_one(query)
    except Exception:
        return False
    if not row:
        return False

    try:
        created_at = row["created_at"]  # type: ignore[index]
    except Exception:
        created_at = None
    if not isinstance(created_at, datetime):
        return False

    now = datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at >= now - timedelta(seconds=int(window_seconds))


async def create_question(
    *,
    user_id: str,
    subject_type: str,
    subject_id: str,
    question: str,
    window_seconds: int = 60,
) -> int:
    await ensure_ugc_tables_exist()
    uid = str(user_id or "").strip()
    st = str(subject_type or "").strip()
    sid = str(subject_id or "").strip()
    q = str(question or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="NOT_AUTHENTICATED")
    if not st or not sid:
        raise HTTPException(status_code=400, detail="INVALID_SUBJECT")
    if len(q) < 5:
        raise HTTPException(status_code=400, detail="QUESTION_TOO_SHORT")
    if len(q) > 2000:
        raise HTTPException(status_code=400, detail="QUESTION_TOO_LONG")

    if await is_question_rate_limited(user_id=uid, subject_type=st, subject_id=sid, window_seconds=window_seconds):
        raise HTTPException(status_code=429, detail="RATE_LIMITED")

    try:
        question_id = await database.execute(
            ugc_questions.insert().values(
                user_id=uid,
                subject_type=st,
                subject_id=sid,
                question=q,
                status="active",
                created_at=datetime.now(timezone.utc),
            )
        )
    except Exception:
        raise HTTPException(status_code=500, detail="QUESTION_CREATE_FAILED")

    try:
        return int(question_id)
    except Exception:
        return 0
