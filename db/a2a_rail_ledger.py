"""Persistence for the A2A token rail (migration 267): buyer refs + purchases.

The second payment rail behind the rail-neutral ledger (db/agent_purchase_ledger.py, executor
`rail_managed`: the rail's own checkout agent places the merchant order, as Reap's does). The
transport is services/payment_orchestration/a2a_rail.py. This module is the WHOLE data layer for the
rail: no client, no route, no worker. Those call the shapes exported here.

  a2a_rail_buyer_refs  - one opaque member id per (agent, end user), minted here and never derived
                         from the user: a derived id is enumerable, and rotating its key would
                         orphan every card the buyer bound on the rail.
  a2a_rail_purchases   - one row per purchase attempt, a state machine in a column.

── THE STATE MACHINE ────────────────────────────────────────────────────────────────────────

  opened -> needs_payment_method -> quoting -> awaiting_authorization -> paying -> completed
     opened                  the row exists; nothing has been sent to the rail
     needs_payment_method    the buyer must bind a card on the rail's hosted page
     quoting                 the rail prices the exact cart at the merchant; nothing is charged
     awaiting_authorization  the buyer must approve this purchase on the rail's page
     paying                  the rail's checkout agent is placing the order with the buyer's token
  terminal: completed | failed | refused | expired

`paying` is the only state in which money may be moving, so it is the only state no sweep may
expire: it ends only on the rail's own answer (completed / failed), read by the worker.

── WRITE-AHEAD, BECAUSE THE RAIL CANNOT DEDUPLICATE (YET) ────────────────────────────────────

The rail mints its own idempotency key per call, so a write resent after a timeout is a NEW write.
Before any call that may change state on the rail, the worker records `pending_write` (the action)
under its claim; it clears it only on a definite answer. A row found with `pending_write` set had a
write whose outcome nobody saw: the next worker must READ the rail's state before it may send any
other write. `mark_pending_write` / `clear_pending_write` are fenced on the claim like every worker
write here.

── PROPERTIES OF THIS DRIVER (see db/reap_agentic_ledger.py, the same six apply) ────────────────

1. An UPDATE's `execute()` has no rowcount on Postgres: every conditional write is
   `fetch_one(... RETURNING ...)`, and "did it change?" is "did a row come back?".
2. A raw jsonb column reads back as text: `_purchase` decodes it.
3. `CAST(:x AS JSONB)` stores 0 on SQLite: dialect splits.
4. Every cutoff is computed server-side; no naive datetime is bound.
5. Every statement is a module-level literal and dialect splits are `if`/`else` statements, so the
   PREPARE gate (tests/test_repo_sql_prepare_postgres.py) plans each one. State lists in SQL are the
   source of truth; the Python tuples are parsed out of them (`_states_in`).
6. clock_timestamp() on Postgres (statement time), never CURRENT_TIMESTAMP inside a worker loop.

No PII leaves this module except through `get_purchase_internal` (the worker's read). The owner
read returns the full row to the caller that proved ownership; the agent-facing projection is
services/payment_orchestration/a2a_purchase_view.py, which never includes the email, the address,
the member id, the rail's ids or a URL it has not vetted. Every terminal write NULLs the email, the
address, the buyer page and the pending write, and clears the claim, in the same statement.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence

from db.database import IS_POSTGRES, database
from db.reap_agentic_ledger import _bind_dt, _decode_dt, _decode_json, _states_in, require_consent_version

RAIL = "a2a"
ID_PREFIX = "ap_"

# ── the state machine ────────────────────────────────────────────────────────────────────────

TERMINAL_STATES: FrozenSet[str] = frozenset({"completed", "failed", "refused", "expired"})

#: THE ONE MAP. `transition` refuses an illegal pair before any SQL runs. A terminal state maps to
#: the empty set: "exists and permits nothing", not "unknown".
ALLOWED_TRANSITIONS: Dict[str, FrozenSet[str]] = {
    "opened": frozenset({"needs_payment_method", "quoting", "refused", "failed"}),
    "needs_payment_method": frozenset({"quoting", "expired", "failed"}),
    "quoting": frozenset({"awaiting_authorization", "refused", "failed", "expired"}),
    "awaiting_authorization": frozenset({"paying", "refused", "failed", "expired"}),
    # Money may be moving: only the rail's own answer ends it. No expiry, no refusal after the fact.
    "paying": frozenset({"completed", "failed"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "refused": frozenset(),
    "expired": frozenset(),
}
PURCHASE_STATES: FrozenSet[str] = frozenset(ALLOWED_TRANSITIONS)

#: What the buyer is asked to do on the rail's page, by state.
BUYER_ACTION_KINDS = ("card_binding", "approval", "authenticate")

_ID_RE = re.compile(r"^ap_[0-9a-f]{24}\Z")
_MEMBER_RE = re.compile(r"^pvm_[0-9a-f]{24}\Z")
_CODE_RE = re.compile(r"^[a-z0-9_:.-]{1,64}\Z")
_ACTION_RE = re.compile(r"^[a-z0-9_.:#-]{1,64}\Z")
_RAIL_REF_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}\Z")
_WORKER_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}\Z")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}\Z")
_DOMAIN_RE = re.compile(r"^(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\Z")


def new_purchase_id() -> str:
    return f"{ID_PREFIX}{uuid.uuid4().hex[:24]}"


def new_member_id() -> str:
    return f"pvm_{uuid.uuid4().hex[:24]}"


# ── schema ───────────────────────────────────────────────────────────────────────────────────
#
# db/migrations/267_a2a_rail_ledger.sql is the same DDL; db/schema_guard.ensure_required_schema_light
# calls `ensure_a2a_rail_schema` on both dialects (a fast-mode boot skips db/migrations/), and
# tests/test_a2a_rail_ledger_postgres.py proves both build the same schema. New, empty tables: they
# lock nothing that exists.

_CREATE_BUYER_REFS_PG = """
CREATE TABLE IF NOT EXISTS a2a_rail_buyer_refs (
    member_id VARCHAR(40) PRIMARY KEY,
    agent_id VARCHAR(128) NOT NULL,
    agent_user_ref_hash VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
_CREATE_BUYER_REFS_SQLITE = _CREATE_BUYER_REFS_PG.replace("TIMESTAMPTZ", "TIMESTAMP").replace(
    "now()", "CURRENT_TIMESTAMP"
)
_CREATE_BUYER_REFS_OWNER_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_a2a_rail_buyer_refs_owner
    ON a2a_rail_buyer_refs (agent_id, agent_user_ref_hash)
"""

_CREATE_PURCHASES_PG = """
CREATE TABLE IF NOT EXISTS a2a_rail_purchases (
    id VARCHAR(40) PRIMARY KEY,
    member_id VARCHAR(40) NOT NULL,
    agent_id VARCHAR(128) NOT NULL,
    agent_user_ref_hash VARCHAR(64) NOT NULL,
    state VARCHAR(32) NOT NULL DEFAULT 'opened' CONSTRAINT ck_a2a_rail_purchases_state CHECK (state IN (
        'opened', 'needs_payment_method', 'quoting', 'awaiting_authorization', 'paying',
        'completed', 'failed', 'refused', 'expired')),
    merchant_domain VARCHAR(253) NOT NULL,
    checkout_url TEXT NOT NULL,
    product_key VARCHAR(255),
    product_name TEXT,
    variant_title TEXT,
    quantity INTEGER NOT NULL DEFAULT 1 CONSTRAINT ck_a2a_rail_purchases_quantity CHECK (quantity > 0),
    currency VARCHAR(3) NOT NULL,
    our_price_minor BIGINT NOT NULL CONSTRAINT ck_a2a_rail_purchases_price CHECK (our_price_minor > 0),
    consent_version VARCHAR(32),
    consented_at TIMESTAMPTZ,
    payment_method_id VARCHAR(128),
    payment_token_id VARCHAR(128),
    quote_id VARCHAR(128),
    order_id VARCHAR(128),
    quoted_total_minor BIGINT,
    final_total_minor BIGINT,
    shipping_minor BIGINT,
    tax_minor BIGINT,
    buyer_action_kind VARCHAR(24) CONSTRAINT ck_a2a_rail_purchases_action CHECK (
        buyer_action_kind IS NULL OR buyer_action_kind IN ('card_binding', 'approval', 'authenticate')),
    buyer_action_url TEXT,
    buyer_action_expires_at TIMESTAMPTZ,
    merchant_order_id VARCHAR(128),
    merchant_order_status_url TEXT,
    refusal_reason VARCHAR(64),
    last_error_code VARCHAR(64),
    pending_write VARCHAR(64),
    pending_write_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_poll_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_by VARCHAR(64),
    claimed_at TIMESTAMPTZ,
    buyer_email TEXT,
    shipping_address JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    state_entered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    terminal_at TIMESTAMPTZ
)
"""
_CREATE_PURCHASES_SQLITE = (
    _CREATE_PURCHASES_PG.replace("TIMESTAMPTZ", "TIMESTAMP")
    .replace("JSONB", "TEXT")
    .replace("now()", "CURRENT_TIMESTAMP")
)
_CREATE_PURCHASES_OWNER_INDEX = """
CREATE INDEX IF NOT EXISTS idx_a2a_rail_purchases_owner
    ON a2a_rail_purchases (agent_id, agent_user_ref_hash, created_at)
"""
_CREATE_PURCHASES_DUE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_a2a_rail_purchases_due
    ON a2a_rail_purchases (state, next_poll_at)
"""


async def ensure_a2a_rail_schema() -> None:
    """Self-heal parity with migration 267. Idempotent; called at every startup."""
    if IS_POSTGRES:
        await database.execute(_CREATE_BUYER_REFS_PG)
        await database.execute(_CREATE_PURCHASES_PG)
    else:
        await database.execute(_CREATE_BUYER_REFS_SQLITE)
        await database.execute(_CREATE_PURCHASES_SQLITE)
    await database.execute(_CREATE_BUYER_REFS_OWNER_INDEX)
    await database.execute(_CREATE_PURCHASES_OWNER_INDEX)
    await database.execute(_CREATE_PURCHASES_DUE_INDEX)


# ── row decoding ─────────────────────────────────────────────────────────────────────────────

_TS_COLUMNS = (
    "consented_at", "buyer_action_expires_at", "pending_write_at", "next_poll_at", "claimed_at",
    "created_at", "updated_at", "state_entered_at", "terminal_at",
)


def _purchase(row: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    out = dict(row)
    out["shipping_address"] = _decode_json(out.get("shipping_address"))
    for column in _TS_COLUMNS:
        if column in out:
            out[column] = _decode_dt(out[column])
    return out


# ── validation ───────────────────────────────────────────────────────────────────────────────


def _require(pattern: "re.Pattern[str]", value: Any, name: str) -> str:
    text = str(value or "")
    if not pattern.match(text):
        raise ValueError(f"{name} is not valid")
    return text


def _optional(pattern: "re.Pattern[str]", value: Any, name: str) -> Optional[str]:
    return None if value is None else _require(pattern, value, name)


def _require_https_url(value: Any, name: str, *, required: bool) -> Optional[str]:
    if value is None and not required:
        return None
    text = str(value or "").strip()
    if not text.startswith("https://") or "@" in text.split("/", 3)[2] or len(text) > 2048:
        raise ValueError(f"{name} must be an https URL without credentials")
    return text


def _require_minor(value: Any, name: str, *, positive: bool = False) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer number of minor units")
    if value < (1 if positive else 0):
        raise ValueError(f"{name} is out of range")
    return value


# ── buyer refs ───────────────────────────────────────────────────────────────────────────────

_INSERT_BUYER_REF_SQL = """
    INSERT INTO a2a_rail_buyer_refs (member_id, agent_id, agent_user_ref_hash)
    VALUES (:member_id, :agent_id, :agent_user_ref_hash)
    ON CONFLICT (agent_id, agent_user_ref_hash) DO NOTHING
"""
_SELECT_BUYER_REF_SQL = """
    SELECT member_id FROM a2a_rail_buyer_refs
     WHERE agent_id = :agent_id AND agent_user_ref_hash = :agent_user_ref_hash
"""


async def member_id_for(agent_id: str, agent_user_ref_hash: str) -> str:
    """The rail member id for this (agent, end user), minted on first use. Race-safe: the insert is
    ON CONFLICT DO NOTHING on the owner index and the id is read back, so concurrent callers agree."""
    agent_id = str(agent_id or "").strip()
    owner = str(agent_user_ref_hash or "").strip()
    if not agent_id or not owner:
        raise ValueError("agent_id and agent_user_ref_hash are required")
    params = {"agent_id": agent_id, "agent_user_ref_hash": owner}
    await database.execute(_INSERT_BUYER_REF_SQL, {**params, "member_id": new_member_id()})
    row = await database.fetch_one(_SELECT_BUYER_REF_SQL, params)
    if row is None:  # pragma: no cover - the insert above or a concurrent one made it
        raise RuntimeError("a2a_rail: buyer ref vanished after insert")
    return str(row["member_id"])


# ── purchases: create + read ─────────────────────────────────────────────────────────────────

_INSERT_PURCHASE_SQL = """
    INSERT INTO a2a_rail_purchases (
        id, member_id, agent_id, agent_user_ref_hash, state, merchant_domain, checkout_url,
        product_key, product_name, variant_title, quantity, currency, our_price_minor,
        consent_version, consented_at, buyer_email, shipping_address
    ) VALUES (
        :id, :member_id, :agent_id, :agent_user_ref_hash, 'opened', :merchant_domain, :checkout_url,
        :product_key, :product_name, :variant_title, :quantity, :currency, :our_price_minor,
        :consent_version, CASE WHEN CAST(:consent_version_probe AS TEXT) IS NULL THEN NULL ELSE clock_timestamp() END,
        :buyer_email, CAST(:shipping_address AS JSONB)
    )
    RETURNING *
"""
_INSERT_PURCHASE_SQL_SQLITE = """
    INSERT INTO a2a_rail_purchases (
        id, member_id, agent_id, agent_user_ref_hash, state, merchant_domain, checkout_url,
        product_key, product_name, variant_title, quantity, currency, our_price_minor,
        consent_version, consented_at, buyer_email, shipping_address
    ) VALUES (
        :id, :member_id, :agent_id, :agent_user_ref_hash, 'opened', :merchant_domain, :checkout_url,
        :product_key, :product_name, :variant_title, :quantity, :currency, :our_price_minor,
        :consent_version, CASE WHEN CAST(:consent_version_probe AS TEXT) IS NULL THEN NULL ELSE CURRENT_TIMESTAMP END,
        :buyer_email, :shipping_address
    )
    RETURNING *
"""


async def create_purchase(
    *,
    agent_id: str,
    agent_user_ref_hash: str,
    merchant_domain: str,
    checkout_url: str,
    currency: str,
    our_price_minor: int,
    quantity: int = 1,
    product_key: Optional[str] = None,
    product_name: Optional[str] = None,
    variant_title: Optional[str] = None,
    consent_version: Optional[str] = None,
    buyer_email: Optional[str] = None,
    shipping_address: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Open a purchase in `opened`. Validates everything it stores; the checkout URL must be https
    on exactly `merchant_domain` (the rail checks it out as received, byte for byte)."""
    domain = _require(_DOMAIN_RE, str(merchant_domain or "").strip().lower(), "merchant_domain")
    url = _require_https_url(checkout_url, "checkout_url", required=True)
    host = url.split("/", 3)[2].split(":", 1)[0].lower()
    if host != domain:
        raise ValueError("checkout_url must be on merchant_domain")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 99:
        raise ValueError("quantity is out of range")
    consent = require_consent_version(consent_version)
    if shipping_address is not None and not isinstance(shipping_address, Mapping):
        raise ValueError("shipping_address must be an object")
    member_id = await member_id_for(agent_id, agent_user_ref_hash)
    values = {
        "id": new_purchase_id(),
        "member_id": member_id,
        "agent_id": str(agent_id).strip(),
        "agent_user_ref_hash": str(agent_user_ref_hash).strip(),
        "merchant_domain": domain,
        "checkout_url": url,
        "product_key": product_key,
        "product_name": product_name,
        "variant_title": variant_title,
        "quantity": quantity,
        "currency": _require(_CURRENCY_RE, currency, "currency"),
        "our_price_minor": _require_minor(our_price_minor, "our_price_minor", positive=True),
        "consent_version": consent,
        "consent_version_probe": consent,
        "buyer_email": (str(buyer_email).strip() or None) if buyer_email is not None else None,
        "shipping_address": json.dumps(dict(shipping_address), sort_keys=True) if shipping_address else None,
    }
    if IS_POSTGRES:
        row = await database.fetch_one(_INSERT_PURCHASE_SQL, values)
    else:
        row = await database.fetch_one(_INSERT_PURCHASE_SQL_SQLITE, values)
    out = _purchase(row)
    assert out is not None
    return out


_SELECT_PURCHASE_SQL = """
    SELECT * FROM a2a_rail_purchases WHERE id = :id
"""
_SELECT_PURCHASE_FOR_OWNER_SQL = """
    SELECT * FROM a2a_rail_purchases
     WHERE id = :id AND agent_id = :agent_id AND agent_user_ref_hash = :agent_user_ref_hash
"""


async def get_purchase_internal(purchase_id: str) -> Optional[Dict[str, Any]]:
    """The UNSCOPED, UNREDACTED read, for the worker only (it carries the buyer's PII and the rail's
    ids). Never hand its result to an agent; see a2a_purchase_view for the agent-facing shape."""
    if not _ID_RE.match(str(purchase_id or "")):
        return None
    return _purchase(await database.fetch_one(_SELECT_PURCHASE_SQL, {"id": purchase_id}))


async def get_purchase_for_owner(
    purchase_id: str, agent_id: str, agent_user_ref_hash: str
) -> Optional[Dict[str, Any]]:
    """Both owner conjuncts in the SQL: "not yours" and "does not exist" are the same None."""
    if not _ID_RE.match(str(purchase_id or "")):
        return None
    return _purchase(
        await database.fetch_one(
            _SELECT_PURCHASE_FOR_OWNER_SQL,
            {"id": purchase_id, "agent_id": agent_id, "agent_user_ref_hash": agent_user_ref_hash},
        )
    )


# ── purchases: the fenced transition ─────────────────────────────────────────────────────────

#: Columns a transition may write, COALESCEd (None = leave alone). An allowlist, not **fields in SQL.
_TRANSITION_FIELDS: Sequence[str] = (
    "payment_method_id", "payment_token_id", "quote_id", "order_id",
    "quoted_total_minor", "final_total_minor", "shipping_minor", "tax_minor",
    "buyer_action_kind", "buyer_action_url", "buyer_action_expires_at",
    "merchant_order_id", "merchant_order_status_url", "refusal_reason", "last_error_code",
    "next_poll_at",
)
_FROM_SLOTS = 9

# A transition is one statement. The buyer page is replaced or cleared on EVERY transition (a page
# belongs to the state that issued it, so a new state never inherits the old state's link), and
# every terminal write NULLs the PII, the page, the pending write and the claim.
_TRANSITION_SQL = """
    UPDATE a2a_rail_purchases
       SET state = :to_state,
           payment_method_id = COALESCE(:payment_method_id, payment_method_id),
           payment_token_id = COALESCE(:payment_token_id, payment_token_id),
           quote_id = COALESCE(:quote_id, quote_id),
           order_id = COALESCE(:order_id, order_id),
           quoted_total_minor = COALESCE(:quoted_total_minor, quoted_total_minor),
           final_total_minor = COALESCE(:final_total_minor, final_total_minor),
           shipping_minor = COALESCE(:shipping_minor, shipping_minor),
           tax_minor = COALESCE(:tax_minor, tax_minor),
           buyer_action_kind = CASE WHEN :to_state_probe IN ('completed', 'failed', 'refused', 'expired')
                THEN NULL ELSE CAST(:buyer_action_kind AS TEXT) END,
           buyer_action_url = CASE WHEN :to_state_probe IN ('completed', 'failed', 'refused', 'expired')
                THEN NULL ELSE CAST(:buyer_action_url AS TEXT) END,
           buyer_action_expires_at = CASE WHEN :to_state_probe IN ('completed', 'failed', 'refused', 'expired')
                THEN NULL ELSE CAST(:buyer_action_expires_at AS TIMESTAMPTZ) END,
           merchant_order_id = COALESCE(:merchant_order_id, merchant_order_id),
           merchant_order_status_url = COALESCE(:merchant_order_status_url, merchant_order_status_url),
           refusal_reason = COALESCE(:refusal_reason, refusal_reason),
           last_error_code = CASE WHEN :to_state_probe IN ('completed', 'failed', 'refused', 'expired')
                THEN :last_error_code ELSE COALESCE(:last_error_code, last_error_code) END,
           next_poll_at = COALESCE(:next_poll_at, clock_timestamp()),
           pending_write = NULL,
           pending_write_at = NULL,
           buyer_email = CASE WHEN :to_state_probe IN ('completed', 'failed', 'refused', 'expired')
                THEN NULL ELSE buyer_email END,
           shipping_address = CASE WHEN :to_state_probe IN ('completed', 'failed', 'refused', 'expired')
                THEN NULL ELSE shipping_address END,
           claimed_by = CASE WHEN :to_state_probe IN ('completed', 'failed', 'refused', 'expired')
                THEN NULL ELSE claimed_by END,
           claimed_at = CASE WHEN :to_state_probe IN ('completed', 'failed', 'refused', 'expired')
                THEN NULL ELSE claimed_at END,
           terminal_at = CASE WHEN :to_state_probe IN ('completed', 'failed', 'refused', 'expired')
                THEN clock_timestamp() ELSE terminal_at END,
           state_entered_at = clock_timestamp(),
           updated_at = clock_timestamp()
     WHERE id = :id
       AND state IN (:from_0, :from_1, :from_2, :from_3, :from_4, :from_5, :from_6, :from_7, :from_8)
       AND claimed_by = :holder
    RETURNING *
"""
# SQLite must not see CAST(... AS TIMESTAMPTZ): an unknown type name gets NUMERIC affinity and stores
# a number (driver property 3). The text casts are harmless there.
_TRANSITION_SQL_SQLITE = _TRANSITION_SQL.replace("clock_timestamp()", "CURRENT_TIMESTAMP").replace(
    "CAST(:buyer_action_expires_at AS TIMESTAMPTZ)", ":buyer_action_expires_at"
)


async def transition_as_holder(
    purchase_id: str,
    worker_id: str,
    *,
    from_states: Iterable[str],
    to_state: str,
    **fields: Any,
) -> Optional[Dict[str, Any]]:
    """Advance a purchase the worker holds, atomically. Returns the new row, or None when the row
    is not in `from_states` or the claim has moved on: a lost race, never an error. Re-read, do not
    retry the same write.

    Illegal pairs, unknown fields and malformed values raise before any SQL. The buyer page fields
    are replaced on every transition: pass them for a state that asks the buyer to act, omit them
    to clear the previous state's page.
    """
    _require(_WORKER_RE, worker_id, "worker_id")
    sources = list(dict.fromkeys(from_states))
    if not sources or not 1 <= len(sources) <= _FROM_SLOTS:
        raise ValueError("from_states must name 1..9 states")
    for state in sources:
        if to_state not in ALLOWED_TRANSITIONS.get(state, frozenset()):
            raise ValueError(f"illegal transition {state!r} -> {to_state!r}")
    unknown = set(fields) - set(_TRANSITION_FIELDS)
    if unknown:
        raise TypeError(f"unknown transition fields: {sorted(unknown)}")
    values: Dict[str, Any] = {name: None for name in _TRANSITION_FIELDS}
    values.update(fields)
    for name in ("payment_method_id", "payment_token_id", "quote_id", "order_id", "merchant_order_id"):
        values[name] = _optional(_RAIL_REF_RE, values[name], name)
    for name in ("quoted_total_minor", "final_total_minor", "shipping_minor", "tax_minor"):
        values[name] = _require_minor(values[name], name)
    for name in ("refusal_reason", "last_error_code"):
        values[name] = _optional(_CODE_RE, values[name], name)
    kind = values["buyer_action_kind"]
    if kind is not None and kind not in BUYER_ACTION_KINDS:
        raise ValueError("buyer_action_kind is not valid")
    values["buyer_action_url"] = _require_https_url(values["buyer_action_url"], "buyer_action_url", required=False)
    if (kind is None) != (values["buyer_action_url"] is None):
        raise ValueError("buyer_action_kind and buyer_action_url go together")
    values["merchant_order_status_url"] = _require_https_url(
        values["merchant_order_status_url"], "merchant_order_status_url", required=False
    )
    values["buyer_action_expires_at"] = _bind_dt(values["buyer_action_expires_at"])
    values["next_poll_at"] = _bind_dt(values["next_poll_at"])
    padded = sources + [sources[0]] * (_FROM_SLOTS - len(sources))
    values.update({f"from_{i}": state for i, state in enumerate(padded)})
    values.update({"id": purchase_id, "to_state": to_state, "to_state_probe": to_state, "holder": worker_id})
    if IS_POSTGRES:
        return _purchase(await database.fetch_one(_TRANSITION_SQL, values))
    return _purchase(await database.fetch_one(_TRANSITION_SQL_SQLITE, values))


# ── write-ahead ──────────────────────────────────────────────────────────────────────────────

_MARK_PENDING_WRITE_SQL = """
    UPDATE a2a_rail_purchases
       SET pending_write = :action, pending_write_at = clock_timestamp(), updated_at = clock_timestamp()
     WHERE id = :id AND claimed_by = :holder AND pending_write IS NULL
       AND state IN ('opened', 'needs_payment_method', 'quoting', 'awaiting_authorization', 'paying')
    RETURNING *
"""
_MARK_PENDING_WRITE_SQL_SQLITE = _MARK_PENDING_WRITE_SQL.replace("clock_timestamp()", "CURRENT_TIMESTAMP")

_CLEAR_PENDING_WRITE_SQL = """
    UPDATE a2a_rail_purchases
       SET pending_write = NULL, pending_write_at = NULL, updated_at = clock_timestamp()
     WHERE id = :id AND claimed_by = :holder AND pending_write = :action
    RETURNING *
"""
_CLEAR_PENDING_WRITE_SQL_SQLITE = _CLEAR_PENDING_WRITE_SQL.replace("clock_timestamp()", "CURRENT_TIMESTAMP")


async def mark_pending_write(purchase_id: str, worker_id: str, action: str) -> Optional[Dict[str, Any]]:
    """Record, BEFORE sending it, a write whose outcome must be seen. None when the worker does not
    hold the row, the row is terminal, or another write is already pending: in every one of those
    cases the write must NOT be sent."""
    values = {
        "id": purchase_id,
        "holder": _require(_WORKER_RE, worker_id, "worker_id"),
        "action": _require(_ACTION_RE, action, "action"),
    }
    if IS_POSTGRES:
        return _purchase(await database.fetch_one(_MARK_PENDING_WRITE_SQL, values))
    return _purchase(await database.fetch_one(_MARK_PENDING_WRITE_SQL_SQLITE, values))


async def clear_pending_write(purchase_id: str, worker_id: str, action: str) -> Optional[Dict[str, Any]]:
    """The write's outcome is known (from its answer, or from a read of the rail's state)."""
    values = {
        "id": purchase_id,
        "holder": _require(_WORKER_RE, worker_id, "worker_id"),
        "action": _require(_ACTION_RE, action, "action"),
    }
    if IS_POSTGRES:
        return _purchase(await database.fetch_one(_CLEAR_PENDING_WRITE_SQL, values))
    return _purchase(await database.fetch_one(_CLEAR_PENDING_WRITE_SQL_SQLITE, values))


# ── the worker's claim pair ──────────────────────────────────────────────────────────────────
#
# House style (db/reap_agentic_ledger): an advisory SELECT of due candidates, then ONE conditional
# UPDATE per candidate that re-checks the state, the lease and the due time. Waiting on the buyer is
# not a failed attempt, so `attempts` does not move in the two buyer states.

_SELECT_DUE_SQL = """
    SELECT id FROM a2a_rail_purchases
     WHERE state IN ('opened', 'needs_payment_method', 'quoting', 'awaiting_authorization', 'paying')
       AND claimed_by IS NULL
       AND next_poll_at <= clock_timestamp()
     ORDER BY next_poll_at, id
     LIMIT :limit
"""
_SELECT_DUE_SQL_SQLITE = _SELECT_DUE_SQL.replace("clock_timestamp()", "CURRENT_TIMESTAMP")

_CLAIM_SQL = """
    UPDATE a2a_rail_purchases
       SET claimed_by = :worker_id,
           claimed_at = clock_timestamp(),
           attempts = CASE WHEN state IN ('needs_payment_method', 'awaiting_authorization')
                THEN attempts ELSE attempts + 1 END,
           updated_at = clock_timestamp()
     WHERE id = :id
       AND claimed_by IS NULL
       AND next_poll_at <= clock_timestamp()
       AND state IN ('opened', 'needs_payment_method', 'quoting', 'awaiting_authorization', 'paying')
    RETURNING *
"""
_CLAIM_SQL_SQLITE = _CLAIM_SQL.replace("clock_timestamp()", "CURRENT_TIMESTAMP")

#: Releases the lease; `state_entered_at` is NOT touched (the expiry sweep's clock).
_RELEASE_SQL = """
    UPDATE a2a_rail_purchases
       SET claimed_by = NULL,
           claimed_at = NULL,
           next_poll_at = COALESCE(:next_poll_at, next_poll_at),
           last_error_code = COALESCE(:last_error_code, last_error_code),
           updated_at = clock_timestamp()
     WHERE id = :id AND claimed_by = :worker_id
    RETURNING *
"""
_RELEASE_SQL_SQLITE = _RELEASE_SQL.replace("clock_timestamp()", "CURRENT_TIMESTAMP")

_REQUEUE_STALE_SQL = """
    UPDATE a2a_rail_purchases
       SET claimed_by = NULL, claimed_at = NULL, updated_at = clock_timestamp()
     WHERE id IN (
            SELECT id FROM a2a_rail_purchases
             WHERE claimed_by IS NOT NULL
               AND claimed_at < clock_timestamp() - (:lease_seconds * INTERVAL '1 second')
             ORDER BY claimed_at
             LIMIT :limit
           )
    RETURNING id
"""
_REQUEUE_STALE_SQL_SQLITE = """
    UPDATE a2a_rail_purchases
       SET claimed_by = NULL, claimed_at = NULL, updated_at = CURRENT_TIMESTAMP
     WHERE id IN (
            SELECT id FROM a2a_rail_purchases
             WHERE claimed_by IS NOT NULL
               AND claimed_at < datetime('now', '-' || :lease_seconds || ' seconds')
             ORDER BY claimed_at
             LIMIT :limit
           )
    RETURNING id
"""

POLLABLE_STATES = _states_in(_CLAIM_SQL, "AND state IN (")
CLAIM_ATTEMPT_EXEMPT_STATES = _states_in(_CLAIM_SQL, "attempts = CASE WHEN state IN (")


def _limit(value: Any, maximum: int) -> int:
    return max(1, min(maximum, int(value)))


async def claim_due_purchases(worker_id: str, *, limit: int = 10) -> List[Dict[str, Any]]:
    """Lease up to `limit` purchases whose next poll has come."""
    _require(_WORKER_RE, worker_id, "worker_id")
    params = {"limit": _limit(limit, 100)}
    if IS_POSTGRES:
        candidates = await database.fetch_all(_SELECT_DUE_SQL, params)
    else:
        candidates = await database.fetch_all(_SELECT_DUE_SQL_SQLITE, params)
    claimed: List[Dict[str, Any]] = []
    for candidate in candidates:
        values = {"id": candidate["id"], "worker_id": worker_id}
        if IS_POSTGRES:
            row = await database.fetch_one(_CLAIM_SQL, values)
        else:
            row = await database.fetch_one(_CLAIM_SQL_SQLITE, values)
        got = _purchase(row)
        if got is not None:
            claimed.append(got)
    return claimed


async def release_claim(
    purchase_id: str, worker_id: str, *, next_poll_at: Any = None, last_error_code: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Give the lease back, scheduling the next poll and recording why. None when not held."""
    values = {
        "id": purchase_id,
        "worker_id": _require(_WORKER_RE, worker_id, "worker_id"),
        "next_poll_at": _bind_dt(next_poll_at),
        "last_error_code": _optional(_CODE_RE, last_error_code, "last_error_code"),
    }
    if IS_POSTGRES:
        return _purchase(await database.fetch_one(_RELEASE_SQL, values))
    return _purchase(await database.fetch_one(_RELEASE_SQL_SQLITE, values))


async def requeue_stale_claims(*, lease_seconds: int = 300, limit: int = 50) -> int:
    """Free leases held past `lease_seconds` (a worker that died mid-step). Its pending write, if
    any, stays: the next holder must read the rail before it writes."""
    values = {"lease_seconds": _limit(lease_seconds, 86400), "limit": _limit(limit, 500)}
    if IS_POSTGRES:
        rows = await database.fetch_all(_REQUEUE_STALE_SQL, values)
    else:
        rows = await database.fetch_all(_REQUEUE_STALE_SQL_SQLITE, values)
    return len(rows)


# ── the expiry sweep ─────────────────────────────────────────────────────────────────────────
#
# Unfenced and in bulk, like Reap's: it may end a purchase a worker holds, and clears the claim when
# it does (the worker learns it from a None transition). NEVER 'paying' (money may be moving), and
# never a row with a pending write (a write whose outcome nobody has seen yet).
_EXPIRE_SQL = """
    UPDATE a2a_rail_purchases
       SET state = 'expired',
           last_error_code = CASE WHEN buyer_action_expires_at IS NOT NULL
                AND buyer_action_expires_at < clock_timestamp() THEN 'buyer_action_expired' ELSE 'stale' END,
           buyer_action_kind = NULL, buyer_action_url = NULL, buyer_action_expires_at = NULL,
           buyer_email = NULL, shipping_address = NULL,
           claimed_by = NULL, claimed_at = NULL,
           terminal_at = clock_timestamp(), state_entered_at = clock_timestamp(),
           updated_at = clock_timestamp()
     WHERE id IN (
            SELECT id FROM a2a_rail_purchases
             WHERE state IN ('needs_payment_method', 'quoting', 'awaiting_authorization')
               AND pending_write IS NULL
               AND ((buyer_action_expires_at IS NOT NULL AND buyer_action_expires_at < clock_timestamp())
                    OR state_entered_at < clock_timestamp() - (:max_age_seconds * INTERVAL '1 second'))
             ORDER BY state_entered_at
             LIMIT :limit
           )
    RETURNING id
"""
_EXPIRE_SQL_SQLITE = """
    UPDATE a2a_rail_purchases
       SET state = 'expired',
           last_error_code = CASE WHEN buyer_action_expires_at IS NOT NULL
                AND buyer_action_expires_at < CURRENT_TIMESTAMP THEN 'buyer_action_expired' ELSE 'stale' END,
           buyer_action_kind = NULL, buyer_action_url = NULL, buyer_action_expires_at = NULL,
           buyer_email = NULL, shipping_address = NULL,
           claimed_by = NULL, claimed_at = NULL,
           terminal_at = CURRENT_TIMESTAMP, state_entered_at = CURRENT_TIMESTAMP,
           updated_at = CURRENT_TIMESTAMP
     WHERE id IN (
            SELECT id FROM a2a_rail_purchases
             WHERE state IN ('needs_payment_method', 'quoting', 'awaiting_authorization')
               AND pending_write IS NULL
               AND ((buyer_action_expires_at IS NOT NULL AND buyer_action_expires_at < CURRENT_TIMESTAMP)
                    OR state_entered_at < datetime('now', '-' || :max_age_seconds || ' seconds'))
             ORDER BY state_entered_at
             LIMIT :limit
           )
    RETURNING id
"""
EXPIRE_SOURCE_STATES = _states_in(_EXPIRE_SQL, "WHERE state IN (")
MAX_AGE_SECONDS_DEFAULT = 86400


async def expire_overdue_purchases(*, max_age_seconds: int = MAX_AGE_SECONDS_DEFAULT, limit: int = 100) -> int:
    values = {"max_age_seconds": _limit(max_age_seconds, 7 * 86400), "limit": _limit(limit, 1000)}
    if IS_POSTGRES:
        rows = await database.fetch_all(_EXPIRE_SQL, values)
    else:
        rows = await database.fetch_all(_EXPIRE_SQL_SQLITE, values)
    return len(rows)
