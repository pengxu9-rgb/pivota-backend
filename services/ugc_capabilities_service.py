from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select, text

from db.database import database
from db.orders import orders as orders_table
from db.reviews_center import buyer_review_user_subject, product_reviews, ugc_question_replies, ugc_questions
from services.review_moderation_policy import assess_review_text_risk_with_deepseek, merge_moderation_risk_flags


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
                  order_id TEXT NULL,
                  review_id BIGINT NOT NULL REFERENCES product_reviews(id) ON DELETE CASCADE,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_buyer_review_user_subject_user
                  ON buyer_review_user_subject(user_id);
                CREATE INDEX IF NOT EXISTS idx_buyer_review_user_subject_subject
                  ON buyer_review_user_subject(subject_type, subject_id);
                CREATE INDEX IF NOT EXISTS idx_buyer_review_user_subject_order_id
                  ON buyer_review_user_subject(order_id);
                CREATE UNIQUE INDEX IF NOT EXISTS ux_buyer_review_user_subject_order
                  ON buyer_review_user_subject(user_id, subject_type, subject_id, order_id);
                CREATE UNIQUE INDEX IF NOT EXISTS ux_buyer_review_user_subject_legacy_null_order
                  ON buyer_review_user_subject(user_id, subject_type, subject_id)
                  WHERE order_id IS NULL;
                """
            )
        )
    # Backward-compatible in-place hardening for environments with old schema.
    try:
        await database.execute(text("ALTER TABLE buyer_review_user_subject ADD COLUMN IF NOT EXISTS order_id TEXT"))
        await database.execute(text("ALTER TABLE buyer_review_user_subject DROP CONSTRAINT IF EXISTS ux_buyer_review_user_subject"))
        await database.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_buyer_review_user_subject_order_id "
                "ON buyer_review_user_subject(order_id)"
            )
        )
        await database.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_buyer_review_user_subject_order "
                "ON buyer_review_user_subject(user_id, subject_type, subject_id, order_id)"
            )
        )
        await database.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_buyer_review_user_subject_legacy_null_order "
                "ON buyer_review_user_subject(user_id, subject_type, subject_id) "
                "WHERE order_id IS NULL"
            )
        )
    except Exception:
        # Best-effort only; callers can still proceed with degraded behavior.
        pass

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
                  risk_flags JSONB NULL,
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
    try:
        await database.execute(text("ALTER TABLE ugc_questions ADD COLUMN IF NOT EXISTS risk_flags JSONB"))
    except Exception:
        pass

    try:
        await database.fetch_one(text("SELECT 1 FROM ugc_question_replies LIMIT 1"))
    except Exception:
        await database.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS ugc_question_replies (
                  id BIGSERIAL PRIMARY KEY,
                  question_id BIGINT NOT NULL REFERENCES ugc_questions(id) ON DELETE CASCADE,
                  user_id TEXT NOT NULL,
                  body TEXT NOT NULL,
                  risk_flags JSONB NULL,
                  status VARCHAR(16) NOT NULL DEFAULT 'active',
                  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_ugc_question_replies_question_created
                  ON ugc_question_replies(question_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_ugc_question_replies_user_created
                  ON ugc_question_replies(user_id, created_at DESC);
                """
            )
        )
    try:
        await database.execute(text("ALTER TABLE ugc_question_replies ADD COLUMN IF NOT EXISTS risk_flags JSONB"))
    except Exception:
        pass


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
            # Plain string, not text(): databases.Database._build_query calls
            # .values() on a ClauseElement when params are passed, and
            # TextClause has no .values -> AttributeError.
            """
            SELECT platform_product_id
            FROM product_group_members
            WHERE product_group_id = :pgid
              AND platform_product_id IS NOT NULL
              AND platform_product_id != ''
            """,
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
    """
    Best-effort purchase/fulfillment gate used for UGC eligibility.

    Business intent:
      - allow paid / shipped(fulfilled) / delivered orders
      - do not treat cancelled/refunded as eligible

    NOTE: Different integrations may populate different fields; we accept multiple signals.
    """
    payment_status = str(row.get("payment_status") or "").strip().lower()
    status = str(row.get("status") or "").strip().lower()
    fulfillment_status = str(row.get("fulfillment_status") or "").strip().lower()

    cancelled = row.get("cancelled_at") is not None or status in {"cancelled", "canceled"}
    if cancelled:
        return False
    if payment_status in {"refunded"} or status in {"refunded"}:
        return False

    if payment_status in {"paid", "succeeded"}:
        return True
    if row.get("paid_at") is not None:
        return True

    # Some deployments only populate fulfillment timestamps/fields.
    if fulfillment_status in {"fulfilled", "shipped", "delivered"}:
        return True
    if row.get("delivered_at") is not None:
        return True
    if row.get("shipped_at") is not None:
        return True

    # Legacy deployments may encode purchase state in status only.
    if status in {"paid", "shipped", "delivered", "completed", "fulfilled"}:
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


def _normalize_order_id(value: Any) -> Optional[str]:
    oid = str(value or "").strip()
    return oid or None


def _subject_group_id(subject: UgcSubject) -> str:
    return str(subject.product_group_id or "").strip() or (
        subject.subject_id if subject.subject_type == "product_group" else ""
    )


async def _subject_candidate_product_ids(subject: UgcSubject) -> Tuple[Set[str], str]:
    product_ids: Set[str] = set()
    if subject.product_id:
        pid = str(subject.product_id).strip()
        if pid:
            product_ids.add(pid)

    group_id = _subject_group_id(subject)
    if group_id:
        member_ids = await get_product_group_member_product_ids(group_id)
        product_ids.update(member_ids)

    sid = str(subject.subject_id).strip()
    if sid:
        # Fallback for deployments where order item only carries subject_id.
        product_ids.add(sid)

    return product_ids, group_id


def _order_matches_subject(*, row: Dict[str, Any], product_ids: Set[str], group_id: str) -> bool:
    items = _coerce_items(row.get("items"))
    for it in items:
        if group_id and any(gid == group_id for gid in _iter_candidate_group_ids(it)):
            return True
        if product_ids:
            for pid in _iter_candidate_item_ids(it):
                if pid in product_ids:
                    return True
    return False


async def get_paid_order_ids_for_subject(*, email_normalized: str, subject: UgcSubject) -> List[str]:
    """
    Returns matched paid order ids for this subject, sorted by latest order first.
    """
    await _ensure_database_connected()

    email_norm = str(email_normalized or "").strip().lower()
    if not email_norm:
        return []

    product_ids, group_id = await _subject_candidate_product_ids(subject)

    query = (
        select(orders_table)
        .where(
            and_(
                orders_table.c.is_deleted.is_(False),
                func.lower(orders_table.c.customer_email) == email_norm,
                or_(
                    func.lower(orders_table.c.payment_status).in_(["paid", "succeeded"]),
                    orders_table.c.paid_at.isnot(None),
                    func.lower(orders_table.c.status).in_(["paid", "shipped", "delivered", "completed", "fulfilled"]),
                    func.lower(orders_table.c.fulfillment_status).in_(["fulfilled", "shipped", "delivered"]),
                    orders_table.c.shipped_at.isnot(None),
                    orders_table.c.delivered_at.isnot(None),
                ),
            )
        )
        .order_by(orders_table.c.created_at.desc())
        .limit(200)
    )

    try:
        rows = await database.fetch_all(query)
    except Exception:
        return []

    out: List[str] = []
    seen: Set[str] = set()
    for r in rows:
        row = dict(r)
        if not _is_paid_order(row):
            continue
        oid = _normalize_order_id(row.get("order_id"))
        if not oid or oid in seen:
            continue
        if _order_matches_subject(row=row, product_ids=product_ids, group_id=group_id):
            out.append(oid)
            seen.add(oid)

    return out


async def user_has_purchased_subject(*, email_normalized: str, subject: UgcSubject) -> bool:
    """
    Returns whether the accounts user (by email) has at least one paid/completed order
    containing a matching item for this subject.
    """
    return bool(await get_paid_order_ids_for_subject(email_normalized=email_normalized, subject=subject))


async def list_user_review_bindings(*, user_id: str, subject_type: str, subject_id: str) -> List[Dict[str, Any]]:
    await ensure_ugc_tables_exist()
    uid = str(user_id or "").strip()
    st = str(subject_type or "").strip()
    sid = str(subject_id or "").strip()
    if not uid or not st or not sid:
        return []
    try:
        rows = await database.fetch_all(
            buyer_review_user_subject.select()
            .where(
                (buyer_review_user_subject.c.user_id == uid)
                & (buyer_review_user_subject.c.subject_type == st)
                & (buyer_review_user_subject.c.subject_id == sid)
            )
            .order_by(buyer_review_user_subject.c.created_at.desc())
        )
    except Exception:
        return []

    out: List[Dict[str, Any]] = []
    for row in rows:
        try:
            rid = int(row["review_id"])  # type: ignore[index]
        except Exception:
            rid = 0
        if rid <= 0:
            continue
        try:
            created_at = row["created_at"]  # type: ignore[index]
        except Exception:
            created_at = None
        try:
            order_id_raw = row["order_id"]  # type: ignore[index]
        except Exception:
            order_id_raw = None
        out.append(
            {
                "review_id": rid,
                "order_id": _normalize_order_id(order_id_raw),
                "created_at": created_at,
            }
        )
    return out


def compute_review_slot_usage(*, paid_order_ids: List[str], bindings: List[Dict[str, Any]]) -> Dict[str, Any]:
    paid_newest: List[str] = []
    seen_paid: Set[str] = set()
    for raw in paid_order_ids:
        oid = _normalize_order_id(raw)
        if not oid or oid in seen_paid:
            continue
        paid_newest.append(oid)
        seen_paid.add(oid)

    used_set: Set[str] = set()
    legacy_binding_count = 0
    for b in bindings:
        oid = _normalize_order_id((b or {}).get("order_id"))
        if oid:
            if oid in seen_paid:
                used_set.add(oid)
            continue
        legacy_binding_count += 1

    paid_oldest = list(reversed(paid_newest))
    legacy_consumed: List[str] = []
    for oid in paid_oldest:
        if oid in used_set:
            continue
        if len(legacy_consumed) >= legacy_binding_count:
            break
        legacy_consumed.append(oid)
        used_set.add(oid)

    used_order_ids = [oid for oid in paid_newest if oid in used_set]
    available_order_ids = [oid for oid in paid_newest if oid not in used_set]
    return {
        "paid_order_ids": paid_newest,
        "used_order_ids": used_order_ids,
        "available_order_ids": available_order_ids,
        "legacy_binding_count": int(legacy_binding_count),
        "legacy_consumed_order_ids": legacy_consumed,
        "total_paid_orders": len(paid_newest),
        "used_slots": len(used_order_ids),
        "available_slots": len(available_order_ids),
    }


async def get_review_slot_summary(
    *,
    email_normalized: str,
    user_id: str,
    subject: UgcSubject,
) -> Dict[str, Any]:
    paid_order_ids = await get_paid_order_ids_for_subject(
        email_normalized=email_normalized,
        subject=subject,
    )
    bindings = await list_user_review_bindings(
        user_id=user_id,
        subject_type=subject.subject_type,
        subject_id=subject.subject_id,
    )
    usage = compute_review_slot_usage(paid_order_ids=paid_order_ids, bindings=bindings)
    usage["bindings"] = bindings
    return usage


async def has_user_reviewed_subject(*, user_id: str, subject_type: str, subject_id: str) -> bool:
    bindings = await list_user_review_bindings(user_id=user_id, subject_type=subject_type, subject_id=subject_id)
    return bool(bindings)


async def get_user_review_for_subject(
    *,
    user_id: str,
    subject_type: str,
    subject_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Returns the bound review (if any) for this user+subject, including verification + whether it has a rating.

    This is user-specific and should only be used in non-cacheable personalization flows.
    """
    await ensure_ugc_tables_exist()
    uid = str(user_id or "").strip()
    st = str(subject_type or "").strip()
    sid = str(subject_id or "").strip()
    if not uid or not st or not sid:
        return None

    bindings = await list_user_review_bindings(user_id=uid, subject_type=st, subject_id=sid)
    if not bindings:
        return None
    top_binding = bindings[0]
    review_id = int(top_binding.get("review_id") or 0)

    if review_id <= 0:
        return None

    try:
        review = await database.fetch_one(product_reviews.select().where(product_reviews.c.id == review_id))
    except Exception:
        review = None

    verification = ""
    has_rating = False
    if review:
        try:
            verification = str(review["verification"] or "")  # type: ignore[index]
        except Exception:
            verification = str(getattr(review, "verification", "") or "")
        try:
            has_rating = review["rating"] is not None  # type: ignore[index]
        except Exception:
            has_rating = getattr(review, "rating", None) is not None

    return {
        "review_id": int(review_id),
        "verification": verification.strip() or "unverified",
        "has_rating": bool(has_rating),
        "order_id": _normalize_order_id(top_binding.get("order_id")),
    }


async def bind_user_review_subject(
    *,
    user_id: str,
    subject_type: str,
    subject_id: str,
    review_id: int,
    order_id: Optional[str] = None,
) -> None:
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
                order_id=_normalize_order_id(order_id),
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
            & (ugc_questions.c.status.in_(["active", "under_review"]))
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
) -> Dict[str, Any]:
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

    moderation = await assess_review_text_risk_with_deepseek(title="Product question", body=q)
    moderation_state = _normalize_ugc_moderation_status(str(moderation.get("moderation_state") or "under_review"))
    risk_flags = merge_moderation_risk_flags(
        None,
        moderation=moderation,
        extra={
            "source": "accounts",
            "accounts_user_id": uid,
            "ugc_actor_type": "guest" if uid.startswith("guest:") else "account",
            "ugc_content_type": "question",
            "subject_type": st,
            "subject_id": sid,
        },
    )

    try:
        question_id = await database.execute(
            ugc_questions.insert().values(
                user_id=uid,
                subject_type=st,
                subject_id=sid,
                question=q,
                risk_flags=risk_flags,
                status=moderation_state,
                created_at=datetime.now(timezone.utc),
            )
        )
    except Exception:
        raise HTTPException(status_code=500, detail="QUESTION_CREATE_FAILED")

    try:
        qid = int(question_id)
    except Exception:
        qid = 0
    return {"question_id": qid, "moderation_status": moderation_state}


async def list_questions(
    *,
    subject_type: str,
    subject_id: str,
    limit: int = 10,
) -> Dict[str, Any]:
    """
    List recent questions for a subject.

    This endpoint is safe to expose publicly (no user_id returned).
    """
    await ensure_ugc_tables_exist()
    st = str(subject_type or "").strip()
    sid = str(subject_id or "").strip()
    try:
        limit_n = int(limit)
    except Exception:
        limit_n = 10
    limit_n = max(1, min(50, limit_n))

    if not st or not sid:
        raise HTTPException(status_code=400, detail="INVALID_SUBJECT")

    base_filter = (
        (ugc_questions.c.subject_type == st)
        & (ugc_questions.c.subject_id == sid)
        & (ugc_questions.c.status == "active")
    )

    try:
        total = await database.fetch_val(
            select(func.count()).select_from(ugc_questions).where(base_filter)
        )
    except Exception:
        total = 0

    rows = await database.fetch_all(
        select(ugc_questions.c.id, ugc_questions.c.question, ugc_questions.c.created_at)
        .where(base_filter)
        .order_by(ugc_questions.c.created_at.desc())
        .limit(limit_n)
    )

    question_ids: List[int] = []
    for row in rows:
        try:
            qid = int(row["id"])  # type: ignore[index]
        except Exception:
            qid = 0
        if qid:
            question_ids.append(qid)

    reply_counts: Dict[int, int] = {}
    if question_ids:
        try:
            reply_rows = await database.fetch_all(
                select(ugc_question_replies.c.question_id, func.count().label("cnt"))
                .where(
                    (ugc_question_replies.c.question_id.in_(question_ids))
                    & (ugc_question_replies.c.status == "active")
                )
                .group_by(ugc_question_replies.c.question_id)
            )
            for r in reply_rows:
                try:
                    qid = int(r["question_id"])  # type: ignore[index]
                    cnt = int(r["cnt"] or 0)  # type: ignore[index]
                except Exception:
                    continue
                reply_counts[qid] = cnt
        except Exception:
            reply_counts = {}

    items: List[Dict[str, Any]] = []
    for row in rows:
        try:
            qid = int(row["id"])  # type: ignore[index]
        except Exception:
            qid = 0
        try:
            question = str(row["question"] or "").strip()  # type: ignore[index]
        except Exception:
            question = ""
        try:
            created_at = row["created_at"]  # type: ignore[index]
        except Exception:
            created_at = None
        created_at_iso: Optional[str]
        if isinstance(created_at, datetime):
            created_at_iso = (
                created_at.replace(tzinfo=timezone.utc).isoformat()
                if created_at.tzinfo is None
                else created_at.isoformat()
            )
        else:
            created_at_iso = None

        if not question:
            continue

        items.append(
            {
                "question_id": qid,
                "question": question,
                "created_at": created_at_iso,
                "replies": int(reply_counts.get(qid, 0)),
            }
        )

    return {"count": int(total or 0), "items": items}


def _normalize_ugc_moderation_status(status: str) -> str:
    next_status = str(status or "").strip().lower()
    if next_status not in {"active", "under_review", "removed"}:
        raise HTTPException(status_code=400, detail="INVALID_STATUS")
    return next_status


async def set_question_status(
    *,
    question_id: int,
    status: str,
    reason: Optional[str] = None,
    actor: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    del reason, actor
    await ensure_ugc_tables_exist()
    try:
        qid = int(question_id)
    except Exception:
        qid = 0
    if qid <= 0:
        raise HTTPException(status_code=400, detail="INVALID_QUESTION")

    next_status = _normalize_ugc_moderation_status(status)
    row = await database.fetch_one(ugc_questions.select().where(ugc_questions.c.id == qid))
    if not row:
        raise HTTPException(status_code=404, detail="QUESTION_NOT_FOUND")

    await database.execute(
        ugc_questions.update()
        .where(ugc_questions.c.id == qid)
        .values(status=next_status)
    )
    return {"status": "success", "question_id": qid, "new_status": next_status}


async def list_questions_for_moderation(
    *,
    status: Optional[str] = "under_review",
    subject_type: Optional[str] = None,
    subject_id: Optional[str] = None,
    question_id: Optional[int] = None,
    moderation_decision: Optional[str] = None,
    risk_level: Optional[str] = None,
    employee_review_queue: Optional[bool] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    await ensure_ugc_tables_exist()
    limit_n = max(1, min(int(limit or 50), 200))
    where = ["1=1"]
    params: Dict[str, Any] = {}

    status_norm = str(status or "").strip().lower()
    if status_norm:
        where.append("ugc_questions.status = :status")
        params["status"] = _normalize_ugc_moderation_status(status_norm)
    subject_type_norm = str(subject_type or "").strip()
    if subject_type_norm:
        where.append("ugc_questions.subject_type = :subject_type")
        params["subject_type"] = subject_type_norm
    subject_id_norm = str(subject_id or "").strip()
    if subject_id_norm:
        where.append("ugc_questions.subject_id = :subject_id")
        params["subject_id"] = subject_id_norm
    if question_id is not None:
        where.append("ugc_questions.id = :question_id")
        params["question_id"] = int(question_id)
    moderation_decision_norm = str(moderation_decision or "").strip().lower()
    if moderation_decision_norm:
        where.append("ugc_questions.risk_flags ->> 'moderation_decision' = :moderation_decision")
        params["moderation_decision"] = moderation_decision_norm
    risk_level_norm = str(risk_level or "").strip().lower()
    if risk_level_norm:
        where.append("ugc_questions.risk_flags ->> 'text_risk_level' = :risk_level")
        params["risk_level"] = risk_level_norm
    if employee_review_queue is not None:
        where.append("COALESCE(ugc_questions.risk_flags ->> 'employee_review_queue', 'false') = :employee_review_queue")
        params["employee_review_queue"] = "true" if employee_review_queue else "false"

    rows = await database.fetch_all(
        f"""
        SELECT
          ugc_questions.id,
          ugc_questions.user_id,
          ugc_questions.subject_type,
          ugc_questions.subject_id,
          ugc_questions.question,
          ugc_questions.risk_flags,
          ugc_questions.status,
          ugc_questions.created_at,
          COALESCE(reply_stats.pending_reply_count, 0)::int AS pending_reply_count,
          COALESCE(reply_stats.active_reply_count, 0)::int AS active_reply_count,
          COALESCE(reply_stats.total_reply_count, 0)::int AS total_reply_count
        FROM {ugc_questions.name}
        LEFT JOIN (
          SELECT
            question_id,
            COUNT(*)::int AS total_reply_count,
            COALESCE(SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END), 0)::int AS active_reply_count,
            COALESCE(SUM(CASE WHEN status = 'under_review' THEN 1 ELSE 0 END), 0)::int AS pending_reply_count
          FROM {ugc_question_replies.name}
          GROUP BY question_id
        ) AS reply_stats ON reply_stats.question_id = ugc_questions.id
        WHERE {' AND '.join(where)}
        ORDER BY ugc_questions.created_at DESC, ugc_questions.id DESC
        LIMIT {limit_n}
        """,
        params,
    )
    return {"items": [dict(r) for r in rows], "limit": limit_n}


async def set_question_reply_status(
    *,
    question_id: int,
    reply_id: int,
    status: str,
    reason: Optional[str] = None,
    actor: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    del reason, actor
    await ensure_ugc_tables_exist()
    try:
        qid = int(question_id)
        rid = int(reply_id)
    except Exception:
        qid = 0
        rid = 0
    if qid <= 0:
        raise HTTPException(status_code=400, detail="INVALID_QUESTION")
    if rid <= 0:
        raise HTTPException(status_code=400, detail="INVALID_REPLY")

    next_status = _normalize_ugc_moderation_status(status)
    row = await database.fetch_one(
        ugc_question_replies.select().where(
            (ugc_question_replies.c.id == rid)
            & (ugc_question_replies.c.question_id == qid)
        )
    )
    if not row:
        raise HTTPException(status_code=404, detail="REPLY_NOT_FOUND")

    await database.execute(
        ugc_question_replies.update()
        .where(
            (ugc_question_replies.c.id == rid)
            & (ugc_question_replies.c.question_id == qid)
        )
        .values(status=next_status)
    )
    return {"status": "success", "question_id": qid, "reply_id": rid, "new_status": next_status}


async def list_question_replies_for_moderation(
    *,
    question_id: Optional[int] = None,
    status: Optional[str] = "under_review",
    moderation_decision: Optional[str] = None,
    risk_level: Optional[str] = None,
    employee_review_queue: Optional[bool] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    await ensure_ugc_tables_exist()
    limit_n = max(1, min(int(limit or 50), 200))
    where = ["1=1"]
    params: Dict[str, Any] = {}

    if question_id is not None:
        where.append("ugc_question_replies.question_id = :question_id")
        params["question_id"] = int(question_id)
    status_norm = str(status or "").strip().lower()
    if status_norm:
        where.append("ugc_question_replies.status = :status")
        params["status"] = _normalize_ugc_moderation_status(status_norm)
    moderation_decision_norm = str(moderation_decision or "").strip().lower()
    if moderation_decision_norm:
        where.append("ugc_question_replies.risk_flags ->> 'moderation_decision' = :moderation_decision")
        params["moderation_decision"] = moderation_decision_norm
    risk_level_norm = str(risk_level or "").strip().lower()
    if risk_level_norm:
        where.append("ugc_question_replies.risk_flags ->> 'text_risk_level' = :risk_level")
        params["risk_level"] = risk_level_norm
    if employee_review_queue is not None:
        where.append("COALESCE(ugc_question_replies.risk_flags ->> 'employee_review_queue', 'false') = :employee_review_queue")
        params["employee_review_queue"] = "true" if employee_review_queue else "false"

    rows = await database.fetch_all(
        f"""
        SELECT
          ugc_question_replies.id,
          ugc_question_replies.question_id,
          ugc_question_replies.user_id,
          ugc_question_replies.body,
          ugc_question_replies.risk_flags,
          ugc_question_replies.status,
          ugc_question_replies.created_at,
          ugc_questions.subject_type,
          ugc_questions.subject_id,
          ugc_questions.question
        FROM {ugc_question_replies.name}
        LEFT JOIN {ugc_questions.name} ON ugc_questions.id = ugc_question_replies.question_id
        WHERE {' AND '.join(where)}
        ORDER BY ugc_question_replies.created_at DESC, ugc_question_replies.id DESC
        LIMIT {limit_n}
        """,
        params,
    )
    return {"items": [dict(r) for r in rows], "limit": limit_n}


async def _question_exists(question_id: int) -> bool:
    await ensure_ugc_tables_exist()
    try:
        qid = int(question_id)
    except Exception:
        return False
    if qid <= 0:
        return False
    try:
        row = await database.fetch_one(
            select(ugc_questions.c.id)
            .where((ugc_questions.c.id == qid) & (ugc_questions.c.status == "active"))
            .limit(1)
        )
    except Exception:
        row = None
    return bool(row)


async def is_reply_rate_limited(
    *,
    user_id: str,
    question_id: int,
    window_seconds: int = 30,
) -> bool:
    await ensure_ugc_tables_exist()
    uid = str(user_id or "").strip()
    try:
        qid = int(question_id)
    except Exception:
        qid = 0
    if not uid or qid <= 0:
        return False

    try:
        row = await database.fetch_one(
            select(ugc_question_replies.c.created_at)
            .where(
                (ugc_question_replies.c.user_id == uid)
                & (ugc_question_replies.c.question_id == qid)
                & (ugc_question_replies.c.status.in_(["active", "under_review"]))
            )
            .order_by(ugc_question_replies.c.created_at.desc())
            .limit(1)
        )
    except Exception:
        row = None
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


async def create_question_reply(
    *,
    user_id: str,
    question_id: int,
    body: str,
    window_seconds: int = 30,
) -> Dict[str, Any]:
    await ensure_ugc_tables_exist()
    uid = str(user_id or "").strip()
    try:
        qid = int(question_id)
    except Exception:
        qid = 0
    b = str(body or "").strip()

    if not uid:
        raise HTTPException(status_code=401, detail="NOT_AUTHENTICATED")
    if qid <= 0:
        raise HTTPException(status_code=400, detail="INVALID_QUESTION")
    if not await _question_exists(qid):
        raise HTTPException(status_code=404, detail="QUESTION_NOT_FOUND")
    if len(b) < 2:
        raise HTTPException(status_code=400, detail="REPLY_TOO_SHORT")
    if len(b) > 4000:
        raise HTTPException(status_code=400, detail="REPLY_TOO_LONG")

    if await is_reply_rate_limited(user_id=uid, question_id=qid, window_seconds=window_seconds):
        raise HTTPException(status_code=429, detail="RATE_LIMITED")

    moderation = await assess_review_text_risk_with_deepseek(title="Product question answer", body=b)
    moderation_state = _normalize_ugc_moderation_status(str(moderation.get("moderation_state") or "under_review"))
    risk_flags = merge_moderation_risk_flags(
        None,
        moderation=moderation,
        extra={
            "source": "accounts",
            "accounts_user_id": uid,
            "ugc_actor_type": "guest" if uid.startswith("guest:") else "account",
            "ugc_content_type": "question_reply",
            "question_id": qid,
        },
    )

    try:
        reply_id = await database.execute(
            ugc_question_replies.insert().values(
                question_id=qid,
                user_id=uid,
                body=b,
                risk_flags=risk_flags,
                status=moderation_state,
                created_at=datetime.now(timezone.utc),
            )
        )
    except Exception:
        raise HTTPException(status_code=500, detail="REPLY_CREATE_FAILED")

    try:
        rid = int(reply_id)
    except Exception:
        rid = 0
    return {"reply_id": rid, "moderation_status": moderation_state}


async def list_question_replies(
    *,
    question_id: int,
    limit: int = 20,
) -> Dict[str, Any]:
    await ensure_ugc_tables_exist()
    try:
        qid = int(question_id)
    except Exception:
        qid = 0
    if qid <= 0:
        raise HTTPException(status_code=400, detail="INVALID_QUESTION")
    if not await _question_exists(qid):
        raise HTTPException(status_code=404, detail="QUESTION_NOT_FOUND")

    try:
        limit_n = int(limit)
    except Exception:
        limit_n = 20
    limit_n = max(1, min(50, limit_n))

    base_filter = (ugc_question_replies.c.question_id == qid) & (ugc_question_replies.c.status == "active")
    try:
        total = await database.fetch_val(
            select(func.count()).select_from(ugc_question_replies).where(base_filter)
        )
    except Exception:
        total = 0

    rows = await database.fetch_all(
        select(ugc_question_replies.c.id, ugc_question_replies.c.body, ugc_question_replies.c.created_at)
        .where(base_filter)
        .order_by(ugc_question_replies.c.created_at.desc())
        .limit(limit_n)
    )

    items: List[Dict[str, Any]] = []
    for row in rows:
        try:
            rid = int(row["id"])  # type: ignore[index]
        except Exception:
            rid = 0
        try:
            body = str(row["body"] or "").strip()  # type: ignore[index]
        except Exception:
            body = ""
        try:
            created_at = row["created_at"]  # type: ignore[index]
        except Exception:
            created_at = None

        if not body:
            continue

        created_at_iso: Optional[str]
        if isinstance(created_at, datetime):
            created_at_iso = (
                created_at.replace(tzinfo=timezone.utc).isoformat()
                if created_at.tzinfo is None
                else created_at.isoformat()
            )
        else:
            created_at_iso = None

        items.append(
            {
                "reply_id": rid,
                "body": body,
                "created_at": created_at_iso,
            }
        )

    return {"count": int(total or 0), "items": items}


async def get_question(
    *,
    question_id: int,
) -> Dict[str, Any]:
    await ensure_ugc_tables_exist()
    try:
        qid = int(question_id)
    except Exception:
        qid = 0
    if qid <= 0:
        raise HTTPException(status_code=400, detail="INVALID_QUESTION")

    row = await database.fetch_one(
        select(
            ugc_questions.c.id,
            ugc_questions.c.subject_type,
            ugc_questions.c.subject_id,
            ugc_questions.c.question,
            ugc_questions.c.created_at,
        )
        .where((ugc_questions.c.id == qid) & (ugc_questions.c.status == "active"))
        .limit(1)
    )
    if not row:
        raise HTTPException(status_code=404, detail="QUESTION_NOT_FOUND")

    try:
        question = str(row["question"] or "").strip()  # type: ignore[index]
    except Exception:
        question = ""
    try:
        subject_type = str(row["subject_type"] or "").strip()  # type: ignore[index]
    except Exception:
        subject_type = ""
    try:
        subject_id = str(row["subject_id"] or "").strip()  # type: ignore[index]
    except Exception:
        subject_id = ""
    try:
        created_at = row["created_at"]  # type: ignore[index]
    except Exception:
        created_at = None
    created_at_iso: Optional[str]
    if isinstance(created_at, datetime):
        created_at_iso = (
            created_at.replace(tzinfo=timezone.utc).isoformat()
            if created_at.tzinfo is None
            else created_at.isoformat()
        )
    else:
        created_at_iso = None

    try:
        replies_count = await database.fetch_val(
            select(func.count()).select_from(ugc_question_replies).where(
                (ugc_question_replies.c.question_id == qid) & (ugc_question_replies.c.status == "active")
            )
        )
    except Exception:
        replies_count = 0

    return {
        "question_id": qid,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "question": question,
        "created_at": created_at_iso,
        "replies": int(replies_count or 0),
    }
