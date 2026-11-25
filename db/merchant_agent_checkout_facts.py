"""The stored fact "this merchant publishes an agentic checkout protocol" (ADR-030 D8 and D11).

ADR-030 D11 lets a payment partner's checkout agent run only on a merchant that publishes an
agentic checkout protocol: a UCP profile that lists the checkout capability, or ACP checkout. D8
says that is read from a STORED fact, never from a request-time fetch. This module is that store:
one row per merchant domain in `merchant_agent_checkout_facts` (migration 294).

WHO WRITES. `jobs/merchant_protocol_profile_census`, and only when it is run with `--store`. It
reads each domain's robots.txt, `/.well-known/ucp` and `/.well-known/acp.json` from the crawl
egress and hands this module one `Check` per domain. Nothing a request sends ever writes here.

EACH PATH IS READ ON ITS OWN, AND "CANNOT TELL" IS NOT A NEGATIVE. A check carries one answer per
path (`ucp_checkout`, `acp_checkout`):

    True    a readable document offers checkout: that path's `*_checkout_at` is set to the
            database's clock
    False   the path answered cleanly without checkout (a readable document that does not list
            it, or a 404 / 410): that path's `*_checkout_at` is CLEARED, at once
    None    the path could not be read (robots.txt, a refused redirect, a timeout, a 403 or 5xx,
            a body that does not parse): that path's column is left exactly as it was, so a path
            that cannot be read neither loses its positive nor renews it, and ages out

and one outcome for the whole check, which must agree with the two answers:

    publishes          at least one True. `negative_at` is cleared.
    does_not_publish   both False. `negative_at` is set.
    unreadable         no True and at least one None. `negative_at` is left as it was.

THE DELIBERATE RULES (the challenge record and the filter that reads this fact inherit them):

  1. A CLEAN NEGATIVE REVOKES AT ONCE. There is no two-strikes rule here, unlike the
     purchasability fact.
  2. A CLEAN NEGATIVE NEEDS BOTH PATHS TO ANSWER CLEANLY. One path that could not be read (a bot
     wall on `/.well-known/ucp` beside a clean 404 on ACP) makes the whole check unreadable, and
     `negative_at` is not set.
  3. A NEGATIVE NEVER AGES OUT. Only a later positive clears it. A positive does age out.
  4. A ROW THE RUN'S DEADLINE KEPT FROM BEING ASKED is stored as unreadable.
  5. AN ACP POSITIVE needs a readable discovery document that lists `checkout` among its
     SERVICES. An extension of that name does not count, and a readable document without the
     service is a clean "no" for that path.
  6. A PATH THAT ANSWERS CLEANLY WITHOUT CHECKOUT LOSES ITS OWN POSITIVE AT ONCE, whatever the
     other path did. A profile that stopped listing checkout beside an ACP path behind a bot wall
     is an unreadable check, and the merchant still stops meeting the run condition unless its
     ACP positive is fresh.

Rules 2, 4 and 5 are applied by the writer's classifier
(`jobs.merchant_protocol_profile_census.checkout_check`); rules 1, 3 and 6 by the upsert and the
read below.

THE READ. `meets_run_condition(domain)` is True only while `negative_at` is NULL and one of the two
`*_checkout_at` is younger than `max_age_days()` (30 by default). It answers False for a missing
row, an old positive, a negative, an unusable domain, a slow or failed read, and a table that
does not exist. It never raises.

FOR A CALLER ON A REQUEST PATH (services/payment_orchestration/agent_run_condition.py is the one).
"Never raises" has two side effects on Postgres that such a caller must plan for:

  * INSIDE `database.transaction()`, a statement that fails (the table does not exist) aborts the
    surrounding transaction even though the error is swallowed here. Read the fact OUTSIDE any
    transaction, or under a savepoint.
  * THE READ'S TIMEOUT CANCELS A QUERY IN FLIGHT. With databases 0.7 a request's tasks can share
    one connection, so a cancelled read can disturb a sibling query on it. Read it before the
    request fans out, as the worker-heartbeat read is done.

ONE READER. services/payment_orchestration/agent_run_condition.py calls `meets_run_condition` on the
create path, only for a rail on PARTNER_AGENT_RUN_CONDITION_RAILS (unset: nothing reads this). No
worker reads it.

NO STARTUP HEAL. Migration 294 is applied in production once, by hand, as a gated one-off. Nothing
in db/schema_guard creates the table, so the code can be deployed before the table exists; that
is why every function here treats a missing table as "no fact". The SQLite tests build their own
copy of the table (tests/test_merchant_agent_checkout_facts.py).

THE KEY is `db.merchant_purchasability.normalize_domain`'s form (lower case, one leading `www.`
removed), applied on every read and every write, so the row the census wrote under
`brand.example` is the row a request for `www.Brand.example` reads.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Dict, NamedTuple, Optional

from db.database import IS_POSTGRES, database
from db.merchant_purchasability import normalize_domain

__all__ = [
    "TABLE",
    "PUBLISHES",
    "DOES_NOT_PUBLISH",
    "UNREADABLE",
    "OUTCOMES",
    "Check",
    "MAX_AGE_ENV",
    "DEFAULT_MAX_AGE_DAYS",
    "max_age_days",
    "table_is_readable",
    "record_check",
    "get_fact",
    "meets_run_condition",
]

logger = logging.getLogger(__name__)

TABLE = "merchant_agent_checkout_facts"

PUBLISHES = "publishes"
DOES_NOT_PUBLISH = "does_not_publish"
UNREADABLE = "unreadable"
OUTCOMES = frozenset({PUBLISHES, DOES_NOT_PUBLISH, UNREADABLE})

MAX_AGE_ENV = "MERCHANT_AGENT_CHECKOUT_FACT_MAX_AGE_DAYS"
DEFAULT_MAX_AGE_DAYS = 30
MIN_MAX_AGE_DAYS = 1
MAX_MAX_AGE_DAYS = 90

#: One statement's bound. The read sits on a purchase request's path once a filter uses it.
READ_TIMEOUT_SECONDS = 2.0
WRITE_TIMEOUT_SECONDS = 10.0

_CODE_RE = re.compile(r"[a-z0-9_]{1,64}")
_VERSION_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_SOURCE_RE = re.compile(r"[a-z0-9_]{1,32}")
CODE_OTHER = "other"


class Check(NamedTuple):
    """One domain's check, as the reader of its documents classified it.

    `ucp_checkout` / `acp_checkout`: True = a readable document offers checkout, False = the path
    answered cleanly without it, None = the path could not be read. `outcome` must agree with
    them (see the module header). `ucp_version` is the readable UCP profile's own, else None."""

    outcome: str
    code: str
    ucp_checkout: Optional[bool] = None
    acp_checkout: Optional[bool] = None
    ucp_version: Optional[str] = None


def max_age_days() -> int:
    """How old a positive may be and still count. Unset, empty, not a whole number or out of
    range all answer the default: a typo never widens the window past 90 days or closes it."""
    raw = (os.getenv(MAX_AGE_ENV) or "").strip()
    if not raw.isdigit():
        return DEFAULT_MAX_AGE_DAYS
    value = int(raw)
    return value if MIN_MAX_AGE_DAYS <= value <= MAX_MAX_AGE_DAYS else DEFAULT_MAX_AGE_DAYS


# ── statements (module-level literals, passed by name) ───────────────────────────────────────
#
# ONE UPSERT, evaluated server-side: the booleans the caller derived pick which columns move, and
# every timestamp is the database's own clock. Per path: yes sets its `*_checkout_at`, a clean no
# clears it, cannot-tell keeps what the row had. For the whole check: a positive clears
# `negative_at`, a both-clean negative sets it, anything else keeps it.

_UPSERT_SQL = """
INSERT INTO merchant_agent_checkout_facts
    (merchant_domain, checked_at, last_outcome, last_code,
     ucp_checkout_at, acp_checkout_at, negative_at, ucp_version, source)
VALUES
    (:merchant_domain, CURRENT_TIMESTAMP, :outcome, :code,
     CASE WHEN CAST(:ucp_yes AS BOOLEAN) THEN CURRENT_TIMESTAMP END,
     CASE WHEN CAST(:acp_yes AS BOOLEAN) THEN CURRENT_TIMESTAMP END,
     CASE WHEN CAST(:negative AS BOOLEAN) THEN CURRENT_TIMESTAMP END,
     :ucp_version, :source)
ON CONFLICT (merchant_domain) DO UPDATE SET
    checked_at = EXCLUDED.checked_at,
    last_outcome = EXCLUDED.last_outcome,
    last_code = EXCLUDED.last_code,
    ucp_checkout_at = CASE
        WHEN CAST(:ucp_yes AS BOOLEAN) THEN EXCLUDED.ucp_checkout_at
        WHEN CAST(:ucp_no AS BOOLEAN) THEN NULL
        ELSE merchant_agent_checkout_facts.ucp_checkout_at
    END,
    acp_checkout_at = CASE
        WHEN CAST(:acp_yes AS BOOLEAN) THEN EXCLUDED.acp_checkout_at
        WHEN CAST(:acp_no AS BOOLEAN) THEN NULL
        ELSE merchant_agent_checkout_facts.acp_checkout_at
    END,
    negative_at = CASE
        WHEN CAST(:positive AS BOOLEAN) THEN NULL
        ELSE COALESCE(EXCLUDED.negative_at, merchant_agent_checkout_facts.negative_at)
    END,
    ucp_version = CASE
        WHEN CAST(:ucp_yes AS BOOLEAN) OR CAST(:ucp_no AS BOOLEAN) THEN EXCLUDED.ucp_version
        ELSE merchant_agent_checkout_facts.ucp_version
    END,
    source = EXCLUDED.source
RETURNING merchant_domain
"""

_SELECT_FACT_SQL = """
SELECT merchant_domain, checked_at, last_outcome, last_code,
       ucp_checkout_at, acp_checkout_at, negative_at, ucp_version, source
  FROM merchant_agent_checkout_facts
 WHERE merchant_domain = :merchant_domain
"""

_FACT_COLUMNS = (
    "merchant_domain", "checked_at", "last_outcome", "last_code",
    "ucp_checkout_at", "acp_checkout_at", "negative_at", "ucp_version", "source",
)

_RUN_CONDITION_SQL = """
SELECT 1 AS met
  FROM merchant_agent_checkout_facts
 WHERE merchant_domain = :merchant_domain
   AND negative_at IS NULL
   AND (ucp_checkout_at > CURRENT_TIMESTAMP - (CAST(:max_age_days AS INTEGER) * INTERVAL '1 day')
        OR acp_checkout_at > CURRENT_TIMESTAMP - (CAST(:max_age_days AS INTEGER) * INTERVAL '1 day'))
"""

_RUN_CONDITION_SQL_SQLITE = """
SELECT 1 AS met
  FROM merchant_agent_checkout_facts
 WHERE merchant_domain = :merchant_domain
   AND negative_at IS NULL
   AND (ucp_checkout_at > datetime('now', '-' || CAST(:max_age_days AS INTEGER) || ' days')
        OR acp_checkout_at > datetime('now', '-' || CAST(:max_age_days AS INTEGER) || ' days'))
"""

_TABLE_READABLE_SQL = """
SELECT 1 AS present FROM merchant_agent_checkout_facts LIMIT 1
"""

# ── writes ───────────────────────────────────────────────────────────────────────────────────


def _usable(check: Any) -> bool:
    """A check this module will store: a known outcome that agrees with its two path answers."""
    if not isinstance(check, Check) or check.outcome not in OUTCOMES:
        return False
    answers = (check.ucp_checkout, check.acp_checkout)
    if any(answer is not None and type(answer) is not bool for answer in answers):
        return False
    if any(answer is True for answer in answers):
        expected = PUBLISHES
    elif all(answer is False for answer in answers):
        expected = DOES_NOT_PUBLISH
    else:
        expected = UNREADABLE
    return check.outcome == expected


async def table_is_readable() -> bool:
    """True when the table exists and answers. A storing run asks this before its first request,
    so no crawl is spent on a run that cannot write. Never raises."""
    try:
        await asyncio.wait_for(database.fetch_one(_TABLE_READABLE_SQL), READ_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - timeouts and a missing table included
        return False
    return True


async def record_check(domain: Any, check: Check, *, source: str = "census") -> bool:
    """Store one domain's check. True when the row was written.

    Refuses (False, nothing written) an unusable domain, a check whose outcome disagrees with its
    booleans, and a source that is not a short token. A failed or slow write is False as well:
    one merchant's write never stops a run. Never raises."""
    key = normalize_domain(domain)
    if not key or not _usable(check) or not (isinstance(source, str) and _SOURCE_RE.fullmatch(source)):
        logger.warning("merchant_agent_checkout_facts: refusing a write with an unusable key, check or source")
        return False
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
        "source": source,
    }
    try:
        record = await asyncio.wait_for(database.fetch_one(_UPSERT_SQL, values), WRITE_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - timeouts included; the run goes on
        logger.exception("merchant_agent_checkout_facts: write failed for one merchant")
        return False
    return record is not None


# ── reads ────────────────────────────────────────────────────────────────────────────────────


async def get_fact(domain: Any) -> Optional[Dict[str, Any]]:
    """The stored row for an operator or a test, or None. Never raises."""
    key = normalize_domain(domain)
    if not key:
        return None
    try:
        record = await asyncio.wait_for(
            database.fetch_one(_SELECT_FACT_SQL, {"merchant_domain": key}), READ_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - timeouts and a missing table included
        return None
    return {column: record[column] for column in _FACT_COLUMNS} if record is not None else None


async def meets_run_condition(domain: Any) -> bool:
    """True only for a merchant with a fresh positive and no negative since (ADR-030 D11).

    False for a missing row, a positive older than `max_age_days()`, a negative, an unusable
    domain, a slow or failed read and a missing table. Never raises."""
    key = normalize_domain(domain)
    if not key:
        return False
    statement = _RUN_CONDITION_SQL if IS_POSTGRES else _RUN_CONDITION_SQL_SQLITE
    try:
        record = await asyncio.wait_for(
            database.fetch_one(statement, {"merchant_domain": key, "max_age_days": max_age_days()}),
            READ_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - timeouts and a missing table included: no fact, no run
        return False
    return record is not None
