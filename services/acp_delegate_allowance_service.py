"""ACP delegated-token ALLOWANCE registry (P1 delegated-token design, PR-A).

ACP's design method for external agents is *delegated payment*: the platform
vaults the buyer's card and hands the business a single-use, amount-capped,
merchant-scoped token. **Pivota never runs that vault.** We do not implement
`POST /agentic_commerce/delegate_payment`, we never receive cardholder data, and
this registry stores NO payment-method material of any kind — not a PAN, not a
CVC, not a cryptogram, not even a display last4/brand/IIN. It records only the
ALLOWANCE a delegated token was minted under, so `complete_session` can enforce
that allowance locally and fail-closed IN ADDITION to whatever the PSP enforces
authoritatively on its own side.

(The retired pivota-acp service's `delegate_payment` stored the entire request —
raw PAN and CVC — unencrypted in a JSONB `payload` column. That is a PCI Req 3.2
violation by design, and CVC storage is prohibited outright. This schema is
shaped so the same mistake is not expressible; a schema-guard test asserts no
column here matches number|cvc|pan|cryptogram.)

There is deliberately NO public route in this module's PR. The registry is
populated by conformance tests and, later, by PSP-relay integrations; the public
mint door (if one is ever exposed) is the gateway's concern, and it will still
never be a vault.

Three closed gaps versus the retired service, all fail-closed:
  * `expires_at` is NOT NULL and actually checked at completion.
  * single-use is real — `bind_allowance_to_session` is a CAS consumption.
  * `merchant_id` is validated against the completing session's merchant.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from db.acp_delegate_allowances import acp_delegate_allowances
from db.database import IS_POSTGRES, database, engine, metadata
from utils.logger import logger

# Wire-parity with the retired pivota-acp service: `vt_` + 14 hex chars.
DELEGATE_TOKEN_PREFIX = "vt_"
DELEGATE_TOKEN_HEX_LEN = 14

# The only delegation reason this registry mints or accepts. The column exists
# because the spec's allowance object carries the field, not because recurring
# delegation is supported.
ALLOWANCE_REASON_ONE_TIME = "one_time"


class AcpDelegateAllowanceError(Exception):
    """A registry operation failed for an infrastructure reason (the table could
    not be reached/healed, or a mint was malformed).

    Deliberately its OWN type rather than AcpCheckoutSessionError: the session
    service imports THIS module, so importing its error class back here would be
    a circular import. `complete_session` translates this into the session
    service's error vocabulary at the one place it calls in.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: Optional[int] = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


def new_delegate_token_id() -> str:
    """A fresh `vt_<14 hex>` token id (the retired service's exact format)."""
    return f"{DELEGATE_TOKEN_PREFIX}{uuid.uuid4().hex[:DELEGATE_TOKEN_HEX_LEN]}"


def is_delegate_token(token: Optional[str]) -> bool:
    """The token gate, spelled EXACTLY as the wire contract does:
    `startswith("vt_")`. `pm_` (a real PSP payment method) and a future `spt_`
    (Stripe SharedPaymentToken) are NOT delegate tokens and never touch this
    registry."""
    return str(token or "").startswith(DELEGATE_TOKEN_PREFIX)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_datetime_utc(value: Any) -> Optional[datetime]:
    """Same defensiveness as the session service: the `databases` driver returns
    a datetime on Postgres but may hand back an ISO string on SQLite."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _coerce_bool(value: Any) -> bool:
    """SQLite stores BOOLEAN as 0/1; Postgres returns a real bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"true", "t", "1", "yes", "on"}


async def _ensure_acp_delegate_allowances_table() -> None:
    """Best-effort self-healing for environments where migrations cannot be run
    manually. Safe to call multiple times (IF NOT EXISTS). Mirrors migration
    192_acp_delegate_allowances.sql. The raw DDL runs FIRST so on Postgres the
    canonical TEXT/BOOLEAN/TIMESTAMPTZ shape always wins; the SQLAlchemy
    metadata create is the SQLite dev/test path (pattern:
    services/acp_checkout_session_service._ensure_acp_checkout_sessions_table).
    """
    if IS_POSTGRES:
        await database.execute(
            """
            CREATE TABLE IF NOT EXISTS acp_delegate_allowances (
                token_id TEXT PRIMARY KEY,
                checkout_session_id TEXT NOT NULL,
                merchant_id TEXT NOT NULL,
                max_amount INTEGER NOT NULL,
                currency TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT 'one_time',
                expires_at TIMESTAMPTZ NOT NULL,
                used BOOLEAN NOT NULL DEFAULT FALSE,
                used_at TIMESTAMPTZ,
                used_by_session TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        await database.execute(
            "CREATE INDEX IF NOT EXISTS idx_acp_delegate_allowances_session "
            "ON acp_delegate_allowances (checkout_session_id)"
        )

    try:
        metadata.create_all(engine, tables=[acp_delegate_allowances])
    except Exception:
        pass


async def _with_table_self_heal(label: str, run) -> Any:
    """Run a DB coroutine factory, healing a missing/drifted table once and
    retrying. A failure we cannot heal surfaces as a NAMED registry error (which
    the caller maps to a 503), never a raw 500 — the same posture as the session
    service's `_with_table_self_heal`."""
    try:
        return await run()
    except Exception:
        try:
            await _ensure_acp_delegate_allowances_table()
            return await run()
        except Exception as exc:  # noqa: BLE001 — a registry we cannot read is a named 503
            raise AcpDelegateAllowanceError(
                f"{label}: {str(exc)[:200]}",
                code="acp_allowance_registry_unavailable",
                status_code=503,
            ) from exc


def _row_to_allowance(row: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    data = dict(row)
    return {
        "token_id": str(data.get("token_id") or ""),
        "checkout_session_id": str(data.get("checkout_session_id") or ""),
        "merchant_id": str(data.get("merchant_id") or ""),
        "max_amount": (
            int(data["max_amount"]) if data.get("max_amount") is not None else None
        ),
        "currency": str(data.get("currency") or ""),
        "reason": str(data.get("reason") or ""),
        "expires_at": _coerce_datetime_utc(data.get("expires_at")),
        "used": _coerce_bool(data.get("used")),
        "used_at": _coerce_datetime_utc(data.get("used_at")),
        "used_by_session": (str(data.get("used_by_session") or "") or None),
        "created_at": _coerce_datetime_utc(data.get("created_at")),
    }


async def mint_allowance(
    *,
    token_id: Optional[str] = None,
    checkout_session_id: str,
    merchant_id: str,
    max_amount: int,
    currency: str,
    reason: str = ALLOWANCE_REASON_ONE_TIME,
    expires_at: datetime,
) -> Dict[str, Any]:
    """Record an allowance for a delegated token. SERVICE-LEVEL ONLY — this PR
    exposes no public route, because minting an allowance is not the same thing
    as vaulting a card and Pivota does neither on a public door.

    Validates fail-closed at the mint, so an unenforceable row can never exist:
    only `one_time` is a legal reason, and `expires_at` is required AND must be
    in the future (the retired service accepted — and then ignored — anything).
    """
    session_id = str(checkout_session_id or "").strip()
    if not session_id:
        raise AcpDelegateAllowanceError(
            "checkout_session_id is required", code="invalid_allowance", status_code=422
        )
    merchant = str(merchant_id or "").strip()
    if not merchant:
        raise AcpDelegateAllowanceError(
            "merchant_id is required", code="invalid_allowance", status_code=422
        )
    cur = str(currency or "").strip()
    if not cur:
        raise AcpDelegateAllowanceError(
            "currency is required", code="invalid_allowance", status_code=422
        )
    try:
        amount = int(max_amount)
    except (TypeError, ValueError):
        amount = -1
    if amount < 0:
        raise AcpDelegateAllowanceError(
            "max_amount must be a non-negative minor-unit integer",
            code="invalid_allowance",
            status_code=422,
        )
    if str(reason or "").strip() != ALLOWANCE_REASON_ONE_TIME:
        raise AcpDelegateAllowanceError(
            f"unsupported allowance reason {reason!r}: only "
            f"{ALLOWANCE_REASON_ONE_TIME!r} delegation is supported",
            code="invalid_allowance",
            status_code=422,
        )
    expiry = _coerce_datetime_utc(expires_at)
    if expiry is None:
        raise AcpDelegateAllowanceError(
            "expires_at is required", code="invalid_allowance", status_code=422
        )
    if expiry <= _now_utc():
        raise AcpDelegateAllowanceError(
            "expires_at must be in the future", code="invalid_allowance", status_code=422
        )

    tid = str(token_id or "").strip() or new_delegate_token_id()
    now = _now_utc()
    values = {
        "token_id": tid,
        "checkout_session_id": session_id,
        "merchant_id": merchant,
        "max_amount": amount,
        "currency": cur,
        "reason": ALLOWANCE_REASON_ONE_TIME,
        "expires_at": expiry,
        "used": False,
        "used_at": None,
        "used_by_session": None,
        "created_at": now,
    }

    async def _run():
        return await database.execute(acp_delegate_allowances.insert().values(**values))

    await _with_table_self_heal("allowance_mint_failed", _run)
    # NEVER log the token id: a delegate token is bearer material on the money
    # path (invariant: no token logging).
    logger.info(
        "[AcpDelegateAllowance] minted allowance session=%s merchant=%s "
        "max_amount=%s currency=%s",
        session_id, merchant, amount, cur,
    )
    return dict(values)


async def get_allowance(token_id: str) -> Optional[Dict[str, Any]]:
    """The recorded allowance for a delegate token, or None when we have no
    record of it. A miss is REFUSED by the caller, never waved through: we do not
    charge a token whose allowance we cannot verify."""
    tid = str(token_id or "").strip()
    if not tid:
        return None

    async def _run():
        return await database.fetch_one(
            acp_delegate_allowances.select().where(
                acp_delegate_allowances.c.token_id == tid
            )
        )

    row = await _with_table_self_heal("allowance_lookup_failed", _run)
    return _row_to_allowance(row)


# The single-use CAS. Same technique as the session claim: the condition lives in
# the WHERE clause and RETURNING tells us whether OUR update is the one that
# landed (portable — SQLite >= 3.35 and Postgres both support UPDATE ...
# RETURNING).
#
# `used = FALSE OR used_by_session = :session_id` is the whole idempotency
# story: an unconsumed token binds; a token this SAME session already bound
# re-binds harmlessly (a completion retry, or a stale-resume of that session's
# own attempt, must NOT be refused by its own earlier bind — that would wedge a
# session that may already have money in flight); a token bound by ANY OTHER
# session refuses.
_BIND_SQL = """
UPDATE acp_delegate_allowances
SET used = TRUE,
    used_at = :now,
    used_by_session = :session_id
WHERE token_id = :token_id
  AND (used = FALSE OR used_by_session = :session_id)
RETURNING token_id, used_by_session
"""


async def bind_allowance_to_session(*, token_id: str, session_id: str) -> bool:
    """Consume the allowance for `session_id`. True when this session holds the
    token (freshly bound, or re-binding its own prior bind); False when the token
    is unknown or already bound to a DIFFERENT session."""
    tid = str(token_id or "").strip()
    sid = str(session_id or "").strip()
    if not tid or not sid:
        return False

    async def _run():
        return await database.fetch_one(
            _BIND_SQL, {"now": _now_utc(), "session_id": sid, "token_id": tid}
        )

    row = await _with_table_self_heal("allowance_bind_failed", _run)
    if row is None:
        return False
    return str(dict(row).get("used_by_session") or "") == sid
