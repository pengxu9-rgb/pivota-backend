"""Persisted lifecycle for browser collector tokens.

The JWT remains the credential; this module is what lets a merchant see,
revoke, and renew one. Every issued token gets a row keyed by its ``jti``.
Every store has a token generation (``min_token_version``); a token whose
``sv`` claim is below it is refused whether or not its row exists, which is
how tokens issued before this registry existed (format v1, no ``jti``) are
revoked as a set.

Verification is fail-CLOSED on a registry error: a browser token can only
write observational rows, and the ledger it would write to lives in the same
database, so refusing during an outage loses nothing that would have landed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, select

from db.database import database
from db.merchant_collector_tokens import (
    merchant_collector_token_policy,
    merchant_collector_tokens,
)
from services.merchant_web_collector_service import (
    LEGACY_TOKEN_VERSION,
    RENEWAL_WINDOW_DAYS,
    WebCollectorError,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _text(value: Any) -> str:
    return str(value or "").strip()


async def current_store_token_version(store_id: str) -> int:
    row = await database.fetch_one(
        select(merchant_collector_token_policy.c.min_token_version).where(
            merchant_collector_token_policy.c.store_id == _text(store_id)
        )
    )
    if not row:
        return 1
    try:
        return max(1, int(dict(row)["min_token_version"]))
    except (TypeError, ValueError, KeyError):
        return 1


async def register_issued_token(
    *,
    issued: Dict[str, Any],
    merchant_id: str,
    store_id: str,
    issued_by: Optional[str] = None,
    supersedes: Optional[str] = None,
) -> Dict[str, Any]:
    """Record a token the collector service just signed."""
    jti = _text(issued.get("jti"))
    if not jti:
        raise ValueError("issued token carries no jti")
    row = {
        "jti": jti,
        "merchant_id": _text(merchant_id),
        "store_id": _text(store_id),
        "token_type": _text(issued.get("token_type")),
        "token_version": int(issued.get("token_version") or 0),
        "store_token_version": int(issued.get("store_token_version") or 1),
        "allowed_origins": issued.get("allowed_origins"),
        "issued_at": _aware(issued.get("issued_at")) or _now(),
        "expires_at": _aware(issued.get("expires_at")),
        "issued_by": _text(issued_by)[:128] or None,
    }
    if row["expires_at"] is None:
        raise ValueError("issued token carries no expires_at")
    await database.execute(merchant_collector_tokens.insert().values(**row))
    if supersedes:
        await database.execute(
            merchant_collector_tokens.update()
            .where(
                merchant_collector_tokens.c.jti == _text(supersedes),
                merchant_collector_tokens.c.merchant_id == row["merchant_id"],
            )
            .values(superseded_by=jti)
        )
    return row


async def fetch_token(jti: str) -> Optional[Dict[str, Any]]:
    row = await database.fetch_one(
        select(merchant_collector_tokens).where(merchant_collector_tokens.c.jti == _text(jti))
    )
    return dict(row) if row else None


async def enforce_token_registry(claims: Dict[str, Any]) -> None:
    """Refuse a syntactically valid token the registry no longer honours."""
    store_id = _text(claims.get("store_id"))
    try:
        min_version = await current_store_token_version(store_id)
        generation = int(claims.get("sv") or 1)
        if generation < min_version:
            raise WebCollectorError(401, "Web collector token has been revoked")
        jti = _text(claims.get("jti"))
        if not jti:
            # Legacy v1: no row to consult. Honoured only while the store has
            # never bumped its generation (checked above).
            if int(claims.get("v") or 0) != LEGACY_TOKEN_VERSION:
                raise WebCollectorError(401, "Invalid web collector token")
            return
        row = await fetch_token(jti)
        if row is None:
            raise WebCollectorError(401, "Web collector token is not registered")
        if row.get("revoked_at") is not None:
            raise WebCollectorError(401, "Web collector token has been revoked")
        if _text(row.get("store_id")) != store_id or _text(row.get("merchant_id")) != _text(
            claims.get("merchant_id")
        ):
            raise WebCollectorError(401, "Invalid web collector token")
    except WebCollectorError:
        raise
    except Exception as exc:
        raise WebCollectorError(503, "Web collector token registry is unavailable") from exc


async def revoke_token(*, jti: str, merchant_id: str, reason: str) -> bool:
    """Revoke one token; True only if it was live and owned by ``merchant_id``.

    Deliberately SELECT-then-UPDATE rather than reading the UPDATE's return:
    `databases` on asyncpg returns no rowcount for an UPDATE (the Postgres
    dialect gate proved a rowcount-based version always reported False),
    while SQLite happily returns one. The two engines must agree.
    """
    row = await database.fetch_one(
        select(
            merchant_collector_tokens.c.jti,
            merchant_collector_tokens.c.merchant_id,
            merchant_collector_tokens.c.revoked_at,
        ).where(merchant_collector_tokens.c.jti == _text(jti))
    )
    if not row:
        return False
    current = dict(row)
    if _text(current.get("merchant_id")) != _text(merchant_id) or current.get("revoked_at") is not None:
        return False
    await database.execute(
        merchant_collector_tokens.update()
        .where(
            merchant_collector_tokens.c.jti == _text(jti),
            merchant_collector_tokens.c.merchant_id == _text(merchant_id),
            merchant_collector_tokens.c.revoked_at.is_(None),
        )
        .values(revoked_at=_now(), revoked_reason=_text(reason)[:64] or "revoked")
    )
    return True


async def revoke_store_tokens(*, store_id: str, merchant_id: str, reason: str) -> Dict[str, Any]:
    """Bump the store's generation so every earlier token, registered or not,
    is refused; mark the registered ones revoked for the listing."""
    now = _now()
    store = _text(store_id)
    merchant = _text(merchant_id)
    current = await current_store_token_version(store)
    new_version = current + 1
    existing = await database.fetch_one(
        select(merchant_collector_token_policy.c.store_id).where(
            merchant_collector_token_policy.c.store_id == store
        )
    )
    if existing:
        await database.execute(
            merchant_collector_token_policy.update()
            .where(merchant_collector_token_policy.c.store_id == store)
            .values(min_token_version=new_version, merchant_id=merchant, updated_at=now)
        )
    else:
        await database.execute(
            merchant_collector_token_policy.insert().values(
                store_id=store, merchant_id=merchant, min_token_version=new_version, updated_at=now
            )
        )
    # Count first, then mark: see revoke_token for why the UPDATE's return
    # value cannot be trusted across engines.
    live = await database.fetch_all(
        select(merchant_collector_tokens.c.jti).where(
            merchant_collector_tokens.c.store_id == store,
            merchant_collector_tokens.c.merchant_id == merchant,
            merchant_collector_tokens.c.revoked_at.is_(None),
        )
    )
    await database.execute(
        merchant_collector_tokens.update()
        .where(
            merchant_collector_tokens.c.store_id == store,
            merchant_collector_tokens.c.merchant_id == merchant,
            merchant_collector_tokens.c.revoked_at.is_(None),
        )
        .values(revoked_at=now, revoked_reason=_text(reason)[:64] or "store_revoked")
    )
    return {"store_id": store, "min_token_version": new_version, "revoked_count": len(live)}


def token_state(row: Dict[str, Any], *, now: Optional[datetime] = None) -> str:
    now = now or _now()
    if row.get("revoked_at") is not None:
        return "revoked"
    if row.get("superseded_by"):
        return "superseded"
    expires_at = _aware(row.get("expires_at"))
    if expires_at is not None and expires_at <= now:
        return "expired"
    return "active"


def token_public_view(row: Dict[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or _now()
    expires_at = _aware(row.get("expires_at"))
    state = token_state(row, now=now)
    return {
        "jti": row.get("jti"),
        "store_id": row.get("store_id"),
        "token_type": row.get("token_type"),
        "token_version": row.get("token_version"),
        "store_token_version": row.get("store_token_version"),
        "allowed_origins": row.get("allowed_origins"),
        "issued_at": (_aware(row.get("issued_at")) or now).isoformat(),
        "expires_at": expires_at.isoformat() if expires_at else None,
        "revoked_at": _aware(row.get("revoked_at")).isoformat() if row.get("revoked_at") else None,
        "revoked_reason": row.get("revoked_reason"),
        "superseded_by": row.get("superseded_by"),
        "state": state,
        "renewal_due": bool(
            state == "active"
            and expires_at is not None
            and expires_at <= now + timedelta(days=RENEWAL_WINDOW_DAYS)
        ),
    }


async def list_store_tokens(*, store_id: str, merchant_id: str) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        select(merchant_collector_tokens)
        .where(
            merchant_collector_tokens.c.store_id == _text(store_id),
            merchant_collector_tokens.c.merchant_id == _text(merchant_id),
        )
        .order_by(merchant_collector_tokens.c.issued_at.desc())
    )
    now = _now()
    return [token_public_view(dict(row), now=now) for row in rows]


async def expiring_tokens(
    *, within_days: int, merchant_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Live tokens that expire within the window and have not been renewed:
    the list a renewal alert is built from."""
    now = _now()
    horizon = now + timedelta(days=max(1, int(within_days)))
    conditions = [
        merchant_collector_tokens.c.revoked_at.is_(None),
        merchant_collector_tokens.c.superseded_by.is_(None),
        merchant_collector_tokens.c.expires_at <= horizon,
        merchant_collector_tokens.c.expires_at > now,
    ]
    if merchant_id:
        conditions.append(merchant_collector_tokens.c.merchant_id == _text(merchant_id))
    rows = await database.fetch_all(
        select(merchant_collector_tokens)
        .where(and_(*conditions))
        .order_by(merchant_collector_tokens.c.expires_at.asc())
    )
    return [token_public_view(dict(row), now=now) for row in rows]
