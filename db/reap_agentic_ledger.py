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
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence

from db.database import IS_POSTGRES, database

# The minor-unit converter. REUSED, not reimplemented: services/reap_webhooks.major_to_minor is
# already the one place this repo turns a decimal major-unit amount into an integer minor-unit
# one, it already refuses (rather than rounds) NaN/inf/non-positive/over-precise values and
# already knows the zero-decimal currencies. A second implementation would be a second rounding
# policy, and the rounding policy is the whole point of the function.
#
# It returns Optional[int] — None IS the refusal — where this package's brief asked for `-> int`.
# The repo's contract wins: None is what every existing caller checks for, and raising here would
# make this module the odd one out on the same rail. Callers must handle None.
#
# services/reap_webhooks imports nothing from db/, so this module-level import is not a cycle
# (unlike db/agent_card_auth_decisions.py's lazy import of services.reap_external_auth, which
# is).
from services.reap_webhooks import major_to_minor  # noqa: F401  (re-exported on purpose)

# `get_purchase_internal` is deliberately ABSENT: it is the unscoped, unredacted read, and
# leaving it out of the public surface is half of what stops a route reaching for it. The other
# half is its name.
__all__ = [
    "ALLOWED_TRANSITIONS",
    "PURCHASE_STATES",
    "TERMINAL_STATES",
    "amount_minor_or_none",
    "public_purchase_view",
    "create_purchase",
    "get_purchase_for_owner",
    "list_purchases_for_owner",
    "transition",
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

    A thin, named wrapper over `services.reap_webhooks.major_to_minor` — which is the one
    converter in this repo and REFUSES rather than rounds (NaN, inf, a binary float, a negative,
    an amount with more decimals than the currency has, or one over the ceiling all come back
    None). It is not re-implemented here, because a second implementation would be a second
    rounding policy and the rounding policy is the entire point of the function.

    WHY A WRAPPER AT ALL, rather than a bare re-export. The re-export in the first cut of this
    module was imported, named in `__all__`, used by nothing and tested by nothing — decoration
    that reads as an endorsed entry point. This has the caller this module actually needs
    (`our_price_minor`, `quoted_total_minor`, `final_total_minor` are all BIGINT minor units and
    every one of them arrives from an upstream decimal) and it has tests. The name says what the
    None means, which the upstream name does not.
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

# States the poller may hold a lease on. Deliberately NOT "everything non-terminal minus
# nothing": it is spelled out so that adding a state to the machine forces a decision about
# whether the poller owns it.
_POLLABLE_STATES = ("resolving", "needs_enrollment", "quoting", "awaiting_approval", "processing")

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


# Columns `public_purchase_view` strips. Two different kinds of thing, both of which an agent's
# response has no use for:
#
#   PII and buyer identity — shipping_address, buyer_email, buyer_ref, agent_user_ref_hash.
#       The first two are the columns this rail NULLs on terminal; returning them from a read
#       before then would put them in whatever the caller logs, which is the leak the nulling
#       exists to prevent, arriving by a different door.
#   internals — claimed_by, claimed_at (poller bookkeeping), reap_product_id, reap_variant_id
#       (EVIDENCE, and a substituted variant id would read as identity to anyone downstream),
#       queries_tried (our resolver's search terms).
_PRIVATE_PURCHASE_COLUMNS = frozenset({
    "shipping_address",
    "buyer_email",
    "buyer_ref",
    "agent_user_ref_hash",
    "claimed_by",
    "claimed_at",
    "reap_product_id",
    "reap_variant_id",
    "queries_tried",
})


def public_purchase_view(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Project a purchase row down to what an agent-facing caller may see.

    An ALLOW-by-removal rather than an allowlist of kept keys, deliberately: a column added to
    the table later shows up in this view, which is visible and reviewable, instead of silently
    vanishing from every response. A column that must NOT show up goes in the frozenset above,
    and tests/test_reap_agentic_ledger.py asserts the set is actually absent from the default
    owner reads.
    """
    if row is None:
        return None
    return {key: value for key, value in row.items() if key not in _PRIVATE_PURCHASE_COLUMNS}


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
        CAST(:queries_tried AS JSONB), COALESCE(:next_poll_at, CURRENT_TIMESTAMP),
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
                THEN CURRENT_TIMESTAMP ELSE terminal_at END,
           updated_at = CURRENT_TIMESTAMP
     WHERE id = :id
       AND state IN (:from_0, :from_1, :from_2, :from_3, :from_4, :from_5, :from_6, :from_7, :from_8)
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
           updated_at = CURRENT_TIMESTAMP
     WHERE id = :id
       AND state IN (:from_0, :from_1, :from_2, :from_3, :from_4, :from_5, :from_6, :from_7, :from_8)
    RETURNING *
"""


async def transition(
    purchase_id: str,
    *,
    from_states: Iterable[str],
    to_state: str,
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

    values: Dict[str, Any] = {
        "id": purchase_id,
        "to_state": to_state,
        "to_state_probe": to_state,
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
_SELECT_DUE_PURCHASES_SQL = """
    SELECT id FROM reap_agentic_purchases
     WHERE state IN ('resolving', 'needs_enrollment', 'quoting', 'awaiting_approval', 'processing')
       AND claimed_by IS NULL
       AND next_poll_at IS NOT NULL
       AND next_poll_at <= CURRENT_TIMESTAMP
     ORDER BY next_poll_at ASC, id ASC
     LIMIT :limit
"""

_CLAIM_PURCHASE_SQL = """
    UPDATE reap_agentic_purchases
       SET claimed_by = :worker_id,
           claimed_at = CURRENT_TIMESTAMP,
           attempts = attempts + 1,
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
_RELEASE_CLAIM_SQL = """
    UPDATE reap_agentic_purchases
       SET claimed_by = NULL,
           claimed_at = NULL,
           next_poll_at = COALESCE(:next_poll_at, next_poll_at),
           updated_at = CURRENT_TIMESTAMP
     WHERE id = :id
       AND claimed_by = :worker_id
    RETURNING *
"""

# THE CUTOFF IS COMPUTED BY THE SERVER. `CURRENT_TIMESTAMP - interval` binds no datetime at all,
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
           updated_at = CURRENT_TIMESTAMP
     WHERE id IN (
        SELECT id FROM reap_agentic_purchases
         WHERE claimed_by IS NOT NULL
           AND claimed_at IS NOT NULL
           AND claimed_at < CURRENT_TIMESTAMP - (:lease_seconds * INTERVAL '1 second')
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


async def claim_due_purchases(worker_id: str, *, limit: int = 10) -> List[Dict[str, Any]]:
    """Take a lease on up to `limit` purchases whose next_poll_at has come.

    THERE IS NO `lease_seconds` PARAMETER, and there used to be. It was accepted here, documented
    as "what requeue_stale_claims measures against", and connected to nothing: the lease length
    lives entirely in `requeue_stale_claims`, and a caller that passed 60 here and took the
    default 300 there would have had a 300-second lease and no way to tell. A parameter that
    reads as configuration and configures nothing is worse than no parameter.

    A claim is released either by `release_claim` (the worker finished), by a transition into a
    terminal state (which clears it in the same UPDATE), or by `requeue_stale_claims` (the worker
    died).
    """
    if not (worker_id or "").strip():
        raise ValueError("worker_id is required — an anonymous claim cannot be requeued")
    candidates = await database.fetch_all(
        _SELECT_DUE_PURCHASES_SQL, {"limit": max(1, min(500, int(limit)))}
    )
    claimed: List[Dict[str, Any]] = []
    for candidate in candidates:
        row = await database.fetch_one(
            _CLAIM_PURCHASE_SQL, {"id": candidate["id"], "worker_id": worker_id}
        )
        # None means another worker claimed it between the SELECT and here. Expected, not an
        # error: skip it.
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
    holds the claim."""
    return _purchase(
        await database.fetch_one(
            _RELEASE_CLAIM_SQL,
            {
                "id": purchase_id,
                "worker_id": worker_id,
                "next_poll_at": _bind_dt(next_poll_at),
            },
        )
    )


async def requeue_stale_claims(*, lease_seconds: int = 300, limit: int = 50) -> int:
    """Free leases held past `lease_seconds`; return how many moved.

    This is the only thing that recovers a purchase whose worker died mid-poll, and it is the ONE
    place the lease length is stated. The cutoff is server-side — see the constant above.

    A lease shorter than 30 seconds is a ValueError, not a silently-raised floor. This used to be
    `max(30, lease_seconds)`: a caller asking for a 5-second lease got 30 and no indication, which
    is the shape where an operator tunes a number and watches nothing change.
    """
    if int(lease_seconds) < 30:
        raise ValueError(
            f"lease_seconds must be at least 30 (got {lease_seconds}); a shorter lease requeues "
            "work a live worker is still doing"
        )
    seconds = int(lease_seconds)
    capped = max(1, min(500, int(limit)))
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
# Both are ONE conditional UPDATE over a set of rows, and both write a terminal state — which
# means both must do everything a terminal transition does: NULL the PII, stamp terminal_at, and
# clear the claim. Written out here rather than left to the poller to assemble from `transition`
# in a loop, because a loop is N round trips with N chances to be interrupted halfway, and the
# half that gets skipped is the PII.
#
# THE SOURCE STATES ARE CONSTANTS SO A TEST CAN CHECK THEM AGAINST ALLOWED_TRANSITIONS. These
# statements bypass `transition`, so nothing at runtime enforces the map on them; the edges are
# asserted by tests/test_reap_agentic_ledger.py::test_the_bulk_sweeps_only_use_legal_edges on
# both dialects. Both lists are legal today: needs_enrollment -> expired and
# awaiting_approval -> expired exist, and every pollable state permits -> failed.
_EXPIRE_SOURCE_STATES = ("needs_enrollment", "awaiting_approval")
_FAIL_EXHAUSTED_SOURCE_STATES = (
    "resolving", "needs_enrollment", "quoting", "awaiting_approval", "processing",
)

# Portable as written: CURRENT_TIMESTAMP and a literal IN list mean no dialect split is needed,
# which is the better answer than two constants whenever it is available (see
# services/pdp_governance_service.py for the same call).
_EXPIRE_OVERDUE_SQL = """
    UPDATE reap_agentic_purchases
       SET state = 'expired',
           shipping_address = NULL,
           buyer_email = NULL,
           claimed_by = NULL,
           claimed_at = NULL,
           last_error_code = COALESCE(last_error_code, 'hosted_url_expired'),
           terminal_at = CURRENT_TIMESTAMP,
           updated_at = CURRENT_TIMESTAMP
     WHERE state IN ('needs_enrollment', 'awaiting_approval')
       AND hosted_url_expires_at IS NOT NULL
       AND hosted_url_expires_at < CURRENT_TIMESTAMP
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
           terminal_at = CURRENT_TIMESTAMP,
           updated_at = CURRENT_TIMESTAMP
     WHERE state IN (
           'resolving', 'needs_enrollment', 'quoting', 'awaiting_approval', 'processing'
       )
       AND attempts >= :max_attempts
    RETURNING id
"""


async def expire_overdue_purchases() -> List[str]:
    """Expire purchases whose hosted approval page has run out; return the ids that moved.

    The clock is the SERVER's, never the caller's — same rule and same reason as the requeue's
    cutoff. A buyer who never finished on Reap's page leaves a row that would otherwise sit in
    'awaiting_approval' holding their address and email indefinitely, so this is a PII deadline
    as much as a state one.
    """
    rows = await database.fetch_all(_EXPIRE_OVERDUE_SQL)
    return [str(r["id"]) for r in rows]


async def fail_exhausted_purchases(max_attempts: int) -> List[str]:
    """Fail non-terminal purchases that have been claimed `max_attempts` times; return the ids.

    `attempts` counts CLAIMS, so this is "the poller has picked this up N times and it is still
    not finished" — the backstop that stops a permanently-stuck row being leased forever.
    """
    if int(max_attempts) < 1:
        raise ValueError(f"max_attempts must be at least 1 (got {max_attempts})")
    rows = await database.fetch_all(
        _FAIL_EXHAUSTED_SQL, {"max_attempts": int(max_attempts)}
    )
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

# THE DEMOTION. Every OTHER active row for this buyer goes 'dead' in the same transaction that
# promotes this one. Without it a buyer who re-enrols has two active cards and "which card did
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
           updated_at = CURRENT_TIMESTAMP
     WHERE status = 'active'
       AND id <> :id
       AND buyer_ref = (
           SELECT buyer_ref FROM reap_agentic_enrollments
            WHERE id = :id AND status IN ('pending', 'active')
       )
    RETURNING id
"""

_ACTIVATE_ENROLLMENT_SQL = """
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
        updated = await database.fetch_one(
            _UPDATE_PENDING_ENROLLMENT_SQL, {**updates, "id": enrollment_id}
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

    Returns None when the target is not activatable (it is 'dead', or it does not exist) and when
    another activation for the same buyer won a concurrent race. In BOTH cases nothing changed —
    in particular the buyer's existing active enrollment is left alone, which is the property the
    ungated version of this function did not have.

    Both statements run in ONE transaction: a demotion that commits without the promotion leaves
    the buyer with no active card, and a promotion that commits without the demotion leaves two —
    which the partial unique index would reject anyway, but only on a database where that index
    exists.

    THE UNIQUE VIOLATION IS CAUGHT OUTSIDE THE `async with`, NOT INSIDE IT. Catching inside would
    let the transaction COMMIT the demotion it had already done, which is the exact "buyer left
    with no active card" outcome this function exists to avoid; letting the exception leave the
    block rolls the demotion back first. Every other lost race in this module answers None, and
    so does this one — a caller that got a raw driver exception could not tell "you lost" from
    "the database is broken".

    card_last4 is the ONLY digits of the card that may be stored, and card_network the only other
    card attribute. Nothing else from Reap's payload belongs in this table.
    """
    if card_last4 is not None and (len(card_last4) != 4 or not card_last4.isdigit()):
        raise ValueError("card_last4 must be exactly four digits, or None")
    try:
        async with database.transaction():
            await database.fetch_all(_DEMOTE_OTHER_ACTIVE_SQL, {"id": enrollment_id})
            row = await database.fetch_one(
                _ACTIVATE_ENROLLMENT_SQL,
                {
                    "id": enrollment_id,
                    "reap_enrollment_id": reap_enrollment_id,
                    "reap_status": reap_status,
                    "card_network": card_network,
                    "card_last4": card_last4,
                },
            )
    except Exception as exc:  # noqa: BLE001 — narrowed immediately below
        if _is_unique_violation(exc):
            return None
        raise
    return _enrollment(row)


async def mark_enrollment_dead(
    enrollment_id: str, *, reap_status: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Retire an enrollment. None when it was already dead — idempotent, not an error."""
    return _enrollment(
        await database.fetch_one(
            _MARK_ENROLLMENT_DEAD_SQL, {"id": enrollment_id, "reap_status": reap_status}
        )
    )


async def get_active_enrollment(buyer_ref: str) -> Optional[Dict[str, Any]]:
    """The read every purchase makes before it quotes. At most one row can match — see
    uq_reap_agentic_enrollments_one_active and `mark_enrollment_active`."""
    return _enrollment(
        await database.fetch_one(_SELECT_ACTIVE_ENROLLMENT_SQL, {"buyer_ref": buyer_ref})
    )
