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


#: THE DEDICATED KEY for the agent_user_ref hash (S-29). Every hash written while it is set is made
#: with it and nothing else.
BUYER_IDENTITY_HASH_SECRET_ENV = "BUYER_IDENTITY_LINK_SECRET"

#: The keys the hash fell back to before it had its own. Each one ALSO SIGNS A TOKEN: the buyer
#: save token (routes.buyer_api._buyer_action_secret) or the checkout token
#: (routes.agent_checkout_intents.mint_checkout_token). Those tokens are
#: HMAC-SHA256(key, payload_b64), and agent_user_ref is a caller-chosen string, so a stored hash made
#: with one of these keys IS a valid token signature for a payload the caller picked. Kept only to
#: match rows written before the dedicated key existed (the legacy read below).
_LEGACY_HASH_SECRET_ENVS = ("BUYER_ACTION_TOKEN_SECRET", "CHECKOUT_TOKEN_SECRET", "AGENT_CHECKOUT_TOKEN_SECRET")

#: The legacy-read window. Open unless set to an off value; it only matters while the dedicated key
#: is set (without it the legacy key IS the write key).
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


def _dedicated_hash_secret() -> str:
    return (os.getenv(BUYER_IDENTITY_HASH_SECRET_ENV) or "").strip()


def _legacy_hash_secret() -> str:
    for name in _LEGACY_HASH_SECRET_ENVS:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def _keyed_hash(secret: str, raw: str) -> str:
    if secret:
        return hmac.new(secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def legacy_read_window_open() -> bool:
    value = (os.getenv(BUYER_IDENTITY_HASH_LEGACY_READ_ENV) or "").strip().lower()
    return value not in {"0", "false", "no", "off", "closed"}


def report_missing_buyer_identity_hash_secret(*, force: bool = False) -> bool:
    """One log line, at most hourly, when a deployed host hashes without the dedicated key.

    ERROR in production, WARNING on any other deployed host, silent on a laptop or in tests. The
    hash itself keeps today's key: failing closed here would change every stored hash at once and
    cut every buyer off from their saved link and purchases on the deploy that did it. Returns
    True when it logged.
    """
    global _missing_secret_reported_at
    if _dedicated_hash_secret():
        return False
    now = time.monotonic()
    if (
        not force
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
    message = (
        "event=buyer_identity_hash_secret_missing %s is unset: agent_user_ref hashes fall back to a "
        "token-signing key (%s). Mount the dedicated secret."
    )
    keyed = "yes" if _legacy_hash_secret() else "no key, unkeyed sha256"
    if production:
        logger.error(message, BUYER_IDENTITY_HASH_SECRET_ENV, keyed)
    else:
        logger.warning(message, BUYER_IDENTITY_HASH_SECRET_ENV, keyed)
    return True


def hash_agent_user_ref(agent_user_ref: str) -> str:
    """
    The hash a NEW identity is written under: HMAC-SHA256 with BUYER_IDENTITY_LINK_SECRET.

    Without that key it falls back, as before, to the first legacy key that is set (see
    `_LEGACY_HASH_SECRET_ENVS`), else a bare SHA-256 for local dev, and reports the missing key
    (`report_missing_buyer_identity_hash_secret`). Routes that look an identity up must use
    `resolve_agent_user_ref_hash`, which also matches rows written under the legacy key.
    """
    raw = str(agent_user_ref or "").strip()
    if not raw:
        return ""
    dedicated = _dedicated_hash_secret()
    if dedicated:
        return _keyed_hash(dedicated, raw)
    report_missing_buyer_identity_hash_secret()
    return _keyed_hash(_legacy_hash_secret(), raw)


def legacy_agent_user_ref_hash(agent_user_ref: str) -> Optional[str]:
    """The hash this ref had before the dedicated key, while the legacy-read window is open.

    None when there is nothing to match: no dedicated key (the legacy key is then the write key),
    the window is closed, or the two hashes are the same.
    """
    raw = str(agent_user_ref or "").strip()
    if not raw or not legacy_read_window_open():
        return None
    dedicated = _dedicated_hash_secret()
    if not dedicated:
        return None
    legacy = _keyed_hash(_legacy_hash_secret(), raw)
    return None if legacy == _keyed_hash(dedicated, raw) else legacy


async def _owner_rows_exist(agent_id: str, agent_user_ref_hash: str) -> bool:
    """True if any owner-keyed table holds a row for (agent_id, hash). One index-backed statement."""
    clauses = " OR ".join(
        f"EXISTS (SELECT 1 FROM {table} WHERE agent_id = :agent_id AND agent_user_ref_hash = :h)"
        for table in _OWNER_KEYED_TABLES
    )
    row = await database.fetch_one(
        f"SELECT ({clauses}) AS found", {"agent_id": agent_id, "h": agent_user_ref_hash}
    )
    if row is None:
        return False
    return bool(dict(row).get("found"))


async def resolve_agent_user_ref_hash(agent_id: str, agent_user_ref: str) -> str:
    """The hash to read AND write this identity under. Never raises.

    THE LEGACY READ (S-29). When the dedicated key is set and the window is open, an identity that
    already has rows under its legacy hash keeps that hash, in every owner-keyed table at once, so
    its saved link, its purchases and its idempotency keys all still match after the deploy. An
    identity with no legacy rows gets the dedicated-key hash. So no NEW legacy-key value is ever
    produced for a ref that was not already stored under it, which is what closes the forgery
    oracle; already-stored values were exposed before this change either way.

    Sticky per identity, never mixed: one identity never has rows under both hashes because of
    this function, and every owner-scoped query below it keeps comparing one value.

    A failed lookup answers the legacy hash, today's behaviour: a transient DB error must not cut a
    buyer off from their rows, and on the purchase routes a missed idempotency key could open a
    second purchase. The cost is that a ref first seen during such a failure may be stored under its
    legacy hash; nobody outside can choose when the lookup fails.
    """
    primary = hash_agent_user_ref(agent_user_ref)
    if not primary:
        return ""
    legacy = legacy_agent_user_ref_hash(agent_user_ref)
    if legacy is None:
        return primary
    try:
        found = await _owner_rows_exist(str(agent_id or ""), legacy)
    except Exception as exc:  # noqa: BLE001
        # The type only: a driver error stringifies its bound parameters, and one is the hash.
        logger.warning("event=buyer_identity_hash_legacy_lookup_failed error=%s", type(exc).__name__)
        return legacy
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
