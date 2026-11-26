"""Persistence for the A2A token rail (migration 268): buyer refs + purchases.

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
expire: it ends only on the rail's own answer (completed / failed), read by the worker. There is no
`awaiting_authorization -> completed` edge (Reap has one): every completion passes through `paying`,
where the pay write-ahead lives. A merchant challenge (3DS) during `paying` is a buyer page set with
`set_payment_challenge`, without leaving the state. `attempts` counts every claim outside the two
buyer states, `paying` included: a worker must never end `paying` on an attempts cap (money may be
moving); only the rail's answer ends it. A purchase that ends (expired, failed, refused) while it
holds a `payment_token_id` keeps the id on purpose: the worker must void that token on the rail
(`list_orphan_tokens` / `forget_orphan_token`; until the rail has a void, the worker counts them). There is NO re-quote edge (`awaiting_authorization ->
quoting`): nothing wrote it, and the AgenPas approval consent binds to a purchase's one approval
(re-adding it needs an approval epoch on agent_purchase_consents). The transition's quoting branch
still replaces the quote, order and token fields on entry and refuses entry while a payment token is
recorded: an abandoned authorization must be voided, which needs its id (`forget_voided_token`).
Entering `quoting` also requires the contact not to have been purged (the quote needs the address).

── WRITE-AHEAD, BECAUSE THE RAIL CANNOT DEDUPLICATE (YET) ────────────────────────────────────

The rail mints its own idempotency key per call, so a write resent after a timeout is a NEW write.
Before any call that may change state on the rail, the worker records `pending_write` (the action)
under its claim; it clears it only on a definite answer. A row found with `pending_write` set had a
write whose outcome nobody saw: the next worker must READ the rail's state before it may send any
other write. The ledger enforces that, not only documents it:
  - `mark_pending_write` refuses a second write while one is pending;
  - `transition_as_holder` refuses to move a row with a pending write unless it names that write in
    `resolves_write` (its outcome is known: answered, or read back from the rail). So a worker that
    gives up ("rail unreachable -> failed") cannot erase the only evidence that a charge may be in
    flight: its transition returns None, like any lost race;
  - the expiry sweep never touches a row with a pending write, and the requeue keeps it.

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

import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence

from db.database import IS_POSTGRES, database
from db.reap_agentic_ledger import _bind_dt, _decode_dt, _decode_json, _states_in, require_consent_version

logger = logging.getLogger(__name__)

RAIL = "a2a"
ID_PREFIX = "ap_"

# ── the state machine ────────────────────────────────────────────────────────────────────────

TERMINAL_STATES: FrozenSet[str] = frozenset({"completed", "failed", "refused", "expired"})

#: THE ONE MAP. `transition` refuses an illegal pair before any SQL runs. A terminal state maps to
#: the empty set: "exists and permits nothing", not "unknown".
ALLOWED_TRANSITIONS: Dict[str, FrozenSet[str]] = {
    "opened": frozenset({"needs_payment_method", "quoting", "refused", "failed", "expired"}),
    "needs_payment_method": frozenset({"quoting", "expired", "failed"}),
    "quoting": frozenset({"awaiting_authorization", "refused", "failed", "expired"}),
    # NO -> quoting (re-quote) edge: nothing wrote it, and the AgenPas approval consent
    # (db/agent_purchase_consents, services/payment_orchestration/purchase_consent.approval_epoch) binds to the
    # purchase's one approval. Re-adding needs an approval epoch on agent_purchase_consents.
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
# db/migrations/268_a2a_rail_ledger.sql is the same DDL; db/schema_guard.ensure_required_schema_light
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
    contact_purged_at TIMESTAMPTZ,
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

# Migration 278: the worker's heartbeat. ONE row, stamped at the start of every armed tick, so the
# create route (which runs on another service and cannot see the worker's settings) can tell whether
# anything will ever advance a purchase it opens.
_CREATE_WORKER_HEARTBEAT_PG = """
CREATE TABLE IF NOT EXISTS a2a_rail_worker_heartbeat (
    id SMALLINT PRIMARY KEY CONSTRAINT ck_a2a_rail_worker_heartbeat_one CHECK (id = 1),
    ticked_at TIMESTAMPTZ NOT NULL
)
"""
_CREATE_WORKER_HEARTBEAT_SQLITE = _CREATE_WORKER_HEARTBEAT_PG.replace("TIMESTAMPTZ", "TIMESTAMP")

# Migration 299: the contact purge's heartbeat. ONE row, stamped by every enabled run of
# jobs/a2a_rail_contact_purge that finished its purge, so the create route can refuse A2A purchases
# while nothing would delete their buyer's contact within the 72 h the terms promise. Its own table:
# a2a_rail_worker_heartbeat is CHECK (id = 1).
_CREATE_CONTACT_PURGE_HEARTBEAT_PG = """
CREATE TABLE IF NOT EXISTS a2a_rail_contact_purge_heartbeat (
    id SMALLINT PRIMARY KEY CONSTRAINT ck_a2a_rail_contact_purge_heartbeat_one CHECK (id = 1),
    ran_at TIMESTAMPTZ NOT NULL
)
"""
_CREATE_CONTACT_PURGE_HEARTBEAT_SQLITE = _CREATE_CONTACT_PURGE_HEARTBEAT_PG.replace("TIMESTAMPTZ", "TIMESTAMP")

# Migration 287: the provider's checkout task id (`ckout_...`), written once the checkout's confirm is
# answered, read to poll and look the submission up. Nullable, no default: a catalog-only change on
# Postgres 11+. Only the checkout step (`record_checkout_task_as_holder`) and its readers touch it, so
# every other statement works on a table that does not have it yet.
_ADD_CHECKOUT_TASK_ID_PG = (
    "ALTER TABLE IF EXISTS a2a_rail_purchases ADD COLUMN IF NOT EXISTS checkout_task_id VARCHAR(128)"
)
_ADD_CHECKOUT_TASK_ID_SQLITE = "ALTER TABLE a2a_rail_purchases ADD COLUMN checkout_task_id VARCHAR(128)"
# Migration 288: how the checkout path's approval ceiling (`quoted_total_minor`) was built, e.g.
# `allowance_v1` (Pivota's own estimate: items + a tax and a shipping allowance). Written only by
# `lock_checkout_order_as_holder`, with the ceiling, so the view can say the amount is an upper bound.
# Nullable, no default, like 287; every other statement works on a table that does not have it yet.
_ADD_CEILING_SOURCE_PG = (
    "ALTER TABLE IF EXISTS a2a_rail_purchases ADD COLUMN IF NOT EXISTS ceiling_source VARCHAR(32)"
)
_ADD_CEILING_SOURCE_SQLITE = "ALTER TABLE a2a_rail_purchases ADD COLUMN ceiling_source VARCHAR(32)"
# Migration 290: how many times a held checkout was looked up, counted BEFORE each lookup goes out
# (`count_checkout_lookup_as_holder`), so a crash mid-lookup still counts. Nullable, no default, like 287.
_ADD_CHECKOUT_LOOKUPS_PG = (
    "ALTER TABLE IF EXISTS a2a_rail_purchases ADD COLUMN IF NOT EXISTS checkout_lookups INTEGER"
)
_ADD_CHECKOUT_LOOKUPS_SQLITE = "ALTER TABLE a2a_rail_purchases ADD COLUMN checkout_lookups INTEGER"
#: Both checkout columns in ONE guarded ALTER on Postgres (the DDL of 287 and 288, as one statement): they
#: land together or not at all, so a boot that times out never leaves one without the other. The worker
#: also refuses the checkout path on a row read without either (`a2a_rail_worker._checkout_schema_ready`):
#: that row-level check is the real guard. SQLite cannot add two columns in one ALTER: one each.
_HEAL_CHECKOUT_COLUMNS_PG = (
    "ALTER TABLE IF EXISTS a2a_rail_purchases"
    " ADD COLUMN IF NOT EXISTS checkout_task_id VARCHAR(128),"
    " ADD COLUMN IF NOT EXISTS ceiling_source VARCHAR(32),"
    " ADD COLUMN IF NOT EXISTS checkout_lookups INTEGER"
)
# Migration 298: the paying card's two display attributes for the buyer page, and the card they belong to
# (`record_payment_card_as_holder`, written as the token is minted on it). Display only, never PII, nullable, no
# default; every other statement works on a table that does not have them yet. One guarded ALTER on Postgres, so the
# three land together or not at all; SQLite one each.
_HEAL_CARD_COLUMNS_PG = (
    "ALTER TABLE IF EXISTS a2a_rail_purchases"
    " ADD COLUMN IF NOT EXISTS card_network VARCHAR(32),"
    " ADD COLUMN IF NOT EXISTS card_last4 VARCHAR(4),"
    " ADD COLUMN IF NOT EXISTS card_method_id VARCHAR(128)"
)
# Migration 300: the buyer's card choice before the token (`park_for_card_choice_as_holder`,
# `confirm_card_choice`): the ACTIVE cards the buyer may pick from, and when they confirmed one. Nullable, no
# default; only the card-choice wait reads them (dark: AGENPAS_CARD_PICKER_ENABLED). One guarded ALTER on Postgres,
# so the two land together or not at all; SQLite one each.
_HEAL_CARD_CHOICE_COLUMNS_PG = (
    "ALTER TABLE IF EXISTS a2a_rail_purchases"
    " ADD COLUMN IF NOT EXISTS card_options TEXT,"
    " ADD COLUMN IF NOT EXISTS card_confirmed_at TIMESTAMPTZ"
)
#: (column, SQLite heal), in migration order.
_HEALED_COLUMNS_SQLITE = (
    ("checkout_task_id", _ADD_CHECKOUT_TASK_ID_SQLITE),
    ("ceiling_source", _ADD_CEILING_SOURCE_SQLITE),
    ("checkout_lookups", _ADD_CHECKOUT_LOOKUPS_SQLITE),
    ("card_network", "ALTER TABLE a2a_rail_purchases ADD COLUMN card_network VARCHAR(32)"),
    ("card_last4", "ALTER TABLE a2a_rail_purchases ADD COLUMN card_last4 VARCHAR(4)"),
    ("card_method_id", "ALTER TABLE a2a_rail_purchases ADD COLUMN card_method_id VARCHAR(128)"),
    ("card_options", "ALTER TABLE a2a_rail_purchases ADD COLUMN card_options TEXT"),
    ("card_confirmed_at", "ALTER TABLE a2a_rail_purchases ADD COLUMN card_confirmed_at TIMESTAMP"),
)
_SQLITE_PURCHASE_COLUMNS = "SELECT name FROM pragma_table_info('a2a_rail_purchases')"


async def ensure_a2a_rail_schema() -> None:
    """Self-heal parity with migrations 268, 278, 287, 288, 290, 298, 299 and 300. Idempotent; called at every startup.
    NOT 296 (a2a_rail_manual_resolution_audit): that table has no startup heal, on purpose (see its section)."""
    if IS_POSTGRES:
        await database.execute(_CREATE_BUYER_REFS_PG)
        await database.execute(_CREATE_PURCHASES_PG)
        # Built only while missing, under the heal lock_timeout (see db/agent_purchase_ledger): once
        # these tables are live, a bare CREATE INDEX IF NOT EXISTS would queue every boot behind writers.
        from db.schema_guard import _ensure_index, guarded_add_columns

        for statement in (_CREATE_BUYER_REFS_OWNER_INDEX, _CREATE_PURCHASES_OWNER_INDEX, _CREATE_PURCHASES_DUE_INDEX):
            await _ensure_index(statement)
        await database.execute(_CREATE_WORKER_HEARTBEAT_PG)
        await database.execute(_CREATE_CONTACT_PURGE_HEARTBEAT_PG)
        # A lock-free catalog check first; the ALTER (ACCESS EXCLUSIVE) runs only while a column is
        # missing, under schema_guard's lock_timeout. A timeout defers the heal to the next boot: only
        # the dark checkout step needs these columns.
        for statement in guarded_add_columns(_HEAL_CHECKOUT_COLUMNS_PG + ";"):
            try:
                await database.execute(statement)
            except Exception as exc:  # noqa: BLE001
                logger.warning("a2a_rail_ledger: checkout columns heal deferred to next boot error_type=%s",
                               type(exc).__name__)
        # The card columns, the same way: only the buyer page's card reads them, so a deferred heal hides the card.
        for statement in guarded_add_columns(_HEAL_CARD_COLUMNS_PG + ";"):
            try:
                await database.execute(statement)
            except Exception as exc:  # noqa: BLE001
                logger.warning("a2a_rail_ledger: card columns heal deferred to next boot error_type=%s",
                               type(exc).__name__)
        # The card-choice columns, the same way: only the dark card-choice wait needs them. The worker never waits
        # on a row read without them (`a2a_rail_worker._card_choice_ready`), so a deferred heal mints as before.
        for statement in guarded_add_columns(_HEAL_CARD_CHOICE_COLUMNS_PG + ";"):
            try:
                await database.execute(statement)
            except Exception as exc:  # noqa: BLE001
                logger.warning("a2a_rail_ledger: card choice columns heal deferred to next boot error_type=%s",
                               type(exc).__name__)
    else:
        await database.execute(_CREATE_BUYER_REFS_SQLITE)
        await database.execute(_CREATE_PURCHASES_SQLITE)
        await database.execute(_CREATE_WORKER_HEARTBEAT_SQLITE)
        await database.execute(_CREATE_CONTACT_PURGE_HEARTBEAT_SQLITE)
        columns = {row["name"] for row in await database.fetch_all(_SQLITE_PURCHASE_COLUMNS)}
        for column, heal_sqlite in _HEALED_COLUMNS_SQLITE:
            if column not in columns:
                await database.execute(heal_sqlite)
        await database.execute(_CREATE_BUYER_REFS_OWNER_INDEX)
        await database.execute(_CREATE_PURCHASES_OWNER_INDEX)
        await database.execute(_CREATE_PURCHASES_DUE_INDEX)


# ── row decoding ─────────────────────────────────────────────────────────────────────────────

_TS_COLUMNS = (
    "contact_purged_at", "consented_at", "card_confirmed_at", "buyer_action_expires_at", "pending_write_at",
    "next_poll_at", "claimed_at",
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


#: Order references are legitimately all digits (a merchant's order number, the rail's order id), and
#: about 1 in 10 such 14-19 digit numbers pass the Luhn check: the same exemption the transport makes
#: (a2a_rail._REFERENCE_KEYS + _PURE_DIGITS_RE). Refusing them would strand a paid purchase in `paying`.
_ORDER_REF_FIELDS = frozenset({"order_id", "merchant_order_id"})
#: `vic:` is the VIC order step's marker on `order_id` (a2a_rail_worker.VIC_ORDER_PREFIX): the digits after
#: it are the rail's order reference, exempt the same way.
_PURE_DIGITS_RE = re.compile(r"^(?:vic:)?\d{1,124}\Z|^\d{1,128}\Z")


def _rail_ref(value: Any, name: str) -> Optional[str]:
    """A rail id (method, token, quote, order). Never a card number: a PAN or DPAN shaped value is
    refused before it can be stored (the transport strips them; this is the second line). An order
    reference may be all digits; a card number embedded in other text, or spaced, is still refused."""
    from services.payment_orchestration.a2a_rail import _has_card_number

    ref = _optional(_RAIL_REF_RE, value, name)
    if ref is None:
        return None
    if name in _ORDER_REF_FIELDS and _PURE_DIGITS_RE.match(ref):
        return ref
    if _has_card_number(ref):
        raise ValueError(f"{name} looks like a card number")
    return ref


def _require_https_url(value: Any, name: str, *, required: bool) -> Optional[str]:
    if value is None and not required:
        return None
    text = str(value or "").strip()
    authority = text.split("/", 3)[2] if text.startswith("https://") else ""
    if (
        not authority or "@" in authority or len(text) > 2048
        or any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text)
    ):
        raise ValueError(f"{name} must be an https URL with a host, no credentials and no control characters")
    return text


#: The amount columns are BIGINT: a larger value is refused here, not by the database mid-step.
_BIGINT_MAX = 2 ** 63 - 1


def _require_minor(value: Any, name: str, *, positive: bool = False) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer number of minor units")
    if not (1 if positive else 0) <= value <= _BIGINT_MAX:
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
# every terminal write NULLs the PII, the page, the pending write and the claim. `last_error_code` is
# replaced, not kept (a transient code from an earlier release must not ride into a healthy state),
# and `next_poll_at` is left alone unless given (scheduling belongs to `release_claim`). A pending write
# blocks the transition unless `resolves_write` names it.
_TRANSITION_SQL = """
    UPDATE a2a_rail_purchases
       SET state = :to_state,
           payment_method_id = COALESCE(:payment_method_id, payment_method_id),
           payment_token_id = CASE WHEN :to_state_probe = 'quoting'
                THEN CAST(:payment_token_id AS TEXT) ELSE COALESCE(:payment_token_id, payment_token_id) END,
           quote_id = CASE WHEN :to_state_probe = 'quoting'
                THEN CAST(:quote_id AS TEXT) ELSE COALESCE(:quote_id, quote_id) END,
           order_id = CASE WHEN :to_state_probe = 'quoting'
                THEN CAST(:order_id AS TEXT) ELSE COALESCE(:order_id, order_id) END,
           quoted_total_minor = CASE WHEN :to_state_probe = 'quoting'
                THEN CAST(:quoted_total_minor AS BIGINT) ELSE COALESCE(:quoted_total_minor, quoted_total_minor) END,
           final_total_minor = COALESCE(:final_total_minor, final_total_minor),
           shipping_minor = CASE WHEN :to_state_probe = 'quoting'
                THEN CAST(:shipping_minor AS BIGINT) ELSE COALESCE(:shipping_minor, shipping_minor) END,
           tax_minor = CASE WHEN :to_state_probe = 'quoting'
                THEN CAST(:tax_minor AS BIGINT) ELSE COALESCE(:tax_minor, tax_minor) END,
           buyer_action_kind = CASE WHEN :to_state_probe IN ('completed', 'failed', 'refused', 'expired')
                THEN NULL ELSE CAST(:buyer_action_kind AS TEXT) END,
           buyer_action_url = CASE WHEN :to_state_probe IN ('completed', 'failed', 'refused', 'expired')
                THEN NULL ELSE CAST(:buyer_action_url AS TEXT) END,
           buyer_action_expires_at = CASE WHEN :to_state_probe IN ('completed', 'failed', 'refused', 'expired')
                THEN NULL ELSE CAST(:buyer_action_expires_at AS TIMESTAMPTZ) END,
           merchant_order_id = COALESCE(:merchant_order_id, merchant_order_id),
           merchant_order_status_url = COALESCE(:merchant_order_status_url, merchant_order_status_url),
           refusal_reason = COALESCE(:refusal_reason, refusal_reason),
           last_error_code = :last_error_code,
           next_poll_at = COALESCE(:next_poll_at, next_poll_at),
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
       AND (pending_write IS NULL OR pending_write = :resolves_write)
       AND (:to_state_probe <> 'quoting' OR (payment_token_id IS NULL AND contact_purged_at IS NULL))
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
    resolves_write: Optional[str] = None,
    **fields: Any,
) -> Optional[Dict[str, Any]]:
    """Advance a purchase the worker holds, atomically. Returns the new row, or None when the row
    is not in `from_states`, the claim has moved on, or a write is pending that `resolves_write` does
    not name: a lost race or an unseen outcome, never an error. Re-read, do not retry the same write.

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
        values[name] = _rail_ref(values[name], name)
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
    values.update({"id": purchase_id, "to_state": to_state, "to_state_probe": to_state, "holder": worker_id,
                   "resolves_write": _optional(_ACTION_RE, resolves_write, "resolves_write")})
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


# The provider's quote for a purchase in `quoting` (the VIC order step): the all-in total the order is then
# sent with and the buyer approves. Only while the worker holds the row, before any token or pending write
# (the quote precedes the order's write-ahead). A fresh quote replaces an earlier one: a quote is free and
# no order was sent with the old one (a sent order holds a pending write until its answer is seen).
_RECORD_QUOTE_SQL = """
    UPDATE a2a_rail_purchases
       SET quote_id = :quote_id,
           quoted_total_minor = :quoted_total_minor,
           shipping_minor = :shipping_minor,
           tax_minor = :tax_minor,
           updated_at = clock_timestamp()
     WHERE id = :id
       AND state = 'quoting'
       AND claimed_by = :holder
       AND pending_write IS NULL
       AND payment_token_id IS NULL
       AND contact_purged_at IS NULL
    RETURNING *
"""
_RECORD_QUOTE_SQL_SQLITE = _RECORD_QUOTE_SQL.replace("clock_timestamp()", "CURRENT_TIMESTAMP")


async def record_quote_as_holder(
    purchase_id: str,
    worker_id: str,
    *,
    quote_id: str,
    quoted_total_minor: int,
    shipping_minor: int,
    tax_minor: int,
) -> Optional[Dict[str, Any]]:
    """Record the provider's quote on a held `quoting` row. None when the row is not held in `quoting`,
    holds a token or a pending write, or its contact was purged: the quote must not be used then.
    Malformed values raise before any SQL."""
    values = {
        "id": purchase_id,
        "holder": _require(_WORKER_RE, worker_id, "worker_id"),
        "quote_id": _rail_ref(_require(_RAIL_REF_RE, quote_id, "quote_id"), "quote_id"),
        "quoted_total_minor": _require_minor(quoted_total_minor, "quoted_total_minor"),
        "shipping_minor": _require_minor(shipping_minor, "shipping_minor"),
        "tax_minor": _require_minor(tax_minor, "tax_minor"),
    }
    if values["quoted_total_minor"] is None or values["quoted_total_minor"] <= 0:
        raise ValueError("quoted_total_minor must be a positive number of minor units")
    if values["shipping_minor"] is None or values["tax_minor"] is None:
        raise ValueError("shipping_minor and tax_minor are required")
    if IS_POSTGRES:
        return _purchase(await database.fetch_one(_RECORD_QUOTE_SQL, values))
    return _purchase(await database.fetch_one(_RECORD_QUOTE_SQL_SQLITE, values))


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


# ── the paying card's display attributes (migration 298) ─────────────────────────────────────

#: A network name as the buyer may see it: the rail's own word (`visa`), a letter first, nothing else.
_CARD_NETWORK_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{0,31}\Z")
_CARD_LAST4_RE = re.compile(r"^[0-9]{4}\Z")
#: The states in which the recorded card IS the card paying: its token was minted on it (awaiting the buyer's
#: approval of that token, paying with it, or paid). Never before: the buyer may still change the card.
PAYMENT_CARD_STATES = ("awaiting_authorization", "paying", "completed")

# Only on the held row, in `quoting` (the token step), and only for the card the row names: the card the token is
# minted on. Never cleared afterwards: not by a terminal state, not by the contact purge (a network, four digits and an
# opaque card id are display attributes, not cardholder data). The worker trusts the rail to mint on the card it named;
# if the rail's own approval page ever let the buyer switch cards, the page would still name the earlier one.
_RECORD_PAYMENT_CARD_SQL = """
    UPDATE a2a_rail_purchases
       SET card_network = :card_network,
           card_last4 = :card_last4,
           card_method_id = :card_method_id
     WHERE id = :id AND state = 'quoting' AND claimed_by = :holder AND payment_method_id = :card_method_id
    RETURNING id
"""
_SELECT_PAYMENT_CARD_FOR_OWNER_SQL = """
    SELECT state, payment_method_id, card_method_id, card_network, card_last4
      FROM a2a_rail_purchases
     WHERE id = :id AND agent_id = :agent_id AND agent_user_ref_hash = :agent_user_ref_hash
"""


async def record_payment_card_as_holder(
    purchase_id: str, worker_id: str, *, method_id: str, network: Any, last4: Any
) -> bool:
    """Record the paying card's network and last four, from the rail's own card list, on the held row whose card it
    is. Nothing is written without exactly four digits; a network that is not a plain name is stored as NULL (the
    page then says "Card"). True when the row took it. Display only: the caller never lets a fault stop a step."""
    last4_text = str(last4 or "").strip() if isinstance(last4, str) else ""
    if not _CARD_LAST4_RE.match(last4_text):
        return False
    network_text = str(network or "").strip() if isinstance(network, str) else ""
    row = await database.fetch_one(_RECORD_PAYMENT_CARD_SQL, {
        "id": purchase_id, "holder": _require(_WORKER_RE, worker_id, "worker_id"),
        "card_method_id": str(method_id), "card_last4": last4_text,
        "card_network": network_text if _CARD_NETWORK_RE.match(network_text) else None,
    })
    return row is not None


async def get_payment_card_for_owner(
    purchase_id: str, agent_id: str, agent_user_ref_hash: str
) -> Optional[Dict[str, Any]]:
    """`{"network": str | None, "last4": "4242"}` for the card this owner's purchase is paying with, or None: not the
    owner's, not yet in a PAYMENT_CARD_STATES state, no card recorded, or one recorded for ANOTHER card than the one
    the row now names (the buyer changed card). Writes nothing."""
    row = await database.fetch_one(_SELECT_PAYMENT_CARD_FOR_OWNER_SQL, {
        "id": purchase_id, "agent_id": agent_id, "agent_user_ref_hash": agent_user_ref_hash})
    if row is None:
        return None
    row = dict(row)
    if row.get("state") not in PAYMENT_CARD_STATES:
        return None
    method_id = str(row.get("payment_method_id") or "")
    if not method_id or str(row.get("card_method_id") or "") != method_id:
        return None
    last4 = str(row.get("card_last4") or "")
    if not _CARD_LAST4_RE.match(last4):
        return None
    network = str(row.get("card_network") or "")
    return {"network": network if _CARD_NETWORK_RE.match(network) else None, "last4": last4}


# ── the buyer's card choice before the token (migration 300) ─────────────────────────────────
#
# With AGENPAS_CARD_PICKER_ENABLED on, the worker locks the order, then WAITS in `quoting` until the buyer confirms a
# card on the AgenPas page; only then does it ask the rail for the token. The rail has no void, so the card is chosen
# before the token is asked for, or not at all. The worker writes the buyer's ACTIVE cards (`card_options`) as the
# row starts waiting; the page reads them (`get_card_choice_for_owner`, no rail call) and the buyer's confirmation is a
# compare-and-swap on exactly that list (`confirm_card_choice`). Display attributes and opaque rail ids only.

#: The dial. Read per call, on BOTH web (the page's read and write) and the worker (the wait). Turn it on on `web`
#: FIRST, then the worker (web alone shows nothing: no row has options until the worker waits); off: the worker FIRST,
#: then `web` (a waiting row then mints on the card it names). Worker on with web off strands rows until they expire.
CARD_PICKER_ENABLED_ENV = "AGENPAS_CARD_PICKER_ENABLED"
#: The park code while the buyer has not confirmed.
CARD_CHOICE_REQUIRED = "card_choice_required"
#: The most cards one choice offers, newest first.
CARD_OPTIONS_MAX = 10
_CARD_ADDED_AT_RE = re.compile(r"^[0-9T:.+\-Z ]{1,40}\Z")
_CARD_HANDLE_RE = re.compile(r"^[0-9a-f]{24}\Z")
_TRUTHY = frozenset({"1", "true", "on", "yes"})


def card_picker_enabled() -> bool:
    return (os.getenv(CARD_PICKER_ENABLED_ENV) or "").strip().lower() in _TRUTHY


def card_handle(purchase_id: str, method_id: str) -> str:
    """The page's name for one of this purchase's cards: never the rail's card id itself, and different per purchase,
    so a handle names nothing outside the purchase it was shown for."""
    return hashlib.sha256(f"a2a-card:{purchase_id}:{method_id}".encode("utf-8")).hexdigest()[:24]


def is_card_handle(value: Any) -> bool:
    return isinstance(value, str) and bool(_CARD_HANDLE_RE.match(value))


def _card_option(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """One card as the choice keeps it. The id must be a rail id (never a card number: `_rail_ref` refuses one); the
    network, last four and date are kept only in their display shapes, else None."""
    method_id = _rail_ref(raw.get("id"), "card option id")
    if method_id is None:
        raise ValueError("card option id is required")
    network, last4, added = raw.get("network"), raw.get("last4"), raw.get("added_at")
    return {
        "id": method_id,
        "network": network if isinstance(network, str) and _CARD_NETWORK_RE.match(network) else None,
        "last4": last4 if isinstance(last4, str) and _CARD_LAST4_RE.match(last4) else None,
        "added_at": added if isinstance(added, str) and _CARD_ADDED_AT_RE.match(added) else None,
    }


def card_options_text(options: Iterable[Mapping[str, Any]]) -> str:
    """The canonical text the row keeps (and the compare-and-swap compares): 1..CARD_OPTIONS_MAX cards, ids unique."""
    cleaned = [_card_option(o) for o in options]
    if not 1 <= len(cleaned) <= CARD_OPTIONS_MAX:
        raise ValueError("card options must name 1..10 cards")
    if len({o["id"] for o in cleaned}) != len(cleaned):
        raise ValueError("card option ids must be unique")
    return json.dumps(cleaned, sort_keys=True, separators=(",", ":"))


def decode_card_options(text: Any) -> Optional[List[Dict[str, Any]]]:
    """The cards a row offers, or None (none recorded, or a value this code would not have written)."""
    if not isinstance(text, str) or not text:
        return None
    try:
        raw = json.loads(text)
        if not isinstance(raw, list) or not all(isinstance(o, dict) for o in raw):
            return None
        options = [_card_option(o) for o in raw]
    except (ValueError, TypeError):
        return None
    return options if 1 <= len(options) <= CARD_OPTIONS_MAX else None


# Park the held row for the buyer's choice: the cards written, the card the row names set to the default (both only
# while unconfirmed), the deadline set once (never moved later), the claim given back in the same statement. When the
# buyer confirmed meanwhile (the write below landed while the worker read the rail), nothing of theirs is touched and the
# row is due at once.
_PARK_FOR_CARD_CHOICE_SQL = """
    UPDATE a2a_rail_purchases
       SET card_options = CASE WHEN card_confirmed_at IS NULL THEN :card_options ELSE card_options END,
           payment_method_id = CASE WHEN card_confirmed_at IS NULL THEN :default_method_id ELSE payment_method_id END,
           buyer_action_expires_at = CASE WHEN card_confirmed_at IS NULL
                THEN COALESCE(buyer_action_expires_at, CAST(:expires_at AS TIMESTAMPTZ)) ELSE buyer_action_expires_at END,
           last_error_code = CASE WHEN card_confirmed_at IS NULL THEN 'card_choice_required' ELSE NULL END,
           next_poll_at = CASE WHEN card_confirmed_at IS NULL
                THEN CAST(:next_poll_at AS TIMESTAMPTZ) ELSE clock_timestamp() END,
           claimed_by = NULL,
           claimed_at = NULL,
           updated_at = clock_timestamp()
     WHERE id = :id AND claimed_by = :holder AND state = 'quoting' AND pending_write IS NULL
       AND payment_token_id IS NULL
    RETURNING *
"""
_PARK_FOR_CARD_CHOICE_SQL_SQLITE = (
    _PARK_FOR_CARD_CHOICE_SQL.replace("clock_timestamp()", "CURRENT_TIMESTAMP")
    .replace("CAST(:expires_at AS TIMESTAMPTZ)", ":expires_at")
    .replace("CAST(:next_poll_at AS TIMESTAMPTZ)", ":next_poll_at")
)


async def park_for_card_choice_as_holder(
    purchase_id: str, worker_id: str, *, options: Iterable[Mapping[str, Any]], default_method_id: str,
    expires_at: Any, next_poll_at: Any,
) -> Optional[Dict[str, Any]]:
    """Park a held `quoting` row (order locked, no token, no pending write) until the buyer confirms a card: see the SQL
    above. `default_method_id` must be one of `options`. None when the row is not held, not in that state, or has a
    pending write or a token."""
    text = card_options_text(options)
    default = _rail_ref(default_method_id, "default_method_id")
    if default is None or default not in {o["id"] for o in json.loads(text)}:
        raise ValueError("default_method_id must be one of the options")
    values = {
        "id": _require(_ID_RE, purchase_id, "purchase_id"),
        "holder": _require(_WORKER_RE, worker_id, "worker_id"),
        "card_options": text,
        "default_method_id": default,
        "expires_at": _bind_dt(expires_at),
        "next_poll_at": _bind_dt(next_poll_at),
    }
    if values["expires_at"] is None or values["next_poll_at"] is None:
        raise ValueError("expires_at and next_poll_at are required")
    if IS_POSTGRES:
        return _purchase(await database.fetch_one(_PARK_FOR_CARD_CHOICE_SQL, values))
    return _purchase(await database.fetch_one(_PARK_FOR_CARD_CHOICE_SQL_SQLITE, values))


# THE BUYER'S CONFIRMATION, a compare-and-swap. Owner-scoped like every agent read. Only while the row waits (`quoting`,
# no token, no pending write, never purged, not confirmed yet, the deadline not passed), only while NO worker holds it
# (a held row may be on its way to the token on the card it names: a worker whose dial is off mints without waiting, so
# a card changed under it would be a token on a card the buyer did not pick, which the rail cannot void), and only on
# EXACTLY the cards the page showed (`card_options` as read): a list the worker rewrote since is a refusal, and the page
# re-reads. Sets the card, records the confirmation, clears the deadline (the row now waits on nobody) and makes the row
# due at once.
_CONFIRM_CARD_CHOICE_SQL = """
    UPDATE a2a_rail_purchases
       SET payment_method_id = :method_id,
           card_confirmed_at = clock_timestamp(),
           buyer_action_expires_at = CASE WHEN buyer_action_kind IS NULL THEN NULL ELSE buyer_action_expires_at END,
           last_error_code = CASE WHEN last_error_code = 'card_choice_required' THEN NULL ELSE last_error_code END,
           next_poll_at = clock_timestamp(),
           updated_at = clock_timestamp()
     WHERE id = :id AND agent_id = :agent_id AND agent_user_ref_hash = :agent_user_ref_hash
       AND state = 'quoting' AND pending_write IS NULL AND payment_token_id IS NULL
       AND contact_purged_at IS NULL AND card_confirmed_at IS NULL AND claimed_by IS NULL
       AND card_options = :card_options
       AND (buyer_action_expires_at IS NULL OR buyer_action_expires_at > clock_timestamp())
    RETURNING *
"""
_CONFIRM_CARD_CHOICE_SQL_SQLITE = _CONFIRM_CARD_CHOICE_SQL.replace("clock_timestamp()", "CURRENT_TIMESTAMP")

# The dial was turned off while the row waited: it no longer waits on the buyer, so the wait's deadline goes (else the
# sweep would end it at 30 minutes as `buyer_action_expired`). Only the wait's own park: the held `quoting` row, no
# buyer page, the park code still on it.
_END_CARD_CHOICE_WAIT_SQL = """
    UPDATE a2a_rail_purchases
       SET buyer_action_expires_at = NULL, last_error_code = NULL, updated_at = clock_timestamp()
     WHERE id = :id AND claimed_by = :holder AND state = 'quoting' AND buyer_action_kind IS NULL
       AND last_error_code = 'card_choice_required'
    RETURNING *
"""
_END_CARD_CHOICE_WAIT_SQL_SQLITE = _END_CARD_CHOICE_WAIT_SQL.replace("clock_timestamp()", "CURRENT_TIMESTAMP")


async def end_card_choice_wait_as_holder(purchase_id: str, worker_id: str) -> Optional[Dict[str, Any]]:
    """Drop the wait's deadline and park code from a held row that no longer waits (the SQL above); None otherwise."""
    values = {"id": _require(_ID_RE, purchase_id, "purchase_id"), "holder": _require(_WORKER_RE, worker_id, "worker_id")}
    if IS_POSTGRES:
        return _purchase(await database.fetch_one(_END_CARD_CHOICE_WAIT_SQL, values))
    return _purchase(await database.fetch_one(_END_CARD_CHOICE_WAIT_SQL_SQLITE, values))


_SELECT_CARD_CHOICE_FOR_OWNER_SQL = """
    SELECT state, payment_method_id, payment_token_id, pending_write, contact_purged_at, card_options,
           card_confirmed_at, buyer_action_expires_at
      FROM a2a_rail_purchases
     WHERE id = :id AND agent_id = :agent_id AND agent_user_ref_hash = :agent_user_ref_hash
"""


async def get_card_choice_for_owner(
    purchase_id: str, agent_id: str, agent_user_ref_hash: str
) -> Optional[Dict[str, Any]]:
    """The choice this owner's purchase offers, or None (not the owner's, not waiting on a card, none recorded):
    `{"options": [...], "options_text": <the stored text>, "selected": <rail id>, "confirmed": bool, "expires_at"}`.
    A row past its token (or its deadline) offers nothing. Writes nothing, calls no rail."""
    if not _ID_RE.match(str(purchase_id or "")):
        return None
    row = await database.fetch_one(_SELECT_CARD_CHOICE_FOR_OWNER_SQL, {
        "id": purchase_id, "agent_id": agent_id, "agent_user_ref_hash": agent_user_ref_hash})
    if row is None:
        return None
    row = dict(row)
    if row.get("state") != "quoting" or row.get("payment_token_id") or row.get("contact_purged_at"):
        return None
    options = decode_card_options(row.get("card_options"))
    selected = str(row.get("payment_method_id") or "")
    if options is None or selected not in {o["id"] for o in options}:
        return None
    expires_at = _decode_dt(row.get("buyer_action_expires_at"))
    confirmed = row.get("card_confirmed_at") is not None
    if not confirmed and isinstance(expires_at, datetime) and expires_at <= datetime.now(timezone.utc):
        return None
    return {"options": options, "options_text": row["card_options"], "selected": selected,
            "confirmed": confirmed, "expires_at": expires_at}


async def confirm_card_choice(
    purchase_id: str, agent_id: str, agent_user_ref_hash: str, *, method_id: str, options_text: str
) -> Optional[Dict[str, Any]]:
    """Record the buyer's card (the compare-and-swap above). `method_id` must be one of the cards in `options_text`, the
    stored list the caller read. None when the row is not this owner's or no longer waits with exactly those cards: the
    caller re-reads to tell an identical repeat (the same card, already confirmed) from a conflict."""
    if not _ID_RE.match(str(purchase_id or "")):
        return None
    method = _rail_ref(method_id, "method_id")
    options = decode_card_options(options_text)
    if method is None or options is None or method not in {o["id"] for o in options}:
        raise ValueError("method_id must be one of the options")
    values = {
        "id": purchase_id, "agent_id": str(agent_id or "").strip(),
        "agent_user_ref_hash": str(agent_user_ref_hash or "").strip(),
        "method_id": method, "card_options": options_text,
    }
    if IS_POSTGRES:
        return _purchase(await database.fetch_one(_CONFIRM_CARD_CHOICE_SQL, values))
    return _purchase(await database.fetch_one(_CONFIRM_CARD_CHOICE_SQL_SQLITE, values))


# The checkout path's order lock: Pivota's own order reference and the approval ceiling the token is
# then minted for, on a held `quoting` row that has neither yet, no token and no pending write. Never a
# second time: a locked row is not locked again (a re-quote clears both first, through the transition).
_LOCK_CHECKOUT_ORDER_SQL = """
    UPDATE a2a_rail_purchases
       SET order_id = :order_id,
           quoted_total_minor = :quoted_total_minor,
           ceiling_source = :ceiling_source,
           updated_at = clock_timestamp()
     WHERE id = :id
       AND state = 'quoting'
       AND claimed_by = :holder
       AND pending_write IS NULL
       AND payment_token_id IS NULL
       AND order_id IS NULL
       AND quoted_total_minor IS NULL
       AND contact_purged_at IS NULL
    RETURNING *
"""
_LOCK_CHECKOUT_ORDER_SQL_SQLITE = _LOCK_CHECKOUT_ORDER_SQL.replace("clock_timestamp()", "CURRENT_TIMESTAMP")
_CEILING_SOURCE_RE = re.compile(r"^[a-z0-9_]{1,32}\Z")


async def lock_checkout_order_as_holder(
    purchase_id: str, worker_id: str, *, order_id: str, quoted_total_minor: int, ceiling_source: str
) -> Optional[Dict[str, Any]]:
    """Lock a held `quoting` row for the checkout path. None when the row is not held in `quoting`, is
    already locked, holds a token or a pending write, or its contact was purged. Malformed values (a
    card-number shaped order id included) raise before any SQL."""
    values = {
        "id": purchase_id,
        "holder": _require(_WORKER_RE, worker_id, "worker_id"),
        "order_id": _rail_ref(_require(_RAIL_REF_RE, order_id, "order_id"), "order_id"),
        "quoted_total_minor": _require_minor(quoted_total_minor, "quoted_total_minor", positive=True),
        "ceiling_source": _require(_CEILING_SOURCE_RE, ceiling_source, "ceiling_source"),
    }
    if values["quoted_total_minor"] is None:
        raise ValueError("quoted_total_minor is required")
    if IS_POSTGRES:
        return _purchase(await database.fetch_one(_LOCK_CHECKOUT_ORDER_SQL, values))
    return _purchase(await database.fetch_one(_LOCK_CHECKOUT_ORDER_SQL_SQLITE, values))


# The provider's checkout task, once its confirm is answered: recorded in the SAME statement that
# resolves the confirm's write-ahead, on a held `paying` row with no task yet, so an answer seen twice
# (or a task id from another flow) can never replace the one being polled.
_RECORD_CHECKOUT_TASK_SQL = """
    UPDATE a2a_rail_purchases
       SET checkout_task_id = :checkout_task_id,
           pending_write = NULL,
           pending_write_at = NULL,
           updated_at = clock_timestamp()
     WHERE id = :id
       AND state = 'paying'
       AND claimed_by = :holder
       AND pending_write = :resolves_write
       AND checkout_task_id IS NULL
    RETURNING *
"""
_RECORD_CHECKOUT_TASK_SQL_SQLITE = _RECORD_CHECKOUT_TASK_SQL.replace("clock_timestamp()", "CURRENT_TIMESTAMP")


async def record_checkout_task_as_holder(
    purchase_id: str, worker_id: str, *, checkout_task_id: str, resolves_write: str
) -> Optional[Dict[str, Any]]:
    """Record the checkout's task id and resolve the confirm's pending write (`resolves_write`). None
    when the row is not held in `paying` with that write pending and no task yet. Malformed values
    (a card-number shaped id included) raise before any SQL."""
    values = {
        "id": purchase_id,
        "holder": _require(_WORKER_RE, worker_id, "worker_id"),
        "checkout_task_id": _rail_ref(_require(_RAIL_REF_RE, checkout_task_id, "checkout_task_id"), "checkout_task_id"),
        "resolves_write": _require(_ACTION_RE, resolves_write, "resolves_write"),
    }
    if IS_POSTGRES:
        return _purchase(await database.fetch_one(_RECORD_CHECKOUT_TASK_SQL, values))
    return _purchase(await database.fetch_one(_RECORD_CHECKOUT_TASK_SQL_SQLITE, values))


# A held checkout's lookups, counted WRITE-AHEAD (before the read goes out), so a step that crashes in the
# middle of one still counts it: at most `max_tries` per row, ever. Fenced on the holder and `paying`.
_COUNT_CHECKOUT_LOOKUP_SQL = """
    UPDATE a2a_rail_purchases
       SET checkout_lookups = COALESCE(checkout_lookups, 0) + 1,
           updated_at = clock_timestamp()
     WHERE id = :id
       AND state = 'paying'
       AND claimed_by = :holder
       AND COALESCE(checkout_lookups, 0) < :max_tries
    RETURNING *
"""
_COUNT_CHECKOUT_LOOKUP_SQL_SQLITE = _COUNT_CHECKOUT_LOOKUP_SQL.replace("clock_timestamp()", "CURRENT_TIMESTAMP")


async def count_checkout_lookup_as_holder(purchase_id: str, worker_id: str, *, max_tries: int) -> Optional[Dict[str, Any]]:
    """Count one lookup of a held checkout before it is sent. None when the cap is reached, or the row is not
    this worker's `paying` row: then no lookup may go out."""
    if isinstance(max_tries, bool) or not isinstance(max_tries, int) or not 1 <= max_tries <= 50:
        raise ValueError("max_tries must be in 1..50")
    values = {"id": _require(_ID_RE, purchase_id, "purchase_id"),
              "holder": _require(_WORKER_RE, worker_id, "worker_id"), "max_tries": max_tries}
    if IS_POSTGRES:
        return _purchase(await database.fetch_one(_COUNT_CHECKOUT_LOOKUP_SQL, values))
    return _purchase(await database.fetch_one(_COUNT_CHECKOUT_LOOKUP_SQL_SQLITE, values))


# A task id an answer AFTER the confirm named, noted on a row held for that answer (its card code was
# scrubbed): recorded so a human has the task to look up, WITHOUT resolving the confirm's pending write.
_NOTE_CHECKOUT_TASK_SQL = """
    UPDATE a2a_rail_purchases
       SET checkout_task_id = :checkout_task_id, updated_at = clock_timestamp()
     WHERE id = :id AND state = 'paying' AND claimed_by = :holder AND checkout_task_id IS NULL
    RETURNING *
"""
_NOTE_CHECKOUT_TASK_SQL_SQLITE = _NOTE_CHECKOUT_TASK_SQL.replace("clock_timestamp()", "CURRENT_TIMESTAMP")


async def note_checkout_task_as_holder(purchase_id: str, worker_id: str, *, checkout_task_id: str) -> Optional[Dict[str, Any]]:
    """Record the task id on a `paying` row this worker holds that has none, leaving any pending write. None
    otherwise. Malformed values raise before any SQL."""
    values = {
        "id": _require(_ID_RE, purchase_id, "purchase_id"),
        "holder": _require(_WORKER_RE, worker_id, "worker_id"),
        "checkout_task_id": _rail_ref(_require(_RAIL_REF_RE, checkout_task_id, "checkout_task_id"), "checkout_task_id"),
    }
    if IS_POSTGRES:
        return _purchase(await database.fetch_one(_NOTE_CHECKOUT_TASK_SQL, values))
    return _purchase(await database.fetch_one(_NOTE_CHECKOUT_TASK_SQL_SQLITE, values))


# ── the operator's resolution of a HELD checkout ─────────────────────────────────────────────
#
# A checkout the worker HELD (a code in a2a_rail_worker._CHECKOUT_HOLD_CODES) stays in `paying` for good:
# no sweep ends `paying`, and the worker never resolves a hold by itself. A HUMAN who has checked the
# provider's side (no order, no charge for this purchase) ends it here, through jobs/a2a_rail_operator.py.
# Compare-and-swap on the hold code the human read, on an UNCLAIMED `paying` checkout row (it has a token
# id): a row that moved since, or that a worker holds right now, is not touched. The terminal write NULLs
# the contact, the page, the pending write and the claim, like every terminal write here.
_OPERATOR_FAIL_HELD_CHECKOUT_SQL = """
    UPDATE a2a_rail_purchases
       SET state = 'failed',
           last_error_code = :code,
           pending_write = NULL,
           pending_write_at = NULL,
           buyer_action_kind = NULL,
           buyer_action_url = NULL,
           buyer_action_expires_at = NULL,
           buyer_email = NULL,
           shipping_address = NULL,
           claimed_by = NULL,
           claimed_at = NULL,
           terminal_at = clock_timestamp(),
           state_entered_at = clock_timestamp(),
           updated_at = clock_timestamp()
     WHERE id = :id
       AND state = 'paying'
       AND claimed_by IS NULL
       AND payment_token_id IS NOT NULL
       AND last_error_code = :expected_code
       AND state_entered_at < clock_timestamp() - (:min_age_seconds * INTERVAL '1 second')
    RETURNING *
"""
_OPERATOR_FAIL_HELD_CHECKOUT_SQL_SQLITE = """
    UPDATE a2a_rail_purchases
       SET state = 'failed',
           last_error_code = :code,
           pending_write = NULL,
           pending_write_at = NULL,
           buyer_action_kind = NULL,
           buyer_action_url = NULL,
           buyer_action_expires_at = NULL,
           buyer_email = NULL,
           shipping_address = NULL,
           claimed_by = NULL,
           claimed_at = NULL,
           terminal_at = CURRENT_TIMESTAMP,
           state_entered_at = CURRENT_TIMESTAMP,
           updated_at = CURRENT_TIMESTAMP
     WHERE id = :id
       AND state = 'paying'
       AND claimed_by IS NULL
       AND payment_token_id IS NOT NULL
       AND last_error_code = :expected_code
       AND state_entered_at < datetime('now', '-' || :min_age_seconds || ' seconds')
    RETURNING *
"""
#: The operator's list: `paying` checkout rows whose code looks like a checkout code (filtered to the hold
#: set by the caller). Names its columns: no contact, no URL, no rail id beyond the purchase's own.
_LIST_PAYING_CHECKOUTS_SQL = """
    SELECT id, state, last_error_code, pending_write, claimed_by,
           CASE WHEN checkout_task_id IS NULL THEN 0 ELSE 1 END AS has_task, state_entered_at, updated_at
      FROM a2a_rail_purchases
     WHERE state = 'paying'
       AND payment_token_id IS NOT NULL
       AND last_error_code LIKE 'checkout%'
     ORDER BY updated_at, id
     LIMIT :limit
"""
#: The preview's read of ONE row, by id (the same columns as the list).
_GET_PAYING_CHECKOUT_SQL = """
    SELECT id, state, last_error_code, pending_write, claimed_by,
           CASE WHEN checkout_task_id IS NULL THEN 0 ELSE 1 END AS has_task, state_entered_at, updated_at
      FROM a2a_rail_purchases
     WHERE id = :id
       AND state = 'paying'
       AND payment_token_id IS NOT NULL
"""


async def operator_fail_held_checkout(
    purchase_id: str, *, expected_code: str, code: str, min_age_seconds: int
) -> Optional[Dict[str, Any]]:
    """End a HELD checkout as `failed` with `code`. None when the row is not an unclaimed `paying` checkout
    whose code is exactly `expected_code`, in `paying` for at least `min_age_seconds`. Callers: the operator
    job only (a2a_rail_worker's wrapper, which also refuses every code that recorded payment)."""
    if isinstance(min_age_seconds, bool) or not isinstance(min_age_seconds, int) or min_age_seconds < 1800:
        raise ValueError("min_age_seconds must be at least 1800")
    values = {
        "id": _require(_ID_RE, purchase_id, "purchase_id"),
        "expected_code": _require(_CODE_RE, expected_code, "expected_code"),
        "code": _require(_CODE_RE, code, "code"),
        "min_age_seconds": min_age_seconds,
    }
    if IS_POSTGRES:
        return _purchase(await database.fetch_one(_OPERATOR_FAIL_HELD_CHECKOUT_SQL, values))
    return _purchase(await database.fetch_one(_OPERATOR_FAIL_HELD_CHECKOUT_SQL_SQLITE, values))


# ── the buyer's shipping address, when the agent did not send it (AgenPas asks the buyer) ──────────
#
# NO NEW COLUMN, NO NEW STATE. A row waits in `opened` with `last_error_code` = 'shipping_address_required'
# and its deadline in `buyer_action_expires_at` (no `buyer_action_kind` / `_url`: there is no rail page). The
# worker parks it (`park_for_shipping_address_as_holder`); the gateway's write (`submit_shipping_address`)
# fills the email and the address ONLY while the row waits and the part it fills is still empty; the expiry
# sweep ends a row past its deadline as 'shipping_address_expired'. Nothing here logs a value.
SHIPPING_ADDRESS_REQUIRED_CODE = "shipping_address_required"
SHIPPING_ADDRESS_EXPIRED_CODE = "shipping_address_expired"

_PARK_FOR_SHIPPING_ADDRESS_SQL = """
    UPDATE a2a_rail_purchases
       SET claimed_by = NULL,
           claimed_at = NULL,
           -- The buyer's submit can land while this worker holds the row (it takes no claim): when both parts
           -- are there by now, the wait is over. The code and the deadline are cleared, never re-armed, so the
           -- expiry sweep can never end a purchase whose contact is complete. An address still EXACTLY the
           -- incomplete one the worker read (incomplete_address) is not there: it counts as absent, as in
           -- a2a_shipping_address.awaits_contact and the submit below.
           buyer_action_expires_at = CASE WHEN buyer_email IS NOT NULL AND shipping_address IS NOT NULL
                AND (CAST(:incomplete_address AS JSONB) IS NULL OR shipping_address <> CAST(:incomplete_address AS JSONB))
                THEN NULL ELSE COALESCE(buyer_action_expires_at, CAST(:expires_at AS TIMESTAMPTZ)) END,
           last_error_code = CASE WHEN buyer_email IS NOT NULL AND shipping_address IS NOT NULL
                AND (CAST(:incomplete_address AS JSONB) IS NULL OR shipping_address <> CAST(:incomplete_address AS JSONB))
                THEN NULL ELSE 'shipping_address_required' END,
           next_poll_at = CASE WHEN buyer_email IS NOT NULL AND shipping_address IS NOT NULL
                AND (CAST(:incomplete_address AS JSONB) IS NULL OR shipping_address <> CAST(:incomplete_address AS JSONB))
                THEN clock_timestamp() ELSE CAST(:next_poll_at AS TIMESTAMPTZ) END,
           updated_at = clock_timestamp()
     WHERE id = :id AND claimed_by = :holder AND state = 'opened' AND pending_write IS NULL
    RETURNING *
"""
_PARK_FOR_SHIPPING_ADDRESS_SQL_SQLITE = (
    _PARK_FOR_SHIPPING_ADDRESS_SQL.replace("clock_timestamp()", "CURRENT_TIMESTAMP")
    .replace("CAST(:expires_at AS TIMESTAMPTZ)", ":expires_at")
    .replace("CAST(:next_poll_at AS TIMESTAMPTZ)", ":next_poll_at")
    .replace("CAST(:incomplete_address AS JSONB)", ":incomplete_address")
)


async def park_for_shipping_address_as_holder(
    purchase_id: str, worker_id: str, *, expires_at: Any, next_poll_at: Any,
    incomplete_address: Optional[Mapping[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Park an `opened` row the worker holds until the buyer's contact arrives: the claim given back, the
    deadline set once (never moved later), the park code written. When the contact landed meanwhile, the
    row is due at once. `incomplete_address`: the stored address the worker read when it lacks a required
    field (a2a_shipping_address.incomplete_address); while the row still holds exactly that, its address is
    not there. None when the row is not held, not `opened`, or has a pending write."""
    values = {
        "id": _require(_ID_RE, purchase_id, "purchase_id"),
        "holder": _require(_WORKER_RE, worker_id, "worker_id"),
        "expires_at": _bind_dt(expires_at),
        "next_poll_at": _bind_dt(next_poll_at),
        "incomplete_address": _address_text(incomplete_address),
    }
    if values["expires_at"] is None or values["next_poll_at"] is None:
        raise ValueError("expires_at and next_poll_at are required")
    if IS_POSTGRES:
        return _purchase(await database.fetch_one(_PARK_FOR_SHIPPING_ADDRESS_SQL, values))
    return _purchase(await database.fetch_one(_PARK_FOR_SHIPPING_ADDRESS_SQL_SQLITE, values))


# THE COMPARE-AND-SWAP. Only while the row waits (`opened`, a part of the contact empty, the deadline not
# passed, the contact never purged, no write pending), and only into an EMPTY part: a stored email (compared
# case-insensitively) or address that differs is never overwritten. Owner-scoped like every agent read.
# "Empty" is THE waiting rule's (a2a_shipping_address.awaits_contact): an address that lacks a required field
# counts as absent. The caller passes the incomplete address it read (:replaces_address), and only a row that
# still holds exactly that one may have it replaced (a compare-and-swap on the value, so a concurrent write
# of a complete address is never overwritten).
_SUBMIT_SHIPPING_ADDRESS_SQL = """
    UPDATE a2a_rail_purchases
       SET buyer_email = COALESCE(buyer_email, CAST(:buyer_email AS TEXT)),
           shipping_address = CASE WHEN shipping_address IS NULL OR shipping_address = CAST(:replaces_address AS JSONB)
                THEN COALESCE(CAST(:shipping_address AS JSONB), shipping_address) ELSE shipping_address END,
           last_error_code = CASE WHEN last_error_code = 'shipping_address_required'
                THEN NULL ELSE last_error_code END,
           buyer_action_expires_at = CASE WHEN buyer_action_kind IS NULL
                THEN NULL ELSE buyer_action_expires_at END,
           next_poll_at = clock_timestamp(),
           updated_at = clock_timestamp()
     WHERE id = :id AND agent_id = :agent_id AND agent_user_ref_hash = :agent_user_ref_hash
       AND state = 'opened' AND pending_write IS NULL AND contact_purged_at IS NULL
       AND (buyer_email IS NULL OR shipping_address IS NULL OR shipping_address = CAST(:replaces_address AS JSONB))
       AND (buyer_action_expires_at IS NULL OR buyer_action_expires_at > clock_timestamp())
       AND (buyer_email IS NOT NULL OR CAST(:email_sent AS TEXT) IS NOT NULL)
       AND ((shipping_address IS NOT NULL AND CAST(:replaces_sent AS TEXT) IS NULL)
            OR CAST(:address_sent AS TEXT) IS NOT NULL)
       AND (CAST(:email_sent AS TEXT) IS NULL OR buyer_email IS NULL
            OR lower(buyer_email) = lower(CAST(:buyer_email AS TEXT)))
       AND (CAST(:address_sent AS TEXT) IS NULL OR shipping_address IS NULL
            OR shipping_address = CAST(:shipping_address AS JSONB)
            OR shipping_address = CAST(:replaces_address AS JSONB))
    RETURNING *
"""
_SUBMIT_SHIPPING_ADDRESS_SQL_SQLITE = (
    _SUBMIT_SHIPPING_ADDRESS_SQL.replace("clock_timestamp()", "CURRENT_TIMESTAMP")
    .replace("CAST(:shipping_address AS JSONB)", ":shipping_address")
    .replace("CAST(:replaces_address AS JSONB)", ":replaces_address")
)


def _address_text(address: Optional[Mapping[str, Any]]) -> Optional[str]:
    """An address as the door stores it (create_purchase): one canonical text, so equal addresses compare
    equal on both dialects. None for none."""
    if address is None:
        return None
    if not isinstance(address, Mapping):
        raise ValueError("an address must be an object")
    return json.dumps(dict(address), sort_keys=True) if address else None


async def submit_shipping_address(
    purchase_id: str, agent_id: str, agent_user_ref_hash: str, *,
    buyer_email: Optional[str], shipping_address: Optional[Mapping[str, Any]],
    replaces_address: Optional[Mapping[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Fill the waiting row's empty email and/or address (the compare-and-swap above) and make it due now.
    `replaces_address`: the stored address the caller read when it lacks a required field
    (a2a_shipping_address.incomplete_address); it counts as empty, and is replaced only while the row still
    holds exactly it. None when the row is not this owner's, does not wait, or a part sent differs from the
    stored one: the caller re-reads to tell an identical resubmission (idempotent) from a conflict."""
    if not _ID_RE.match(str(purchase_id or "")):
        return None
    if buyer_email is None and not shipping_address:
        raise ValueError("nothing to submit")
    if shipping_address is not None and not isinstance(shipping_address, Mapping):
        raise ValueError("shipping_address must be an object")
    values = {
        "id": purchase_id,
        "agent_id": str(agent_id or "").strip(),
        "agent_user_ref_hash": str(agent_user_ref_hash or "").strip(),
        "buyer_email": (str(buyer_email).strip() or None) if buyer_email is not None else None,
        # The door's own encoding (create_purchase): one canonical text, so equal addresses compare equal.
        "shipping_address": json.dumps(dict(shipping_address), sort_keys=True) if shipping_address else None,
    }
    # Whether each part was sent, as its own TEXT parameter: one placeholder is never cast two ways.
    values["email_sent"] = "1" if values["buyer_email"] is not None else None
    values["address_sent"] = "1" if values["shipping_address"] is not None else None
    values["replaces_address"] = _address_text(replaces_address)
    values["replaces_sent"] = "1" if values["replaces_address"] is not None else None
    if IS_POSTGRES:
        return _purchase(await database.fetch_one(_SUBMIT_SHIPPING_ADDRESS_SQL, values))
    return _purchase(await database.fetch_one(_SUBMIT_SHIPPING_ADDRESS_SQL_SQLITE, values))


async def get_paying_checkout(purchase_id: str) -> Optional[Dict[str, Any]]:
    """ONE `paying` checkout row by id (the preview's read), or None. PII-free columns only."""
    if not _ID_RE.match(str(purchase_id or "")):
        return None
    row = await database.fetch_one(_GET_PAYING_CHECKOUT_SQL, {"id": purchase_id})
    if row is None:
        return None
    item = dict(row)
    for column in ("state_entered_at", "updated_at"):
        item[column] = _decode_dt(item.get(column))
    return item


async def list_paying_checkouts(*, limit: int = 50) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(_LIST_PAYING_CHECKOUTS_SQL, {"limit": _limit(limit, 500)})
    out = []
    for row in rows:
        item = dict(row)
        for column in ("state_entered_at", "updated_at"):
            item[column] = _decode_dt(item.get(column))
        out.append(item)
    return out


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

# THE OUTER WHERE RESTATES THE SUBQUERY'S WHOLE PREDICATE (state, pending write AND the clocks),
# here and in the expiry sweep. Under READ
# COMMITTED an UPDATE that waited on a row lock re-checks only its OWN qual against the row's new
# version; `id IN (subquery)` is already decided. Without the restatement a row released and
# re-claimed meanwhile loses its fresh lease here, and the sweep below expires a row whose worker
# recorded a pay write while the sweep waited (db/reap_agentic_ledger.py, the same guard).
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
       AND claimed_by IS NOT NULL
       AND claimed_at < clock_timestamp() - (:lease_seconds * INTERVAL '1 second')
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
       AND claimed_by IS NOT NULL
       AND claimed_at < datetime('now', '-' || :lease_seconds || ' seconds')
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
# it does (the worker learns it from a None transition). NEVER 'paying' (money may be moving), never a
# row with a pending write (a write whose outcome nobody has seen yet), and never a row parked on an
# answer that reported money moving (`money_result_seen`): that park is for a human, and expiring it
# would overwrite the only record that money may have moved.
_EXPIRE_SQL = """
    UPDATE a2a_rail_purchases
       SET state = 'expired',
           last_error_code = CASE WHEN buyer_action_expires_at IS NOT NULL
                AND buyer_action_expires_at < clock_timestamp() AND last_error_code = 'shipping_address_required'
                THEN 'shipping_address_expired'
                WHEN buyer_action_expires_at IS NOT NULL
                AND buyer_action_expires_at < clock_timestamp() THEN 'buyer_action_expired' ELSE 'stale' END,
           buyer_action_kind = NULL, buyer_action_url = NULL, buyer_action_expires_at = NULL,
           buyer_email = NULL, shipping_address = NULL,
           claimed_by = NULL, claimed_at = NULL,
           terminal_at = clock_timestamp(), state_entered_at = clock_timestamp(),
           updated_at = clock_timestamp()
     WHERE id IN (
            SELECT id FROM a2a_rail_purchases
             WHERE state IN ('opened', 'needs_payment_method', 'quoting', 'awaiting_authorization')
               AND pending_write IS NULL
               AND (last_error_code IS NULL OR last_error_code NOT LIKE '%money_result_seen%')
               AND ((buyer_action_expires_at IS NOT NULL AND buyer_action_expires_at < clock_timestamp())
                    OR state_entered_at < clock_timestamp() - (:max_age_seconds * INTERVAL '1 second')
                    OR created_at < clock_timestamp() - (:max_lifetime_seconds * INTERVAL '1 second'))
             ORDER BY state_entered_at
             LIMIT :limit
           )
       AND state IN ('opened', 'needs_payment_method', 'quoting', 'awaiting_authorization')
       AND pending_write IS NULL
       AND (last_error_code IS NULL OR last_error_code NOT LIKE '%money_result_seen%')
       AND ((buyer_action_expires_at IS NOT NULL AND buyer_action_expires_at < clock_timestamp())
            OR state_entered_at < clock_timestamp() - (:max_age_seconds * INTERVAL '1 second')
            OR created_at < clock_timestamp() - (:max_lifetime_seconds * INTERVAL '1 second'))
    RETURNING id
"""
_EXPIRE_SQL_SQLITE = """
    UPDATE a2a_rail_purchases
       SET state = 'expired',
           last_error_code = CASE WHEN buyer_action_expires_at IS NOT NULL
                AND buyer_action_expires_at < CURRENT_TIMESTAMP AND last_error_code = 'shipping_address_required'
                THEN 'shipping_address_expired'
                WHEN buyer_action_expires_at IS NOT NULL
                AND buyer_action_expires_at < CURRENT_TIMESTAMP THEN 'buyer_action_expired' ELSE 'stale' END,
           buyer_action_kind = NULL, buyer_action_url = NULL, buyer_action_expires_at = NULL,
           buyer_email = NULL, shipping_address = NULL,
           claimed_by = NULL, claimed_at = NULL,
           terminal_at = CURRENT_TIMESTAMP, state_entered_at = CURRENT_TIMESTAMP,
           updated_at = CURRENT_TIMESTAMP
     WHERE id IN (
            SELECT id FROM a2a_rail_purchases
             WHERE state IN ('opened', 'needs_payment_method', 'quoting', 'awaiting_authorization')
               AND pending_write IS NULL
               AND (last_error_code IS NULL OR last_error_code NOT LIKE '%money_result_seen%')
               AND ((buyer_action_expires_at IS NOT NULL AND buyer_action_expires_at < CURRENT_TIMESTAMP)
                    OR state_entered_at < datetime('now', '-' || :max_age_seconds || ' seconds')
                    OR created_at < datetime('now', '-' || :max_lifetime_seconds || ' seconds'))
             ORDER BY state_entered_at
             LIMIT :limit
           )
       AND state IN ('opened', 'needs_payment_method', 'quoting', 'awaiting_authorization')
       AND pending_write IS NULL
       AND (last_error_code IS NULL OR last_error_code NOT LIKE '%money_result_seen%')
       AND ((buyer_action_expires_at IS NOT NULL AND buyer_action_expires_at < CURRENT_TIMESTAMP)
            OR state_entered_at < datetime('now', '-' || :max_age_seconds || ' seconds')
            OR created_at < datetime('now', '-' || :max_lifetime_seconds || ' seconds'))
    RETURNING id
"""
#: Read from the OUTER predicate (the race guard); a test holds the inner list equal to it.
EXPIRE_SOURCE_STATES = _states_in(_EXPIRE_SQL, "AND state IN (")
EXPIRE_CANDIDATE_STATES = _states_in(_EXPIRE_SQL, "WHERE state IN (")
MAX_AGE_SECONDS_DEFAULT = 86400
#: An absolute ceiling for a purchase that never reached `paying`, measured from creation: the
#: re-quote edge resets `state_entered_at`, so without it a re-quoting row would never expire. Below
#: the contact retention, so a pre-pay row ends before its address is purged.
MAX_LIFETIME_SECONDS_DEFAULT = 2 * 86400


async def expire_overdue_purchases(
    *,
    max_age_seconds: int = MAX_AGE_SECONDS_DEFAULT,
    max_lifetime_seconds: int = MAX_LIFETIME_SECONDS_DEFAULT,
    limit: int = 100,
) -> int:
    values = {
        "max_age_seconds": _limit(max_age_seconds, 7 * 86400),
        "max_lifetime_seconds": _limit(max_lifetime_seconds, 7 * 86400),
        "limit": _limit(limit, 1000),
    }
    if IS_POSTGRES:
        rows = await database.fetch_all(_EXPIRE_SQL, values)
    else:
        rows = await database.fetch_all(_EXPIRE_SQL_SQLITE, values)
    return len(rows)


# ── contact retention ────────────────────────────────────────────────────────────────────────
#
# The buyer's email and address are needed only until the rail has the cart. Terminal writes NULL
# them, and the expiry sweep ends every pre-pay row within MAX_LIFETIME_SECONDS_DEFAULT; this purge is
# the BACKSTOP for what neither reaches (a long `paying`, a row parked on a money result, which the sweep
# never ends), in any state, without moving the state: such a row keeps its contact for up to
# CONTACT_RETENTION_SECONDS_DEFAULT (72 h). The purge runs inside the worker's tick AND, on its own
# clock with no dependency on the rail's dials, in jobs/a2a_rail_contact_purge (71 h, every 15 min, on by
# default, kill switch A2A_RAIL_CONTACT_PURGE_DISABLED): with the worker's dial off, that job still purges.
# Postgres skips rows another purge (the tick, the job, a run-now) has locked: two purges never wait on
# each other, and what one skips the other is already NULLing. A purged row cannot re-enter `quoting` (see the transition). Restated in the outer WHERE for the same reason as the sweeps above.
_PURGE_CONTACT_SQL = """
    UPDATE a2a_rail_purchases
       SET buyer_email = NULL, shipping_address = NULL, contact_purged_at = clock_timestamp(),
           updated_at = clock_timestamp()
     WHERE id IN (
            SELECT id FROM a2a_rail_purchases
             WHERE (buyer_email IS NOT NULL OR shipping_address IS NOT NULL)
               AND created_at < clock_timestamp() - (:max_age_seconds * INTERVAL '1 second')
             ORDER BY created_at
             LIMIT :limit
             FOR UPDATE SKIP LOCKED
           )
       AND (buyer_email IS NOT NULL OR shipping_address IS NOT NULL)
    RETURNING id
"""
_PURGE_CONTACT_SQL_SQLITE = """
    UPDATE a2a_rail_purchases
       SET buyer_email = NULL, shipping_address = NULL, contact_purged_at = CURRENT_TIMESTAMP,
           updated_at = CURRENT_TIMESTAMP
     WHERE id IN (
            SELECT id FROM a2a_rail_purchases
             WHERE (buyer_email IS NOT NULL OR shipping_address IS NOT NULL)
               AND created_at < datetime('now', '-' || :max_age_seconds || ' seconds')
             ORDER BY created_at
             LIMIT :limit
           )
       AND (buyer_email IS NOT NULL OR shipping_address IS NOT NULL)
    RETURNING id
"""
CONTACT_RETENTION_SECONDS_DEFAULT = 3 * 86400


async def purge_stale_contact(*, max_age_seconds: int = CONTACT_RETENTION_SECONDS_DEFAULT, limit: int = 200) -> int:
    """NULL the email and address of purchases opened more than `max_age_seconds` ago."""
    values = {"max_age_seconds": max(3600, _limit(max_age_seconds, 30 * 86400)), "limit": _limit(limit, 1000)}
    if IS_POSTGRES:
        rows = await database.fetch_all(_PURGE_CONTACT_SQL, values)
    else:
        rows = await database.fetch_all(_PURGE_CONTACT_SQL_SQLITE, values)
    return len(rows)


# ── the worker's heartbeat ───────────────────────────────────────────────────────────────────
#
# Stamped by jobs/a2a_rail_purchase_poll at the start of every tick that will step purchases (the job
# armed AND the rail client configured). Read by the create route: a purchase opened while nothing
# ticks never advances, never expires (the sweep runs in the tick) and never has its contact purged.
_RECORD_HEARTBEAT_SQL = """
    INSERT INTO a2a_rail_worker_heartbeat (id, ticked_at) VALUES (1, clock_timestamp())
    ON CONFLICT (id) DO UPDATE SET ticked_at = excluded.ticked_at
"""
_RECORD_HEARTBEAT_SQL_SQLITE = _RECORD_HEARTBEAT_SQL.replace("clock_timestamp()", "CURRENT_TIMESTAMP")
_WORKER_ALIVE_SQL = """
    SELECT 1 AS alive FROM a2a_rail_worker_heartbeat
     WHERE id = 1 AND ticked_at > clock_timestamp() - (:max_age_seconds * INTERVAL '1 second')
"""
_WORKER_ALIVE_SQL_SQLITE = """
    SELECT 1 AS alive FROM a2a_rail_worker_heartbeat
     WHERE id = 1 AND ticked_at > datetime('now', '-' || :max_age_seconds || ' seconds')
"""
#: How old the last tick may be for the worker to count as running. Above the job's longest interval
#: (A2A_RAIL_POLL_INTERVAL_SECONDS is clamped to 600) plus a tick's run deadline (300 s).
WORKER_HEARTBEAT_MAX_AGE_SECONDS = 900


async def record_worker_heartbeat() -> None:
    if IS_POSTGRES:
        await database.execute(_RECORD_HEARTBEAT_SQL)
    else:
        await database.execute(_RECORD_HEARTBEAT_SQL_SQLITE)


async def worker_is_alive(*, max_age_seconds: int = WORKER_HEARTBEAT_MAX_AGE_SECONDS) -> bool:
    """True when an armed worker tick started within `max_age_seconds`. Raises on a database error:
    the caller decides (the create route treats it as not alive)."""
    values = {"max_age_seconds": _limit(max_age_seconds, 7 * 86400)}
    if IS_POSTGRES:
        row = await database.fetch_one(_WORKER_ALIVE_SQL, values)
    else:
        row = await database.fetch_one(_WORKER_ALIVE_SQL_SQLITE, values)
    return row is not None


# ── the contact purge's heartbeat ──────────────────────────────────────────────────────────────
#
# Stamped by jobs/a2a_rail_contact_purge after every enabled run whose purge finished (a failed or
# killed run stamps nothing). Read by the create route beside the worker's heartbeat: no fresh stamp,
# no new A2A purchase, because nothing would delete its buyer's contact on time.
_RECORD_CONTACT_PURGE_HEARTBEAT_SQL = """
    INSERT INTO a2a_rail_contact_purge_heartbeat (id, ran_at) VALUES (1, clock_timestamp())
    ON CONFLICT (id) DO UPDATE SET ran_at = excluded.ran_at
"""
_RECORD_CONTACT_PURGE_HEARTBEAT_SQL_SQLITE = _RECORD_CONTACT_PURGE_HEARTBEAT_SQL.replace(
    "clock_timestamp()", "CURRENT_TIMESTAMP")
_CONTACT_PURGE_ALIVE_SQL = """
    SELECT 1 AS alive FROM a2a_rail_contact_purge_heartbeat
     WHERE id = 1 AND ran_at > clock_timestamp() - (:max_age_seconds * INTERVAL '1 second')
"""
_CONTACT_PURGE_ALIVE_SQL_SQLITE = """
    SELECT 1 AS alive FROM a2a_rail_contact_purge_heartbeat
     WHERE id = 1 AND ran_at > datetime('now', '-' || :max_age_seconds || ' seconds')
"""
#: How old the purge's last finished run may be for it to count as running. Two missed runs put three
#: 15-minute slots (2700 s) between stamps, and a run may start late (misfire grace) and take up to its
#: 120 s deadline: 3000 s keeps a healthy-but-late purge from closing A2A creates. Stalled or switched
#: off for longer and they close; the 71 h threshold leaves the hour before 72 h for what is open.
CONTACT_PURGE_HEARTBEAT_MAX_AGE_SECONDS = 3000


async def record_contact_purge_heartbeat() -> None:
    if IS_POSTGRES:
        await database.execute(_RECORD_CONTACT_PURGE_HEARTBEAT_SQL)
    else:
        await database.execute(_RECORD_CONTACT_PURGE_HEARTBEAT_SQL_SQLITE)


async def contact_purge_is_alive(*, max_age_seconds: int = CONTACT_PURGE_HEARTBEAT_MAX_AGE_SECONDS) -> bool:
    """True when an enabled contact purge run finished within `max_age_seconds`. Raises on a database
    error: the caller decides (the create route treats it as not alive)."""
    values = {"max_age_seconds": _limit(max_age_seconds, 7 * 86400)}
    if IS_POSTGRES:
        row = await database.fetch_one(_CONTACT_PURGE_ALIVE_SQL, values)
    else:
        row = await database.fetch_one(_CONTACT_PURGE_ALIVE_SQL_SQLITE, values)
    return row is not None


# ── a challenge while paying ─────────────────────────────────────────────────────────────────
#
# The merchant may challenge the payment (3DS) while the rail's agent is placing the order. The buyer
# must open a page, but the purchase stays in `paying`: money may be moving, so it must not go back
# to a state the sweep can expire. Fenced on the claim and on `paying`.
_SET_CHALLENGE_SQL = """
    UPDATE a2a_rail_purchases
       SET buyer_action_kind = CASE WHEN CAST(:url AS TEXT) IS NULL THEN NULL ELSE 'authenticate' END,
           buyer_action_url = CAST(:url AS TEXT),
           buyer_action_expires_at = CAST(:expires_at AS TIMESTAMPTZ),
           updated_at = clock_timestamp()
     WHERE id = :id AND claimed_by = :holder AND state = 'paying'
    RETURNING *
"""
_SET_CHALLENGE_SQL_SQLITE = """
    UPDATE a2a_rail_purchases
       SET buyer_action_kind = CASE WHEN CAST(:url AS TEXT) IS NULL THEN NULL ELSE 'authenticate' END,
           buyer_action_url = CAST(:url AS TEXT),
           buyer_action_expires_at = :expires_at,
           updated_at = CURRENT_TIMESTAMP
     WHERE id = :id AND claimed_by = :holder AND state = 'paying'
    RETURNING *
"""


async def set_payment_challenge(
    purchase_id: str, worker_id: str, *, url: Optional[str], expires_at: Any = None
) -> Optional[Dict[str, Any]]:
    """Show the buyer the merchant's challenge page (`url`), or clear it (`url=None`). None when the
    worker does not hold the row or it is not `paying`."""
    if url is not None and isinstance(expires_at, datetime):
        aware = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
        if aware <= datetime.now(timezone.utc):
            raise ValueError("expires_at must be in the future")
    bound = _bind_dt(expires_at) if url is not None else None
    values = {
        "id": purchase_id,
        "holder": _require(_WORKER_RE, worker_id, "worker_id"),
        "url": _require_https_url(url, "url", required=False),
        "expires_at": bound,
    }
    if IS_POSTGRES:
        return _purchase(await database.fetch_one(_SET_CHALLENGE_SQL, values))
    return _purchase(await database.fetch_one(_SET_CHALLENGE_SQL_SQLITE, values))


# ── a voided token ───────────────────────────────────────────────────────────────────────────
_FORGET_TOKEN_SQL = """
    UPDATE a2a_rail_purchases
       SET payment_token_id = NULL, updated_at = clock_timestamp()
     WHERE id = :id AND claimed_by = :holder AND payment_token_id = :payment_token_id
       AND state IN ('quoting', 'awaiting_authorization')
       AND pending_write IS NULL
    RETURNING *
"""
_FORGET_TOKEN_SQL_SQLITE = _FORGET_TOKEN_SQL.replace("clock_timestamp()", "CURRENT_TIMESTAMP")


async def forget_voided_token(purchase_id: str, worker_id: str, payment_token_id: str) -> Optional[Dict[str, Any]]:
    """Drop the token id AFTER the worker has voided that token on the rail (and seen the answer), so
    a re-quote may proceed. Names the token it forgets; None when it does not match, the worker does
    not hold the row, a write is pending, or the purchase is past authorization."""
    values = {
        "id": purchase_id,
        "holder": _require(_WORKER_RE, worker_id, "worker_id"),
        "payment_token_id": _rail_ref(_require(_RAIL_REF_RE, payment_token_id, "payment_token_id"), "payment_token_id"),
    }
    if IS_POSTGRES:
        return _purchase(await database.fetch_one(_FORGET_TOKEN_SQL, values))
    return _purchase(await database.fetch_one(_FORGET_TOKEN_SQL_SQLITE, values))


# ── tokens left on ended purchases ───────────────────────────────────────────────────────────
#
# A purchase that ended without paying (failed, refused, expired) and still names a token: that token is
# live on the rail until it is voided or lapses. A completed purchase's token paid: not an orphan.
#: Only purchases that ended within ORPHAN_MAX_AGE_SECONDS: a token outlives its purchase by at most the
#: rail's own token lifetime (an unbounded authorisation is 30 days), so an older one has lapsed and is
#: neither voided nor counted (the hourly warning does not fire for ever).
_LIST_ORPHAN_TOKENS_SQL = """
    SELECT id, state, member_id, payment_token_id, terminal_at FROM a2a_rail_purchases
     WHERE state IN ('failed', 'refused', 'expired')
       AND payment_token_id IS NOT NULL
       AND terminal_at > clock_timestamp() - (:max_age_seconds * INTERVAL '1 second')
       AND NOT (COALESCE(last_error_code, '') = 'operator_confirmed_declined'
                AND terminal_at > clock_timestamp() - (:declined_hold_seconds * INTERVAL '1 second'))
     ORDER BY terminal_at, id
     LIMIT :limit
"""
_LIST_ORPHAN_TOKENS_SQL_SQLITE = """
    SELECT id, state, member_id, payment_token_id, terminal_at FROM a2a_rail_purchases
     WHERE state IN ('failed', 'refused', 'expired')
       AND payment_token_id IS NOT NULL
       AND terminal_at > datetime('now', '-' || :max_age_seconds || ' seconds')
       AND NOT (COALESCE(last_error_code, '') = 'operator_confirmed_declined'
                AND terminal_at > datetime('now', '-' || :declined_hold_seconds || ' seconds'))
     ORDER BY terminal_at, id
     LIMIT :limit
"""
_COUNT_ORPHAN_TOKENS_SQL = """
    SELECT COUNT(*) AS n FROM a2a_rail_purchases
     WHERE state IN ('failed', 'refused', 'expired')
       AND payment_token_id IS NOT NULL
       AND terminal_at > clock_timestamp() - (:max_age_seconds * INTERVAL '1 second')
"""
_COUNT_ORPHAN_TOKENS_SQL_SQLITE = """
    SELECT COUNT(*) AS n FROM a2a_rail_purchases
     WHERE state IN ('failed', 'refused', 'expired')
       AND payment_token_id IS NOT NULL
       AND terminal_at > datetime('now', '-' || :max_age_seconds || ' seconds')
"""
ORPHAN_MAX_AGE_SECONDS = 31 * 86400
#: A purchase a human closed as a provider-confirmed decline (`operator_confirmed_declined`) keeps its token
#: OFF the orphan list (so never voided or forgotten) for this long after it ended: a late authorisation or a
#: dispute can still be matched to a live token record first. The count below still counts it.
ORPHAN_DECLINED_HOLD_SECONDS = 72 * 3600
#: Fenced on the token it names and on the row having ended: a voided token is forgotten only where it
#: was an orphan. `last_error_code` is left alone (it is what the agent was told about the purchase).
_FORGET_ORPHAN_TOKEN_SQL = """
    UPDATE a2a_rail_purchases
       SET payment_token_id = NULL, updated_at = clock_timestamp()
     WHERE id = :id AND payment_token_id = :payment_token_id
       AND state IN ('failed', 'refused', 'expired')
    RETURNING id
"""
_FORGET_ORPHAN_TOKEN_SQL_SQLITE = _FORGET_ORPHAN_TOKEN_SQL.replace("clock_timestamp()", "CURRENT_TIMESTAMP")
ORPHAN_TOKEN_STATES = _states_in(_LIST_ORPHAN_TOKENS_SQL, "WHERE state IN (")


async def list_orphan_tokens(*, limit: int = 10) -> List[Dict[str, Any]]:
    """Ended purchases that still name a token, oldest end first. Worker-only (carries the member id)."""
    values = {"limit": _limit(limit, 500), "max_age_seconds": ORPHAN_MAX_AGE_SECONDS,
              "declined_hold_seconds": ORPHAN_DECLINED_HOLD_SECONDS}
    if IS_POSTGRES:
        rows = await database.fetch_all(_LIST_ORPHAN_TOKENS_SQL, values)
    else:
        rows = await database.fetch_all(_LIST_ORPHAN_TOKENS_SQL_SQLITE, values)
    return [_purchase(row) for row in rows]


async def count_orphan_tokens() -> int:
    values = {"max_age_seconds": ORPHAN_MAX_AGE_SECONDS}
    if IS_POSTGRES:
        row = await database.fetch_one(_COUNT_ORPHAN_TOKENS_SQL, values)
    else:
        row = await database.fetch_one(_COUNT_ORPHAN_TOKENS_SQL_SQLITE, values)
    return int(row["n"]) if row else 0


async def forget_orphan_token(purchase_id: str, payment_token_id: str) -> Optional[Dict[str, Any]]:
    """Drop the token id of an ENDED purchase after its token was voided on the rail. None when the row
    has not ended, names another token, or does not exist."""
    values = {"id": purchase_id, "payment_token_id": _require(_RAIL_REF_RE, payment_token_id, "payment_token_id")}
    if IS_POSTGRES:
        return _purchase(await database.fetch_one(_FORGET_ORPHAN_TOKEN_SQL, values))
    return _purchase(await database.fetch_one(_FORGET_ORPHAN_TOKEN_SQL_SQLITE, values))


# ── a human's close of a provider-confirmed decline (append-only audit; migration 296) ─────────
#
# ONE caller: services/payment_orchestration/a2a_rail_decline_close.py (jobs/a2a_rail_operator.py
# `close-declined`). It ends ONE `paying` row held as `checkout_payment_reached_declined` (or, with a bound
# lookup that agrees, the legacy `checkout_payment_reached_unconfirmed`) as `failed` /
# `operator_confirmed_declined`, and records why in `a2a_rail_manual_resolution_audit`, in ONE statement on Postgres.
# The audit row is append-only in the database (a trigger refuses UPDATE, DELETE and TRUNCATE). It holds no
# statement text, no card data, no contact, no token id: an opaque reference, hashes, the operator handle,
# times and the two attestations.
MANUAL_RESOLUTION_AUDIT_TABLE = "a2a_rail_manual_resolution_audit"
MANUAL_RESOLUTION_AUDIT_TRIGGER = "a2a_rail_manual_resolution_audit_immutable"
MANUAL_RESOLUTION_AUDIT_FUNCTION = "prevent_a2a_rail_manual_resolution_audit_mutation"
#: pg_trigger.tgtype event bits (TRIGGER_TYPE_DELETE 8 | UPDATE 16 | TRUNCATE 32): migration 296's trigger
#: fires on all three, and the gate requires all three.
_AUDIT_TRIGGER_EVENT_BITS = 8 | 16 | 32
# NO STARTUP HEAL, ON PURPOSE. Production does not run db/migrations/ at boot, and migration 296 is applied
# there once, as a gated one-off after its PR has merged: nothing in this module or db/schema_guard creates
# the table, its function or its trigger, so a deploy never builds them ahead of that run. Until it has run,
# `manual_resolution_audit_ready` answers False and the close refuses (`manual_resolution_audit_not_ready`).
# tests/test_a2a_rail_decline_close.py pins that no startup path creates any of the three.


async def manual_resolution_audit_ready() -> bool:
    """The audit table exists AND carries its append-only trigger(s) (migration 296 on Postgres; the test
    arm's own builder on SQLite). The close refuses otherwise.

    On Postgres the trigger must also FIRE in an ordinary session: `tgenabled` 'O' (origin, the default) or
    'A' (always). 'D' (ALTER TABLE ... DISABLE TRIGGER) and 'R' (replica-only) leave the audit row writable by
    UPDATE/DELETE/TRUNCATE, so they read as not ready. It must also be migration 296's guard, not only its name:
    it runs the append-only function and fires on UPDATE, DELETE and TRUNCATE. SQLite has no disabled
    trigger: present = enforced."""
    if IS_POSTGRES:
        row = await database.fetch_one(
            "SELECT to_regclass(:t) IS NOT NULL AS has_table, EXISTS (SELECT 1 FROM pg_trigger t "
            "JOIN pg_proc p ON p.oid = t.tgfoid "
            "WHERE t.tgrelid = to_regclass(:t) AND t.tgname = :g AND NOT t.tgisinternal "
            "AND t.tgenabled IN ('O', 'A') AND p.proname = :f "
            "AND (CAST(t.tgtype AS INTEGER) & :events) = :events) AS has_trigger",
            {"t": MANUAL_RESOLUTION_AUDIT_TABLE, "g": MANUAL_RESOLUTION_AUDIT_TRIGGER,
             "f": MANUAL_RESOLUTION_AUDIT_FUNCTION, "events": _AUDIT_TRIGGER_EVENT_BITS})
        return bool(row and row["has_table"] and row["has_trigger"])
    names = {r["name"] for r in await database.fetch_all(
        "SELECT name FROM sqlite_master WHERE (type = 'table' AND name = :t) OR (type = 'trigger' AND tbl_name = :t)",
        {"t": MANUAL_RESOLUTION_AUDIT_TABLE})}
    return {MANUAL_RESOLUTION_AUDIT_TABLE, f"{MANUAL_RESOLUTION_AUDIT_TRIGGER}_no_update",
            f"{MANUAL_RESOLUTION_AUDIT_TRIGGER}_no_delete"} <= names


_AUDIT_INSERT_COLUMNS = (
    "purchase_id", "from_state", "from_code", "from_state_entered_at", "resolved_state", "resolution_code",
    "provider_status", "operator_ref", "evidence_source", "evidence_reference", "evidence_file_sha256",
    "evidence_observed_at", "provider_reference_kind", "provider_reference", "attests_no_authorization",
    "attests_no_merchant_order", "lookup_ran", "lookup_agreed", "provider_origin", "evidence_sha256",
)

#: The candidate, by id (no lock). The decision (a2a_rail_decline_close.decide) runs on what this returns; the
#: write then fences on the SAME stored values (`_raw_fence`), so a row that moved in between is not touched.
_GET_DECLINE_CANDIDATE_SQL = """
    SELECT id, state, last_error_code, state_entered_at, payment_token_id, checkout_task_id, order_id,
           pending_write, claimed_by, terminal_at
      FROM a2a_rail_purchases WHERE id = :id
"""
#: The fenced terminal write. NO `claimed_by` fence: a held payment-reached row is claimed and released every
#: POLL_PARKED by the worker, which only ever releases it (its release is fenced `claimed_by = :worker_id`, so
#: it writes nothing once this NULLs the claim). Fenced on what only a real transition changes.
_CLOSE_DECLINED_SET_AND_FENCE = """
       SET state = 'failed',
           last_error_code = 'operator_confirmed_declined',
           pending_write = NULL,
           pending_write_at = NULL,
           buyer_action_kind = NULL,
           buyer_action_url = NULL,
           buyer_action_expires_at = NULL,
           buyer_email = NULL,
           shipping_address = NULL,
           claimed_by = NULL,
           claimed_at = NULL,
           terminal_at = clock_timestamp(),
           state_entered_at = clock_timestamp(),
           updated_at = clock_timestamp()
     WHERE id = :id
       AND state = 'paying'
       AND last_error_code = :expected_code
       AND state_entered_at = CAST(:fence_state_entered_at AS TIMESTAMPTZ)
       AND payment_token_id = :fence_payment_token_id
       AND checkout_task_id = :fence_checkout_task_id
       AND order_id IS NOT DISTINCT FROM CAST(:fence_order_id AS VARCHAR)
       AND pending_write IS NOT DISTINCT FROM CAST(:fence_pending_write AS VARCHAR)
       AND state_entered_at < clock_timestamp() - (:min_age_seconds * INTERVAL '1 second')
"""
_AUDIT_CASTS = {
    "from_state_entered_at": "TIMESTAMPTZ", "evidence_observed_at": "TIMESTAMPTZ",
    "attests_no_authorization": "BOOLEAN", "attests_no_merchant_order": "BOOLEAN",
    "lookup_ran": "BOOLEAN", "lookup_agreed": "BOOLEAN",
}
#: Postgres: ONE statement, so the transition and its audit row land together or not at all (a moved row
#: returns nothing from `moved`, and the INSERT then inserts nothing).
_CLOSE_DECLINED_SQL = (
    "WITH moved AS (UPDATE a2a_rail_purchases" + _CLOSE_DECLINED_SET_AND_FENCE + " RETURNING id) "
    f"INSERT INTO {MANUAL_RESOLUTION_AUDIT_TABLE} ({', '.join(_AUDIT_INSERT_COLUMNS)}) SELECT moved.id, "
    + ", ".join(f"CAST(:a_{c} AS {_AUDIT_CASTS.get(c, 'VARCHAR')})" for c in _AUDIT_INSERT_COLUMNS[1:])
    + " FROM moved RETURNING purchase_id"
)
#: SQLite (the test arm only; production is Postgres) has no data-modifying CTE: the fenced UPDATE, then the
#: INSERT, in sequence.
_CLOSE_DECLINED_UPDATE_SQL_SQLITE = (
    ("UPDATE a2a_rail_purchases" + _CLOSE_DECLINED_SET_AND_FENCE + " RETURNING id")
    .replace("clock_timestamp() - (:min_age_seconds * INTERVAL '1 second')",
             "datetime('now', '-' || :min_age_seconds || ' seconds')")
    .replace("clock_timestamp()", "CURRENT_TIMESTAMP")
    .replace("CAST(:fence_state_entered_at AS TIMESTAMPTZ)", ":fence_state_entered_at")
    .replace("CAST(:fence_order_id AS VARCHAR)", ":fence_order_id")
    .replace("CAST(:fence_pending_write AS VARCHAR)", ":fence_pending_write")
    .replace("IS NOT DISTINCT FROM", "IS")
)
_INSERT_MANUAL_RESOLUTION_AUDIT_SQL_SQLITE = (
    f"INSERT INTO {MANUAL_RESOLUTION_AUDIT_TABLE} ({', '.join(_AUDIT_INSERT_COLUMNS)}) "
    f"VALUES ({', '.join(':a_' + c for c in _AUDIT_INSERT_COLUMNS)})"
)
_GET_MANUAL_RESOLUTION_SQL = f"SELECT * FROM {MANUAL_RESOLUTION_AUDIT_TABLE} WHERE purchase_id = :id"


async def get_manual_resolution(purchase_id: str) -> Optional[Dict[str, Any]]:
    if not _ID_RE.match(str(purchase_id or "")):
        return None
    row = await database.fetch_one(_GET_MANUAL_RESOLUTION_SQL, {"id": purchase_id})
    return dict(row) if row is not None else None


async def get_decline_candidate(purchase_id: str) -> Optional[Dict[str, Any]]:
    """The close's read of ONE row by id. Decoded columns for the decision, plus `_raw_fence`: the STORED
    values the write fences on (exact on both dialects). Carries the token id and the task id for the
    CALLER'S comparisons only; the caller never prints either."""
    if not _ID_RE.match(str(purchase_id or "")):
        return None
    row = await database.fetch_one(_GET_DECLINE_CANDIDATE_SQL, {"id": purchase_id})
    if row is None:
        return None
    raw = dict(row)
    out = _purchase(dict(raw))
    out["_raw_fence"] = {k: raw[k] for k in ("state_entered_at", "payment_token_id", "checkout_task_id",
                                             "order_id", "pending_write")}
    return out


async def close_declined(purchase_id: str, *, expected_code: str, fence: Mapping[str, Any],
                         audit: Mapping[str, Any], min_age_seconds: int) -> bool:
    """End ONE held row as `failed` / `operator_confirmed_declined` and record `audit`, fenced on `fence`
    (get_decline_candidate's `_raw_fence`). True when written; False when the row moved (nothing written).
    One caller: a2a_rail_decline_close.close, after its decision."""
    if isinstance(min_age_seconds, bool) or not isinstance(min_age_seconds, int) or min_age_seconds < 1800:
        raise ValueError("min_age_seconds must be at least 1800")
    values: Dict[str, Any] = {
        "id": _require(_ID_RE, purchase_id, "purchase_id"),
        "expected_code": _require(_CODE_RE, expected_code, "expected_code"),
        "min_age_seconds": min_age_seconds,
    }
    for key in ("state_entered_at", "payment_token_id", "checkout_task_id", "order_id", "pending_write"):
        values[f"fence_{key}"] = fence[key]
    if not values["fence_payment_token_id"] or not values["fence_checkout_task_id"]:
        raise ValueError("fence_incomplete")
    record = {c: audit[c] for c in _AUDIT_INSERT_COLUMNS}
    if record["purchase_id"] != values["id"]:
        raise ValueError("audit_purchase_mismatch")
    for column in ("from_state_entered_at", "evidence_observed_at"):
        record[column] = _bind_dt(record[column])
    if IS_POSTGRES:
        values.update({f"a_{c}": record[c] for c in _AUDIT_INSERT_COLUMNS[1:]})
        return await database.fetch_one(_CLOSE_DECLINED_SQL, values) is not None
    moved = await database.fetch_one(_CLOSE_DECLINED_UPDATE_SQL_SQLITE, values)
    if moved is None:
        return False
    await database.execute(_INSERT_MANUAL_RESOLUTION_AUDIT_SQL_SQLITE, {f"a_{c}": record[c] for c in _AUDIT_INSERT_COLUMNS})
    return True
