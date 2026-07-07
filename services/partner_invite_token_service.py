from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from config.settings import settings
from db.database import database


DEFAULT_INVITE_EXPIRY_DAYS = 90
TOKEN_PREFIX_LENGTH = 8


class PartnerInviteTokenError(Exception):
    """Base error."""


class TokenInvalidError(PartnerInviteTokenError):
    """Token doesn't exist or hash mismatch."""


class TokenNotRedeemableError(PartnerInviteTokenError):
    """Token exists but status != active OR is expired."""


@dataclass(frozen=True)
class IssueResult:
    token_id: int
    raw_token: str
    expires_at: datetime
    signup_url: str


async def issue(
    *,
    channel_partner_id: int,
    issued_by: str,
    expires_in_days: int = DEFAULT_INVITE_EXPIRY_DAYS,
    notes: str | None = None,
    max_uses: int | None = None,
) -> IssueResult:
    """Generate a new multi-use invite token for a partner.

    max_uses is an optional soft cap on how many distinct merchants may redeem
    the link; None (default) means unlimited until it expires or is revoked.
    """

    if int(channel_partner_id) <= 0:
        raise ValueError("channel_partner_id must be positive")
    if int(expires_in_days) <= 0:
        raise ValueError("expires_in_days must be positive")
    if max_uses is not None and int(max_uses) <= 0:
        raise ValueError("max_uses must be positive when set")

    partner = await database.fetch_one(
        """
        SELECT id
        FROM channel_partners
        WHERE id = :channel_partner_id
        LIMIT 1
        """,
        {"channel_partner_id": int(channel_partner_id)},
    )
    if not partner:
        raise ValueError(f"Channel partner not found: {channel_partner_id}")

    raw_token = _generate_raw_token()
    token_hash = _hash_token(raw_token)
    token_prefix = raw_token[:TOKEN_PREFIX_LENGTH]
    expires_at = _utcnow() + timedelta(days=int(expires_in_days))

    row = await database.fetch_one(
        """
        INSERT INTO partner_invite_tokens (
          channel_partner_id,
          token_hash,
          token_prefix,
          expires_at,
          status,
          issued_by,
          notes,
          max_uses
        ) VALUES (
          :channel_partner_id,
          :token_hash,
          :token_prefix,
          :expires_at,
          'active',
          :issued_by,
          :notes,
          :max_uses
        )
        RETURNING id
        """,
        {
            "channel_partner_id": int(channel_partner_id),
            "token_hash": token_hash,
            "token_prefix": token_prefix,
            "expires_at": expires_at,
            "issued_by": str(issued_by or "").strip() or None,
            "notes": notes,
            "max_uses": int(max_uses) if max_uses is not None else None,
        },
    )
    if not row:
        raise RuntimeError("Unable to insert partner invite token")

    return IssueResult(
        token_id=int(_row_get(row, "id")),
        raw_token=raw_token,
        expires_at=expires_at,
        signup_url=_build_signup_url(raw_token),
    )


async def consume(
    *,
    raw_token: str,
    merchant_id: str,
) -> int:
    """Redeem a MULTI-USE token for a merchant. Returns partner_attribution id.

    The link stays 'active' and reusable until it expires or is revoked — each
    distinct merchant that redeems it gets a partner_attribution row and bumps
    use_count. A merchant redeeming twice is idempotent (unique merchant+partner)
    and does not double-count. max_uses, when set, is a soft cap.
    """

    normalized_token = _normalize_token(raw_token)
    normalized_merchant_id = _normalize_merchant_id(merchant_id)
    token_hash = _hash_token(normalized_token)

    token = await database.fetch_one(
        """
        SELECT
          id,
          channel_partner_id,
          status,
          expires_at,
          use_count,
          max_uses
        FROM partner_invite_tokens
        WHERE token_hash = :token_hash
        LIMIT 1
        """,
        {"token_hash": token_hash},
    )
    if not token:
        raise TokenInvalidError("Invite token not found")

    token_id = int(_row_get(token, "id"))
    status = str(_row_get(token, "status") or "")
    if status != "active":
        raise TokenNotRedeemableError(f"Invite token status is {status}")

    expires_at = _coerce_datetime(_row_get(token, "expires_at"))
    if expires_at <= _utcnow():
        # Mark expired outside the redemption transaction so the status flip is
        # not rolled back by the raise below.
        await database.execute(
            """
            UPDATE partner_invite_tokens
            SET status = 'expired'
            WHERE id = :token_id
              AND status = 'active'
            """,
            {"token_id": token_id},
        )
        raise TokenNotRedeemableError("Invite token is expired")

    async with database.transaction():
        # Re-read under a row lock so a concurrent revoke/expire, or a parallel
        # redemption bumping use_count, is observed consistently.
        locked = await database.fetch_one(
            """
            SELECT channel_partner_id, status, expires_at, use_count, max_uses
            FROM partner_invite_tokens
            WHERE id = :token_id
            FOR UPDATE
            """,
            {"token_id": token_id},
        )
        if locked is None or str(_row_get(locked, "status") or "") != "active":
            raise TokenNotRedeemableError(
                "Invite token was revoked or expired"
            )
        if _coerce_datetime(_row_get(locked, "expires_at")) <= _utcnow():
            raise TokenNotRedeemableError("Invite token is expired")

        channel_partner_id = int(_row_get(locked, "channel_partner_id"))
        max_uses = _row_get(locked, "max_uses")
        use_count = int(_row_get(locked, "use_count") or 0)
        if max_uses is not None and use_count >= int(max_uses):
            raise TokenNotRedeemableError(
                "Invite token has reached its usage limit"
            )

        inserted = await database.fetch_one(
            """
            INSERT INTO partner_attribution (
              merchant_id,
              channel_partner_id,
              status,
              registered_at
            ) VALUES (
              :merchant_id,
              :channel_partner_id,
              'registered',
              NOW()
            )
            ON CONFLICT (merchant_id, channel_partner_id)
            DO NOTHING
            RETURNING id
            """,
            {
                "merchant_id": normalized_merchant_id,
                "channel_partner_id": channel_partner_id,
            },
        )
        if inserted:
            # Count the use only for a genuinely new merchant; the link stays
            # 'active' and reusable. consumed_at records the first redemption.
            await database.execute(
                """
                UPDATE partner_invite_tokens
                SET use_count = use_count + 1,
                    consumed_at = COALESCE(consumed_at, NOW())
                WHERE id = :token_id
                """,
                {"token_id": token_id},
            )
            return int(_row_get(inserted, "id"))

        existing = await database.fetch_one(
            """
            SELECT id
            FROM partner_attribution
            WHERE merchant_id = :merchant_id
              AND channel_partner_id = :channel_partner_id
            LIMIT 1
            """,
            {
                "merchant_id": normalized_merchant_id,
                "channel_partner_id": channel_partner_id,
            },
        )
        if not existing:
            raise RuntimeError("Unable to resolve partner attribution row")
        return int(_row_get(existing, "id"))


async def revoke(
    *,
    token_id: int,
    revoked_by: str,
    reason: str | None = None,
    channel_partner_id: int | None = None,
) -> None:
    """Mark a token revoked. Only active tokens can be revoked."""

    if int(token_id) <= 0:
        raise TokenInvalidError("token_id must be positive")
    revoker = str(revoked_by or "").strip()

    # Build the partner-scope filter conditionally rather than
    # `:channel_partner_id IS NULL OR ...`: a bare `$n IS NULL` bind gives
    # Postgres no way to infer the parameter type and it raises 42P08
    # (AmbiguousParameterError). The clause is a fixed literal — the value
    # still binds through :channel_partner_id — so there is no injection risk.
    partner_clause = ""
    if channel_partner_id is not None:
        partner_clause = "AND channel_partner_id = :channel_partner_id"

    update_params: dict[str, Any] = {
        "token_id": int(token_id),
        "revoked_by": revoker,
        "revoked_reason": reason,
    }
    if channel_partner_id is not None:
        update_params["channel_partner_id"] = int(channel_partner_id)

    updated = await database.fetch_one(
        f"""
        UPDATE partner_invite_tokens
        SET status = 'revoked',
            revoked_at = NOW(),
            revoked_by = :revoked_by,
            revoked_reason = :revoked_reason
        WHERE id = :token_id
          AND status = 'active'
          {partner_clause}
        RETURNING id
        """,
        update_params,
    )
    if updated:
        return

    select_params: dict[str, Any] = {"token_id": int(token_id)}
    if channel_partner_id is not None:
        select_params["channel_partner_id"] = int(channel_partner_id)

    current = await database.fetch_one(
        f"""
        SELECT status
        FROM partner_invite_tokens
        WHERE id = :token_id
          {partner_clause}
        LIMIT 1
        """,
        select_params,
    )
    if not current:
        raise TokenInvalidError(f"Invite token not found: {token_id}")
    raise TokenNotRedeemableError(
        f"Invite token status is {str(_row_get(current, 'status') or '')}"
    )


async def list_for_partner(
    *,
    channel_partner_id: int,
    include_terminal: bool = True,
) -> list[dict[str, Any]]:
    """List token metadata for a partner. Raw tokens and hashes are never returned."""

    status_filter = "" if include_terminal else "AND status = 'active'"
    rows = await database.fetch_all(
        f"""
        SELECT
          id AS token_id,
          token_prefix,
          status,
          issued_by,
          expires_at,
          created_at,
          consumed_by_merchant_id,
          use_count,
          max_uses,
          revoked_by,
          revoked_reason,
          notes
        FROM partner_invite_tokens
        WHERE channel_partner_id = :channel_partner_id
          {status_filter}
        ORDER BY created_at DESC, id DESC
        """,
        {"channel_partner_id": int(channel_partner_id)},
    )
    return [
        {
            "token_id": int(_row_get(row, "token_id")),
            "token_prefix": _row_get(row, "token_prefix"),
            "status": _row_get(row, "status"),
            "issued_by": _row_get(row, "issued_by"),
            "expires_at": _row_get(row, "expires_at"),
            "created_at": _row_get(row, "created_at"),
            "consumed_by_merchant_id": _row_get(row, "consumed_by_merchant_id"),
            "use_count": int(_row_get(row, "use_count") or 0),
            "max_uses": _row_get(row, "max_uses"),
            "revoked_by": _row_get(row, "revoked_by"),
            "revoked_reason": _row_get(row, "revoked_reason"),
            "notes": _row_get(row, "notes"),
        }
        for row in rows or []
    ]


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _generate_raw_token() -> str:
    return "mkto_" + secrets.token_urlsafe(24)


def _build_signup_url(raw_token: str) -> str:
    base_url = str(
        getattr(settings, "merchant_signup_base_url", "")
        or "https://merchant.pivota.cc/signup"
    ).strip()
    parts = urlsplit(base_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["ref"] = raw_token
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query),
            parts.fragment,
        )
    )


def _normalize_token(raw_token: str) -> str:
    normalized = str(raw_token or "").strip()
    if not normalized:
        raise TokenInvalidError("Invite token is required")
    return normalized


def _normalize_merchant_id(merchant_id: str) -> str:
    normalized = str(merchant_id or "").strip()
    if not normalized:
        raise ValueError("merchant_id must be non-empty")
    return normalized


def _coerce_datetime(value: Any) -> datetime:
    if not isinstance(value, datetime):
        raise TokenNotRedeemableError("Invite token expiry is invalid")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _row_get(row: Any, key: str) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except Exception:
        return getattr(row, key)
