from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import logging
import time
import os
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone, date
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from uuid import UUID, uuid4

from fastapi import HTTPException

from db.database import database
from db.reviews_center import (
    employee_audit_logs,
    external_identities,
    import_batches,
    import_items,
    media_assets,
    product_reviews,
    review_featured,
    review_group,
    review_group_membership,
    review_interactions,
    seller_feedback,
)
from models.standard_product import StandardProduct
from models.reviews_refs import VARIANT_ID_SENTINEL

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """
    Normalize access across different row/record types:
    - SQLAlchemy Row: use `. _mapping`
    - databases.Record: supports `__getitem__`
    - plain dict: supports `.get`
    """
    if row is None:
        return default

    mapping: Optional[Mapping[str, Any]] = getattr(row, "_mapping", None)
    if mapping is not None:
        return mapping.get(key, default)

    try:
        return row[key]
    except Exception:
        pass

    try:
        getter = getattr(row, "get")
    except Exception:
        return default
    try:
        return getter(key, default)
    except Exception:
        return default


def _as_datetime(value: Any) -> Optional[datetime]:
    """
    Coerce DB-returned timestamps to `datetime`.

    Notes:
    - On SQLite, `databases` often returns TIMESTAMPTZ columns as strings.
    - We keep this permissive to avoid crashing read paths during local dev.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            # Handle both `2026-01-01T...Z` and SQLite-style `2026-01-01 00:00:00`.
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    return None


def _as_iso_datetime(value: Any) -> Optional[str]:
    dt = _as_datetime(value)
    if dt is not None:
        return dt.isoformat()
    if isinstance(value, str):
        s = value.strip()
        return s or None
    return None


def build_product_key(*, merchant_id: str, platform: str, platform_product_id: str) -> str:
    return f"{merchant_id}|{platform}|{platform_product_id}"


def build_sku_key(
    *,
    merchant_id: str,
    platform: str,
    platform_product_id: str,
    variant_id: Optional[str],
) -> str:
    product_key = build_product_key(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
    )
    v = _as_text(variant_id) or VARIANT_ID_SENTINEL
    return f"{product_key}|{v}"


def _encode_cursor(created_at: Any, review_id: int) -> str:
    """
    Encode a stable cursor for pagination: `${timestamp}|${id}` (base64url).

    Important: preserve SQLite's raw timestamp string formatting when available.
    SQLite commonly stores timestamps as strings like `YYYY-MM-DD HH:MM:SS`, and
    string-based comparisons are sensitive to formatting differences (space vs `T`,
    microseconds, timezone suffixes).
    """
    ts_s: Optional[str]
    if isinstance(created_at, str):
        ts_s = created_at.strip() or None
    else:
        ts = _as_datetime(created_at)
        ts_s = ts.isoformat() if ts is not None else None
    if not ts_s:
        raise ValueError("cursor created_at must be datetime-like")
    raw = f"{ts_s}|{int(review_id)}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _decode_cursor(cursor: str) -> Optional[Tuple[Any, int]]:
    if not cursor:
        return None
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8")
        ts_s, rid_s = raw.split("|", 1)
        # SQLite compares timestamps as strings; preserve the exact string used in the cursor to
        # avoid false "<" matches from formatting differences (space vs `T`, microseconds, tz).
        from db.database import IS_SQLITE

        if IS_SQLITE:
            return ts_s, int(rid_s)
        ts = datetime.fromisoformat(ts_s.replace("Z", "+00:00"))
        return ts, int(rid_s)
    except Exception:
        return None


def _safe_snippet(text: Optional[str], n: int = 140) -> str:
    s = _as_text(text)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


def _hash_dedupe_key(parts: Sequence[str]) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()


def _wilson_lower_bound(pos: int, n: int, z: float = 1.96) -> float:
    if n <= 0:
        return 0.0
    phat = pos / n
    denom = 1.0 + z * z / n
    centre = phat + z * z / (2.0 * n)
    margin = z * ((phat * (1 - phat) + z * z / (4.0 * n)) / n) ** 0.5
    return (centre - margin) / denom


def _json_sanitize(value: Any) -> Any:
    """
    Make a value JSON-serializable for audit logs.

    Important: audit must be best-effort; it should never break the main flow.
    """
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        # Use ISO for stable readability and cross-language parsing.
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes):
        # Avoid raw bytes in JSON (also avoids logging potentially sensitive binary data verbatim).
        return base64.b64encode(value).decode("utf-8")
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            out[str(k)] = _json_sanitize(v)
        return out
    if isinstance(value, (list, tuple, set)):
        return [_json_sanitize(v) for v in value]
    # Fallback for SQLAlchemy Rows / Records / unknown objects.
    try:
        if hasattr(value, "_mapping"):
            return _json_sanitize(dict(value._mapping))  # type: ignore[attr-defined]
    except Exception:
        pass
    return str(value)


async def _audit(
    *,
    actor: Dict[str, Any],
    action: str,
    target_type: str,
    target_id: str,
    reason: Optional[str],
    before: Optional[Dict[str, Any]],
    after: Optional[Dict[str, Any]],
) -> None:
    try:
        await database.execute(
            employee_audit_logs.insert().values(
                actor_employee_id=actor.get("employee_id") or actor.get("user_id") or actor.get("sub"),
                actor_email=actor.get("email"),
                action=action,
                target_type=target_type,
                target_id=str(target_id),
                reason=reason,
                before_json=_json_sanitize(before) if before is not None else None,
                after_json=_json_sanitize(after) if after is not None else None,
                created_at=_now(),
            )
        )
    except Exception as e:
        # Audit logging must be best-effort; do not break the request path.
        try:
            logger.warning("reviews.audit.failed action=%s target=%s:%s err=%s", action, target_type, target_id, e)
        except Exception:
            pass


async def get_active_group_membership_for_sku_key(sku_key: str) -> Optional[Dict[str, Any]]:
    # Only treat a membership as active when the group itself is active.
    row = await database.fetch_one(
        """
        SELECT m.*
        FROM review_group_membership m
        JOIN review_group g ON g.id = m.group_id
        WHERE m.sku_key = :sku_key
          AND m.status = 'active'
          AND g.status = 'active'
        LIMIT 1
        """,
        {"sku_key": sku_key},
    )
    return dict(row) if row else None


async def get_group_counts_by_merchant(group_id: int) -> Dict[str, int]:
    q = """
    SELECT merchant_id, COUNT(*)::int AS cnt
    FROM product_reviews
    WHERE group_id = :gid AND status = 'active'
    GROUP BY merchant_id
    ORDER BY cnt DESC
    """
    rows = await database.fetch_all(q, {"gid": int(group_id)})
    out: Dict[str, int] = {}
    for r in rows:
        out[str(r["merchant_id"])] = int(r["cnt"] or 0)
    return out


async def get_seller_feedback_summary(merchant_id: str) -> Dict[str, Any]:
    mid = _as_text(merchant_id)
    if not mid:
        return {
            "merchant_id": merchant_id,
            "total_count": 0,
            "rating_overall_avg": None,
            "dims_avg": None,
        }

    row = await database.fetch_one(
        """
        SELECT COUNT(*)::int AS total,
               AVG(rating_overall)::float AS avg_rating
        FROM seller_feedback
        WHERE merchant_id = :mid AND status = 'active'
        """,
        {"mid": mid},
    )
    total = int(row["total"] or 0) if row else 0
    avg_rating = float(row["avg_rating"]) if row and row["avg_rating"] is not None else None

    # Lightweight dims average (best-effort): only for known numeric dims.
    dims_keys = ["shipping_speed", "communication", "after_sales", "packaging", "accuracy"]
    dims_sum: Dict[str, float] = {k: 0.0 for k in dims_keys}
    dims_n: Dict[str, int] = {k: 0 for k in dims_keys}
    rows = await database.fetch_all(
        seller_feedback.select()
        .with_only_columns(seller_feedback.c.dims_json)
        .where((seller_feedback.c.merchant_id == mid) & (seller_feedback.c.status == "active"))
        .order_by(seller_feedback.c.created_at.desc())
        .limit(200)
    )
    for r in rows:
        dims = _row_get(r, "dims_json")
        if not isinstance(dims, dict):
            continue
        for k in dims_keys:
            v = dims.get(k)
            if isinstance(v, (int, float)):
                dims_sum[k] += float(v)
                dims_n[k] += 1

    dims_avg: Optional[Dict[str, float]] = None
    if any(dims_n.values()):
        dims_avg = {}
        for k in dims_keys:
            if dims_n[k] > 0:
                dims_avg[k] = round(dims_sum[k] / dims_n[k], 3)

    return {
        "merchant_id": mid,
        "total_count": total,
        "rating_overall_avg": avg_rating,
        "dims_avg": dims_avg,
    }


async def list_seller_feedback(
    *,
    merchant_id: str,
    limit: int = 20,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    mid = _as_text(merchant_id)
    limit = max(1, min(int(limit or 20), 50))
    cursor_pair = _decode_cursor(cursor or "")

    where = ["merchant_id = :mid", "status = 'active'"]
    params: Dict[str, Any] = {"mid": mid}
    if cursor_pair:
        # Avoid row-value tuple comparisons for SQLite compatibility.
        where.append("(created_at < :cursor_ts OR (created_at = :cursor_ts AND id < :cursor_id))")
        params["cursor_ts"] = cursor_pair[0]
        params["cursor_id"] = cursor_pair[1]

    where_sql = " AND ".join(where)

    rows = await database.fetch_all(
        f"""
        SELECT id, merchant_id, order_ref, rating_overall, dims_json, body, created_at
        FROM seller_feedback
        WHERE {where_sql}
        ORDER BY created_at DESC, id DESC
        LIMIT {limit}
        """,
        params,
    )

    items: List[Dict[str, Any]] = []
    next_cursor: Optional[str] = None
    for r in rows:
        rid = int(r["id"])
        created_at = _row_get(r, "created_at")
        items.append(
            {
                "id": rid,
                "merchant_id": str(r["merchant_id"]),
                "rating_overall": _row_get(r, "rating_overall"),
                "dims": _row_get(r, "dims_json") if isinstance(_row_get(r, "dims_json"), dict) else None,
                "body": _row_get(r, "body"),
                "created_at": _as_iso_datetime(created_at),
            }
        )
        if _as_datetime(created_at):
            next_cursor = _encode_cursor(created_at, rid)

    return {"items": items, "next_cursor": next_cursor, "limit": limit}


async def get_review_summary_for_sku(
    *,
    merchant_id: str,
    platform: str,
    platform_product_id: str,
    variant_id: Optional[str],
) -> Dict[str, Any]:
    product_key = build_product_key(
        merchant_id=merchant_id, platform=platform, platform_product_id=platform_product_id
    )
    sku_key = build_sku_key(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
        variant_id=variant_id,
    )

    membership = await get_active_group_membership_for_sku_key(sku_key)
    gid_raw = _row_get(membership, "group_id") if membership else None
    group_id = int(gid_raw) if gid_raw is not None else None

    merchant_row = await database.fetch_one(
        """
        SELECT COUNT(*)::int AS total,
               COALESCE(SUM(media_count), 0)::int AS media_count
        FROM product_reviews
        WHERE sku_key = :sku_key AND status = 'active'
        """,
        {"sku_key": sku_key},
    )
    merchant_review_count = int(merchant_row["total"] or 0) if merchant_row else 0
    merchant_media_count = int(merchant_row["media_count"] or 0) if merchant_row else 0

    group_total_review_count = 0
    group_media_count = 0
    featured_review_count = 0
    counts_by_merchant: Optional[Dict[str, int]] = None
    top_featured_preview: List[Dict[str, Any]] = []

    if group_id is not None:
        featured_enabled = os.getenv("REVIEWS_FEATURED_ENABLED", "true").lower() == "true"
        group_row = await database.fetch_one(
            """
            SELECT COUNT(*)::int AS total,
                   COALESCE(SUM(media_count), 0)::int AS media_count
            FROM product_reviews
            WHERE group_id = :gid AND status = 'active'
            """,
            {"gid": group_id},
        )
        group_total_review_count = int(group_row["total"] or 0) if group_row else 0
        group_media_count = int(group_row["media_count"] or 0) if group_row else 0

        if featured_enabled:
            feat_row = await database.fetch_one(
                "SELECT COUNT(*)::int AS c FROM review_featured WHERE group_id = :gid",
                {"gid": group_id},
            )
            featured_review_count = int(feat_row["c"] or 0) if feat_row else 0

        counts_by_merchant = await get_group_counts_by_merchant(group_id)

        if featured_enabled and featured_review_count > 0:
            feat_rows = await database.fetch_all(
                """
                SELECT f.review_id, f.rank, f.score, f.is_pinned, r.merchant_id, r.created_at, r.title, r.body_redacted, r.body, r.media_count
                FROM review_featured f
                JOIN product_reviews r ON r.id = f.review_id
                WHERE f.group_id = :gid AND r.status = 'active'
                ORDER BY f.is_pinned DESC, f.rank ASC, f.score DESC, r.created_at DESC
                LIMIT 6
                """,
                {"gid": group_id},
            )
            for fr in feat_rows:
                review_id = int(fr["review_id"])
                media_row = await database.fetch_one(
                    """
                    SELECT id, url, type, public_id
                    FROM media_assets
                    WHERE review_id = :rid AND status = 'active'
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    {"rid": review_id},
                )
                media_thumb = None
                media_type = None
                if media_row:
                    media_type = _row_get(media_row, "type")
                    pid = _as_text(_row_get(media_row, "public_id"))
                    media_thumb = _signed_media_url(public_id=pid or None, media_id=int(media_row["id"]))
                top_featured_preview.append(
                    {
                        "review_id": review_id,
                        "merchant_id": str(fr["merchant_id"]),
                        "rank": int(fr["rank"] or 0),
                        "score": float(fr["score"] or 0.0),
                        "is_pinned": bool(fr["is_pinned"]),
                        "snippet": _safe_snippet(_row_get(fr, "body_redacted") or _row_get(fr, "body")),
                        "media_thumb": media_thumb,
                        "media_type": media_type,
                    }
                )

    has_group = group_id is not None

    return {
        "has_group": has_group,
        "group_id": group_id,
        "group_total_review_count": group_total_review_count,
        "merchant_review_count": merchant_review_count,
        "media_count": group_media_count if has_group else merchant_media_count,
        "featured_review_count": featured_review_count,
        "top_featured_preview": top_featured_preview,
        "counts_by_merchant": counts_by_merchant,
        "default_view": "group" if has_group else "merchant",
        "product_key": product_key,
        "sku_key": sku_key,
    }


def _merge_label(match_type: Optional[str]) -> Optional[str]:
    t = (match_type or "").strip().upper()
    if t == "GTIN":
        return "同款·条码匹配"
    if t == "BRAND_MPN":
        return "相似·型号匹配"
    if t == "MANUAL":
        return "手工归并"
    return None


async def list_group_reviews(
    *,
    group_id: int,
    merchant_ids: Optional[List[str]] = None,
    featured_only: bool = False,
    has_media: bool = False,
    limit: int = 20,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    gid = int(group_id)
    limit = max(1, min(int(limit or 20), 50))
    cursor_pair = _decode_cursor(cursor or "")

    g = await database.fetch_one(review_group.select().where(review_group.c.id == gid))
    if not g or str(_row_get(g, "status")) != "active":
        return {"items": [], "next_cursor": None, "limit": limit}

    where = ["r.group_id = :gid", "r.status = 'active'"]
    params: Dict[str, Any] = {"gid": gid}

    if merchant_ids:
        where.append("r.merchant_id = ANY(:merchant_ids)")
        params["merchant_ids"] = list(merchant_ids)

    if has_media:
        where.append("r.media_count > 0")

    if featured_only:
        where.append("EXISTS (SELECT 1 FROM review_featured f WHERE f.group_id = :gid AND f.review_id = r.id)")

    if cursor_pair:
        # Avoid row-value tuple comparisons for SQLite compatibility.
        where.append("(r.created_at < :cursor_ts OR (r.created_at = :cursor_ts AND r.id < :cursor_id))")
        params["cursor_ts"] = cursor_pair[0]
        params["cursor_id"] = cursor_pair[1]

    where_sql = " AND ".join(where)

    rows = await database.fetch_all(
        f"""
        SELECT
          r.id,
          r.merchant_id,
          r.product_key,
          r.sku_key,
          r.platform,
          r.platform_product_id,
          r.variant_id,
          r.verification,
          r.rating,
          r.title,
          COALESCE(NULLIF(r.body_redacted, ''), r.body) AS body_effective,
          r.media_count,
          r.created_at,
          m.match_type,
          m.evidence,
          m.confidence AS match_confidence,
          (EXISTS (SELECT 1 FROM review_featured f WHERE f.group_id = :gid AND f.review_id = r.id)) AS is_featured
        FROM product_reviews r
        LEFT JOIN review_group_membership m
          ON m.sku_key = r.sku_key AND m.status = 'active'
        WHERE {where_sql}
        ORDER BY r.created_at DESC, r.id DESC
        LIMIT {limit}
        """,
        params,
    )

    items: List[Dict[str, Any]] = []
    next_cursor: Optional[str] = None
    for row in rows:
        rid = int(row["id"])
        media_rows = await database.fetch_all(
            """
            SELECT id, type, url, public_id
            FROM media_assets
            WHERE review_id = :rid AND status = 'active'
            ORDER BY id ASC
            LIMIT 6
            """,
            {"rid": rid},
        )
        media_out = []
        for m in media_rows:
            pid = _as_text(_row_get(m, "public_id"))
            media_out.append(
                {
                    "id": int(m["id"]),
                    "type": m["type"],
                    "url": _signed_media_url(public_id=pid or None, media_id=int(m["id"])),
                }
            )
        items.append(
            {
                "review_id": rid,
                "merchant_id": str(row["merchant_id"]),
                "product_key": row["product_key"],
                "sku_key": row["sku_key"],
                "platform": row["platform"],
                "platform_product_id": row["platform_product_id"],
                "variant_id": row["variant_id"],
                "verification": row["verification"],
                "rating": row["rating"],
                "title": row["title"],
                "body": row["body_effective"],
                "snippet": _safe_snippet(row["body_effective"]),
                "created_at": _as_iso_datetime(_row_get(row, "created_at")),
                "media": media_out,
                "is_featured": bool(row["is_featured"]),
                "merge": {
                    "type": _row_get(row, "match_type"),
                    "label": _merge_label(_row_get(row, "match_type")),
                    "evidence": _row_get(row, "evidence")
                    if isinstance(_row_get(row, "evidence"), dict)
                    else None,
                    "confidence": float(_row_get(row, "match_confidence") or 0.0),
                },
            }
        )
        next_cursor = _encode_cursor(_row_get(row, "created_at"), rid) if _as_datetime(_row_get(row, "created_at")) else None

    return {"items": items, "next_cursor": next_cursor, "limit": limit}


async def list_sku_reviews(
    *,
    sku_key: str,
    featured_only: bool = False,
    has_media: bool = False,
    limit: int = 20,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    limit = max(1, min(int(limit or 20), 50))
    cursor_pair = _decode_cursor(cursor or "")

    where = ["r.sku_key = :sku_key", "r.status = 'active'"]
    params: Dict[str, Any] = {"sku_key": sku_key}

    if has_media:
        where.append("r.media_count > 0")

    if featured_only:
        mem = await get_active_group_membership_for_sku_key(sku_key)
        gid_raw = _row_get(mem, "group_id") if mem else None
        gid = int(gid_raw) if gid_raw is not None else None
        if not gid:
            return {"items": [], "next_cursor": None, "limit": limit}
        where.append("EXISTS (SELECT 1 FROM review_featured f WHERE f.group_id = :gid AND f.review_id = r.id)")
        params["gid"] = gid

    if cursor_pair:
        # Avoid row-value tuple comparisons for SQLite compatibility.
        where.append("(r.created_at < :cursor_ts OR (r.created_at = :cursor_ts AND r.id < :cursor_id))")
        params["cursor_ts"] = cursor_pair[0]
        params["cursor_id"] = cursor_pair[1]

    where_sql = " AND ".join(where)

    rows = await database.fetch_all(
        f"""
        SELECT r.id, r.merchant_id, r.product_key, r.sku_key, r.platform, r.platform_product_id, r.variant_id,
               r.verification, r.rating, r.title,
               COALESCE(NULLIF(r.body_redacted, ''), r.body) AS body_effective,
               r.media_count, r.created_at
        FROM product_reviews r
        WHERE {where_sql}
        ORDER BY r.created_at DESC, r.id DESC
        LIMIT {limit}
        """,
        params,
    )

    items: List[Dict[str, Any]] = []
    next_cursor: Optional[str] = None
    for row in rows:
        rid = int(row["id"])
        media_rows = await database.fetch_all(
            """
            SELECT id, type, url, public_id
            FROM media_assets
            WHERE review_id = :rid AND status = 'active'
            ORDER BY id ASC
            LIMIT 6
            """,
            {"rid": rid},
        )
        media_out = []
        for m in media_rows:
            pid = _as_text(_row_get(m, "public_id"))
            media_out.append(
                {
                    "id": int(m["id"]),
                    "type": m["type"],
                    "url": _signed_media_url(public_id=pid or None, media_id=int(m["id"])),
                }
            )
        items.append(
            {
                "review_id": rid,
                "merchant_id": str(row["merchant_id"]),
                "product_key": row["product_key"],
                "sku_key": row["sku_key"],
                "platform": row["platform"],
                "platform_product_id": row["platform_product_id"],
                "variant_id": row["variant_id"],
                "verification": row["verification"],
                "rating": row["rating"],
                "title": row["title"],
                "body": row["body_effective"],
                "snippet": _safe_snippet(row["body_effective"]),
                "created_at": _as_iso_datetime(_row_get(row, "created_at")),
                "media": media_out,
                "is_featured": False,
                "merge": None,
            }
        )
        next_cursor = _encode_cursor(_row_get(row, "created_at"), rid) if _as_datetime(_row_get(row, "created_at")) else None

    return {"items": items, "next_cursor": next_cursor, "limit": limit}


def _apply_redaction_rules(body: str, fields: Sequence[str]) -> Tuple[str, Dict[str, Any]]:
    text = body or ""
    applied: Dict[str, Any] = {"fields": list(fields), "rules": []}

    out = text
    if "email" in fields:
        out2 = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}", "[redacted_email]", out, flags=re.I)
        if out2 != out:
            applied["rules"].append("email")
        out = out2
    if "phone" in fields:
        out2 = re.sub(r"\\+?\\d[\\d\\s().-]{7,}\\d", "[redacted_phone]", out)
        if out2 != out:
            applied["rules"].append("phone")
        out = out2

    return out, applied


async def set_review_status(
    *,
    actor: Dict[str, Any],
    review_id: int,
    status: str,
    reason: Optional[str],
) -> Dict[str, Any]:
    rid = int(review_id)
    status = _as_text(status).lower()
    if status not in {"active", "folded", "removed", "under_review"}:
        raise HTTPException(status_code=400, detail="INVALID_STATUS")
    if status == "removed" and not _as_text(reason):
        raise HTTPException(status_code=400, detail="REASON_REQUIRED")

    before_row = await database.fetch_one(product_reviews.select().where(product_reviews.c.id == rid))
    if not before_row:
        raise HTTPException(status_code=404, detail="REVIEW_NOT_FOUND")

    await database.execute(
        product_reviews.update()
        .where(product_reviews.c.id == rid)
        .values(status=status, updated_at=_now())
    )
    after_row = await database.fetch_one(product_reviews.select().where(product_reviews.c.id == rid))

    await _audit(
        actor=actor,
        action="reviews.moderate.status",
        target_type="product_review",
        target_id=str(rid),
        reason=reason,
        before=dict(before_row),
        after=dict(after_row) if after_row else None,
    )
    return {"status": "success", "review_id": rid, "new_status": status}


async def redact_review(
    *,
    actor: Dict[str, Any],
    review_id: int,
    fields: Sequence[str],
    reason: Optional[str],
    editor_note: Optional[str] = None,
) -> Dict[str, Any]:
    rid = int(review_id)
    before_row = await database.fetch_one(product_reviews.select().where(product_reviews.c.id == rid))
    if not before_row:
        raise HTTPException(status_code=404, detail="REVIEW_NOT_FOUND")

    body = _as_text(_row_get(before_row, "body"))
    redacted, applied = _apply_redaction_rules(body, fields)

    await database.execute(
        product_reviews.update()
        .where(product_reviews.c.id == rid)
        .values(
            body_redacted=redacted,
            redaction=applied,
            editor_note=editor_note,
            updated_at=_now(),
        )
    )
    after_row = await database.fetch_one(product_reviews.select().where(product_reviews.c.id == rid))
    await _audit(
        actor=actor,
        action="reviews.moderate.redact",
        target_type="product_review",
        target_id=str(rid),
        reason=reason,
        before=dict(before_row),
        after=dict(after_row) if after_row else None,
    )
    return {"status": "success", "review_id": rid, "redacted": True, "applied": applied}


async def remove_review_media(
    *,
    actor: Dict[str, Any],
    review_id: int,
    media_id: int,
    reason: Optional[str],
) -> Dict[str, Any]:
    rid = int(review_id)
    mid = int(media_id)
    before_media = await database.fetch_one(
        media_assets.select().where((media_assets.c.id == mid) & (media_assets.c.review_id == rid))
    )
    if not before_media:
        raise HTTPException(status_code=404, detail="MEDIA_NOT_FOUND")

    await database.execute(
        media_assets.update()
        .where(media_assets.c.id == mid)
        .values(status="removed")
    )

    # Recompute denormalized media_count for the review.
    row = await database.fetch_one(
        "SELECT COUNT(*)::int AS c FROM media_assets WHERE review_id = :rid AND status = 'active'",
        {"rid": rid},
    )
    cnt = int(row["c"] or 0) if row else 0
    await database.execute(
        product_reviews.update()
        .where(product_reviews.c.id == rid)
        .values(media_count=cnt, updated_at=_now())
    )

    after_media = await database.fetch_one(media_assets.select().where(media_assets.c.id == mid))
    await _audit(
        actor=actor,
        action="reviews.moderate.media",
        target_type="review_media_asset",
        target_id=str(mid),
        reason=reason,
        before=dict(before_media),
        after=dict(after_media) if after_media else None,
    )
    return {"status": "success", "review_id": rid, "media_id": mid, "review_media_count": cnt}


async def _get_or_create_group(
    *,
    group_key: str,
    group_type: str,
    confidence: float,
    created_by: str,
    created_by_employee_id: Optional[str],
) -> int:
    existing = await database.fetch_one(
        review_group.select().where(review_group.c.group_key == group_key)
    )
    if existing:
        return int(existing["id"])

    gid = await database.execute(
        review_group.insert().values(
            group_key=group_key,
            group_type=group_type,
            confidence=float(confidence or 0.0),
            status="active",
            created_by=created_by,
            created_by_employee_id=created_by_employee_id,
            created_at=_now(),
            updated_at=_now(),
        )
    )
    return int(gid)


async def resolve_review_group_for_product(
    *,
    merchant_id: str,
    platform: str,
    platform_product_id: str,
    variant_id: Optional[str],
    product: Optional[StandardProduct],
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Resolve a soft-canonical review group for a sku/product.
    Returns dict {group_key, group_type, confidence, evidence} or None.
    """
    payload = payload or {}

    # 1) barcode/GTIN (high confidence)
    barcode = ""
    if product:
        if _as_text(variant_id) and product.variants:
            for v in product.variants:
                if (v.variant_id or v.id) == variant_id and _as_text(v.barcode):
                    barcode = _as_text(v.barcode)
                    break
        if not barcode:
            barcode = _as_text(getattr(product, "barcode", None))

    barcode = barcode or _as_text(payload.get("barcode")) or _as_text(payload.get("gtin"))
    if barcode:
        return {
            "group_key": f"gtin:{barcode}",
            "group_type": "GTIN",
            "confidence": 1.0,
            "evidence": {"barcode": barcode},
        }

    # 2) brand+mpn/model (medium confidence)
    brand = _as_text(payload.get("brand")) or _as_text(payload.get("vendor")) or (product.vendor if product else "")
    mpn = _as_text(payload.get("mpn")) or _as_text(payload.get("model")) or _as_text(payload.get("mpn_model"))
    if brand and mpn:
        key = f"mpn:{brand.lower()}|{mpn.lower()}"
        return {
            "group_key": key,
            "group_type": "BRAND_MPN",
            "confidence": 0.9,
            "evidence": {"brand": brand, "mpn": mpn},
        }

    return None


async def ensure_membership_for_sku(
    *,
    actor: Optional[Dict[str, Any]],
    group_id: int,
    match_type: str,
    confidence: float,
    evidence: Optional[Dict[str, Any]],
    merchant_id: str,
    platform: str,
    platform_product_id: str,
    variant_id: Optional[str],
    created_by: str,
    created_by_employee_id: Optional[str],
) -> None:
    sku_key = build_sku_key(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
        variant_id=variant_id,
    )
    product_key = build_product_key(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
    )

    existing = await get_active_group_membership_for_sku_key(sku_key)
    if existing and int(_row_get(existing, "group_id") or 0) != int(group_id):
        raise HTTPException(status_code=409, detail="SKU_ALREADY_IN_ACTIVE_GROUP")
    if existing:
        return

    await database.execute(
        review_group_membership.insert().values(
            group_id=int(group_id),
            product_key=product_key,
            sku_key=sku_key,
            merchant_id=merchant_id,
            platform=platform,
            platform_product_id=platform_product_id,
            variant_id=variant_id,
            match_type=match_type,
            evidence=evidence,
            confidence=float(confidence or 0.0),
            status="active",
            created_by=created_by,
            created_by_employee_id=created_by_employee_id,
            created_at=_now(),
            updated_at=_now(),
        )
    )

    if actor:
        await _audit(
            actor=actor,
            action="reviews.group.manage.add_member",
            target_type="review_group_membership",
            target_id=sku_key,
            reason="add_member",
            before=None,
            after={"group_id": int(group_id), "sku_key": sku_key, "match_type": match_type, "evidence": evidence},
        )


async def create_manual_review_group(
    *,
    actor: Dict[str, Any],
    group_key: Optional[str] = None,
    confidence: float = 1.0,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    gk = _as_text(group_key) or f"manual:{uuid4().hex}"
    gid = await _get_or_create_group(
        group_key=gk,
        group_type="MANUAL",
        confidence=float(confidence or 1.0),
        created_by="employee",
        created_by_employee_id=_as_text(actor.get("employee_id") or actor.get("user_id") or actor.get("sub")) or None,
    )
    after = await database.fetch_one(review_group.select().where(review_group.c.id == int(gid)))
    await _audit(
        actor=actor,
        action="reviews.group.manage.create",
        target_type="review_group",
        target_id=str(gid),
        reason=reason or "create_manual_group",
        before=None,
        after=dict(after) if after else {"group_key": gk, "group_type": "MANUAL"},
    )
    return {"status": "success", "group_id": int(gid), "group_key": gk}


async def set_review_group_status(
    *,
    actor: Dict[str, Any],
    group_id: int,
    status: str,
    reason: Optional[str],
) -> Dict[str, Any]:
    gid = int(group_id)
    status = _as_text(status).lower()
    if status not in {"active", "disabled"}:
        raise HTTPException(status_code=400, detail="INVALID_GROUP_STATUS")
    if status == "disabled" and not _as_text(reason):
        raise HTTPException(status_code=400, detail="REASON_REQUIRED")
    before = await database.fetch_one(review_group.select().where(review_group.c.id == gid))
    if not before:
        raise HTTPException(status_code=404, detail="GROUP_NOT_FOUND")
    await database.execute(
        review_group.update()
        .where(review_group.c.id == gid)
        .values(status=status, updated_at=_now())
    )
    after = await database.fetch_one(review_group.select().where(review_group.c.id == gid))
    await _audit(
        actor=actor,
        action="reviews.group.manage.disable" if status == "disabled" else "reviews.group.manage.enable",
        target_type="review_group",
        target_id=str(gid),
        reason=reason,
        before=dict(before),
        after=dict(after) if after else None,
    )
    return {"status": "success", "group_id": gid, "group_status": status}


async def remove_group_member(
    *,
    actor: Dict[str, Any],
    group_id: int,
    sku_key: str,
    reason: Optional[str],
) -> Dict[str, Any]:
    gid = int(group_id)
    sk = _as_text(sku_key)
    before = await database.fetch_one(
        review_group_membership.select().where(
            (review_group_membership.c.group_id == gid)
            & (review_group_membership.c.sku_key == sk)
            & (review_group_membership.c.status == "active")
        )
    )
    if not before:
        raise HTTPException(status_code=404, detail="MEMBERSHIP_NOT_FOUND")
    await database.execute(
        review_group_membership.update()
        .where(review_group_membership.c.id == int(before["id"]))
        .values(status="removed", updated_at=_now())
    )
    after = await database.fetch_one(review_group_membership.select().where(review_group_membership.c.id == int(before["id"])))
    await _audit(
        actor=actor,
        action="reviews.group.manage.remove_member",
        target_type="review_group_membership",
        target_id=sk,
        reason=reason,
        before=dict(before),
        after=dict(after) if after else None,
    )
    return {"status": "success", "group_id": gid, "sku_key": sk, "membership_status": "removed"}


async def list_employee_audit_logs(
    *,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    limit = max(1, min(int(limit or 100), 200))
    where = []
    params: Dict[str, Any] = {}
    if _as_text(target_type):
        where.append("target_type = :tt")
        params["tt"] = _as_text(target_type)
    if _as_text(target_id):
        where.append("target_id = :tid")
        params["tid"] = _as_text(target_id)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = await database.fetch_all(
        f"""
        SELECT id, actor_employee_id, actor_email, action, target_type, target_id, reason, before_json, after_json, created_at
        FROM employee_audit_logs
        {where_sql}
        ORDER BY created_at DESC, id DESC
        LIMIT {limit}
        """,
        params,
    )
    items: List[Dict[str, Any]] = []
    for r in rows:
        items.append(
            {
                "id": int(r["id"]),
                "actor_employee_id": _row_get(r, "actor_employee_id"),
                "actor_email": _row_get(r, "actor_email"),
                "action": _row_get(r, "action"),
                "target_type": _row_get(r, "target_type"),
                "target_id": _row_get(r, "target_id"),
                "reason": _row_get(r, "reason"),
                "before": _row_get(r, "before_json"),
                "after": _row_get(r, "after_json"),
                "created_at": _as_iso_datetime(_row_get(r, "created_at")),
            }
        )
    return {"items": items, "limit": limit}


async def set_featured_pin(
    *,
    actor: Dict[str, Any],
    group_id: int,
    review_id: int,
    pinned: bool,
    reason: Optional[str],
) -> Dict[str, Any]:
    gid = int(group_id)
    rid = int(review_id)

    existing = await database.fetch_one(
        review_featured.select().where(
            (review_featured.c.group_id == gid) & (review_featured.c.review_id == rid)
        )
    )
    before = dict(existing) if existing else None

    if existing:
        await database.execute(
            review_featured.update()
            .where(review_featured.c.id == int(existing["id"]))
            .values(is_pinned=bool(pinned), generated_at=_now())
        )
    else:
        await database.execute(
            review_featured.insert().values(
                group_id=gid,
                review_id=rid,
                rank=0,
                score=0.0,
                reason_tags={"manual": True},
                generated_at=_now(),
                is_pinned=bool(pinned),
            )
        )

    after_row = await database.fetch_one(
        review_featured.select().where(
            (review_featured.c.group_id == gid) & (review_featured.c.review_id == rid)
        )
    )

    await _audit(
        actor=actor,
        action="reviews.feature.manage.pin" if pinned else "reviews.feature.manage.unpin",
        target_type="review_featured",
        target_id=f"{gid}:{rid}",
        reason=reason,
        before=before,
        after=dict(after_row) if after_row else None,
    )
    return {"status": "success", "group_id": gid, "review_id": rid, "is_pinned": bool(pinned)}


async def set_group_featured_frozen(
    *,
    actor: Dict[str, Any],
    group_id: int,
    frozen: bool,
    reason: Optional[str],
) -> Dict[str, Any]:
    gid = int(group_id)
    before = await database.fetch_one(review_group.select().where(review_group.c.id == gid))
    if not before:
        raise HTTPException(status_code=404, detail="GROUP_NOT_FOUND")
    await database.execute(
        review_group.update()
        .where(review_group.c.id == gid)
        .values(featured_frozen=bool(frozen), updated_at=_now())
    )
    after = await database.fetch_one(review_group.select().where(review_group.c.id == gid))
    await _audit(
        actor=actor,
        action="reviews.feature.manage.freeze" if frozen else "reviews.feature.manage.unfreeze",
        target_type="review_group",
        target_id=str(gid),
        reason=reason,
        before=dict(before),
        after=dict(after) if after else None,
    )
    return {"status": "success", "group_id": gid, "featured_frozen": bool(frozen)}


async def generate_featured_reviews_for_group(
    *,
    actor: Optional[Dict[str, Any]],
    group_id: int,
    limit: int = 12,
    per_merchant_cap: int = 2,
) -> Dict[str, Any]:
    gid = int(group_id)
    limit = max(1, min(int(limit or 12), 30))
    per_merchant_cap = max(1, min(int(per_merchant_cap or 2), 5))

    g = await database.fetch_one(review_group.select().where(review_group.c.id == gid))
    if not g:
        raise HTTPException(status_code=404, detail="GROUP_NOT_FOUND")
    if bool(_row_get(g, "featured_frozen")):
        return {"status": "skipped", "group_id": gid, "reason": "FEATURED_FROZEN"}

    rows = await database.fetch_all(
        """
        SELECT
          r.id,
          r.merchant_id,
          r.verification,
          COALESCE(NULLIF(r.body_redacted, ''), r.body) AS body_effective,
          r.media_count,
          r.created_at,
          COALESCE(SUM(CASE WHEN i.type='helpful' THEN i.value ELSE 0 END), 0)::int AS helpful,
          COALESCE(SUM(CASE WHEN i.type='report' THEN i.value ELSE 0 END), 0)::int AS report
        FROM product_reviews r
        LEFT JOIN review_interactions i ON i.review_id = r.id
        WHERE r.group_id = :gid AND r.status = 'active'
        GROUP BY r.id
        ORDER BY r.created_at DESC
        LIMIT 2000
        """,
        {"gid": gid},
    )

    candidates: List[Dict[str, Any]] = []
    for r in rows:
        body = _as_text(_row_get(r, "body_effective"))
        media_cnt = int(_row_get(r, "media_count") or 0)
        if media_cnt < 2 and media_cnt < 1:
            continue
        if len(body) < 50:
            continue
        helpful = int(_row_get(r, "helpful") or 0)
        report = int(_row_get(r, "report") or 0)
        n_votes = helpful + report
        helpful_score = _wilson_lower_bound(helpful, max(n_votes, 1))
        verification = _as_text(_row_get(r, "verification")).lower()
        verification_boost = 0.15 if verification == "verified_purchase" else (0.05 if verification == "partner_verified" else 0.0)
        media_boost = min(0.35, 0.12 * media_cnt)
        length_boost = min(0.25, len(body) / 800.0)
        report_penalty = min(0.4, 0.08 * report)
        score = 0.25 + verification_boost + media_boost + length_boost + helpful_score - report_penalty
        candidates.append(
            {
                "review_id": int(r["id"]),
                "merchant_id": str(r["merchant_id"]),
                "score": float(score),
                "reason_tags": {
                    "media_count": media_cnt,
                    "verified": verification == "verified_purchase",
                    "helpful": helpful,
                    "report": report,
                },
            }
        )

    # Sort and apply diversity cap per merchant.
    candidates.sort(key=lambda x: x["score"], reverse=True)
    picked: List[Dict[str, Any]] = []
    per_merchant: Dict[str, int] = {}
    for c in candidates:
        mid = c["merchant_id"]
        if per_merchant.get(mid, 0) >= per_merchant_cap:
            continue
        picked.append(c)
        per_merchant[mid] = per_merchant.get(mid, 0) + 1
        if len(picked) >= limit:
            break

    # Preserve pinned items; replace the rest.
    pinned_rows = await database.fetch_all(
        "SELECT review_id FROM review_featured WHERE group_id = :gid AND is_pinned = true",
        {"gid": gid},
    )
    pinned_ids = {int(r["review_id"]) for r in pinned_rows if _row_get(r, "review_id") is not None}

    # Delete existing non-pinned featured rows
    await database.execute(
        "DELETE FROM review_featured WHERE group_id = :gid AND is_pinned = false",
        {"gid": gid},
    )

    # Reinsert new set (excluding pinned duplicates)
    rank = 0
    inserted = 0
    for c in picked:
        rid = int(c["review_id"])
        if rid in pinned_ids:
            continue
        await database.execute(
            review_featured.insert().values(
                group_id=gid,
                review_id=rid,
                rank=rank,
                score=float(c["score"]),
                reason_tags=c["reason_tags"],
                generated_at=_now(),
                is_pinned=False,
            )
        )
        inserted += 1
        rank += 1

    if actor:
        await _audit(
            actor=actor,
            action="reviews.feature.generate",
            target_type="review_group",
            target_id=str(gid),
            reason="generate_featured",
            before=None,
            after={"inserted": inserted, "limit": limit, "pinned_kept": len(pinned_ids)},
        )

    return {
        "status": "success",
        "group_id": gid,
        "inserted": inserted,
        "pinned_kept": len(pinned_ids),
        "limit": limit,
    }


# ---------------------------------------------------------------------------
# Import pipeline (employee-only)
# ---------------------------------------------------------------------------


@dataclass
class ImportReport:
    total: int = 0
    matched: int = 0
    downgraded_to_product_level: int = 0
    rejected: int = 0
    deduped: int = 0
    group_resolved_gtin: int = 0
    group_resolved_mpn: int = 0
    group_resolved_none: int = 0
    errors: List[Dict[str, Any]] = None  # type: ignore[assignment]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "matched": self.matched,
            "downgraded_to_product_level": self.downgraded_to_product_level,
            "rejected": self.rejected,
            "deduped": self.deduped,
            "group_resolve_stats": {
                "GTIN": self.group_resolved_gtin,
                "BRAND_MPN": self.group_resolved_mpn,
                "none": self.group_resolved_none,
            },
            "top_errors": (self.errors or [])[:20],
        }


def _parse_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    s = _as_text(value)
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _read_jsonl(raw: bytes) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                out.append(obj)
        except Exception:
            continue
    return out


def _read_csv(raw: bytes) -> List[Dict[str, Any]]:
    text = raw.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    return [dict(r) for r in reader]


def _detect_format(filename: str, content_type: Optional[str]) -> str:
    name = (filename or "").lower()
    if name.endswith(".jsonl") or name.endswith(".ndjson"):
        return "jsonl"
    if name.endswith(".csv"):
        return "csv"
    # content-type fallback
    ct = (content_type or "").lower()
    if "json" in ct:
        return "jsonl"
    return "csv"


def validate_imported_identifiers(*, source_system: str, external_review_id: Optional[str]) -> Optional[str]:
    """
    Enforce that imported reviews have stable external identifiers so that
    merchant-scoped uniqueness works and NULL does not bypass constraints.

    Returns an error_reason string when invalid, otherwise None.
    """
    ss = _as_text(source_system)
    ext = _as_text(external_review_id)
    if not ss:
        return "missing_source_system"
    if not ext:
        return "missing_external_review_id"
    return None


def compute_import_dedupe_key(
    *,
    merchant_id: str,
    source_system: str,
    external_review_id: Optional[str],
    platform: str,
    platform_product_id: str,
    variant_id: Optional[str],
    created_at: Any,
) -> str:
    return _hash_dedupe_key(
        [
            _as_text(merchant_id),
            _as_text(source_system),
            _as_text(external_review_id) or "∅",
            _as_text(platform),
            _as_text(platform_product_id),
            _as_text(variant_id) or "∅",
            _as_text(created_at),
        ]
    )


def _media_signing_secret() -> bytes:
    # Prefer dedicated secret; fall back to JWT secret for dev.
    from config.settings import settings

    s = _as_text(os.getenv("REVIEWS_MEDIA_SIGNING_SECRET")) or _as_text(getattr(settings, "jwt_secret_key", ""))
    if not s:
        s = "dev-insecure-secret"
    return s.encode("utf-8")


def sign_review_media_ref(*, public_id: str, exp: int) -> str:
    msg = f"{public_id}|{int(exp)}".encode("utf-8")
    import hmac

    digest = hmac.new(_media_signing_secret(), msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")


def build_signed_review_media_url(*, public_id: str, ttl_seconds: int = 300) -> str:
    ttl_env = _as_text(os.getenv("REVIEWS_MEDIA_URL_TTL_SECONDS"))
    if ttl_env:
        try:
            ttl_seconds = int(ttl_env)
        except Exception:
            ttl_seconds = 300

    # Keep TTL within safety bounds (verify_review_media_signature rejects > 30m).
    ttl_seconds = max(30, min(int(ttl_seconds), 1800))

    exp = int(_now().timestamp()) + int(ttl_seconds)
    sig = sign_review_media_ref(public_id=public_id, exp=exp)
    return f"/agent/shop/v1/review-media/{public_id}?exp={exp}&sig={sig}"


def verify_review_media_signature(*, public_id: str, exp: int, sig: str) -> bool:
    import hmac

    now = int(_now().timestamp())
    # Basic expiry check
    if int(exp) <= now:
        return False
    # Guardrail: don't accept far-future signed URLs (limits cache abuse).
    if int(exp) > now + 60 * 30:
        return False

    expected = sign_review_media_ref(public_id=public_id, exp=int(exp))
    return hmac.compare_digest(str(sig or ""), expected)


def verify_review_media_signature_with_reason(*, public_id: str, exp: int, sig: str) -> tuple[bool, str]:
    """
    Same semantics as verify_review_media_signature, but returns a failure reason for metrics.
    Reasons: ok | expired | too_far | bad_sig
    """
    import hmac

    now = int(_now().timestamp())
    if int(exp) <= now:
        return False, "expired"
    if int(exp) > now + 60 * 30:
        return False, "too_far"
    expected = sign_review_media_ref(public_id=public_id, exp=int(exp))
    if not hmac.compare_digest(str(sig or ""), expected):
        return False, "bad_sig"
    return True, "ok"


def _allow_legacy_review_media_id() -> bool:
    return os.getenv("ALLOW_LEGACY_REVIEW_MEDIA_ID", "").lower() == "true"


def _signed_media_url(*, public_id: Optional[str], media_id: Optional[int]) -> Optional[str]:
    pid = _as_text(public_id)
    if pid:
        return build_signed_review_media_url(public_id=pid)
    if _allow_legacy_review_media_id() and media_id is not None:
        # Legacy mode: sign over the numeric id. Still not guessable without sig.
        return build_signed_review_media_url(public_id=str(int(media_id)))
    return None


async def create_import_batch(
    *,
    actor: Dict[str, Any],
    merchant_id: str,
    source_system: str,
) -> Dict[str, Any]:
    mid = _as_text(merchant_id)
    ss = _as_text(source_system)
    if not mid:
        raise HTTPException(status_code=400, detail="MISSING_MERCHANT_ID")
    if not ss:
        raise HTTPException(status_code=400, detail="MISSING_SOURCE_SYSTEM")
    bid = await database.execute(
        import_batches.insert().values(
            merchant_id=mid,
            source_system=ss,
            status="created",
            created_by_employee_id=actor.get("employee_id") or actor.get("user_id"),
            created_at=_now(),
            updated_at=_now(),
        )
    )
    await _audit(
        actor=actor,
        action="reviews.import.create",
        target_type="import_batch",
        target_id=str(bid),
        reason="create_batch",
        before=None,
        after={"merchant_id": merchant_id, "source_system": source_system},
    )
    return {"status": "success", "batch_id": int(bid)}


def _import_storage_dir() -> str:
    base = os.getenv("REVIEWS_IMPORT_DIR", os.path.join(os.getcwd(), "tmp", "reviews-imports"))
    os.makedirs(base, exist_ok=True)
    return base


async def attach_import_files(
    *,
    actor: Dict[str, Any],
    batch_id: int,
    reviews_file_path: Optional[str],
    media_zip_path: Optional[str],
) -> Dict[str, Any]:
    bid = int(batch_id)
    before = await database.fetch_one(import_batches.select().where(import_batches.c.id == bid))
    if not before:
        raise HTTPException(status_code=404, detail="BATCH_NOT_FOUND")

    await database.execute(
        import_batches.update()
        .where(import_batches.c.id == bid)
        .values(
            reviews_file_path=reviews_file_path,
            media_zip_path=media_zip_path,
            status="uploaded",
            updated_at=_now(),
        )
    )
    after = await database.fetch_one(import_batches.select().where(import_batches.c.id == bid))
    await _audit(
        actor=actor,
        action="reviews.import.upload",
        target_type="import_batch",
        target_id=str(bid),
        reason="upload_files",
        before=dict(before),
        after=dict(after) if after else None,
    )
    return {"status": "success", "batch_id": bid}


async def validate_import_batch(
    *,
    actor: Dict[str, Any],
    batch_id: int,
) -> Dict[str, Any]:
    started_at = time.time()
    bid = int(batch_id)
    batch = await database.fetch_one(import_batches.select().where(import_batches.c.id == bid))
    if not batch:
        raise HTTPException(status_code=404, detail="BATCH_NOT_FOUND")

    merchant_id = _as_text(batch["merchant_id"])
    source_system = _as_text(batch["source_system"])
    if not source_system:
        raise HTTPException(status_code=400, detail="BATCH_SOURCE_SYSTEM_MISSING")
    path = _as_text(_row_get(batch, "reviews_file_path"))
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=400, detail="REVIEWS_FILE_MISSING")

    raw = open(path, "rb").read()
    fmt = _detect_format(os.path.basename(path), None)
    rows = _read_jsonl(raw) if fmt == "jsonl" else _read_csv(raw)

    report = ImportReport(errors=[])
    seen_external: set[str] = set()

    # Clear existing items for re-validate (safe; batch-local).
    await database.execute("DELETE FROM import_items WHERE batch_id = :bid", {"bid": bid})

    for idx, row in enumerate(rows):
        report.total += 1
        ext_review_id = _as_text(row.get("external_review_id") or row.get("review_id") or row.get("id"))
        id_err = validate_imported_identifiers(source_system=source_system, external_review_id=ext_review_id)
        if id_err:
            report.rejected += 1
            report.errors.append({"row": idx, "error": id_err})
            await database.execute(
                import_items.insert().values(
                    batch_id=bid,
                    merchant_id=merchant_id,
                    source_system=source_system,
                    external_review_id=ext_review_id or None,
                    external_user_id=_as_text(row.get("external_user_id") or row.get("user_id")) or None,
                    payload_json=row,
                    match_product_key=None,
                    match_sku_key=None,
                    match_confidence=0.0,
                    group_id=None,
                    group_confidence=0.0,
                    dedupe_key=compute_import_dedupe_key(
                        merchant_id=merchant_id,
                        source_system=source_system,
                        external_review_id=ext_review_id,
                        platform=_as_text(row.get("platform")),
                        platform_product_id=_as_text(row.get("platform_product_id") or row.get("product_id")),
                        variant_id=_as_text(row.get("variant_id")) or None,
                        created_at=row.get("created_at"),
                    ),
                    status="rejected",
                    error_reason=id_err,
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
            continue
        if ext_review_id:
            if ext_review_id in seen_external:
                report.deduped += 1
                continue
            seen_external.add(ext_review_id)

        platform = _as_text(row.get("platform"))
        platform_product_id = _as_text(row.get("platform_product_id") or row.get("product_id"))
        variant_id = _as_text(row.get("variant_id")) or None

        status = "pending"
        error_reason = None
        match_product_key = None
        match_sku_key = None
        match_confidence = 0.0
        resolved_group_id: Optional[int] = None
        resolved_group_conf = 0.0

        if not platform or not platform_product_id:
            status = "rejected"
            error_reason = "missing_platform_or_platform_product_id"
            report.rejected += 1
        else:
            match_product_key = build_product_key(
                merchant_id=merchant_id, platform=platform, platform_product_id=platform_product_id
            )
            match_sku_key = build_sku_key(
                merchant_id=merchant_id,
                platform=platform,
                platform_product_id=platform_product_id,
                variant_id=variant_id,
            )
            if variant_id:
                status = "matched"
                match_confidence = 1.0
                report.matched += 1
            else:
                status = "downgraded_to_product_level"
                match_confidence = 0.6
                report.downgraded_to_product_level += 1

            # Best-effort group resolve (no side-effects here; only compute stats).
            group_hint = await resolve_review_group_for_product(
                merchant_id=merchant_id,
                platform=platform,
                platform_product_id=platform_product_id,
                variant_id=variant_id,
                product=None,
                payload=row,
            )
            if group_hint:
                resolved_group_conf = float(group_hint.get("confidence") or 0.0)
                gt = str(group_hint.get("group_type") or "")
                if gt == "GTIN":
                    report.group_resolved_gtin += 1
                elif gt == "BRAND_MPN":
                    report.group_resolved_mpn += 1
            else:
                report.group_resolved_none += 1

        if status == "rejected" and error_reason:
            report.errors.append({"row": idx, "error": error_reason})

        dedupe_key = compute_import_dedupe_key(
            merchant_id=merchant_id,
            source_system=source_system,
            external_review_id=ext_review_id or f"row:{idx}",
            platform=platform,
            platform_product_id=platform_product_id,
            variant_id=variant_id,
            created_at=row.get("created_at"),
        )

        await database.execute(
            import_items.insert().values(
                batch_id=bid,
                merchant_id=merchant_id,
                source_system=source_system,
                external_review_id=ext_review_id or None,
                external_user_id=_as_text(row.get("external_user_id") or row.get("user_id")) or None,
                payload_json=row,
                match_product_key=match_product_key,
                match_sku_key=match_sku_key,
                match_confidence=match_confidence,
                group_id=resolved_group_id,
                group_confidence=resolved_group_conf,
                dedupe_key=dedupe_key,
                status=status,
                error_reason=error_reason,
                created_at=_now(),
                updated_at=_now(),
            )
        )

    report_dict = report.to_dict()
    await database.execute(
        import_batches.update()
        .where(import_batches.c.id == bid)
        .values(status="validated", report_json=report_dict, updated_at=_now())
    )
    await _audit(
        actor=actor,
        action="reviews.import.validate",
        target_type="import_batch",
        target_id=str(bid),
        reason="validate",
        before=None,
        after=report_dict,
    )
    try:
        elapsed_ms = int((time.time() - started_at) * 1000)
        logger.info(
            "reviews.import.validate.completed %s",
            json.dumps(
                {
                    "merchant_id": merchant_id,
                    "source_system": source_system,
                    "batch_id": bid,
                    "totals": {
                        "total": report_dict.get("total"),
                        "matched": report_dict.get("matched"),
                        "downgraded_to_product_level": report_dict.get("downgraded_to_product_level"),
                        "rejected": report_dict.get("rejected"),
                        "deduped": report_dict.get("deduped"),
                    },
                    "group_resolve": report_dict.get("group_resolve"),
                    "elapsed_ms": elapsed_ms,
                },
                ensure_ascii=False,
            ),
        )
    except Exception:
        pass
    return {"status": "success", "batch_id": bid, "report": report_dict}


async def _get_or_create_identity(
    *,
    merchant_id: str,
    source_system: str,
    external_user_id: Optional[str],
    display_name: Optional[str],
    author_fingerprint: Optional[str],
) -> int:
    ext_uid = _as_text(external_user_id) or None
    if ext_uid:
        existing = await database.fetch_one(
            external_identities.select().where(
                (external_identities.c.merchant_id == merchant_id)
                & (external_identities.c.source_system == source_system)
                & (external_identities.c.external_user_id == ext_uid)
            )
        )
        if existing:
            return int(existing["id"])

    inserted = await database.execute(
        external_identities.insert().values(
            merchant_id=merchant_id,
            source_system=source_system,
            external_user_id=ext_uid,
            author_fingerprint=_as_text(author_fingerprint) or None,
            display_name=_as_text(display_name) or None,
            status="unclaimed",
            created_at=_now(),
            updated_at=_now(),
        )
    )
    return int(inserted)


def _extract_media_filenames(payload: Dict[str, Any]) -> List[str]:
    raw = payload.get("media_files") or payload.get("media") or payload.get("media_filenames")
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    if isinstance(raw, str):
        # comma-separated
        return [s.strip() for s in raw.split(",") if s.strip()]
    # Try media_1/media_2...
    out: List[str] = []
    for i in range(1, 11):
        v = payload.get(f"media_{i}")
        if v:
            out.append(str(v).strip())
    return [x for x in out if x]


def _new_media_public_id() -> str:
    return uuid4().hex


async def commit_import_batch(
    *,
    actor: Dict[str, Any],
    batch_id: int,
    reason: Optional[str],
) -> Dict[str, Any]:
    started_at = time.time()
    if not _as_text(reason):
        raise HTTPException(status_code=400, detail="REASON_REQUIRED")
    bid = int(batch_id)
    batch = await database.fetch_one(import_batches.select().where(import_batches.c.id == bid))
    if not batch:
        raise HTTPException(status_code=404, detail="BATCH_NOT_FOUND")
    if str(_row_get(batch, "status")) not in {"validated", "uploaded", "created"}:
        raise HTTPException(status_code=400, detail="BATCH_STATUS_INVALID")

    merchant_id = _as_text(batch["merchant_id"])
    source_system = _as_text(batch["source_system"])
    if not source_system:
        raise HTTPException(status_code=400, detail="BATCH_SOURCE_SYSTEM_MISSING")

    # Load media zip (optional). Convention: files are referenced by filename in each row.
    media_zip_path = _as_text(_row_get(batch, "media_zip_path"))
    media_blob: Dict[str, bytes] = {}
    if media_zip_path and os.path.exists(media_zip_path):
        try:
            with zipfile.ZipFile(media_zip_path, "r") as zf:
                for name in zf.namelist():
                    if name.endswith("/") or "__MACOSX" in name:
                        continue
                    media_blob[os.path.basename(name)] = zf.read(name)
        except Exception:
            media_blob = {}

    storage = _import_storage_dir()
    batch_dir = os.path.join(storage, f"batch_{bid}")
    os.makedirs(batch_dir, exist_ok=True)

    rows = await database.fetch_all(
        import_items.select().where(import_items.c.batch_id == bid).order_by(import_items.c.id.asc())
    )

    imported = 0
    rejected = 0
    for r in rows:
        status = str(_row_get(r, "status") or "")
        if status in {"rejected", "imported"}:
            if status == "rejected":
                rejected += 1
            continue

        payload_raw = _row_get(r, "payload_json")
        payload: Dict[str, Any] = {}
        if isinstance(payload_raw, dict):
            payload = payload_raw
        elif isinstance(payload_raw, str):
            # Some drivers/dialects can surface JSON/JSONB as text; accept both.
            try:
                parsed = json.loads(payload_raw)
                if isinstance(parsed, dict):
                    payload = parsed
            except Exception:
                payload = {}
        platform = _as_text(payload.get("platform"))
        platform_product_id = _as_text(payload.get("platform_product_id") or payload.get("product_id"))
        variant_id = _as_text(payload.get("variant_id")) or None
        if not platform or not platform_product_id:
            rejected += 1
            await database.execute(
                import_items.update()
                .where(import_items.c.id == int(r["id"]))
                .values(status="rejected", error_reason="missing_platform_or_platform_product_id", updated_at=_now())
            )
            continue

        pk = build_product_key(merchant_id=merchant_id, platform=platform, platform_product_id=platform_product_id)
        sk = build_sku_key(
            merchant_id=merchant_id,
            platform=platform,
            platform_product_id=platform_product_id,
            variant_id=variant_id,
        )

        # Resolve group and ensure membership (side-effect at commit time only).
        group_hint = await resolve_review_group_for_product(
            merchant_id=merchant_id,
            platform=platform,
            platform_product_id=platform_product_id,
            variant_id=variant_id,
            product=None,
            payload=payload,
        )
        group_id: Optional[int] = None
        if group_hint:
            group_id = await _get_or_create_group(
                group_key=str(group_hint["group_key"]),
                group_type=str(group_hint["group_type"]),
                confidence=float(group_hint.get("confidence") or 0.0),
                created_by="system",
                created_by_employee_id=None,
            )
            await ensure_membership_for_sku(
                actor=None,
                group_id=group_id,
                match_type=str(group_hint["group_type"]),
                confidence=float(group_hint.get("confidence") or 0.0),
                evidence=group_hint.get("evidence"),
                merchant_id=merchant_id,
                platform=platform,
                platform_product_id=platform_product_id,
                variant_id=variant_id,
                created_by="system",
                created_by_employee_id=None,
            )

        ext_user_id = _as_text(payload.get("external_user_id") or payload.get("user_id")) or None
        display_name = _as_text(payload.get("display_name") or payload.get("author")) or None
        author_fp = _as_text(payload.get("author_fingerprint")) or None
        author_id = await _get_or_create_identity(
            merchant_id=merchant_id,
            source_system=source_system,
            external_user_id=ext_user_id,
            display_name=display_name,
            author_fingerprint=author_fp,
        )

        verification = _as_text(payload.get("verification") or "unverified")
        rating = payload.get("rating")
        try:
            rating_int = int(rating) if rating not in (None, "") else None
        except Exception:
            rating_int = None

        title = _as_text(payload.get("title")) or None
        body = _as_text(payload.get("body") or payload.get("text")) or None

        # Dedupe: rely on DB unique index (merchant_id, source_system, external_review_id) when available.
        ext_review_id = (
            _as_text(payload.get("external_review_id") or payload.get("review_id") or _row_get(r, "external_review_id"))
            or None
        )
        id_err = validate_imported_identifiers(source_system=source_system, external_review_id=ext_review_id)
        if id_err:
            rejected += 1
            await database.execute(
                import_items.update()
                .where(import_items.c.id == int(r["id"]))
                .values(status="rejected", error_reason=id_err, updated_at=_now())
            )
            continue
        dedupe_key = _as_text(_row_get(r, "dedupe_key")) or _hash_dedupe_key(
            [merchant_id, source_system, ext_review_id or sk, body or ""]
        )

        try:
            new_review_id = await database.execute(
                product_reviews.insert().values(
                    product_key=pk,
                    sku_key=sk,
                    merchant_id=merchant_id,
                    platform=platform,
                    platform_product_id=platform_product_id,
                    variant_id=variant_id,
                    group_id=group_id,
                    author_user_id=author_id,
                    source_type="imported",
                    source_system=source_system,
                    external_review_id=ext_review_id,
                    dedupe_key=dedupe_key,
                    verification=verification,
                    rating=rating_int,
                    title=title,
                    body=body,
                    media_count=0,
                    risk_flags=None,
                    status="active",
                    created_at=_parse_datetime(payload.get("created_at")) or _now(),
                    updated_at=_now(),
                )
            )
        except Exception as e:
            # Most likely duplicate import; mark as rejected to keep idempotency.
            rejected += 1
            await database.execute(
                import_items.update()
                .where(import_items.c.id == int(r["id"]))
                .values(status="rejected", error_reason="duplicate_import", updated_at=_now())
            )
            continue

        # Attach media files (optional). Convention: payload lists filenames in media_files[] or media_1... etc.
        media_files = _extract_media_filenames(payload)
        media_inserted = 0
        for fname in media_files[:20]:
            blob = media_blob.get(os.path.basename(fname))
            if not blob:
                continue
            out_path = os.path.join(batch_dir, os.path.basename(fname))
            try:
                with open(out_path, "wb") as f:
                    f.write(blob)
                file_hash = hashlib.sha256(blob).hexdigest()
            except Exception:
                continue

            mtype = "video" if os.path.splitext(fname)[1].lower() in {".mp4", ".mov", ".webm"} else "image"
            public_id = _new_media_public_id()
            url = f"/agent/shop/v1/review-media/{public_id}"
            media_id = await database.execute(
                media_assets.insert().values(
                    review_id=int(new_review_id),
                    type=mtype,
                    public_id=public_id,
                    url=url,
                    file_path=out_path,
                    file_hash=file_hash,
                    status="active",
                    created_at=_now(),
                )
            )
            media_inserted += 1

        if media_inserted:
            await database.execute(
                product_reviews.update()
                .where(product_reviews.c.id == int(new_review_id))
                .values(media_count=media_inserted, updated_at=_now())
            )

        imported += 1
        await database.execute(
            import_items.update()
            .where(import_items.c.id == int(r["id"]))
            .values(status="imported", match_product_key=pk, match_sku_key=sk, updated_at=_now())
        )

    await database.execute(
        import_batches.update()
        .where(import_batches.c.id == bid)
        .values(status="committed", updated_at=_now())
    )
    await _audit(
        actor=actor,
        action="reviews.import.commit",
        target_type="import_batch",
        target_id=str(bid),
        reason=_as_text(reason),
        before=None,
        after={"imported": imported, "rejected": rejected},
    )
    try:
        elapsed_ms = int((time.time() - started_at) * 1000)
        logger.info(
            "reviews.import.commit.completed %s",
            json.dumps(
                {
                    "merchant_id": merchant_id,
                    "source_system": source_system,
                    "batch_id": bid,
                    "totals": {"imported": imported, "rejected": rejected},
                    "elapsed_ms": elapsed_ms,
                },
                ensure_ascii=False,
            ),
        )
    except Exception:
        pass
    return {
        "status": "success",
        "batch_id": bid,
        "imported": imported,
        "rejected": rejected,
    }


async def reprocess_import_batch(
    *,
    actor: Dict[str, Any],
    batch_id: int,
    mode: str,
) -> Dict[str, Any]:
    """
    Best-effort reprocessing for an existing import batch.

    Supported:
    - variant_match: re-run validate_import_batch (rebuild import_items)
    - group_resolve: re-run group resolving for current import_items
    """
    bid = int(batch_id)
    mode_norm = _as_text(mode).lower()
    if mode_norm == "variant_match":
        return await validate_import_batch(actor=actor, batch_id=bid)

    if mode_norm != "group_resolve":
        raise HTTPException(status_code=400, detail="INVALID_REPROCESS_MODE")

    batch = await database.fetch_one(import_batches.select().where(import_batches.c.id == bid))
    if not batch:
        raise HTTPException(status_code=404, detail="BATCH_NOT_FOUND")

    merchant_id = _as_text(batch["merchant_id"])
    source_system = _as_text(batch["source_system"])

    items = await database.fetch_all(import_items.select().where(import_items.c.batch_id == bid))
    updated = 0
    created_groups = 0
    for it in items:
        if str(_row_get(it, "status")) in {"imported", "rejected"}:
            continue
        payload_raw = _row_get(it, "payload_json")
        payload = payload_raw if isinstance(payload_raw, dict) else {}
        platform = _as_text(payload.get("platform"))
        platform_product_id = _as_text(payload.get("platform_product_id") or payload.get("product_id"))
        variant_id = _as_text(payload.get("variant_id")) or None
        if not platform or not platform_product_id:
            continue

        group_hint = await resolve_review_group_for_product(
            merchant_id=merchant_id,
            platform=platform,
            platform_product_id=platform_product_id,
            variant_id=variant_id,
            product=None,
            payload=payload,
        )
        if not group_hint:
            continue

        before = dict(it)
        gid = await _get_or_create_group(
            group_key=str(group_hint["group_key"]),
            group_type=str(group_hint["group_type"]),
            confidence=float(group_hint.get("confidence") or 0.0),
            created_by="employee",
            created_by_employee_id=_as_text(actor.get("employee_id") or actor.get("user_id") or actor.get("sub")) or None,
        )
        if not _row_get(it, "group_id"):
            created_groups += 1

        await database.execute(
            import_items.update()
            .where(import_items.c.id == int(it["id"]))
            .values(
                group_id=int(gid),
                group_confidence=float(group_hint.get("confidence") or 0.0),
                updated_at=_now(),
            )
        )
        updated += 1

        after_row = await database.fetch_one(import_items.select().where(import_items.c.id == int(it["id"])))
        await _audit(
            actor=actor,
            action="reviews.import.reprocess",
            target_type="import_item",
            target_id=str(it["id"]),
            reason=f"group_resolve:{source_system}",
            before=before,
            after=dict(after_row) if after_row else None,
        )

    return {"status": "success", "batch_id": bid, "mode": mode_norm, "updated": updated, "groups_touched": created_groups}
