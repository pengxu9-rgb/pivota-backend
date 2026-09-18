"""Persistence for the Reap AGENTIC rail (migration 224): purchases + enrollments.

Two tables, one job each:

  reap_agentic_enrollments — the buyer's own card, enrolled once on Reap's hosted page.
                             Long-lived, at most ONE 'active' row per buyer_ref.
  reap_agentic_purchases   — one row per purchase attempt, a state machine in a column.

This module is the WHOLE data layer for that rail. There is no HTTP client here, no route, no
scheduler job and no state-machine service — those are later work packages, and the shapes they
need are the ones this file exports: `transition` for the service, `claim_due_purchases` /
`release_claim` / `requeue_stale_claims` for the poller, `get_purchase_for_owner` /
`list_purchases_for_owner` for the routes.

── FIVE PROPERTIES OF THIS DRIVER THAT SHAPED EVERY STATEMENT BELOW ─────────────────────────

1. `execute()` OF AN UPDATE RETURNS NO ROWCOUNT ON POSTGRES. databases==0.7.0's asyncpg backend
   implements `execute` as `fetchval`, which yields None for an UPDATE with no RETURNING —
   success and no-match alike. That is not a detail: every conditional update in this file is
   ALSO the concurrency guard, and a guard whose answer is a constant None guards nothing.
   (db/agent_issued_cards.mark_revoked shipped exactly that bug.) So every UPDATE here is
   `fetch_one(... RETURNING *)` or `fetch_all(... RETURNING id)`, and "did anything change?" is
   "did a row come back?".

2. A RAW-SQL jsonb COLUMN COMES BACK AS A STRING. No SQLAlchemy bind/result processor runs on a
   raw statement, so asyncpg hands back the jsonb text verbatim. `_normalize_row` decodes it —
   callers get a dict/list, on both dialects.

3. `CAST(:x AS JSONB)` IS POISON ON SQLITE. SQLite resolves the unknown type name JSONB to
   NUMERIC affinity, so `CAST('{"a":1}' AS JSONB)` silently stores 0 and the payload is gone
   without an error. Hence the dialect splits below — Postgres casts, SQLite must not.

4. A NAIVE DATETIME BINDS WITH THE CLIENT PROCESS TIMEZONE. asyncpg encodes a naive datetime for
   a timestamptz parameter as local wall time, local to THIS PROCESS — measured seven hours off
   from a Pacific box against a UTC database. So: every cutoff in this file is computed
   SERVER-SIDE (`CURRENT_TIMESTAMP - interval`), and every datetime a caller passes goes through
   `_bind_dt`, which makes it timezone-aware UTC.

5. THE DIALECT SPLITS ARE MODULE-LEVEL CONSTANTS, NOT f-STRINGS BUILT AT CALL TIME.
   tests/test_repo_sql_prepare_postgres.py resolves a `database.*` first argument only when it
   is a literal or a MODULE-level name. SQL assembled at call time is invisible to it, and
   statements on a live path that nothing ever PREPAREs are how #1588 reached production.
   The state vocabularies inside those statements are therefore literal too, and the Python
   tuples are parsed back OUT of the SQL — see `_states_in` for why that direction.

6. THIS MODULE OPENS NO TRANSACTIONS, AND THE CUTOFFS DEPEND ON THAT. On Postgres
   `CURRENT_TIMESTAMP` is TRANSACTION-start time, not wall-clock time: inside a long
   caller-supplied transaction it FREEZES, so a poll loop that ran inside one would compare
   every row against the clock as it was when the transaction began and either sweep nothing or
   sweep everything. Every function here runs in autocommit — `mark_enrollment_active` held the
   last `database.transaction()` and no longer does, for reasons of its own — so the two are
   equivalent today. The cutoffs still use `clock_timestamp()` on Postgres rather than rely on
   that: it reads the wall clock at STATEMENT time, so a future caller that wraps one of these
   in a transaction cannot silently freeze it. SQLite has no equivalent and keeps
   CURRENT_TIMESTAMP, which is statement-time there anyway.

── WHY `transition` IS ONE STATEMENT ────────────────────────────────────────────────────────

Read-then-write cannot be made safe here without a lock we may not have. `UPDATE ... WHERE
id = :id AND state IN (...) RETURNING *` is atomic on both engines: two workers advancing the
same purchase from the same state produce exactly one non-empty result, and the loser gets None
— which is not an error, it is the answer "somebody else moved this row first". Every caller
reads that None as "re-read and reconsider", never as a failure.

The optional fields are `COALESCE(:param, column)` in ONE static statement rather than an
assignment list built from whichever keyword arguments were not None. Same contract (None means
"leave this column alone"), but the SQL is a constant, which is what property 5 requires.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence

from db.database import IS_POSTGRES, database

# The shared minor-unit converter. The rationale lives on `amount_minor_or_none` below, which is
# the only thing that calls it; services/reap_webhooks imports nothing from db/, so this is not
# a cycle (unlike db/agent_card_auth_decisions.py's lazy import of services.reap_external_auth).
from services.reap_webhooks import major_to_minor

# `get_purchase_internal` is deliberately ABSENT: it is the unscoped, unredacted read, and
# leaving it out of the public surface is half of what stops a route reaching for it. The other
# half is its name.
__all__ = [
    "ALLOWED_TRANSITIONS",
    "PURCHASE_STATES",
    "TERMINAL_STATES",
    "amount_minor_or_none",
    "public_purchase_view",
    "PUBLIC_PURCHASE_COLUMNS",
    "create_purchase",
    "get_purchase_for_owner",
    "list_purchases_for_owner",
    "transition",
    "transition_as_holder",
    "claim_due_purchases",
    "release_claim",
    "requeue_stale_claims",
    "expire_overdue_purchases",
    "fail_exhausted_purchases",
    "upsert_pending_enrollment",
    "mark_enrollment_active",
    "mark_enrollment_dead",
    "get_active_enrollment",
]


def amount_minor_or_none(value: Any, currency: str) -> Optional[int]:
    """Convert a MAJOR-unit amount to the integer MINOR units this ledger's columns hold.

    REUSED, NOT REIMPLEMENTED. `services.reap_webhooks.major_to_minor` is already the one place
    this repo turns a decimal major-unit amount into integer minor units; it already REFUSES
    rather than rounds (NaN, inf, a binary float, a negative, zero, an amount with more decimals
    than the currency has, or one over the ceiling all come back None) and it already knows the
    zero-decimal currencies. A second implementation would be a second rounding policy, and the
    rounding policy is the entire point of the function.

    IT RETURNS Optional[int] — None IS THE REFUSAL — where this package's brief asked for
    `-> int`. The repo's contract wins: None is what every existing caller on this rail checks
    for, and raising here would make this module the odd one out. Callers must handle None.

    WHY A NAMED WRAPPER rather than a bare re-export. The first cut of this module re-exported
    `major_to_minor`, listed it in `__all__`, and called it from nowhere — decoration that reads
    as an endorsed entry point. This has the caller the ledger actually needs (`our_price_minor`,
    `quoted_total_minor`, `final_total_minor` and `shipping_minor`/`tax_minor` are all BIGINT
    minor units, and every one of them arrives from an upstream decimal), and it has tests. The
    name also says what the None means, which the upstream name does not.
    """
    return major_to_minor(value, currency)


# ── the state machine ────────────────────────────────────────────────────────────────────────

TERMINAL_STATES: FrozenSet[str] = frozenset({"completed", "failed", "refused", "expired"})

# The ONE map. `transition` refuses an illegal pair BEFORE any SQL runs, so an illegal advance is
# a ValueError in the caller's stack trace rather than a row that quietly did not move (which is
# indistinguishable from losing a race, and would therefore be retried forever).
#
# A terminal state maps to the empty set, not to a missing key: "this state exists and permits
# nothing" is a different statement from "I have never heard of this state", and only the first
# one is true of 'completed'.
ALLOWED_TRANSITIONS: Dict[str, FrozenSet[str]] = {
    "resolving": frozenset({"needs_enrollment", "quoting", "refused", "failed"}),
    "needs_enrollment": frozenset({"quoting", "expired", "failed"}),
    "quoting": frozenset({"awaiting_approval", "refused", "failed"}),
    "awaiting_approval": frozenset({"processing", "completed", "failed", "expired"}),
    "processing": frozenset({"completed", "failed"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "refused": frozenset(),
    "expired": frozenset(),
}

PURCHASE_STATES: FrozenSet[str] = frozenset(ALLOWED_TRANSITIONS)

def _states_in(sql: str, anchor: str = "state IN (") -> tuple:
    """Parse the literal state list out of a SQL statement, after `anchor`.

    THE DIRECTION OF THIS DERIVATION IS DELIBERATE, AND IT IS THE OPPOSITE OF THE OBVIOUS ONE.
    The obvious design builds each statement's `IN` list by interpolating a Python tuple; that
    makes the tuple the single source of truth, and it makes every statement an f-string. An
    f-string is INVISIBLE to tests/test_repo_sql_prepare_postgres.py, which resolves a
    `database.*` first argument only when it is a literal or a module-level name bound to one —
    so the whole poller path would ship unplanned, which is the #1588 shape this repo already
    paid for once.

    So the SQL keeps its literal list and the Python constants are parsed OUT of it. There is
    still exactly one source of truth, it is just the statement rather than the tuple, and the
    statements stay PREPARE-visible. This matters: while the constants were written out
    separately, `test_the_bulk_sweeps_only_use_legal_edges` was watching a tuple the SQL never
    read, and adding 'processing' to the expire sweep's IN list — expiring a purchase the buyer
    had ALREADY APPROVED — survived both dialects.
    """
    start = sql.index(anchor) + len(anchor)
    end = sql.index(")", start)
    return tuple(re.findall(r"'([a-z_]+)'", sql[start:end]))

# The `state IN (...)` list in _TRANSITION_SQL is FIXED WIDTH — nine binds, one per state in the
# machine, so the statement can be a module-level constant (property 5) while `from_states`
# stays a caller-chosen set. Short sets are padded by repeating their first element, which
# cannot widen the match. Nine is the number of states, so it can never overflow.
_FROM_SLOTS = 9

# Columns `transition` will write through COALESCE. An allowlist, not `**fields` interpolated
# into SQL: the second form is both a static-analysis blind spot and an injection surface, and
# this way an unknown field name is a TypeError at the call site instead of a silently dropped
# write.
_TRANSITION_FIELDS: Sequence[str] = (
    "enrollment_id",
    "merchant_domain",
    "product_key",
    "variant_key",
    "product_name",
    "variant_title",
    "brand",
    "category",
    "quantity",
    "currency",
    "our_price_minor",
    "click_id",
    "return_url",
    "reap_product_id",
    "reap_variant_id",
    "reap_quote_id",
    "reap_quote_expires_at",
    "reap_checkout_id",
    "reap_order_id",
    "quoted_total_minor",
    "final_total_minor",
    "shipping_minor",
    "tax_minor",
    "hosted_url",
    "hosted_url_expires_at",
    "refusal_reason",
    "queries_tried",
    "last_error_code",
    "next_poll_at",
    "shipping_address",
    "buyer_email",
)

# Columns stored as jsonb on Postgres / TEXT-holding-JSON on SQLite. Encoded on the way in,
# decoded on the way out — see properties 2 and 3.
_JSON_COLUMNS = ("queries_tried", "shipping_address")

_PURCHASE_TS_COLUMNS = (
    "state_entered_at",
    "reap_quote_expires_at",
    "hosted_url_expires_at",
    "claimed_at",
    "next_poll_at",
    "created_at",
    "updated_at",
    "terminal_at",
)
_ENROLLMENT_TS_COLUMNS = ("hosted_url_expires_at", "created_at", "updated_at")

# The format SQLite's own CURRENT_TIMESTAMP emits. Binding datetimes in the SAME text format the
# server writes is what makes `next_poll_at <= CURRENT_TIMESTAMP` a correct comparison on SQLite
# rather than a lexicographic accident: sqlite3's default datetime adapter appends "+00:00",
# which sorts AFTER every server-written timestamp of the same second.
_SQLITE_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


# ── value coercion ───────────────────────────────────────────────────────────────────────────


def _bind_dt(value: Any) -> Any:
    """Make a datetime safe to bind on either engine.

    NAIVE IN MEANS UTC ASSUMED, and the `.replace(tzinfo=utc)` below is the whole of that rule.
    Without it the `.astimezone(utc)` on the next line reads a naive datetime as LOCAL time, so
    the same value written from a Pacific box and a UTC box lands eight hours apart — on
    next_poll_at that is a poll that fires eight hours early or late, and on
    reap_quote_expires_at it is a quote we believe is valid after Reap has expired it. asyncpg
    applies the same local-time reading to a naive bind for a timestamptz parameter, so the
    defect exists on both engines by two different routes and this one line closes both.
    tests/test_reap_agentic_ledger.py::test_a_naive_datetime_is_read_as_utc_not_as_local_time
    runs under a shifted process timezone and kills the removal of it.

    On SQLite the result becomes the server's own text format so comparisons against
    CURRENT_TIMESTAMP are exact rather than lexicographic luck.
    """
    if value is None or not isinstance(value, datetime):
        return value
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    if IS_POSTGRES:
        return aware.astimezone(timezone.utc)
    return aware.astimezone(timezone.utc).strftime(_SQLITE_TS_FORMAT)


def _bind_json(value: Any) -> Optional[str]:
    """Serialize on the way in. A raw dict bound through raw SQL reaches Postgres as a dict with
    no bind processor in front of it and dies at Bind time, which the PREPARE gate structurally
    cannot see — so the encoding happens here, explicitly, for both engines."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def _decode_json(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        # A column that is not valid JSON is evidence of a bad write, not a reason to raise on
        # a read path. Hand back what is there.
        return value


def _decode_dt(value: Any) -> Any:
    """Postgres hands back a datetime; SQLite hands back the text it stored. Out of here it is a
    timezone-AWARE UTC datetime on both, so no caller and no test has to know which engine it is
    talking to."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip().replace("T", " ")
        for fmt in (_SQLITE_TS_FORMAT, "%Y-%m-%d %H:%M:%S.%f"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return value


def _normalize_row(row: Any, ts_columns: Sequence[str]) -> Optional[Dict[str, Any]]:
    """One shape out, whatever the engine put in. jsonb columns come back as dict/list and
    timestamps as timezone-aware UTC datetimes on BOTH dialects — otherwise every caller would
    carry its own dialect branch, and the SQLite tests would prove nothing about Postgres."""
    if row is None:
        return None
    out = dict(row)
    for column in _JSON_COLUMNS:
        if column in out:
            out[column] = _decode_json(out[column])
    for column in ts_columns:
        if column in out:
            out[column] = _decode_dt(out[column])
    return out


def _is_unique_violation(exc: BaseException) -> bool:
    """Is this exception a UNIQUE-constraint violation, on either driver?

    NEITHER DRIVER IS WRAPPED BY `databases` 0.7.0, so there is no one exception type to catch:
    asyncpg raises `asyncpg.exceptions.UniqueViolationError` and aiosqlite raises
    `sqlite3.IntegrityError`. Matched by SQLSTATE and by type rather than by importing asyncpg at
    module scope, which would make this module unimportable on a SQLite-only install.

    SQLAlchemy's `IntegrityError` is unwrapped too, for the paths where something does wrap.
    """
    seen = set()
    candidates = [exc]
    while candidates:
        current = candidates.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        # asyncpg: 23505 is unique_violation. Checked before the type name so a subclass or a
        # re-export cannot slip past.
        if getattr(current, "sqlstate", None) == "23505":
            return True
        if type(current).__name__ == "UniqueViolationError":
            return True
        if isinstance(current, sqlite3.IntegrityError) and "unique" in str(current).lower():
            return True
        for nested in (current.__cause__, current.__context__, getattr(current, "orig", None)):
            if isinstance(nested, BaseException):
                candidates.append(nested)
    return False


# EXACTLY what an owner-facing read may contain. AN ALLOWLIST, and the first cut of this was a
# denylist — which failed for the reason denylists fail: deleting "shipping_address" or
# "agent_user_ref_hash" from it left both suites entirely green, because the only content test
# intersected the view with the very set under test. A test that asks "is the view free of the
# things I currently call private?" cannot notice the definition of private shrinking.
#
# An allowlist inverts the default. A column added to the table later does NOT appear until
# somebody adds it here, and the test asserts the view's key set EQUALS a list written out
# independently in the test file — so the two have to be changed together, deliberately, by
# someone who looked at the new column.
#
# NOT HERE, AND WHY, since the absences are the point:
#   buyer_ref, agent_id, agent_user_ref_hash — identity we minted; the caller already knows who
#       it is, and buyer_ref is what we send Reap as owner.id.
#   buyer_email, shipping_address — PII. These are the columns the rail NULLs on terminal;
#       handing them back on every read before then is that same leak through another door.
#   enrollment_id, click_id, return_url — our plumbing.
#   reap_product_id, reap_variant_id, reap_quote_id, reap_checkout_id — EVIDENCE, never identity.
#       A substituted variant id handed out here would read as identity to everyone downstream.
#   queries_tried — our resolver's search terms.
#   attempts, next_poll_at, claimed_by, claimed_at — poller bookkeeping.
PUBLIC_PURCHASE_COLUMNS = (
    "id",
    "state",
    "merchant_domain",
    "product_key",
    "variant_key",
    "product_name",
    "variant_title",
    "brand",
    "category",
    "quantity",
    "currency",
    "our_price_minor",
    "quoted_total_minor",
    "final_total_minor",
    "shipping_minor",
    "tax_minor",
    "hosted_url",
    "hosted_url_expires_at",
    "reap_quote_expires_at",
    "reap_order_id",
    "refusal_reason",
    "last_error_code",
    "created_at",
    "updated_at",
    "terminal_at",
)


def public_purchase_view(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Project a purchase row down to exactly `PUBLIC_PURCHASE_COLUMNS`.

    Keys absent from the row are absent from the view (a partial row projects to a partial view
    rather than to one padded with None), so this is safe to hand a row from any SELECT.
    """
    if row is None:
        return None
    return {key: row[key] for key in PUBLIC_PURCHASE_COLUMNS if key in row}


def _purchase(row: Any) -> Optional[Dict[str, Any]]:
    return _normalize_row(row, _PURCHASE_TS_COLUMNS)


def _enrollment(row: Any) -> Optional[Dict[str, Any]]:
    return _normalize_row(row, _ENROLLMENT_TS_COLUMNS)


def new_purchase_id() -> str:
    return f"rp_{uuid.uuid4().hex[:24]}"


def new_enrollment_id() -> str:
    return f"re_{uuid.uuid4().hex[:24]}"


# ── purchases: create + read ─────────────────────────────────────────────────────────────────

_INSERT_PURCHASE_SQL = """
    INSERT INTO reap_agentic_purchases (
        id, buyer_ref, agent_id, agent_user_ref_hash, enrollment_id, state,
        merchant_domain, product_key, variant_key, product_name, variant_title,
        brand, category, quantity, currency, our_price_minor, click_id, return_url,
        queries_tried, next_poll_at, shipping_address, buyer_email
    ) VALUES (
        :id, :buyer_ref, :agent_id, :agent_user_ref_hash, :enrollment_id, :state,
        :merchant_domain, :product_key, :variant_key, :product_name, :variant_title,
        :brand, :category, :quantity, :currency, :our_price_minor, :click_id, :return_url,
        CAST(:queries_tried AS JSONB), COALESCE(:next_poll_at, clock_timestamp()),
        CAST(:shipping_address AS JSONB),
        :buyer_email
    )
    RETURNING *
"""

_INSERT_PURCHASE_SQL_SQLITE = """
    INSERT INTO reap_agentic_purchases (
        id, buyer_ref, agent_id, agent_user_ref_hash, enrollment_id, state,
        merchant_domain, product_key, variant_key, product_name, variant_title,
        brand, category, quantity, currency, our_price_minor, click_id, return_url,
        queries_tried, next_poll_at, shipping_address, buyer_email
    ) VALUES (
        :id, :buyer_ref, :agent_id, :agent_user_ref_hash, :enrollment_id, :state,
        :merchant_domain, :product_key, :variant_key, :product_name, :variant_title,
        :brand, :category, :quantity, :currency, :our_price_minor, :click_id, :return_url,
        :queries_tried, COALESCE(:next_poll_at, CURRENT_TIMESTAMP), :shipping_address,
        :buyer_email
    )
    RETURNING *
"""

_SELECT_PURCHASE_SQL = """
    SELECT * FROM reap_agentic_purchases WHERE id = :id
"""

# THE OWNERSHIP CONJUNCT LIVES HERE, IN THE SQL — not in Python after the fetch. A filter applied
# after the read is a filter a later refactor can drop while every test that checks the returned
# value still passes, and the row has already been loaded into the process either way. In the
# statement, "not yours" and "does not exist" are the same answer and there is nothing to forget.
_SELECT_PURCHASE_FOR_OWNER_SQL = """
    SELECT * FROM reap_agentic_purchases
     WHERE id = :id
       AND agent_id = :agent_id
       AND agent_user_ref_hash = :agent_user_ref_hash
"""

_LIST_PURCHASES_FOR_OWNER_SQL = """
    SELECT * FROM reap_agentic_purchases
     WHERE agent_id = :agent_id
       AND agent_user_ref_hash = :agent_user_ref_hash
     ORDER BY created_at DESC, id DESC
     LIMIT :limit OFFSET :offset
"""


async def create_purchase(
    *,
    buyer_ref: str,
    agent_id: str,
    agent_user_ref_hash: str,
    enrollment_id: Optional[str] = None,
    state: str = "resolving",
    purchase_id: Optional[str] = None,
    merchant_domain: Optional[str] = None,
    product_key: Optional[str] = None,
    variant_key: Optional[str] = None,
    product_name: Optional[str] = None,
    variant_title: Optional[str] = None,
    brand: Optional[str] = None,
    category: Optional[str] = None,
    quantity: int = 1,
    currency: Optional[str] = None,
    our_price_minor: Optional[int] = None,
    click_id: Optional[str] = None,
    return_url: Optional[str] = None,
    queries_tried: Any = None,
    next_poll_at: Optional[datetime] = None,
    shipping_address: Any = None,
    buyer_email: Optional[str] = None,
) -> Dict[str, Any]:
    """Open a purchase, always in 'resolving'. `buyer_ref` is the OPAQUE reference we send Reap
    as owner.id — never the global buyer id, which must not leave this system.

    A PURCHASE CANNOT BE CREATED IN ANY OTHER STATE, AND THAT IS THE FIX FOR A REAL DEFECT. This
    used to accept any state in the vocabulary, including the four terminal ones — and creating
    directly in a terminal state produced a row that KEPT shipping_address and buyer_email and
    never stamped terminal_at, because the PII rule lives in the transition statement and a
    create is not a transition. "The resolve failed instantly, record it and stop" is exactly the
    shape the next package will want, and it would have written the buyer's address into a
    permanently-terminal row.

    The replacement is not a reminder, it is an impossibility: an instant refusal is
    `create_purchase(...)` followed by `transition(..., to_state='refused')`, which nulls the PII
    because every terminal write goes through the one statement that does. The `state` parameter
    is kept rather than deleted so the refusal names what happened.

    EMPTY OWNERSHIP IS ALSO REFUSED. A row whose agent_id or agent_user_ref_hash is None or blank
    is invisible to `get_purchase_for_owner` (NULL = NULL is NULL) — its owner could never read
    it back — and "" is a bucket that two unrelated callers would silently share.
    """
    if state != "resolving":
        raise ValueError(
            f"a purchase may only be created in 'resolving' (got {state!r}). To record an "
            "instant refusal, create it and then transition it — the terminal write is what "
            "nulls shipping_address and buyer_email, and a create does not."
        )
    for name, value in (
        ("buyer_ref", buyer_ref),
        ("agent_id", agent_id),
        ("agent_user_ref_hash", agent_user_ref_hash),
    ):
        if not (value or "").strip():
            raise ValueError(
                f"{name} is required and must not be blank — a purchase without it cannot be "
                "read back by its owner"
            )
    if int(quantity) <= 0:
        raise ValueError("quantity must be a positive integer")

    values: Dict[str, Any] = {
        "id": purchase_id or new_purchase_id(),
        "buyer_ref": buyer_ref,
        "agent_id": agent_id,
        "agent_user_ref_hash": agent_user_ref_hash,
        "enrollment_id": enrollment_id,
        "state": state,
        "merchant_domain": merchant_domain,
        "product_key": product_key,
        "variant_key": variant_key,
        "product_name": product_name,
        "variant_title": variant_title,
        "brand": brand,
        "category": category,
        "quantity": int(quantity),
        "currency": currency,
        "our_price_minor": our_price_minor,
        "click_id": click_id,
        "return_url": return_url,
        "queries_tried": _bind_json(queries_tried),
        "next_poll_at": _bind_dt(next_poll_at),
        "shipping_address": _bind_json(shipping_address),
        "buyer_email": buyer_email,
    }
    # THE BRANCH IS AT THE CALL SITE, not `sql = A if IS_POSTGRES else B` one line up. Both
    # forms read the same; only this one is visible to tests/test_repo_sql_prepare_postgres.py,
    # which resolves a `database.*` first argument when it is a module-level name but declines a
    # local bound to a conditional expression. Same shape as
    # db/product_quality_backfill_jobs.requeue_stale_quality_backfill_jobs.
    if IS_POSTGRES:
        row = await database.fetch_one(_INSERT_PURCHASE_SQL, values)
    else:
        row = await database.fetch_one(_INSERT_PURCHASE_SQL_SQLITE, values)
    created = _purchase(row)
    if created is None:  # pragma: no cover — an INSERT ... RETURNING that returns nothing
        raise RuntimeError("purchase insert returned no row")
    return created


async def get_purchase_internal(purchase_id: str) -> Optional[Dict[str, Any]]:
    """The UNSCOPED, UNREDACTED read. For internal callers — the poller, the webhook receiver —
    that already hold the id from our own storage and need the private columns to do their job.

    NAMED `_internal` AND ABSENT FROM `__all__` ON PURPOSE. It was `get_purchase`, which is the
    obvious name and therefore the one a route author reaches for; the difference between it and
    `get_purchase_for_owner` is the entire access-control story on this rail, and a name that
    does not say so is a trap. The suffix is the only thing that makes the wrong call look wrong
    at the call site.
    """
    return _purchase(await database.fetch_one(_SELECT_PURCHASE_SQL, {"id": purchase_id}))


async def get_purchase_for_owner(
    purchase_id: str,
    agent_id: str,
    agent_user_ref_hash: str,
    *,
    include_private: bool = False,
) -> Optional[Dict[str, Any]]:
    """The read every agent-facing route makes. None when the purchase is someone else's — the
    SAME answer as when it does not exist, so the endpoint cannot be used to probe for ids.

    REDACTED BY DEFAULT. `include_private=False` runs the row through `public_purchase_view`,
    which strips the PII and the internals. The default is the safe one because the caller who
    forgets to think about it is a route, and the failure of forgetting is a leak; an internal
    caller that genuinely needs the private columns has to say so, and that word is greppable.
    """
    row = _purchase(
        await database.fetch_one(
            _SELECT_PURCHASE_FOR_OWNER_SQL,
            {
                "id": purchase_id,
                "agent_id": agent_id,
                "agent_user_ref_hash": agent_user_ref_hash,
            },
        )
    )
    return row if include_private else public_purchase_view(row)


async def list_purchases_for_owner(
    agent_id: str,
    agent_user_ref_hash: str,
    *,
    limit: int = 50,
    offset: int = 0,
    include_private: bool = False,
) -> List[Dict[str, Any]]:
    """Owner-scoped history, newest first. The conjunct is the same one `get_purchase_for_owner`
    uses and for the same reason — a list endpoint that leaks is a worse leak than a get, and so
    is the redaction default."""
    rows = await database.fetch_all(
        _LIST_PURCHASES_FOR_OWNER_SQL,
        {
            "agent_id": agent_id,
            "agent_user_ref_hash": agent_user_ref_hash,
            "limit": max(1, min(500, int(limit))),
            "offset": max(0, int(offset)),
        },
    )
    out = [r for r in (_purchase(row) for row in rows) if r is not None]
    if include_private:
        return out
    return [view for view in (public_purchase_view(r) for r in out) if view is not None]


# ── purchases: the conditional advance ───────────────────────────────────────────────────────


# THE ADVANCE, WRITTEN OUT PER DIALECT AS MODULE-LEVEL CONSTANTS.
#
# WHY NOT ONE TEMPLATE WITH A HELPER. These two statements differ in exactly three CASTs and the
# duplication is real, but a `_build_transition_sql(json_cast=...)` call bound to a module-level
# name is INVISIBLE to tests/test_repo_sql_prepare_postgres.py: that sweep resolves a
# `database.*` first argument only when it is a literal or a module-level name bound to one, and
# "a helper call returning SQL" is its own counted blind spot there. Written out, both statements
# are collected and PREPAREd against the real schema; generated, neither is, and the whole
# advance path on this rail would ship unplanned. That is the #1588 shape, so duplication wins.
#
# WHY THE `state IN` LIST IS NINE FIXED BINDS. `from_states` is caller-chosen, and an IN list
# sized to it would have to be built at call time — the same blind spot. Nine is the number of
# states in the machine, so a fixed-width list can never overflow, and `transition` pads a short
# set by repeating its first element, which cannot widen the match.
#
# WHY `:to_state` AND `:to_state_probe` CARRY THE SAME STRING. One is bound only as an assigned
# value, the other only inside comparisons. A single bind used both ways gets two deduced types
# and Postgres refuses the entire statement with AmbiguousParameter — see the note at the top of
# db/agent_card_auth_decisions.py.
#
# WHY THE jsonb COALESCE CASTS BOTH ARMS. The column is jsonb wherever migration 224 or the
# schema-guard self-heal built it, but Postgres has no implicit json<->jsonb coercion, so
# `COALESCE(CAST(:x AS JSONB), queries_tried)` is unplannable if the column ever lands as json.
# Casting both arms is correct under either declaration. On SQLite the cast MUST NOT be there:
# SQLite resolves the unknown type name JSONB to NUMERIC affinity and silently stores 0.
#
# WHY THE PII NULLING IS IN THIS STATEMENT AND NOT A SECOND ONE. A purchase that reaches a
# terminal state must not still carry the buyer's address and email. The only version of that
# rule which cannot be skipped by a crash, a retry, or a caller who forgot is the one that is
# part of the same write that makes the state terminal.
_TRANSITION_SQL = """
    UPDATE reap_agentic_purchases
       SET state = :to_state,
           enrollment_id = COALESCE(:enrollment_id, enrollment_id),
           merchant_domain = COALESCE(:merchant_domain, merchant_domain),
           product_key = COALESCE(:product_key, product_key),
           variant_key = COALESCE(:variant_key, variant_key),
           product_name = COALESCE(:product_name, product_name),
           variant_title = COALESCE(:variant_title, variant_title),
           brand = COALESCE(:brand, brand),
           category = COALESCE(:category, category),
           quantity = COALESCE(:quantity, quantity),
           currency = COALESCE(:currency, currency),
           our_price_minor = COALESCE(:our_price_minor, our_price_minor),
           click_id = COALESCE(:click_id, click_id),
           return_url = COALESCE(:return_url, return_url),
           reap_product_id = COALESCE(:reap_product_id, reap_product_id),
           reap_variant_id = COALESCE(:reap_variant_id, reap_variant_id),
           reap_quote_id = COALESCE(:reap_quote_id, reap_quote_id),
           reap_quote_expires_at = COALESCE(:reap_quote_expires_at, reap_quote_expires_at),
           reap_checkout_id = COALESCE(:reap_checkout_id, reap_checkout_id),
           reap_order_id = COALESCE(:reap_order_id, reap_order_id),
           quoted_total_minor = COALESCE(:quoted_total_minor, quoted_total_minor),
           final_total_minor = COALESCE(:final_total_minor, final_total_minor),
           shipping_minor = COALESCE(:shipping_minor, shipping_minor),
           tax_minor = COALESCE(:tax_minor, tax_minor),
           hosted_url = COALESCE(:hosted_url, hosted_url),
           hosted_url_expires_at = COALESCE(:hosted_url_expires_at, hosted_url_expires_at),
           refusal_reason = COALESCE(:refusal_reason, refusal_reason),
           queries_tried = COALESCE(CAST(:queries_tried AS JSONB), CAST(queries_tried AS JSONB)),
           last_error_code = COALESCE(:last_error_code, last_error_code),
           next_poll_at = COALESCE(:next_poll_at, next_poll_at),
           shipping_address = CASE WHEN :to_state_probe IN ('completed', 'failed', 'refused', 'expired')
                THEN NULL ELSE COALESCE(CAST(:shipping_address AS JSONB), CAST(shipping_address AS JSONB)) END,
           buyer_email = CASE WHEN :to_state_probe IN ('completed', 'failed', 'refused', 'expired')
                THEN NULL ELSE COALESCE(:buyer_email, buyer_email) END,
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
       AND (:holder_unfenced = 1 OR claimed_by = :holder)
    RETURNING *
"""

_TRANSITION_SQL_SQLITE = """
    UPDATE reap_agentic_purchases
       SET state = :to_state,
           enrollment_id = COALESCE(:enrollment_id, enrollment_id),
           merchant_domain = COALESCE(:merchant_domain, merchant_domain),
           product_key = COALESCE(:product_key, product_key),
           variant_key = COALESCE(:variant_key, variant_key),
           product_name = COALESCE(:product_name, product_name),
           variant_title = COALESCE(:variant_title, variant_title),
           brand = COALESCE(:brand, brand),
           category = COALESCE(:category, category),
           quantity = COALESCE(:quantity, quantity),
           currency = COALESCE(:currency, currency),
           our_price_minor = COALESCE(:our_price_minor, our_price_minor),
           click_id = COALESCE(:click_id, click_id),
           return_url = COALESCE(:return_url, return_url),
           reap_product_id = COALESCE(:reap_product_id, reap_product_id),
           reap_variant_id = COALESCE(:reap_variant_id, reap_variant_id),
           reap_quote_id = COALESCE(:reap_quote_id, reap_quote_id),
           reap_quote_expires_at = COALESCE(:reap_quote_expires_at, reap_quote_expires_at),
           reap_checkout_id = COALESCE(:reap_checkout_id, reap_checkout_id),
           reap_order_id = COALESCE(:reap_order_id, reap_order_id),
           quoted_total_minor = COALESCE(:quoted_total_minor, quoted_total_minor),
           final_total_minor = COALESCE(:final_total_minor, final_total_minor),
           shipping_minor = COALESCE(:shipping_minor, shipping_minor),
           tax_minor = COALESCE(:tax_minor, tax_minor),
           hosted_url = COALESCE(:hosted_url, hosted_url),
           hosted_url_expires_at = COALESCE(:hosted_url_expires_at, hosted_url_expires_at),
           refusal_reason = COALESCE(:refusal_reason, refusal_reason),
           queries_tried = COALESCE(:queries_tried, queries_tried),
           last_error_code = COALESCE(:last_error_code, last_error_code),
           next_poll_at = COALESCE(:next_poll_at, next_poll_at),
           shipping_address = CASE WHEN :to_state_probe IN ('completed', 'failed', 'refused', 'expired')
                THEN NULL ELSE COALESCE(:shipping_address, shipping_address) END,
           buyer_email = CASE WHEN :to_state_probe IN ('completed', 'failed', 'refused', 'expired')
                THEN NULL ELSE COALESCE(:buyer_email, buyer_email) END,
           claimed_by = CASE WHEN :to_state_probe IN ('completed', 'failed', 'refused', 'expired')
                THEN NULL ELSE claimed_by END,
           claimed_at = CASE WHEN :to_state_probe IN ('completed', 'failed', 'refused', 'expired')
                THEN NULL ELSE claimed_at END,
           terminal_at = CASE WHEN :to_state_probe IN ('completed', 'failed', 'refused', 'expired')
                THEN CURRENT_TIMESTAMP ELSE terminal_at END,
           state_entered_at = CURRENT_TIMESTAMP,
           updated_at = CURRENT_TIMESTAMP
     WHERE id = :id
       AND state IN (:from_0, :from_1, :from_2, :from_3, :from_4, :from_5, :from_6, :from_7, :from_8)
       AND (:holder_unfenced = 1 OR claimed_by = :holder)
    RETURNING *
"""


async def transition(
    purchase_id: str,
    *,
    from_states: Iterable[str],
    to_state: str,
    holder: Optional[str] = None,
    **fields: Any,
) -> Optional[Dict[str, Any]]:
    """Advance a purchase, atomically.

    Returns the NEW row, or None when the row was not in one of `from_states` — which is the
    concurrency guard, not an error. Two workers advancing the same purchase from the same state
    both issue this statement; exactly one gets a row back.

    Raises ValueError for a pair ALLOWED_TRANSITIONS does not permit, before any SQL runs. The
    distinction matters: an illegal pair is a bug in the caller and must be loud, while a lost
    race is ordinary and must be quiet, and if both produced None the first would be retried
    forever instead of being fixed.

    ── `holder`: THE CLAIM FENCE ────────────────────────────────────────────────────────────

    With `holder` set, the statement also carries `AND claimed_by = :holder`, so a worker can
    only advance a purchase it still holds the lease on. Any poller caller wants
    `transition_as_holder`, which is that call with the fence made non-optional.

    THE INTERLEAVING THIS EXISTS FOR, reproduced on both dialects before it was closed: worker A
    claims a 'quoting' row and stalls. Its lease ages out and `requeue_stale_claims` frees it.
    Worker B claims the row, quotes it, and advances it to 'awaiting_approval'. A then wakes up
    holding nothing, calls `transition(from_states=['awaiting_approval'], to_state='processing',
    reap_checkout_id=…)` — and SUCCEEDS. The row lands in 'processing' carrying a checkout id
    minted by a worker that does not own it, which is a buyer being charged for the wrong
    checkout. Nothing in `from_states` can catch this: A's view of the STATE is correct, it is
    A's view of OWNERSHIP that is stale.

    `claimed_by` is deliberately NOT in `_TRANSITION_FIELDS`, so the poller cannot build this
    fence itself out of a field write — it has to come from here.

    A fenced call whose claim has moved on returns None, exactly like any other lost race, and
    the caller re-reads. `holder=None` leaves the statement unfenced, which is what the route
    that first creates a purchase needs — nothing holds a lease at that point.

    TWO BINDS, `:holder_unfenced` AND `:holder`, for one concept. A single nullable bind used as
    `(:holder IS NULL OR claimed_by = :holder)` is the same parameter deduced two ways, which is
    the AmbiguousParameter shape this file avoids everywhere else. `:holder_unfenced` is only
    ever compared against the integer literal 1 and `:holder` only ever against `claimed_by`, so
    each has exactly one deducible type.
    """
    if to_state not in PURCHASE_STATES:
        raise ValueError(f"unknown target state {to_state!r}")
    sources = [s for s in dict.fromkeys(from_states)]
    if not sources:
        raise ValueError("from_states must name at least one state")
    if len(sources) > _FROM_SLOTS:  # pragma: no cover — there are only _FROM_SLOTS states
        raise ValueError(f"from_states may name at most {_FROM_SLOTS} states")
    for source in sources:
        if source not in PURCHASE_STATES:
            raise ValueError(f"unknown source state {source!r}")
        if to_state not in ALLOWED_TRANSITIONS[source]:
            raise ValueError(
                f"illegal transition {source!r} -> {to_state!r}; "
                f"{source!r} permits {sorted(ALLOWED_TRANSITIONS[source]) or 'nothing'}"
            )

    unknown = [key for key in fields if key not in _TRANSITION_FIELDS]
    if unknown:
        raise TypeError(f"transition() got unexpected field(s): {', '.join(sorted(unknown))}")

    if holder is not None:
        _require_worker_id(holder, "holder")

    values: Dict[str, Any] = {
        "id": purchase_id,
        "to_state": to_state,
        "to_state_probe": to_state,
        "holder": holder,
        "holder_unfenced": 1 if holder is None else 0,
    }
    # Pad the fixed-width IN list by repeating the first source. A repeat cannot widen the match,
    # and it keeps the statement a module-level constant — see _FROM_SLOTS.
    for index in range(_FROM_SLOTS):
        values[f"from_{index}"] = sources[index] if index < len(sources) else sources[0]
    for column in _TRANSITION_FIELDS:
        raw = fields.get(column)
        if column in _JSON_COLUMNS:
            values[column] = _bind_json(raw)
        else:
            values[column] = _bind_dt(raw)

    # Branch at the call site, not into a local — see the note in `create_purchase`.
    if IS_POSTGRES:
        return _purchase(await database.fetch_one(_TRANSITION_SQL, values))
    return _purchase(await database.fetch_one(_TRANSITION_SQL_SQLITE, values))


async def transition_as_holder(
    purchase_id: str,
    worker_id: str,
    *,
    from_states: Iterable[str],
    to_state: str,
    **fields: Any,
) -> Optional[Dict[str, Any]]:
    """THE FORM THE POLLER USES. `transition` with the claim fence made non-optional.

    Every advance a worker makes while holding a lease goes through here. `transition(holder=…)`
    is the same call; this exists so that the poller package never has to remember the keyword,
    and so that a code review can see an unfenced worker advance by its NAME rather than by a
    missing argument.

    Returns None when the lease has moved on — the worker stalled, its claim was requeued, and
    somebody else owns the row now. That is a lost race like any other: re-read and reconsider,
    do not retry blindly.

    NONE ALSO MEANS "A SWEEP GOT THERE FIRST". `expire_overdue_purchases` and
    `fail_exhausted_purchases` are UNFENCED bulk terminal writes: they take no holder, they can
    terminate a purchase this worker currently holds, and they clear the claim when they do. The
    worker finds out here, by getting None. So None is never a reason to retry the same write —
    it is always a reason to re-read the row and decide again.
    """
    _require_worker_id(worker_id, "worker_id")
    return await transition(
        purchase_id,
        from_states=from_states,
        to_state=to_state,
        holder=worker_id,
        **fields,
    )


# ── purchases: the poller's claim pair ───────────────────────────────────────────────────────

# THE CLAIM PAIR, house style (db/product_quality_backfill_jobs.py): a plain SELECT picks the
# next candidates, then ONE conditional UPDATE per candidate decides who actually got it. The
# SELECT is advisory — it can be stale by the time the UPDATE runs, and that is fine, because the
# conjuncts in the UPDATE are what make the claim exclusive. A second worker that read the same
# candidate gets no row back and moves on.
#
# THE UPDATE RE-CHECKS THE STATE, AND NOT ONLY THE LEASE. The SELECT and the UPDATE are two
# statements with a gap between them, and the row can go TERMINAL inside that gap: worker A's
# SELECT offers a due 'processing' row, worker B completes it, worker A's claim then lands. With
# only `claimed_by IS NULL` that claim SUCCEEDS and hands the poller a finished purchase to poll.
# It is also unrecoverable, because a dead pod's claim on a terminal row was invisible to the
# requeue's old state filter — measured, a 99999-second-old claim freed 0 rows. Both halves are
# fixed: the state conjunct below, and a requeue that no longer filters on state at all.
#
# THE SELECT STILL FILTERS `claimed_by IS NULL` EVEN THOUGH THE UPDATE RE-CHECKS IT. Dropping it
# leaves the claim correct and the poller wasteful: N workers would each fill their whole `limit`
# with candidates somebody else already holds, and take none of them.
#
# `clock_timestamp()` RATHER THAN `CURRENT_TIMESTAMP` ON POSTGRES — see property 6 in the module
# header. Every cutoff in this file reads the wall clock at the moment the statement runs, not
# the moment its transaction began.
_SELECT_DUE_PURCHASES_SQL = """
    SELECT id FROM reap_agentic_purchases
     WHERE state IN ('resolving', 'needs_enrollment', 'quoting', 'awaiting_approval', 'processing')
       AND claimed_by IS NULL
       AND next_poll_at IS NOT NULL
       AND next_poll_at <= clock_timestamp()
     ORDER BY next_poll_at ASC, id ASC
     LIMIT :limit
"""

_SELECT_DUE_PURCHASES_SQL_SQLITE = """
    SELECT id FROM reap_agentic_purchases
     WHERE state IN ('resolving', 'needs_enrollment', 'quoting', 'awaiting_approval', 'processing')
       AND claimed_by IS NULL
       AND next_poll_at IS NOT NULL
       AND next_poll_at <= CURRENT_TIMESTAMP
     ORDER BY next_poll_at ASC, id ASC
     LIMIT :limit
"""

# `attempts` COUNTS CLAIMS, AND ONLY WHERE A CLAIM MEANS WORK WAS TRIED.
#
# A purchase sitting in 'awaiting_approval' is waiting on a HUMAN — the buyer has the hosted page
# open, or has not opened it yet. Polled every 30 seconds for the hour a buyer might reasonably
# take, that is ~120 claims on a purchase where nothing has gone wrong at all, and
# `fail_exhausted_purchases` would kill it for being patient. Same for 'needs_enrollment', which
# waits on the buyer enrolling a card.
#
# So the counter is incremented only in the states where a claim means WE tried something:
# resolving, quoting, processing. The two waiting states are excluded here rather than left to
# the poller to compensate for by choosing `max_attempts` against its own poll cadence — that is
# a coupling between two packages that nothing would check. `expire_overdue_purchases` is what
# bounds the waiting states, on a clock rather than a counter, which is the right instrument for
# waiting on a person.
_CLAIM_PURCHASE_SQL = """
    UPDATE reap_agentic_purchases
       SET claimed_by = :worker_id,
           claimed_at = clock_timestamp(),
           attempts = CASE WHEN state IN ('awaiting_approval', 'needs_enrollment')
                THEN attempts ELSE attempts + 1 END,
           updated_at = clock_timestamp()
     WHERE id = :id
       AND claimed_by IS NULL
       AND state IN (
           'resolving', 'needs_enrollment', 'quoting', 'awaiting_approval', 'processing'
       )
    RETURNING *
"""

_CLAIM_PURCHASE_SQL_SQLITE = """
    UPDATE reap_agentic_purchases
       SET claimed_by = :worker_id,
           claimed_at = CURRENT_TIMESTAMP,
           attempts = CASE WHEN state IN ('awaiting_approval', 'needs_enrollment')
                THEN attempts ELSE attempts + 1 END,
           updated_at = CURRENT_TIMESTAMP
     WHERE id = :id
       AND claimed_by IS NULL
       AND state IN (
           'resolving', 'needs_enrollment', 'quoting', 'awaiting_approval', 'processing'
       )
    RETURNING *
"""

# Releasing is conjunct on the WORKER, not just the id: a worker whose lease was already requeued
# and handed to somebody else must not be able to clear the new holder's claim on its way out.
# This is the SAME fence `transition`'s `holder=` applies — `claimed_by = :worker_id` in both,
# both answering None when the claim has moved on.
_RELEASE_CLAIM_SQL = """
    UPDATE reap_agentic_purchases
       SET claimed_by = NULL,
           claimed_at = NULL,
           next_poll_at = COALESCE(:next_poll_at, next_poll_at),
           updated_at = clock_timestamp()
     WHERE id = :id
       AND claimed_by = :worker_id
    RETURNING *
"""

_RELEASE_CLAIM_SQL_SQLITE = """
    UPDATE reap_agentic_purchases
       SET claimed_by = NULL,
           claimed_at = NULL,
           next_poll_at = COALESCE(:next_poll_at, next_poll_at),
           updated_at = CURRENT_TIMESTAMP
     WHERE id = :id
       AND claimed_by = :worker_id
    RETURNING *
"""

# `claimed_at < cutoff` RATHER THAN `<=`, AND THAT CHOICE IS NOT TESTED. A mutation to `<=` frees
# a lease at most one tick early on a lease of at least thirty seconds, and the only way to
# observe the difference is to land exactly on the boundary — which needs control of the server
# clock finer than either engine offers here (on SQLite the column is second-resolution text, so
# ties are possible but arrive only when the wall clock happens to cross a second mid-run). It is
# left untested deliberately: a test that could see it would be a test that fails on timing, and
# the properties that matter — a fresh lease survives, a long-expired one does not — are covered.
# Stated here rather than called "equivalent", which it is not: it is a difference too small to
# be worth a flaky test.
#
# THE CUTOFF IS COMPUTED BY THE SERVER. `clock_timestamp() - interval` binds no datetime at all,
# so it is correct whatever the client process's timezone is and whatever the column's
# declaration turned out to be — see property 4. A Python-bound cutoff was measured seven hours
# wrong on this driver.
#
# `RETURNING id` is what makes the count true on BOTH engines: without it, asyncpg answers None
# however many rows moved, and the caller's "requeued N stale leases" log line disappears while
# the requeue itself silently works.
#
# IT FREES A STALE CLAIM WHATEVER STATE THE ROW IS IN. This used to filter on the pollable
# states, which sounds right and is not: a claim on a TERMINAL row is garbage to clear, not work
# to requeue, and filtering it out made it unclearable forever (measured: a 99999-second-old
# claim on a completed purchase freed 0 rows). Clearing it cannot resurrect the row, because
# `_CLAIM_PURCHASE_SQL` carries its own state conjunct — the two changes are one fix.
_REQUEUE_STALE_CLAIMS_SQL = """
    UPDATE reap_agentic_purchases
       SET claimed_by = NULL,
           claimed_at = NULL,
           updated_at = clock_timestamp()
     WHERE id IN (
        SELECT id FROM reap_agentic_purchases
         WHERE claimed_by IS NOT NULL
           AND claimed_at IS NOT NULL
           AND claimed_at < clock_timestamp() - (:lease_seconds * INTERVAL '1 second')
         ORDER BY claimed_at ASC
         LIMIT :limit
     )
    RETURNING id
"""

_REQUEUE_STALE_CLAIMS_SQL_SQLITE = """
    UPDATE reap_agentic_purchases
       SET claimed_by = NULL,
           claimed_at = NULL,
           updated_at = CURRENT_TIMESTAMP
     WHERE id IN (
        SELECT id FROM reap_agentic_purchases
         WHERE claimed_by IS NOT NULL
           AND claimed_at IS NOT NULL
           AND claimed_at < datetime('now', :lease_window)
         ORDER BY claimed_at ASC
         LIMIT :limit
     )
    RETURNING id
"""

# States the poller may hold a lease on, PARSED OUT OF the statements that actually enforce them
# — see `_states_in`. `_POLLABLE_STATES` is the claim's WHERE-clause list; the select-due
# statement must agree with it, and a test asserts that rather than assuming it.
#
# The anchors are specific because `_CLAIM_PURCHASE_SQL` contains TWO `state IN (…)` lists that
# mean different things — the WHERE clause's pollable states and the attempts CASE's exempt
# states — and a bare "state IN (" anchor silently returns the first one. It did, on the first
# cut of this: `_POLLABLE_STATES` came back as the two WAITING states.
_POLLABLE_STATES = _states_in(_CLAIM_PURCHASE_SQL, "AND state IN (")
_SELECT_DUE_STATES = _states_in(_SELECT_DUE_PURCHASES_SQL, "WHERE state IN (")
_CLAIM_ATTEMPT_EXEMPT_STATES = _states_in(_CLAIM_PURCHASE_SQL, "attempts = CASE WHEN state IN (")


async def claim_due_purchases(worker_id: str, *, limit: int = 10) -> List[Dict[str, Any]]:
    """Take a lease on up to `limit` purchases whose next_poll_at has come.

    THERE IS NO `lease_seconds` PARAMETER, and there used to be. It was accepted here, documented
    as "what requeue_stale_claims measures against", and connected to nothing: the lease length
    lives entirely in `requeue_stale_claims`, and a caller that passed 60 here and took the
    default 300 there would have had a 300-second lease and no way to tell. A parameter that
    reads as configuration and configures nothing is worse than no parameter.

    FOUR THINGS RELEASE A CLAIM, and a caller reasoning about leases needs all four:
      * `release_claim(id, worker_id)` — the worker finished a poll and is scheduling the next;
      * a transition into a TERMINAL state — cleared in the same UPDATE that writes the state;
      * `expire_overdue_purchases()` / `fail_exhausted_purchases()` — the sweeps write terminal
        states in bulk and clear the claim with them;
      * `requeue_stale_claims()` — the worker died and its lease aged out.

    `attempts` is incremented only outside 'awaiting_approval' and 'needs_enrollment'; see the
    note on `_CLAIM_PURCHASE_SQL` for why waiting on a human is not a failed attempt.
    """
    _require_worker_id(worker_id, "worker_id")
    capped = _sweep_limit(limit)
    if IS_POSTGRES:
        candidates = await database.fetch_all(_SELECT_DUE_PURCHASES_SQL, {"limit": capped})
    else:
        candidates = await database.fetch_all(_SELECT_DUE_PURCHASES_SQL_SQLITE, {"limit": capped})
    claimed: List[Dict[str, Any]] = []
    for candidate in candidates:
        params = {"id": candidate["id"], "worker_id": worker_id}
        if IS_POSTGRES:
            row = await database.fetch_one(_CLAIM_PURCHASE_SQL, params)
        else:
            row = await database.fetch_one(_CLAIM_PURCHASE_SQL_SQLITE, params)
        # None means another worker claimed it, or it went terminal, between the SELECT and here.
        # Expected, not an error: skip it.
        claimed_row = _purchase(row)
        if claimed_row is not None:
            claimed.append(claimed_row)
    return claimed


async def release_claim(
    purchase_id: str,
    worker_id: str,
    *,
    next_poll_at: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Give the lease back, optionally scheduling the next poll. None when this worker no longer
    holds the claim — the same fence, and the same answer, as a fenced `transition`."""
    _require_worker_id(worker_id, "worker_id")
    values = {
        "id": purchase_id,
        "worker_id": worker_id,
        "next_poll_at": _bind_dt(next_poll_at),
    }
    if IS_POSTGRES:
        return _purchase(await database.fetch_one(_RELEASE_CLAIM_SQL, values))
    return _purchase(await database.fetch_one(_RELEASE_CLAIM_SQL_SQLITE, values))


async def requeue_stale_claims(*, lease_seconds: int = 300, limit: int = 50) -> int:
    """Free leases held past `lease_seconds`; return how many moved.

    This is the only thing that recovers a purchase whose worker died mid-poll, and it is the ONE
    place the lease length is stated. The cutoff is server-side — see the constant above.

    A lease shorter than 30 seconds is a ValueError, not a silently-raised floor. This used to be
    `max(30, lease_seconds)`: a caller asking for a 5-second lease got 30 and no indication, which
    is the shape where an operator tunes a number and watches nothing change.
    """
    seconds = _require_int(lease_seconds, "lease_seconds", minimum=30)
    capped = _sweep_limit(limit)
    if IS_POSTGRES:
        rows = await database.fetch_all(
            _REQUEUE_STALE_CLAIMS_SQL, {"lease_seconds": seconds, "limit": capped}
        )
    else:
        rows = await database.fetch_all(
            _REQUEUE_STALE_CLAIMS_SQL_SQLITE,
            {"lease_window": f"-{seconds} seconds", "limit": capped},
        )
    return len(rows)


# ── the two sweeps the poller package needs and cannot build safely itself ───────────────────
#
# Both are ONE conditional UPDATE over a BOUNDED set of rows, and both write a terminal state —
# which means both must do everything a terminal transition does: NULL the PII, stamp
# terminal_at, and clear the claim. Written out here rather than left to the poller to assemble
# from `transition` in a loop, because a loop is N round trips with N chances to be interrupted
# halfway, and the half that gets skipped is the PII.
#
# THEY ARE BOUNDED, AND THE BOUND IS NOT COSMETIC. Prod and staging share one Postgres. An
# unbounded UPDATE on first arming — when the whole backlog qualifies at once — is a lock window
# over every matching row at the same time. `WHERE id IN (SELECT ... ORDER BY ... LIMIT :limit)`
# is the shape the requeue already uses; both return the ids they moved, so the caller loops
# until fewer than `limit` come back.
#
# THE STATE PREDICATE IS REPEATED IN THE OUTER `WHERE`, AND THAT REPETITION IS THE RACE GUARD.
# Found by this package's own two-connection test, not reasoned about in advance: with the
# predicate ONLY inside the LIMIT subquery, two concurrent sweeps BOTH moved the same row and
# both reported it (measured, `assert (1 + 1) == 1`). Under READ COMMITTED the second UPDATE
# blocks on the row lock and then re-checks its qual — but the subplan it re-checks has already
# been materialised, so `id IN (…)` is still true and the row is written a second time, its
# terminal_at overwritten and its id counted twice. Re-stating `state IN (…)` at the top level
# gives the re-check something that is actually false by then. The inner copy stays, because it
# is what makes the LIMIT select the right candidates in the first place; a test asserts the two
# lists are identical.
#
# THE SOURCE STATES ARE PARSED OUT OF THESE STATEMENTS (see `_states_in`), so
# tests/test_reap_agentic_ledger.py::test_the_bulk_sweeps_only_use_legal_edges checks the edges
# the SQL will really take against ALLOWED_TRANSITIONS. While the two were written out
# separately, that test watched a tuple the SQL never read, and adding 'processing' to the expire
# sweep's list — expiring a purchase the buyer had ALREADY APPROVED — survived both dialects.

# The expire sweep takes a row on EITHER of two clocks, and the second one is the reason the
# first is not enough:
#
#   hosted_url_expires_at — Reap told us when the approval page dies. Precise, and absent
#                           whenever we never got a hosted URL, or got one without an expiry.
#   updated_at + max_age  — the ABSOLUTE fallback. Without it a row in 'needs_enrollment' or
#                           'awaiting_approval' with a NULL hosted_url_expires_at is never
#                           expired by anything, and keeps the buyer's address and email
#                           indefinitely. The docstring used to claim this sweep was a PII
#                           deadline; with only the first clock that was false for exactly the
#                           rows most likely to be abandoned.
_EXPIRE_OVERDUE_SQL = """
    UPDATE reap_agentic_purchases
       SET state = 'expired',
           shipping_address = NULL,
           buyer_email = NULL,
           claimed_by = NULL,
           claimed_at = NULL,
           last_error_code = COALESCE(last_error_code, 'hosted_url_expired'),
           terminal_at = clock_timestamp(),
           state_entered_at = clock_timestamp(),
           updated_at = clock_timestamp()
     WHERE state IN ('needs_enrollment', 'awaiting_approval')
       AND id IN (
        SELECT id FROM reap_agentic_purchases
         WHERE state IN ('needs_enrollment', 'awaiting_approval')
           AND (
                (hosted_url_expires_at IS NOT NULL
                 AND hosted_url_expires_at < clock_timestamp())
                OR state_entered_at < clock_timestamp() - (:max_age_seconds * INTERVAL '1 second')
           )
         ORDER BY state_entered_at ASC, id ASC
         LIMIT :limit
     )
    RETURNING id
"""

_EXPIRE_OVERDUE_SQL_SQLITE = """
    UPDATE reap_agentic_purchases
       SET state = 'expired',
           shipping_address = NULL,
           buyer_email = NULL,
           claimed_by = NULL,
           claimed_at = NULL,
           last_error_code = COALESCE(last_error_code, 'hosted_url_expired'),
           terminal_at = CURRENT_TIMESTAMP,
           state_entered_at = CURRENT_TIMESTAMP,
           updated_at = CURRENT_TIMESTAMP
     WHERE state IN ('needs_enrollment', 'awaiting_approval')
       AND id IN (
        SELECT id FROM reap_agentic_purchases
         WHERE state IN ('needs_enrollment', 'awaiting_approval')
           AND (
                (hosted_url_expires_at IS NOT NULL
                 AND hosted_url_expires_at < CURRENT_TIMESTAMP)
                OR state_entered_at < datetime('now', :max_age_window)
           )
         ORDER BY state_entered_at ASC, id ASC
         LIMIT :limit
     )
    RETURNING id
"""

_FAIL_EXHAUSTED_SQL = """
    UPDATE reap_agentic_purchases
       SET state = 'failed',
           last_error_code = 'attempts_exhausted',
           shipping_address = NULL,
           buyer_email = NULL,
           claimed_by = NULL,
           claimed_at = NULL,
           terminal_at = clock_timestamp(),
           state_entered_at = clock_timestamp(),
           updated_at = clock_timestamp()
     WHERE state IN (
           'resolving', 'needs_enrollment', 'quoting', 'awaiting_approval', 'processing'
       )
       AND (:include_processing = 1 OR state <> 'processing')
       AND attempts >= :max_attempts
       AND id IN (
        SELECT id FROM reap_agentic_purchases
         WHERE state IN (
               'resolving', 'needs_enrollment', 'quoting', 'awaiting_approval', 'processing'
           )
           AND (:include_processing = 1 OR state <> 'processing')
           AND attempts >= :max_attempts
         ORDER BY attempts DESC, id ASC
         LIMIT :limit
     )
    RETURNING id
"""

_FAIL_EXHAUSTED_SQL_SQLITE = """
    UPDATE reap_agentic_purchases
       SET state = 'failed',
           last_error_code = 'attempts_exhausted',
           shipping_address = NULL,
           buyer_email = NULL,
           claimed_by = NULL,
           claimed_at = NULL,
           terminal_at = CURRENT_TIMESTAMP,
           state_entered_at = CURRENT_TIMESTAMP,
           updated_at = CURRENT_TIMESTAMP
     WHERE state IN (
           'resolving', 'needs_enrollment', 'quoting', 'awaiting_approval', 'processing'
       )
       AND (:include_processing = 1 OR state <> 'processing')
       AND attempts >= :max_attempts
       AND id IN (
        SELECT id FROM reap_agentic_purchases
         WHERE state IN (
               'resolving', 'needs_enrollment', 'quoting', 'awaiting_approval', 'processing'
           )
           AND (:include_processing = 1 OR state <> 'processing')
           AND attempts >= :max_attempts
         ORDER BY attempts DESC, id ASC
         LIMIT :limit
     )
    RETURNING id
"""

# Parsed out of the statements above, not written alongside them. See `_states_in`.
_EXPIRE_SOURCE_STATES = _states_in(_EXPIRE_OVERDUE_SQL, "WHERE state IN (")
_FAIL_EXHAUSTED_SOURCE_STATES = _states_in(_FAIL_EXHAUSTED_SQL, "WHERE state IN (")


def _require_int(value: Any, name: str, *, minimum: int, maximum: Optional[int] = None) -> int:
    """A STRICT integer bound. No `int()` coercion, and `bool` is not an integer here.

    `int()` is quietly generous in ways that matter for these particular parameters: `int(True)`
    is 1, `int(2.9)` is 2, `int("5")` is 5. Every one of these parameters expresses a bound an
    operator chose — a lease length, an age deadline, an attempt ceiling, a batch size — and
    silently reading `limit=2.9` as 2 or `limit=True` as 1 turns a typo into a production setting
    nobody can see. Measured before this: `expire_overdue_purchases(limit=True)` ran happily, and
    so did `limit=2.9` and `fail_exhausted_purchases("5")`.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"{name} must be an int (got {type(value).__name__} {value!r}); this is a bound, "
            "not something to coerce"
        )
    if value < minimum or (maximum is not None and value > maximum):
        upper = f" and at most {maximum}" if maximum is not None else ""
        raise ValueError(f"{name} must be at least {minimum}{upper} (got {value})")
    return value


def _require_worker_id(value: Any, name: str) -> str:
    """A worker id must be a non-empty STRING.

    `bytes` is refused here rather than at the driver: on SQLite `b"w"` sailed straight through
    `(value or "").strip()` and was STORED as the claim holder, while asyncpg raised a DataError
    naming a parameter index. Two different wrong answers for one typo, neither of them saying
    "worker id".
    """
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a str (got {type(value).__name__} {value!r})")
    if not value.strip():
        raise ValueError(f"{name} must not be blank")
    return value


def _sweep_limit(limit: Any) -> int:
    return _require_int(limit, "limit", minimum=1, maximum=500)


async def expire_overdue_purchases(
    *, max_age_seconds: int = 3600, limit: int = 200
) -> List[str]:
    """Expire purchases waiting on a buyer who never came back; return the ids that moved.

    Two clocks, both the SERVER's: Reap's `hosted_url_expires_at` when we have one, and an
    absolute `state_entered_at + max_age_seconds` fallback for the rows where we do not. The
    fallback is what makes this a PII deadline rather than a best-effort one — a row in
    'awaiting_approval' with no hosted-page expiry was previously expired by nothing at all and
    kept the buyer's address and email indefinitely.

    THE FALLBACK MEASURES `state_entered_at`, NOT `updated_at`, AND THAT DISTINCTION IS THE WHOLE
    GUARD. `updated_at` is written by every claim, every release and every requeue, so a deadline
    measured from it is reset by the poll loop itself and NEVER FIRES at any realistic cadence.
    Measured: a row aged 99999 seconds, then ONE ordinary claim+release, was not expired and kept
    the buyer's address and email. These are precisely the rows with no other bound — `attempts`
    is exempt in both waiting states, and 'needs_enrollment' has no hosted URL of its own at all.
    `state_entered_at` moves only when the STATE moves, which is a clock the poller cannot reset.

    BOUNDED. At most `limit` rows per call; loop until fewer than `limit` come back. An unbounded
    UPDATE on first arming locks every qualifying row at once, on a Postgres that prod and
    staging share.

    UNFENCED, AND THE POLLER MUST KNOW IT. This is a bulk terminal write that takes no `holder`,
    so it can terminate a purchase a live worker currently holds the lease on. That is deliberate
    — a worker holding a lease on an abandoned purchase must not be able to keep it alive — and
    it is safe because the worker's next write goes through the fence and gets None back. The
    poller must treat None from `transition_as_holder` as "re-read", never as "retry harder".
    """
    seconds = _require_int(max_age_seconds, "max_age_seconds", minimum=60)
    capped = _sweep_limit(limit)
    if IS_POSTGRES:
        rows = await database.fetch_all(
            _EXPIRE_OVERDUE_SQL, {"max_age_seconds": seconds, "limit": capped}
        )
    else:
        rows = await database.fetch_all(
            _EXPIRE_OVERDUE_SQL_SQLITE,
            {"max_age_window": f"-{seconds} seconds", "limit": capped},
        )
    return [str(r["id"]) for r in rows]


async def fail_exhausted_purchases(
    max_attempts: int, *, limit: int = 200, include_processing: bool = False
) -> List[str]:
    """Fail non-terminal purchases claimed `max_attempts` times or more; return the ids.

    `attempts` COUNTS CLAIMS, NOT RETRIES, and only in the states where a claim means work was
    tried — 'awaiting_approval' and 'needs_enrollment' are exempt, because a buyer taking an hour
    on the partner's page is not a failure and polling them every 30 seconds would otherwise
    reach ~120 attempts on a perfectly healthy purchase. Those two states are bounded by
    `expire_overdue_purchases` instead, on a clock, which is the right instrument for waiting on
    a person. Choose `max_attempts` against how many times you expect to RETRY, not against poll
    cadence.

    'processing' IS SKIPPED UNLESS YOU ASK FOR IT. A purchase in 'processing' has been APPROVED
    BY THE BUYER and its payment is in flight with Reap. Auto-failing that on an attempt counter
    would write a terminal state over a charge we do not know the outcome of — our ledger saying
    'failed' while the buyer's card says otherwise. `include_processing=True` exists because the
    poller package will eventually need a deliberate answer for a payment stuck in flight; that
    answer belongs to whoever can also reconcile it with Reap, not to a counter in a sweep.

    BOUNDED, for the same reason as the expire sweep.

    UNFENCED, like the expire sweep: it takes no `holder` and can terminate a purchase a live
    worker holds. The worker's next write then returns None through the fence, which the poller
    must read as "re-read", never as "retry harder".
    """
    attempts_bound = _require_int(max_attempts, "max_attempts", minimum=1)
    capped = _sweep_limit(limit)
    params = {
        "max_attempts": attempts_bound,
        "limit": capped,
        "include_processing": 1 if include_processing else 0,
    }
    if IS_POSTGRES:
        rows = await database.fetch_all(_FAIL_EXHAUSTED_SQL, params)
    else:
        rows = await database.fetch_all(_FAIL_EXHAUSTED_SQL_SQLITE, params)
    return [str(r["id"]) for r in rows]


# ── enrollments ──────────────────────────────────────────────────────────────────────────────

_SELECT_PENDING_ENROLLMENT_SQL = """
    SELECT * FROM reap_agentic_enrollments
     WHERE buyer_ref = :buyer_ref AND status = 'pending'
     ORDER BY created_at DESC, id DESC
     LIMIT 1
"""

_UPDATE_PENDING_ENROLLMENT_SQL = """
    UPDATE reap_agentic_enrollments
       SET agent_id = COALESCE(:agent_id, agent_id),
           reap_enrollment_id = COALESCE(:reap_enrollment_id, reap_enrollment_id),
           reap_status = COALESCE(:reap_status, reap_status),
           hosted_url = COALESCE(:hosted_url, hosted_url),
           hosted_url_expires_at = COALESCE(:hosted_url_expires_at, hosted_url_expires_at),
           updated_at = clock_timestamp()
     WHERE id = :id
       AND status = 'pending'
    RETURNING *
"""

_UPDATE_PENDING_ENROLLMENT_SQL_SQLITE = """
    UPDATE reap_agentic_enrollments
       SET agent_id = COALESCE(:agent_id, agent_id),
           reap_enrollment_id = COALESCE(:reap_enrollment_id, reap_enrollment_id),
           reap_status = COALESCE(:reap_status, reap_status),
           hosted_url = COALESCE(:hosted_url, hosted_url),
           hosted_url_expires_at = COALESCE(:hosted_url_expires_at, hosted_url_expires_at),
           updated_at = CURRENT_TIMESTAMP
     WHERE id = :id
       AND status = 'pending'
    RETURNING *
"""

_INSERT_PENDING_ENROLLMENT_SQL = """
    INSERT INTO reap_agentic_enrollments (
        id, buyer_ref, agent_id, reap_enrollment_id, status, reap_status,
        hosted_url, hosted_url_expires_at
    ) VALUES (
        :id, :buyer_ref, :agent_id, :reap_enrollment_id, 'pending', :reap_status,
        :hosted_url, :hosted_url_expires_at
    )
    RETURNING *
"""

# THE DEMOTION — statement (a). Every OTHER active row for this buyer goes 'dead'. NOT in a
# transaction (see `mark_enrollment_active` for why this module cannot hold one on this driver),
# and NOT before the promotion: it runs only once the unique index has proved the target is
# activatable. Without it a buyer who re-enrols has two active cards and "which card did
# this buyer authorize?" has no answer — the purchase path would pick one arbitrarily. The
# partial unique index uq_reap_agentic_enrollments_one_active is the belt to this brace; on an
# engine or a database where that index is absent, this statement is the only thing holding the
# invariant.
#
# THE SUBSELECT IS GATED ON THE TARGET BEING ACTIVATABLE, and that gate is the fix for a real
# defect: `mark_enrollment_active(<id of a dead row>)` used to demote the buyer's live
# enrollment and THEN fail to promote the dead one, leaving the buyer with no active card at all
# — a worse state than the one they started in, reached by a call that answered None as though
# nothing had happened. With `AND status IN ('pending', 'active')` inside the subselect, a
# non-activatable target yields NULL, `buyer_ref = NULL` matches nothing, and the demotion is a
# no-op. The promotion below then also matches nothing, so the whole call is inert.
#
# 'active' is in that list as well as 'pending' so that re-activating the row that is ALREADY
# active stays idempotent (a redelivered webhook must not be punished); `id <> :id` keeps such a
# call from demoting its own target.
_DEMOTE_OTHER_ACTIVE_SQL = """
    UPDATE reap_agentic_enrollments
       SET status = 'dead',
           updated_at = clock_timestamp()
     WHERE status = 'active'
       AND id <> :id
       AND buyer_ref = (
           SELECT buyer_ref FROM reap_agentic_enrollments
            WHERE id = :id AND status IN ('pending', 'active')
       )
    RETURNING id
"""

_DEMOTE_OTHER_ACTIVE_SQL_SQLITE = """
    UPDATE reap_agentic_enrollments
       SET status = 'dead',
           updated_at = CURRENT_TIMESTAMP
     WHERE status = 'active'
       AND id <> :id
       AND buyer_ref = (
           SELECT buyer_ref FROM reap_agentic_enrollments
            WHERE id = :id AND status IN ('pending', 'active')
       )
    RETURNING id
"""

# STATEMENT (b). `status IN ('pending', 'active')` is a real guard, not a formality: without it,
# `mark_enrollment_active(<id of a DEAD row>)` RESURRECTS A REVOKED CARD. That is the whole
# reason `mark_enrollment_dead` exists, undone by a retry of an activation that should have
# stopped. 'active' is in the list so a redelivered webhook re-activating the row that is
# already active is idempotent rather than punished.
#
# tests/test_reap_agentic_ledger.py::test_activating_a_dead_row_with_no_competitor_stays_dead is
# what kills its removal. The older test could not: it had a competing active row, so deleting
# this conjunct still produced None — via the unique index, through the except, for entirely the
# wrong reason. A guard whose test passes for the wrong reason is not a tested guard.
_ACTIVATE_ENROLLMENT_SQL = """
    UPDATE reap_agentic_enrollments
       SET status = 'active',
           reap_enrollment_id = COALESCE(:reap_enrollment_id, reap_enrollment_id),
           reap_status = COALESCE(:reap_status, reap_status),
           card_network = COALESCE(:card_network, card_network),
           card_last4 = COALESCE(:card_last4, card_last4),
           hosted_url = NULL,
           hosted_url_expires_at = NULL,
           updated_at = clock_timestamp()
     WHERE id = :id
       AND status IN ('pending', 'active')
    RETURNING *
"""

_ACTIVATE_ENROLLMENT_SQL_SQLITE = """
    UPDATE reap_agentic_enrollments
       SET status = 'active',
           reap_enrollment_id = COALESCE(:reap_enrollment_id, reap_enrollment_id),
           reap_status = COALESCE(:reap_status, reap_status),
           card_network = COALESCE(:card_network, card_network),
           card_last4 = COALESCE(:card_last4, card_last4),
           hosted_url = NULL,
           hosted_url_expires_at = NULL,
           updated_at = CURRENT_TIMESTAMP
     WHERE id = :id
       AND status IN ('pending', 'active')
    RETURNING *
"""

_MARK_ENROLLMENT_DEAD_SQL = """
    UPDATE reap_agentic_enrollments
       SET status = 'dead',
           reap_status = COALESCE(:reap_status, reap_status),
           hosted_url = NULL,
           hosted_url_expires_at = NULL,
           updated_at = clock_timestamp()
     WHERE id = :id
       AND status <> 'dead'
    RETURNING *
"""

_MARK_ENROLLMENT_DEAD_SQL_SQLITE = """
    UPDATE reap_agentic_enrollments
       SET status = 'dead',
           reap_status = COALESCE(:reap_status, reap_status),
           hosted_url = NULL,
           hosted_url_expires_at = NULL,
           updated_at = CURRENT_TIMESTAMP
     WHERE id = :id
       AND status <> 'dead'
    RETURNING *
"""

_SELECT_ACTIVE_ENROLLMENT_SQL = """
    SELECT * FROM reap_agentic_enrollments
     WHERE buyer_ref = :buyer_ref AND status = 'active'
     ORDER BY updated_at DESC, id DESC
     LIMIT 1
"""


async def upsert_pending_enrollment(
    *,
    buyer_ref: str,
    agent_id: Optional[str] = None,
    reap_enrollment_id: Optional[str] = None,
    reap_status: Optional[str] = None,
    hosted_url: Optional[str] = None,
    hosted_url_expires_at: Optional[datetime] = None,
    enrollment_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Mint or refresh the buyer's PENDING enrollment.

    Upsert rather than insert because the hosted page EXPIRES: a buyer who opens the enrol link,
    walks away, and comes back needs a new hosted_url on the row that is already pending, not a
    second pending row. Existing 'active' and 'dead' rows are untouched — this only ever refreshes
    a pending one.
    """
    if not (buyer_ref or "").strip():
        raise ValueError("buyer_ref is required")
    updates = {
        "agent_id": agent_id,
        "reap_enrollment_id": reap_enrollment_id,
        "reap_status": reap_status,
        "hosted_url": hosted_url,
        "hosted_url_expires_at": _bind_dt(hosted_url_expires_at),
    }
    if enrollment_id is None:
        existing = await database.fetch_one(
            _SELECT_PENDING_ENROLLMENT_SQL, {"buyer_ref": buyer_ref}
        )
        enrollment_id = str(existing["id"]) if existing is not None else None
    if enrollment_id is not None:
        if IS_POSTGRES:
            updated = await database.fetch_one(
                _UPDATE_PENDING_ENROLLMENT_SQL, {**updates, "id": enrollment_id}
            )
        else:
            updated = await database.fetch_one(
                _UPDATE_PENDING_ENROLLMENT_SQL_SQLITE, {**updates, "id": enrollment_id}
            )
        # None means the row stopped being pending between the read and the write (the buyer
        # finished enrolling). Fall through and mint a fresh pending row rather than resurrect
        # an active one.
        if updated is not None:
            result = _enrollment(updated)
            assert result is not None
            return result
    row = await database.fetch_one(
        _INSERT_PENDING_ENROLLMENT_SQL,
        {**updates, "id": new_enrollment_id(), "buyer_ref": buyer_ref},
    )
    created = _enrollment(row)
    if created is None:  # pragma: no cover
        raise RuntimeError("enrollment insert returned no row")
    return created


async def mark_enrollment_active(
    enrollment_id: str,
    *,
    reap_enrollment_id: Optional[str] = None,
    reap_status: Optional[str] = None,
    card_network: Optional[str] = None,
    card_last4: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Promote an enrollment to 'active', demoting any other active row for the same buyer.

    Returns None when the target is not activatable — it is 'dead', or it does not exist, or
    another activation for the same buyer won a concurrent race. When the target was never
    activatable the buyer's existing active enrollment is left alone; see the window note below
    for the one case where it is not, which is narrow and no longer unrecoverable.

    ── NO TRANSACTION. TWO AUTOCOMMIT STATEMENTS. ───────────────────────────────────────────

    This used to hold the module's only `database.transaction()`, and an earlier version of this
    docstring argued that it had to. That argument was WRONG, and it was wrong in the specific
    way this repo has been bitten before: databases==0.7.0 scopes its connection by ContextVar,
    so tasks launched with `asyncio.gather` on the app's SHARED `database` object end up inside
    each other's transaction. Measured on real Postgres, deterministically, 5 runs out of 5:

        two DIFFERENT buyers — one task raises TransactionEndedOutOfOrder and ITS WRITE IS
            ROLLED BACK; the other raises NoActiveSQLTransactionError for a write that DID
            commit. Two unrelated buyers, and one of them loses an enrollment they completed.
        the SAME buyer, two pending rows — one raises, the other returns None, and NEITHER is
            activated. A buyer who enrolled ends up with no active card and no error anyone
            could act on.

    A transaction that behaves like that is not protection, so it is gone. What replaces it is
    two statements, each safe to interleave and idempotent to retry:

        (a) demote the buyer's OTHER active rows, gated on the target being activatable;
        (b) activate the target, `WHERE id = :id AND status IN ('pending', 'active')`.

    ── (b) RUNS FIRST, AND THAT ORDER IS THE FIX FOR A REAL DEFECT ──────────────────────────

    Running (a) first meant demoting the buyer's live enrollment before knowing whether the
    promotion could succeed. A concurrent `mark_enrollment_dead(target)` landing in between left
    BOTH rows dead — the buyer with ZERO active enrollments — and no retry heals it, because the
    target is now permanently unactivatable. Verified by the reviewer, and reproduced here on
    both dialects before this reordering.

    So: try (b) first. Three outcomes, and none of them can strand the buyer.

        (b) returns a row   — done. Nothing was demoted because nothing needed to be.
        (b) returns None    — the target was not activatable ('dead', or gone). NOTHING IS
                              DEMOTED. The live enrollment is untouched, which is the property
                              the old order could not offer.
        (b) raises UNIQUE   — the index refused because another row for this buyer is active,
                              which ALSO proves the target itself is activatable. Demoting is
                              now known to be safe: run (a), then (b) once more.

    The demotion therefore never happens unless the promotion is about to succeed.

    ── THE REMAINING WINDOW, STATED HONESTLY ────────────────────────────────────────────────

    It is not zero. Between the demotion and the retry of (b) the buyer momentarily has no
    active enrollment, and if the process dies there, a retry of the SAME activation completes
    it ((a) is then a no-op and (b) accepts 'pending'). What no longer happens is the
    unrecoverable case: the live row is not demoted on behalf of a target that turns out to be
    dead. The buyer never sees TWO active rows in any ordering, because the partial unique index
    makes two impossible.

    A caller that reads `get_active_enrollment` inside the window sees None and does what it does
    for any un-enrolled buyer — re-read, or start enrollment.

    THE UNIQUE-VIOLATION CATCH WRAPS EXACTLY THE (b) CALLS — they are the only statements here
    that can raise one, and with no transaction in play there is nothing else in scope to roll
    back or accidentally commit. Every other lost race in this module answers None, and so does
    this one: a caller handed a raw driver exception cannot tell "you lost" from "the database is
    broken".

    card_last4 is the ONLY digits of the card that may be stored, and card_network the only other
    card attribute. Nothing else from Reap's payload belongs in this table.
    """
    if card_last4 is not None and (len(card_last4) != 4 or not card_last4.isdigit()):
        raise ValueError("card_last4 must be exactly four digits, or None")

    values = {
        "id": enrollment_id,
        "reap_enrollment_id": reap_enrollment_id,
        "reap_status": reap_status,
        "card_network": card_network,
        "card_last4": card_last4,
    }

    async def _activate():
        """Statement (b). The ONLY statement here that can hit the partial unique index."""
        if IS_POSTGRES:
            return await database.fetch_one(_ACTIVATE_ENROLLMENT_SQL, values)
        return await database.fetch_one(_ACTIVATE_ENROLLMENT_SQL_SQLITE, values)

    async def _demote():
        """Statement (a). Gated on the target being activatable, so a dead or missing target
        demotes nothing even if it somehow reaches here."""
        if IS_POSTGRES:
            await database.fetch_all(_DEMOTE_OTHER_ACTIVE_SQL, {"id": enrollment_id})
        else:
            await database.fetch_all(_DEMOTE_OTHER_ACTIVE_SQL_SQLITE, {"id": enrollment_id})

    # (b) FIRST. See the docstring: demoting before knowing whether the promotion can succeed is
    # what left a buyer with zero active enrollments when the target died in between.
    try:
        row = await _activate()
    except Exception as exc:  # noqa: BLE001 — narrowed immediately
        if not _is_unique_violation(exc):
            raise
        # The index refused because ANOTHER row for this buyer is active — which also means the
        # target IS activatable, so demoting is now known to be safe. Demote, then try once more.
        await _demote()
        try:
            row = await _activate()
        except Exception as retry_exc:  # noqa: BLE001
            if _is_unique_violation(retry_exc):
                # A third party activated something else in the gap. Lost race: None, like every
                # other lost race here.
                return None
            raise
    return _enrollment(row)


async def mark_enrollment_dead(
    enrollment_id: str, *, reap_status: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Retire an enrollment. None when it was already dead — idempotent, not an error."""
    values = {"id": enrollment_id, "reap_status": reap_status}
    if IS_POSTGRES:
        return _enrollment(await database.fetch_one(_MARK_ENROLLMENT_DEAD_SQL, values))
    return _enrollment(await database.fetch_one(_MARK_ENROLLMENT_DEAD_SQL_SQLITE, values))


async def get_active_enrollment(buyer_ref: str) -> Optional[Dict[str, Any]]:
    """The read every purchase makes before it quotes. At most one row can match — see
    uq_reap_agentic_enrollments_one_active and `mark_enrollment_active`."""
    return _enrollment(
        await database.fetch_one(_SELECT_ACTIVE_ENROLLMENT_SQL, {"buyer_ref": buyer_ref})
    )
