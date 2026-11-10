"""
Buyer Vault (Unified Buyer Account) tables + lightweight helpers.

Design goals:
- Store buyer profile data (email is handled by shop_users) + shipping addresses.
- Provide agent-scoped, pairwise buyer_ref mapping (opaque, non-enumerable).
- Store mandate + authorization-token skeletons for future agentic payments.
- Never store any reusable payment method credentials (cards/wallets).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    Numeric,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from db.database import IS_SQLITE, JSONB_TYPE, database, metadata
from utils.logger import logger
from utils.transient_errors import db_busy_http_exception


_autoincrement_pk_type = Integer if IS_SQLITE else BigInteger


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

buyer_addresses = Table(
    "buyer_addresses",
    metadata,
    Column("id", String(80), primary_key=True),
    Column("buyer_id", String(50), nullable=False, index=True),
    Column("recipient_name", String(255), nullable=True),
    Column("line1", String(255), nullable=False),
    Column("line2", String(255), nullable=True),
    Column("city", String(120), nullable=False),
    Column("region", String(120), nullable=True),
    Column("postal_code", String(32), nullable=False),
    Column("country", String(2), nullable=False),
    Column("phone", String(32), nullable=True),
    Column("is_default", Boolean, nullable=False, server_default="false"),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), onupdate=func.now()),
)


buyer_agent_links = Table(
    "buyer_agent_links",
    metadata,
    # SQLite requires INTEGER PRIMARY KEY for autoincrement; BigInteger breaks local dev.
    Column("id", _autoincrement_pk_type, primary_key=True, autoincrement=True),
    Column("buyer_id", String(50), nullable=False, index=True),
    Column("agent_id", String(100), nullable=False, index=True),
    Column("agent_scoped_buyer_ref", String(128), nullable=False, unique=True, index=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("last_used_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("buyer_id", "agent_id", name="uq_buyer_agent_links_buyer_agent"),
)


buyer_identity_links = Table(
    "buyer_identity_links",
    metadata,
    Column("id", _autoincrement_pk_type, primary_key=True, autoincrement=True),
    Column("agent_id", String(100), nullable=False, index=True),
    Column("agent_user_ref_hash", String(64), nullable=False, index=True),
    Column("buyer_id", String(50), nullable=False, index=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()),
    Column("last_seen_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("agent_id", "agent_user_ref_hash", name="uq_buyer_identity_links_agent_ref_hash"),
)


mandates = Table(
    "mandates",
    metadata,
    Column("id", String(80), primary_key=True),
    Column("buyer_id", String(50), nullable=False, index=True),
    Column("agent_id", String(100), nullable=False, index=True),
    Column("status", String(20), nullable=False, index=True),  # active | revoked | expired
    Column("constraints_json", JSONB_TYPE, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=True),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
)


authorization_tokens = Table(
    "authorization_tokens",
    metadata,
    Column("id", String(80), primary_key=True),
    Column("mandate_id", String(80), nullable=False, index=True),
    Column("intent_id", String(80), nullable=True, index=True),
    Column("token_hash", String(128), nullable=False, unique=True, index=True),
    Column("scope_json", JSONB_TYPE, nullable=True),
    Column("amount", Numeric(12, 2), nullable=True),
    Column("currency", String(8), nullable=True),
    Column("expires_at", DateTime(timezone=True), nullable=False, index=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("used_at", DateTime(timezone=True), nullable=True),
)


buyer_audit_logs = Table(
    "buyer_audit_logs",
    metadata,
    Column("id", _autoincrement_pk_type, primary_key=True, autoincrement=True),
    Column("buyer_id", String(50), nullable=True, index=True),
    Column("agent_id", String(100), nullable=True, index=True),
    Column("action", String(120), nullable=False, index=True),
    Column("details", JSONB_TYPE, nullable=True),
    Column("ip_address", String(64), nullable=True),
    Column("user_agent", String(512), nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)


buyer_save_challenges = Table(
    "buyer_save_challenges",
    metadata,
    Column("save_token_hash", String(64), primary_key=True),
    Column("intent_id", String(80), nullable=False, index=True),
    Column("order_id", String(80), nullable=True),
    Column("checkout_token_hash", String(64), nullable=True),
    Column("client_nonce_hash", String(64), nullable=False),
    Column("save_email", Boolean, nullable=False, server_default="true"),
    Column("save_address", Boolean, nullable=False, server_default="true"),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False, index=True),
    Column("redeemed_at", DateTime(timezone=True), nullable=True),
    Column("redeemed_buyer_id", String(50), nullable=True),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base64url_no_pad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


#: THE DEDICATED KEY for the agent_user_ref hash (S-29). Every hash written while it is set (and
#: not refused, see `_dedicated_key_problem`) is made with it and nothing else.
#:
#: ROLL FORWARD ONLY. Once this key is mounted and has served a request, never unmount it, and never
#: let a revision without it serve these routes next to one with it. An identity first seen after
#: the mount has rows ONLY under this key's hash; a revision without the key hashes it with the
#: pre-dedicated key instead, so it misses that identity's saved link, purchases and idempotency
#: keys (a retry can open a second purchase) and writes new rows under the other hash. No code can
#: tell a dedicated-key hash from any other hash without the key, so an unmount cannot be detected
#: here: the rollout note carries this rule. Mount it on every service that serves these routes,
#: in one revision.
BUYER_IDENTITY_HASH_SECRET_ENV = "BUYER_IDENTITY_LINK_SECRET"

#: THE LEGACY KEY (S-29 P2-2). A COPY of the value the hash used before the dedicated key existed
#: (the first set token key below, as it was when the copy was made). The legacy read matches old
#: rows with THIS variable and never with a live token key, so the token keys can rotate right
#: after the dedicated key is mounted: the stored legacy hashes then stop being token signatures,
#: and the rows stay readable.
#:
#: Unset while the dedicated key is set and the window is open: the DEDICATED KEY is refused
#: (`_dedicated_key_problem`), an ERROR says so, and buyer-hash resolution FAILS CLOSED (503, see
#: below). Moving buyers onto the dedicated key without a legacy read would split every existing
#: identity, and so would falling back once the token key has rotated. It never falls
#: back to the live token key for the legacy read, which would tie that read to a key that must
#: rotate (and break silently when it does). The copy is READ ONLY by the legacy read, never for a
#: write, so mounting it alone changes no hash; the boot line `legacy_copy_matches_token_key=yes|no`
#: (`report_legacy_copy_matches_token_key`) is how ops confirms the copy before mounting the
#: dedicated key. A WRONG copy cannot be refused by code (after the token key rotates, "differs" is
#: the expected state), so that boot line is the check.
#:
#: FAIL CLOSED without the copy (S-29c). The dedicated key set, the window open and the copy missing:
#: `resolve_agent_user_ref_hash` answers 503 for every buyer and writes nothing. Falling back to
#: the token key is only safe BEFORE the token key rotates; after it (the copy removed one revision
#: before the window closes, or lost on one service), the fallback is the ROTATED key and every
#: buyer, pre- and post-mount, would move onto a new hash. Code cannot tell the two moments apart,
#: so it refuses both: a visible 503 and an ERROR, nothing split. THE ONLY REMEDY for that 503 is
#: to mount the copy again (from its separate secret). Closing the window is NOT a remedy: with it
#: closed `legacy_agent_user_ref_hash` returns None and nothing rehashes the old rows, so every
#: identity first seen before the dedicated key loses its saved link, its purchases and its
#: idempotency keys (a retry can open a second purchase). Closing it is a separate end-of-window
#: decision (RUNBOOK_BUYER_VAULT.md 4.4).
BUYER_IDENTITY_HASH_LEGACY_SECRET_ENV = "BUYER_IDENTITY_LINK_LEGACY_SECRET"

#: The token keys the hash fell back to before it had its own. Each one ALSO SIGNS A TOKEN: the buyer
#: save token (routes.buyer_api._buyer_action_secret) or the checkout token
#: (routes.agent_checkout_intents.mint_checkout_token). Those tokens are
#: HMAC-SHA256(key, payload_b64), and agent_user_ref is a caller-chosen string, so a stored hash made
#: with one of these keys IS a valid token signature for a payload the caller picked. The WRITE key
#: while no usable dedicated key is mounted (today's behaviour, unchanged); otherwise only compared
#: against the dedicated key (an equal value is refused) and against the legacy copy (the boot line).
_TOKEN_SECRET_ENVS = ("BUYER_ACTION_TOKEN_SECRET", "CHECKOUT_TOKEN_SECRET", "AGENT_CHECKOUT_TOKEN_SECRET")

#: The legacy-read window. Open unless set to an off value; it only matters while the dedicated key
#: is set (without it the pre-dedicated key IS the write key).
BUYER_IDENTITY_HASH_LEGACY_READ_ENV = "BUYER_IDENTITY_LINK_LEGACY_READ"

#: Every table keyed on (agent_id, agent_user_ref_hash). Each has an index that leads with both.
_OWNER_KEYED_TABLES = (
    "buyer_identity_links",
    "agent_purchases",
    "reap_agentic_purchases",
    "reap_agentic_purchase_keys",
    "reap_unopened_attempt_retirements",
    "a2a_rail_buyer_refs",
    "a2a_rail_purchases",
)

_MISSING_SECRET_REPORT_INTERVAL_SECONDS = 3600.0
_missing_secret_reported_at: Optional[float] = None
#: The problem the last report was about: a DIFFERENT problem is reported at once, not an hour later.
_reported_problem: Optional[str] = None
#: Misconfigurations already reported at ERROR in this process. Each gets ONE ERROR (at boot or on
#: first use); the hourly repeats are WARNINGs, so a standing misconfiguration is one event, not a
#: page an hour per instance.
_errored_problems: set = set()


class BuyerIdentityHashUnavailable(HTTPException):
    """503 + Retry-After: the lookup that picks the dedicated or the legacy hash could not answer.

    S-29 P2-1. Guessing is never safe. Answering the legacy hash splits an identity first seen after
    the mount (its rows are all under the dedicated hash): it misses its own link, purchases and
    idempotency keys, a retry can open a second purchase, and anything written lands under the
    legacy hash, after which every successful lookup FINDS that row and keeps the identity on the
    legacy hash for good. Answering the dedicated hash does the same to an identity seen before the
    mount. The database is already failing when this fires, and a 503 is retryable and splits
    nothing. An HTTPException, so every route that lets it propagate answers the same 503 the
    codebase uses for a busy database (`utils.transient_errors.db_busy_http_exception`).

    Also raised for a refused key configuration (`legacy_copy_missing`, S-29c): the same code and
    shape, but `message` names the configuration fault, so on-call is not sent to the database.
    """

    def __init__(self, *, retry_after_seconds: int = 1, message: Optional[str] = None) -> None:
        busy = db_busy_http_exception(retry_after_seconds=retry_after_seconds)
        detail = dict(busy.detail)
        if message:
            detail["message"] = message
        super().__init__(status_code=busy.status_code, detail=detail, headers=busy.headers)


#: The 503 message while the copy is missing with the window open (S-29c). It reaches the caller,
#: so it names the fault, not a variable or a value.
BUYER_IDENTITY_HASH_COPY_MISSING_MESSAGE = (
    "Buyer identity hash key configuration refused: the legacy key copy is missing. Please retry shortly."
)


def _env_secret(name: str) -> str:
    """A secret variable's value. Whitespace-only counts as unset."""
    return (os.getenv(name) or "").strip()


def _token_secrets() -> tuple:
    """Every token key that is set, in fallback order."""
    return tuple(value for value in (_env_secret(name) for name in _TOKEN_SECRET_ENVS) if value)


def _legacy_env_secret() -> str:
    return _env_secret(BUYER_IDENTITY_HASH_LEGACY_SECRET_ENV)


def _same_secret(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _dedicated_key_problem() -> Optional[str]:
    """Why a SET dedicated key is refused, else None. Compares values; never logs one.

    Equal to a token key: every hash made with it is still a token signature (the oracle S-29
    closes). Equal to the legacy copy: it is the old key under a new name, so the legacy read
    cannot tell old rows from new and the token-key value lives on as the write key. The copy
    missing while the window is open: every existing buyer would move onto the dedicated hash with
    nothing to match their old rows, a silent split (S-29b N-1). Window closed and no copy is the
    intended end state, not a problem.
    """
    dedicated = _env_secret(BUYER_IDENTITY_HASH_SECRET_ENV)
    if not dedicated:
        return None
    if any(_same_secret(dedicated, token) for token in _token_secrets()):
        return "dedicated_equals_token_key"
    legacy = _legacy_env_secret()
    if legacy and _same_secret(dedicated, legacy):
        return "dedicated_equals_legacy_key"
    if not legacy and legacy_read_window_open():
        return "legacy_copy_missing"
    return None


def _dedicated_hash_secret() -> str:
    """The dedicated key, or "" when it is unset or refused (`_dedicated_key_problem`)."""
    dedicated = _env_secret(BUYER_IDENTITY_HASH_SECRET_ENV)
    if not dedicated or _dedicated_key_problem() is not None:
        return ""
    return dedicated


def _pre_dedicated_hash_secret() -> str:
    """The key a hash is WRITTEN with while there is no usable dedicated key: today's derivation.

    The first token key that is set, else "" (a laptop: unkeyed SHA-256). NEVER the legacy copy:
    a wrong copy mounted ahead of the dedicated key would otherwise change every hash with no
    signal (S-29b N-1). So mounting the copy alone changes nothing, and do not rotate the token
    key before the dedicated key is mounted.
    """
    tokens = _token_secrets()
    return tokens[0] if tokens else ""


def _keyed_hash(secret: str, raw: str) -> str:
    if secret:
        return hmac.new(secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def legacy_read_window_open() -> bool:
    value = (os.getenv(BUYER_IDENTITY_HASH_LEGACY_READ_ENV) or "").strip().lower()
    return value not in {"0", "false", "no", "off", "closed"}


def _hash_key_config_problem() -> Optional[str]:
    """The one thing wrong with the hash keys right now, or None when nothing is."""
    if not _env_secret(BUYER_IDENTITY_HASH_SECRET_ENV):
        return "dedicated_missing"
    return _dedicated_key_problem()


_PROBLEM_MESSAGES = {
    # The expected state between deploying this code and mounting the key: a WARNING, never paged.
    "dedicated_missing": (
        "event=buyer_identity_hash_secret_missing "
        + BUYER_IDENTITY_HASH_SECRET_ENV
        + " is unset: agent_user_ref hashes use the pre-dedicated key (%s). Mount the dedicated secret."
    ),
    "dedicated_equals_token_key": (
        "event=buyer_identity_hash_secret_refused reason=dedicated_equals_token_key "
        + BUYER_IDENTITY_HASH_SECRET_ENV
        + " equals a token-signing key and is IGNORED: hashes stay on the pre-dedicated key (%s). "
        "Mount a fresh random value."
    ),
    "dedicated_equals_legacy_key": (
        "event=buyer_identity_hash_secret_refused reason=dedicated_equals_legacy_key "
        + BUYER_IDENTITY_HASH_SECRET_ENV
        + " equals "
        + BUYER_IDENTITY_HASH_LEGACY_SECRET_ENV
        + " and is IGNORED: hashes stay on the pre-dedicated key (%s). Mount a fresh random value."
    ),
    "legacy_copy_missing": (
        "event=buyer_identity_hash_secret_refused reason=legacy_copy_missing "
        + BUYER_IDENTITY_HASH_SECRET_ENV
        + " is IGNORED because "
        + BUYER_IDENTITY_HASH_LEGACY_SECRET_ENV
        + " is unset while the legacy-read window is open: buyer-hash resolution answers 503 and "
        "writes nothing (%s). The only fix is to mount the legacy copy again. Do not close the window "
        "to clear this: every buyer first seen before the dedicated key would lose their rows."
    ),
}


def report_legacy_copy_matches_token_key() -> Optional[str]:
    """The boot check: does the legacy copy equal today's write key (the first token key set)?

    Logs `event=buyer_identity_hash_legacy_copy_check legacy_copy_matches_token_key=yes|no` and
    returns "yes"/"no"; None (no line) when no copy is mounted. Compares in constant time and logs
    no value. "yes" is required before the dedicated key is mounted; "no" is expected only AFTER the
    token key has rotated. A WARNING so it survives the production log level; it matches no alert.
    """
    legacy = _legacy_env_secret()
    if not legacy:
        return None
    tokens = _token_secrets()
    answer = "yes" if tokens and _same_secret(legacy, tokens[0]) else "no"
    logger.warning(
        "event=buyer_identity_hash_legacy_copy_check legacy_copy_matches_token_key=%s dedicated_mounted=%s",
        answer,
        "yes" if _env_secret(BUYER_IDENTITY_HASH_SECRET_ENV) else "no",
    )
    return answer


def report_missing_buyer_identity_hash_secret(*, force: bool = False) -> bool:
    """One log line, at most hourly, when a deployed host's hash keys are missing or misconfigured.

    Silent on a laptop or in tests. The dedicated key merely being unset (the state between
    deploying this code and mounting the key) is a WARNING. A misconfiguration (a refused dedicated
    key: equal to a token key or to the copy, or set without the copy while the window is open;
    `_dedicated_key_problem`) is ONE ERROR per process in production
    (at boot or on first use) and a WARNING on each hourly repeat, and a WARNING on any other
    deployed host: the codebase's alerts match log text, and Sentry turns every ERROR into an event,
    so an hourly ERROR per instance would be noise that pages. No line carries a key, a ref or a
    hash. Never fail-closed: switching every stored hash at once would cut every buyer off from
    their saved link and purchases on the deploy that did it. Returns True when it logged.
    """
    global _missing_secret_reported_at, _reported_problem
    problem = _hash_key_config_problem()
    if problem is None:
        return False
    now = time.monotonic()
    if (
        not force
        and problem == _reported_problem
        and _missing_secret_reported_at is not None
        and now - _missing_secret_reported_at < _MISSING_SECRET_REPORT_INTERVAL_SECONDS
    ):
        return False
    try:
        from config.platform import is_deployed, is_production

        production = is_production()
        deployed = is_deployed()
    except Exception:  # noqa: BLE001 - a log line must never break a hash
        return False
    if not deployed:
        return False
    _missing_secret_reported_at = now
    _reported_problem = problem
    detail = "a token-signing key" if _pre_dedicated_hash_secret() else "no key, unkeyed sha256"
    first_error = production and problem != "dedicated_missing" and problem not in _errored_problems
    if first_error:
        _errored_problems.add(problem)
        logger.error(_PROBLEM_MESSAGES[problem], detail)
    else:
        logger.warning(_PROBLEM_MESSAGES[problem], detail)
    return True


def hash_agent_user_ref(agent_user_ref: str) -> str:
    """
    The hash a NEW identity is written under: HMAC-SHA256 with BUYER_IDENTITY_LINK_SECRET.

    Without a usable dedicated key it falls back to `_pre_dedicated_hash_secret` (the first token
    key that is set, exactly as before, else a bare SHA-256 for local dev) and reports
    it (`report_missing_buyer_identity_hash_secret`). Routes that look an identity up must use
    `resolve_agent_user_ref_hash`, which also matches rows written under the legacy key.
    """
    raw = str(agent_user_ref or "").strip()
    if not raw:
        return ""
    dedicated = _dedicated_hash_secret()
    if dedicated:
        return _keyed_hash(dedicated, raw)
    report_missing_buyer_identity_hash_secret()
    return _keyed_hash(_pre_dedicated_hash_secret(), raw)


def legacy_agent_user_ref_hash(agent_user_ref: str) -> Optional[str]:
    """The hash this ref had before the dedicated key, while the legacy-read window is open.

    Keyed with BUYER_IDENTITY_LINK_LEGACY_SECRET only, never with a live token key. None when there
    is nothing to match: no usable dedicated key (the pre-dedicated key is then the write key; a
    missing copy with the window open refuses the dedicated key), the window is closed, or the two
    hashes are the same.
    """
    raw = str(agent_user_ref or "").strip()
    if not raw or not legacy_read_window_open():
        return None
    dedicated = _dedicated_hash_secret()
    if not dedicated:
        return None
    legacy_key = _legacy_env_secret()
    if not legacy_key:  # unreachable while the window is open: the dedicated key is then refused
        return None
    legacy = _keyed_hash(legacy_key, raw)
    return None if legacy == _keyed_hash(dedicated, raw) else legacy


async def _owner_rows_exist(agent_id: str, agent_user_ref_hash: str) -> Optional[bool]:
    """True if any owner-keyed table holds a row for (agent_id, hash). One index-backed statement.

    None when the statement returned no answer (no row, or a NULL): the caller must not read that
    as "no rows", which would pick the dedicated hash for an identity that may have legacy rows.
    """
    clauses = " OR ".join(
        f"EXISTS (SELECT 1 FROM {table} WHERE agent_id = :agent_id AND agent_user_ref_hash = :h)"
        for table in _OWNER_KEYED_TABLES
    )
    row = await database.fetch_one(
        f"SELECT ({clauses}) AS found", {"agent_id": agent_id, "h": agent_user_ref_hash}
    )
    if row is None:
        return None
    found = dict(row).get("found")
    if found is None:
        return None
    return bool(found)


async def resolve_agent_user_ref_hash(agent_id: str, agent_user_ref: str) -> str:
    """The hash to read AND write this identity under.

    THE LEGACY READ (S-29). When the dedicated key is set and the window is open, an identity that
    already has rows under its legacy hash keeps that hash, in every owner-keyed table at once, so
    its saved link, its purchases and its idempotency keys all still match after the deploy. An
    identity with no legacy rows gets the dedicated-key hash. So no NEW legacy-key value is ever
    produced for a ref that was not already stored under it, which is what closes the forgery
    oracle; already-stored values stop being token signatures once the token keys rotate (the
    legacy hash is keyed with its own copy, see BUYER_IDENTITY_LINK_LEGACY_SECRET).

    Sticky per identity, never mixed: one identity never has rows under both hashes because of
    this function, and every owner-scoped query below it keeps comparing one value.

    RAISES `BuyerIdentityHashUnavailable` (a 503) when that lookup fails or gives no answer. It
    never guesses either hash: see the exception for why each guess splits an identity.

    Also RAISES it, for every buyer and before any query, while the dedicated key is set, the
    window is open and the copy is missing (`legacy_copy_missing`): see
    BUYER_IDENTITY_LINK_LEGACY_SECRET for why falling back would move every buyer once the token
    key has rotated, and why re-mounting the copy is the only remedy.
    """
    primary = hash_agent_user_ref(agent_user_ref)
    if not primary:
        return ""
    if _dedicated_key_problem() == "legacy_copy_missing":
        raise BuyerIdentityHashUnavailable(retry_after_seconds=30, message=BUYER_IDENTITY_HASH_COPY_MISSING_MESSAGE)
    legacy = legacy_agent_user_ref_hash(agent_user_ref)
    if legacy is None:
        return primary
    try:
        found = await _owner_rows_exist(str(agent_id or ""), legacy)
    except Exception as exc:  # noqa: BLE001
        # The type only: a driver error stringifies its bound parameters, and one is the hash.
        logger.warning("event=buyer_identity_hash_legacy_lookup_failed error=%s", type(exc).__name__)
        raise BuyerIdentityHashUnavailable() from None
    if found is None:
        logger.warning("event=buyer_identity_hash_legacy_lookup_failed error=no_answer")
        raise BuyerIdentityHashUnavailable()
    return legacy if found else primary


def mint_pairwise_buyer_ref() -> str:
    """
    Generate an opaque, non-enumerable buyer_ref suitable for agent-scoped exposure.

    Uses 128 bits of randomness and base64url encoding.
    """
    return _base64url_no_pad(secrets.token_bytes(16))


async def audit_buyer_action(
    *,
    buyer_id: Optional[str],
    action: str,
    agent_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    try:
        await database.execute(
            buyer_audit_logs.insert().values(
                buyer_id=buyer_id,
                agent_id=agent_id,
                action=str(action or "").strip() or "unknown",
                details=details,
                ip_address=ip_address,
                user_agent=(str(user_agent)[:512] if user_agent else None),
            )
        )
    except Exception:
        # Best-effort: never break checkout/user flows on audit failures.
        return
