"""The REQUEST-TIME door-read fact: what a seller's own UCP door said when the gateway built a cart there.

Tier 2 of the stored agentic-checkout fact. When the gateway builds a cart at a seller's own UCP door
it sends the backend the raw evidence it read (routes/agent_internal_merchant_facts.py); the backend
re-parses it with the census job's own classifier (jobs.merchant_protocol_profile_census
.checkout_check) and hands this module one `Check`. One row per merchant domain in
`merchant_agent_checkout_request_facts` (migration 302): the SAME columns as the census table
(merchant_agent_checkout_facts, migration 294), `source` always 'request'.

A SEPARATE TABLE, AND NOBODY COUNTS IT YET. The run condition reads the census table only
(db.merchant_agent_checkout_facts.run_condition_answer). Whether a request-time positive ever counts
is an owner decision that has not been made, so nothing here is read on the run condition's path,
services/payment_orchestration/agent_run_condition.py does not import this module, and the census
population (`--from-directory`) does not read this table. Tests hold all three as text checks.

THE WRITE RULES, all inside ONE conditional upsert whose every time comparison is the database's own
clock (CURRENT_TIMESTAMP on Postgres, its `datetime('now')` twin on SQLite):

  * UNREADABLE WRITES NOTHING. "Cannot tell" never reaches the statement (the census treats it the
    same way for renewal: it neither renews a positive nor makes a negative).
  * AT MOST ONE WRITE PER DOMAIN PER 24 HOURS. An existing row checked within 24 hours is left as it
    is, whatever the new check says. Two concurrent first writes for one domain leave one row: the
    second meets the first's fresh `checked_at` and writes nothing.
  * A POSITIVE renews a positive, and replaces a negative only once that negative is at least
    14 days old. A positive over a younger negative writes nothing.
  * A NEGATIVE never overwrites a positive that is still fresh (either `*_checkout_at` younger than
    `db.merchant_agent_checkout_facts.max_age_days()`, 30 by default). Otherwise it renews or
    replaces the row and sets its columns exactly as the census `record_check` would.

A NEW ROW is always written (subject only to the check being storable). The columns move exactly as
the census upsert moves them (per path: yes sets, a clean no clears, cannot-tell keeps).

`record_request_check` NEVER RAISES. A missing table (migration 302 is applied in production by hand,
as a gated one-off, after its PR has merged) is a no-op answering False, and its WARNING is
rate-limited to one per `WARNING_INTERVAL_SECONDS` per process, never one per request.

NO STARTUP HEAL. Nothing in db/schema_guard creates the table, so the code can be deployed before the
table exists. The SQLite tests build their own copy (tests/test_merchant_agent_checkout_request_facts.py).

THE KEY is `db.merchant_purchasability.normalize_domain`'s form, the one `record_check` writes under.

WHO IS HELD. Only a domain we already hold may be written (`is_held_domain`): one in the census
population (`tierb_cart_link_eligibility.shop_domain`, `merchant_purchasability.merchant_domain`, the
two tables `--from-directory` reads) or one the census already stored a fact for. A read that fails
answers "not held". Both population tables are written under the same normalized key, so each read
is one equality on that key.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from db.database import IS_POSTGRES, database
from db.merchant_agent_checkout_facts import (
    CODE_OTHER,
    DOES_NOT_PUBLISH,
    PUBLISHES,
    UNREADABLE,
    Check,
    _CODE_RE,
    _VERSION_RE,
    _usable,
    max_age_days,
)
from db.merchant_purchasability import normalize_domain

__all__ = [
    "TABLE",
    "SOURCE",
    "PUBLISHES",
    "DOES_NOT_PUBLISH",
    "UNREADABLE",
    "RATE_LIMIT_HOURS",
    "POSITIVE_OVER_NEGATIVE_DAYS",
    "WARNING_INTERVAL_SECONDS",
    "record_request_check",
    "get_request_fact",
    "is_held_domain",
]

logger = logging.getLogger(__name__)

TABLE = "merchant_agent_checkout_request_facts"
SOURCE = "request"

#: At most one write per domain within this many hours.
RATE_LIMIT_HOURS = 24
#: A positive replaces a negative only once the negative is at least this many days old.
POSITIVE_OVER_NEGATIVE_DAYS = 14
#: One warning per this many seconds per process, whatever the request rate.
WARNING_INTERVAL_SECONDS = 600.0

#: One statement's bound. The write sits on an internal request's path.
WRITE_TIMEOUT_SECONDS = 2.0
READ_TIMEOUT_SECONDS = 2.0

# ── statements (module-level literals, passed by name) ───────────────────────────────────────
#
# THE INSERT and THE SET are the census upsert's (db/merchant_agent_checkout_facts._UPSERT_SQL),
# column for column. What is added is the WHERE of the DO UPDATE: the rules in the module header,
# read against the row as it stands, by the database's clock. `CAST(:x AS BOOLEAN)` as in the census
# statement, so Postgres plans every parameter with one type.

_UPSERT_SQL = """
INSERT INTO merchant_agent_checkout_request_facts
    (merchant_domain, checked_at, last_outcome, last_code,
     ucp_checkout_at, acp_checkout_at, negative_at, ucp_version, source)
VALUES
    (:merchant_domain, CURRENT_TIMESTAMP, :outcome, :code,
     CASE WHEN CAST(:ucp_yes AS BOOLEAN) THEN CURRENT_TIMESTAMP END,
     CASE WHEN CAST(:acp_yes AS BOOLEAN) THEN CURRENT_TIMESTAMP END,
     CASE WHEN CAST(:negative AS BOOLEAN) THEN CURRENT_TIMESTAMP END,
     :ucp_version, 'request')
ON CONFLICT (merchant_domain) DO UPDATE SET
    checked_at = EXCLUDED.checked_at,
    last_outcome = EXCLUDED.last_outcome,
    last_code = EXCLUDED.last_code,
    ucp_checkout_at = CASE
        WHEN CAST(:ucp_yes AS BOOLEAN) THEN EXCLUDED.ucp_checkout_at
        WHEN CAST(:ucp_no AS BOOLEAN) THEN NULL
        ELSE merchant_agent_checkout_request_facts.ucp_checkout_at
    END,
    acp_checkout_at = CASE
        WHEN CAST(:acp_yes AS BOOLEAN) THEN EXCLUDED.acp_checkout_at
        WHEN CAST(:acp_no AS BOOLEAN) THEN NULL
        ELSE merchant_agent_checkout_request_facts.acp_checkout_at
    END,
    negative_at = CASE
        WHEN CAST(:positive AS BOOLEAN) THEN NULL
        ELSE COALESCE(EXCLUDED.negative_at, merchant_agent_checkout_request_facts.negative_at)
    END,
    ucp_version = CASE
        WHEN CAST(:ucp_yes AS BOOLEAN) OR CAST(:ucp_no AS BOOLEAN) THEN EXCLUDED.ucp_version
        ELSE merchant_agent_checkout_request_facts.ucp_version
    END,
    source = 'request'
WHERE merchant_agent_checkout_request_facts.checked_at <= CURRENT_TIMESTAMP - INTERVAL '24 hours'
  AND (
        (CAST(:positive AS BOOLEAN)
         AND (merchant_agent_checkout_request_facts.negative_at IS NULL
              OR merchant_agent_checkout_request_facts.negative_at <= CURRENT_TIMESTAMP - INTERVAL '14 days'))
     OR (CAST(:negative AS BOOLEAN)
         AND NOT COALESCE(
               merchant_agent_checkout_request_facts.ucp_checkout_at
                   > CURRENT_TIMESTAMP - (CAST(:max_age_days AS INTEGER) * INTERVAL '1 day')
            OR merchant_agent_checkout_request_facts.acp_checkout_at
                   > CURRENT_TIMESTAMP - (CAST(:max_age_days AS INTEGER) * INTERVAL '1 day'),
             FALSE))
  )
RETURNING merchant_domain
"""

_UPSERT_SQL_SQLITE = """
INSERT INTO merchant_agent_checkout_request_facts
    (merchant_domain, checked_at, last_outcome, last_code,
     ucp_checkout_at, acp_checkout_at, negative_at, ucp_version, source)
VALUES
    (:merchant_domain, datetime('now'), :outcome, :code,
     CASE WHEN CAST(:ucp_yes AS BOOLEAN) THEN datetime('now') END,
     CASE WHEN CAST(:acp_yes AS BOOLEAN) THEN datetime('now') END,
     CASE WHEN CAST(:negative AS BOOLEAN) THEN datetime('now') END,
     :ucp_version, 'request')
ON CONFLICT (merchant_domain) DO UPDATE SET
    checked_at = EXCLUDED.checked_at,
    last_outcome = EXCLUDED.last_outcome,
    last_code = EXCLUDED.last_code,
    ucp_checkout_at = CASE
        WHEN CAST(:ucp_yes AS BOOLEAN) THEN EXCLUDED.ucp_checkout_at
        WHEN CAST(:ucp_no AS BOOLEAN) THEN NULL
        ELSE merchant_agent_checkout_request_facts.ucp_checkout_at
    END,
    acp_checkout_at = CASE
        WHEN CAST(:acp_yes AS BOOLEAN) THEN EXCLUDED.acp_checkout_at
        WHEN CAST(:acp_no AS BOOLEAN) THEN NULL
        ELSE merchant_agent_checkout_request_facts.acp_checkout_at
    END,
    negative_at = CASE
        WHEN CAST(:positive AS BOOLEAN) THEN NULL
        ELSE COALESCE(EXCLUDED.negative_at, merchant_agent_checkout_request_facts.negative_at)
    END,
    ucp_version = CASE
        WHEN CAST(:ucp_yes AS BOOLEAN) OR CAST(:ucp_no AS BOOLEAN) THEN EXCLUDED.ucp_version
        ELSE merchant_agent_checkout_request_facts.ucp_version
    END,
    source = 'request'
WHERE merchant_agent_checkout_request_facts.checked_at <= datetime('now', '-24 hours')
  AND (
        (CAST(:positive AS BOOLEAN)
         AND (merchant_agent_checkout_request_facts.negative_at IS NULL
              OR merchant_agent_checkout_request_facts.negative_at <= datetime('now', '-14 days')))
     OR (CAST(:negative AS BOOLEAN)
         AND NOT COALESCE(
               merchant_agent_checkout_request_facts.ucp_checkout_at
                   > datetime('now', '-' || CAST(:max_age_days AS INTEGER) || ' days')
            OR merchant_agent_checkout_request_facts.acp_checkout_at
                   > datetime('now', '-' || CAST(:max_age_days AS INTEGER) || ' days'),
             FALSE))
  )
RETURNING merchant_domain
"""

_SELECT_REQUEST_FACT_SQL = """
SELECT merchant_domain, checked_at, last_outcome, last_code,
       ucp_checkout_at, acp_checkout_at, negative_at, ucp_version, source
  FROM merchant_agent_checkout_request_facts
 WHERE merchant_domain = :merchant_domain
"""

_FACT_COLUMNS = (
    "merchant_domain", "checked_at", "last_outcome", "last_code",
    "ucp_checkout_at", "acp_checkout_at", "negative_at", "ucp_version", "source",
)

# WHO IS HELD: the census population's two tables, then the census fact table. Read one at a time so
# a table that is missing in one environment does not hide the other two.
_HELD_TIERB_SQL = """
SELECT 1 AS held FROM tierb_cart_link_eligibility WHERE shop_domain = :merchant_domain LIMIT 1
"""

_HELD_PURCHASABILITY_SQL = """
SELECT 1 AS held FROM merchant_purchasability WHERE merchant_domain = :merchant_domain LIMIT 1
"""

_HELD_CENSUS_FACT_SQL = """
SELECT 1 AS held FROM merchant_agent_checkout_facts WHERE merchant_domain = :merchant_domain LIMIT 1
"""

_HELD_STATEMENTS = (_HELD_TIERB_SQL, _HELD_PURCHASABILITY_SQL, _HELD_CENSUS_FACT_SQL)

# ── the rate-limited warning ─────────────────────────────────────────────────────────────────

#: The process clock the warning limit reads; a test may replace it.
_monotonic = time.monotonic
_last_warning_at: Optional[float] = None
_suppressed_warnings = 0


def _warn_write_failed(error: BaseException) -> None:
    """One WARNING per WARNING_INTERVAL_SECONDS per process, with how many were held back. Only the
    error's type is logged: never a value, a statement or anything a merchant sent."""
    global _last_warning_at, _suppressed_warnings
    now = _monotonic()
    if _last_warning_at is not None and now - _last_warning_at < WARNING_INTERVAL_SECONDS:
        _suppressed_warnings += 1
        return
    held_back, _suppressed_warnings, _last_warning_at = _suppressed_warnings, 0, now
    logger.warning(
        "merchant_agent_checkout_request_facts: write skipped (table missing or store unavailable) "
        "error_type=%s suppressed_since_last=%d", type(error).__name__, held_back)


def _reset_warning_limit() -> None:
    """For tests: forget the last warning."""
    global _last_warning_at, _suppressed_warnings
    _last_warning_at, _suppressed_warnings = None, 0


# ── writes ───────────────────────────────────────────────────────────────────────────────────


async def record_request_check(domain: Any, check: Check) -> bool:
    """Keep one request-time door read. True only when a row was inserted or updated.

    False, with no statement sent, for an unusable domain, a check whose outcome disagrees with its
    path answers, and an UNREADABLE check. False, with nothing written, when the rules in the
    module header leave the row as it is, when the table does not exist, and when the write fails
    or is slow. Never raises."""
    try:
        key = normalize_domain(domain) if isinstance(domain, str) else ""
        if not key or not _usable(check):
            return False
        if check.outcome not in (PUBLISHES, DOES_NOT_PUBLISH):
            return False  # unreadable: "cannot tell" renews nothing and is never a negative
        code = check.code if isinstance(check.code, str) and _CODE_RE.fullmatch(check.code) else CODE_OTHER
        version = check.ucp_version
        if not (isinstance(version, str) and _VERSION_RE.fullmatch(version)):
            version = None
        if check.ucp_checkout is None:
            version = None  # no readable profile, no version: the row keeps the one it had
        values = {
            "merchant_domain": key,
            "outcome": check.outcome,
            "code": code,
            "ucp_yes": check.ucp_checkout is True,
            "ucp_no": check.ucp_checkout is False,
            "acp_yes": check.acp_checkout is True,
            "acp_no": check.acp_checkout is False,
            "negative": check.outcome == DOES_NOT_PUBLISH,
            "positive": check.outcome == PUBLISHES,
            "ucp_version": version,
            "max_age_days": max_age_days(),
        }
    except Exception:  # noqa: BLE001 - a malformed check is refused, never raised
        return False
    statement = _UPSERT_SQL if IS_POSTGRES else _UPSERT_SQL_SQLITE
    try:
        record = await asyncio.wait_for(database.fetch_one(statement, values), WRITE_TIMEOUT_SECONDS)
    except Exception as error:  # noqa: BLE001 - a missing table and timeouts included
        _warn_write_failed(error)
        return False
    return record is not None


# ── reads ────────────────────────────────────────────────────────────────────────────────────


async def get_request_fact(domain: Any) -> Optional[Dict[str, Any]]:
    """The stored row for an operator or a test, or None. Never raises."""
    key = normalize_domain(domain) if isinstance(domain, str) else ""
    if not key:
        return None
    try:
        record = await asyncio.wait_for(
            database.fetch_one(_SELECT_REQUEST_FACT_SQL, {"merchant_domain": key}), READ_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - timeouts and a missing table included
        return None
    return {column: record[column] for column in _FACT_COLUMNS} if record is not None else None


async def is_held_domain(domain: Any) -> bool:
    """True when we already hold this domain: it is in the census population (either directory
    table) or the census stored a fact for it. A read that fails or is slow counts as "not this
    source"; all three failing answers False. Never raises."""
    key = normalize_domain(domain) if isinstance(domain, str) else ""
    if not key:
        return False
    for statement in _HELD_STATEMENTS:
        try:
            record = await asyncio.wait_for(
                database.fetch_one(statement, {"merchant_domain": key}), READ_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - a missing table in one environment: try the next source
            continue
        if record is not None:
            return True
    return False
