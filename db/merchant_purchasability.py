"""Persistence for the MERCHANT PURCHASABILITY fact (migration 231).

THE RULE THIS MODULE IS: a merchant x market row carries a PURCHASE affordance only while it
holds a FRESH, POSITIVE purchasability fact. "Unverifiable" is not positive. A frozen row keeps
browse and referral; it never keeps buy. Card only — the fact is positive only when the rendered
checkout offered a CREDIT CARD method.

Written by jobs/merchant_purchasability_sweep.py; read by
`routes/agent_commerce_reap.py` (the Reap eligibility check) and by the ops route. This module is
the only writer. See docs/runbooks/merchant_purchasability.md for the operator view and
db/migrations/231_merchant_purchasability.sql for the incident that motivates it.

── THE THREE RULES `record_check` ENFORCES ──────────────────────────────────────────────────

  1. A POSITIVE fact arms the window. `verdict == 'ELIGIBLE'` AND `card_available IS TRUE`, both
     of them, is the only thing that sets `positive_until = checked_at + TTL` and resets
     `consecutive_failures` to 0. ELIGIBLE alone is NOT enough: the Tier B lane's ELIGIBLE means
     "the permalink landed on a checkout with our line on it", which is exactly what
     flowerbeauty.com satisfied.

  2. A CONFIRMED NEGATIVE advances the counter (`NEGATIVE_VERDICTS`: NO_CARD_PAYMENT,
     NOT_ACCEPTING_ORDERS, LOGIN_REQUIRED, VARIANT_GONE, PRICE_DRIFT — the store answering about
     itself). At `DEMOTE_AFTER_FAILURES` (2) consecutive negatives `positive_until` is CLEARED.
     ONE negative does not demote: a single PRICE_DRIFT on a store mid-sale, or one LOGIN_REQUIRED
     from a redirect that bounced, should not stop a rail. Two in a row is a pattern.

  3. AN UNVERIFIABLE RESULT ADVANCES NOTHING AND CLEARS NOTHING. BLOCKED_UNKNOWN,
     TRANSPORT_ERROR, VARIANT_UNVERIFIED, UNCLASSIFIED, INVALID_INPUT, PASSWORD_PAGE,
     CHECKOUT_MARKET_MISMATCH, VARIANT_UNAVAILABLE, CHECKOUT_PREFILL_MISSING: the row keeps its
     `consecutive_failures` and its `positive_until`, and simply ages out through the TTL if the
     condition persists. THIS IS THE HALF THAT IS EASY TO GET BACKWARDS. A bot challenge is not
     a merchant refusing cards, and judydoll.com RESET direct TCP from one of our egresses while
     answering through another — treating that as a negative would demote a good merchant on a
     network fact. Equally, it must not FREEZE the affordance either, which is the original
     defect; the TTL is what stops that, because a row nobody can verify stops being fresh.

── VANTAGE IS PART OF THE KEY ───────────────────────────────────────────────────────────────

Reachability is EGRESS-DEPENDENT and that was measured, not assumed. `is_purchasable` requires a
positive fact from the vantage named by MERCHANT_PURCHASABILITY_BUYER_VANTAGE (default `worker`),
which an operator must set to match the buyer/partner egress. A positive fact from any OTHER
vantage is evidence for a human, never permission for the door.

── DRIVER NOTES (the same ones db/tierb_cart_link_eligibility.py documents) ─────────────────

  * `execute()` of an INSERT/UPDATE returns no rowcount on Postgres, so each write is
    `fetch_one(... RETURNING *)`.
  * Timestamps are written SERVER-SIDE and freshness is computed SERVER-SIDE — a naive client
    datetime binds as local wall time under asyncpg.
  * Every statement is a MODULE-LEVEL constant and there are NO f-strings in any of them:
    tests/test_repo_sql_prepare_postgres.py resolves a `database.*` first argument only when it
    is a literal or a module-level name, and a live statement nothing ever PREPAREs is the #1588
    shape this repo has already paid for once.
  * THIS MODULE OPENS NO TRANSACTION. `database.transaction()` statements bypass the shared
    connection's query lock on `databases` 0.7.0 and silently lose writes. Every write here is
    ONE upsert statement, which is what makes a concurrent second sweep safe without a lock.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, List, Optional

from db.database import IS_POSTGRES, database
from services.shopify_cart_link_preflight import PreflightResult, Verdict
from utils.market_code import MARKET_UNKNOWN, iso2_market

__all__ = [
    "TABLE",
    "NEGATIVE_VERDICTS",
    "DEMOTE_AFTER_FAILURES",
    "DEFAULT_TTL_HOURS",
    "is_sweep_enabled",
    "is_enforcement_enabled",
    "WORKER_VANTAGE",
    "buyer_vantage",
    "ttl_hours",
    "normalize_domain",
    "normalize_market",
    "MARKET_UNKNOWN",
    "record_check",
    "get_fact",
    "is_purchasable",
    "list_due",
]

logger = logging.getLogger(__name__)

TABLE = "merchant_purchasability"

#: The vantage the sweep runs from by default: the worker service's own egress.
WORKER_VANTAGE = "worker"
#: The vantage a configured VANTAGE_PROXY_URL check is recorded under.
PROXY_VANTAGE = "proxy"

DEFAULT_TTL_HOURS = 72
MIN_TTL_HOURS = 1
MAX_TTL_HOURS = 720  # 30 days; past that "fresh" is not a word that means anything

#: Two consecutive negatives clear the window. One does not — see rule 2 in the header.
DEMOTE_AFTER_FAILURES = 2

#: The store answering about ITSELF. Only these advance `consecutive_failures`.
#: BLOCKED_UNKNOWN IS DELIBERATELY ABSENT and must stay absent: it is "any other 403", which is a
#: bot challenge as often as it is anything, and the preflight's own docstring calls it "not
#: evidence of anything in particular".
NEGATIVE_VERDICTS: FrozenSet[str] = frozenset({
    Verdict.NO_CARD_PAYMENT.value,
    Verdict.NOT_ACCEPTING_ORDERS.value,
    Verdict.LOGIN_REQUIRED.value,
    Verdict.VARIANT_GONE.value,
    Verdict.PRICE_DRIFT.value,
})

_MAX_EVIDENCE_CHARS = 4000
_MAX_PAYMENT_METHODS = 32
_MAX_CHAIN_HOPS = 12
_WIDTHS = {"verdict": 32, "vantage": 32, "variant_id": 32, "landed_currency": 8}


# ── dials ───────────────────────────────────────────────────────────────────────────────────


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """An integer dial, or its default. NEVER raises and never silently zeroes: a typo in an env
    var must not become a TTL of 0, which would expire every fact on arrival."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("merchant_purchasability: %s is not an integer (%r); using %d", name, raw, default)
        return default
    if value < minimum or value > maximum:
        logger.warning(
            "merchant_purchasability: %s=%d is outside %d..%d; using %d", name, value, minimum, maximum, default
        )
        return default
    return value


def ttl_hours() -> int:
    """How long one positive fact stays positive. Read per call, never cached at import."""
    return _env_int("MERCHANT_PURCHASABILITY_TTL_HOURS", DEFAULT_TTL_HOURS, MIN_TTL_HOURS, MAX_TTL_HOURS)


def _truthy(name: str) -> bool:
    return str(os.getenv(name) or "").strip().lower() in ("1", "true", "on", "yes")


def is_sweep_enabled() -> bool:
    """Dial 1 of 2: may the sweep JOB contact merchants and gather facts?

    Gates jobs/merchant_purchasability_sweep.py and NOTHING else. Default OFF, because every
    check it makes creates an abandoned checkout on a live store.
    """
    return _truthy("MERCHANT_PURCHASABILITY_SWEEP_ENABLED")


def is_enforcement_enabled() -> bool:
    """Dial 2 of 2: may a missing fact REFUSE a purchase?

    Gates the consumers — the Reap route's `merchant_not_purchasable` refusal and the checkout
    tier's downgrade — and NOTHING else. Default OFF.

    ── WHY THIS IS TWO DIALS AND NOT ONE ──────────────────────────────────────────────────────

    It was one, and one was a bug. `services.audit_scheduler._add_job` registers every job only
    on the production WORKER (`_queue_worker_enabled()`), and a normal backend deploy does not
    ship the worker. So a single dial armed on the backend would switch the consumers on while
    the sweep that feeds them never ran anywhere: `is_purchasable` would find no fact for any
    merchant and the rail would answer a permanent 409 `merchant_not_purchasable` for the entire
    catalogue, with no way to fix it short of unsetting the dial again.

    Split, the arming ORDER becomes expressible, and it is the only safe one:

        1. MERCHANT_PURCHASABILITY_SWEEP_ENABLED on, ON THE WORKER.
        2. Wait for one full pass over the population (batch x interval; see the runbook).
        3. Verify coverage merchant by merchant through GET /ops/merchant-purchasability.
        4. MERCHANT_PURCHASABILITY_ENFORCE on.

    Turning these on in the other order is the outage. See
    docs/runbooks/merchant_purchasability.md.
    """
    return _truthy("MERCHANT_PURCHASABILITY_ENFORCE")


def buyer_vantage() -> str:
    """The vantage `is_purchasable` demands a positive fact FROM. Defaults to the worker's own
    egress, which is honest but is NOT the buyer's: an operator whose partner pays from another
    network must set this to that vantage and configure it, or the answer describes us."""
    return ((os.getenv("MERCHANT_PURCHASABILITY_BUYER_VANTAGE") or "").strip() or WORKER_VANTAGE)[:32]


# ── key normalisation ───────────────────────────────────────────────────────────────────────


def normalize_domain(value: Any) -> str:
    """Lower case, one leading `www.` stripped, host only. Runs on EVERY read and EVERY write, so
    `www.Judydoll.com` and `judydoll.com` are one row rather than two facts that disagree."""
    text = str(value or "").strip().lower()
    if "//" in text:
        text = text.split("//", 1)[1]
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    text = text.split(":", 1)[0].rstrip(".")
    if text.startswith("www."):
        text = text[4:]
    return text[:255]


#: THE BUYER MARKET KEY: ISO-2 upper-cased, or None. BOUND to `utils.market_code.iso2_market` —
#: the same function object the warm-handoff sink (`click_market`), the Tier B allowlist, the
#: Reap rail and the sweep use — so there is ONE rule for "is this a market we can key a
#: purchasability fact on", identity-asserted in tests/test_purchase_gate_market_not_defaulted.py.
#:
#: AN UNKNOWN MARKET IS UNKNOWN. None, "", "  " and anything that is not exactly two letters after
#: strip+upper answer None — never "US", and never a truncation: the previous rule here was
#: `str(v or "").strip().upper()[:2]`, which read "USA" as "US" and would have answered a
#: three-letter caller with the US fact. Every reader below treats None as "no purchasability
#: claim" (False / no rows) WITHOUT touching the database.
normalize_market = iso2_market


# ── writes ──────────────────────────────────────────────────────────────────────────────────
#
# ONE UPSERT, THREE BRANCHES, ALL EVALUATED SERVER-SIDE. `:positive` and `:negative` are the two
# booleans the caller derived; a result that is neither is the unverifiable case, and every
# CASE below then falls through to "keep what the row had". Writing it this way rather than as a
# read-modify-write is what makes two concurrent sweeps safe with no transaction and no lock.
#
# EXCLUDED.checked_at is the statement's CURRENT_TIMESTAMP, so `positive_until` is computed from
# the SERVER's clock, never from a client datetime.

_UPSERT_SQL = """
INSERT INTO merchant_purchasability (
    merchant_domain, market_country, vantage, checked_at, verdict, card_available,
    payment_methods, landed_price_minor, landed_currency, expected_price_minor,
    price_drift_minor, variant_id, evidence, consecutive_failures, positive_until
) VALUES (
    :merchant_domain, :market_country, :vantage, CURRENT_TIMESTAMP, :verdict, :card_available,
    CAST(:payment_methods AS JSONB), :landed_price_minor, :landed_currency, :expected_price_minor,
    :price_drift_minor, :variant_id, CAST(:evidence AS JSONB),
    CASE WHEN :negative THEN 1 ELSE 0 END,
    CASE WHEN :positive
         THEN CURRENT_TIMESTAMP + (CAST(:ttl_hours AS INTEGER) * INTERVAL '1 hour')
         ELSE NULL END
)
ON CONFLICT (merchant_domain, market_country, vantage) DO UPDATE SET
    consecutive_failures = CASE
        WHEN :positive THEN 0
        WHEN :negative THEN merchant_purchasability.consecutive_failures + 1
        ELSE merchant_purchasability.consecutive_failures END,
    positive_until = CASE
        WHEN :positive THEN EXCLUDED.checked_at
             + (CAST(:ttl_hours AS INTEGER) * INTERVAL '1 hour')
        WHEN :negative AND merchant_purchasability.consecutive_failures + 1
             >= CAST(:demote_after AS INTEGER) THEN NULL
        ELSE merchant_purchasability.positive_until END,
    checked_at = EXCLUDED.checked_at,
    verdict = EXCLUDED.verdict,
    card_available = EXCLUDED.card_available,
    payment_methods = EXCLUDED.payment_methods,
    landed_price_minor = EXCLUDED.landed_price_minor,
    landed_currency = EXCLUDED.landed_currency,
    expected_price_minor = EXCLUDED.expected_price_minor,
    price_drift_minor = EXCLUDED.price_drift_minor,
    variant_id = EXCLUDED.variant_id,
    evidence = EXCLUDED.evidence
RETURNING *
"""

# SQLite twin. Two differences and no others: no `CAST(... AS JSONB)` (the column is TEXT and the
# module encodes), and the interval arithmetic is `datetime(...)` text maths. The CASE structure
# is identical, deliberately — the two dialects must agree about when a window is armed.
_UPSERT_SQL_SQLITE = """
INSERT INTO merchant_purchasability (
    merchant_domain, market_country, vantage, checked_at, verdict, card_available,
    payment_methods, landed_price_minor, landed_currency, expected_price_minor,
    price_drift_minor, variant_id, evidence, consecutive_failures, positive_until
) VALUES (
    :merchant_domain, :market_country, :vantage, CURRENT_TIMESTAMP, :verdict, :card_available,
    :payment_methods, :landed_price_minor, :landed_currency, :expected_price_minor,
    :price_drift_minor, :variant_id, :evidence,
    CASE WHEN :negative THEN 1 ELSE 0 END,
    CASE WHEN :positive
         THEN datetime('now', '+' || CAST(:ttl_hours AS INTEGER) || ' hours')
         ELSE NULL END
)
ON CONFLICT (merchant_domain, market_country, vantage) DO UPDATE SET
    consecutive_failures = CASE
        WHEN :positive THEN 0
        WHEN :negative THEN merchant_purchasability.consecutive_failures + 1
        ELSE merchant_purchasability.consecutive_failures END,
    positive_until = CASE
        WHEN :positive THEN datetime('now', '+' || CAST(:ttl_hours AS INTEGER) || ' hours')
        WHEN :negative AND merchant_purchasability.consecutive_failures + 1
             >= CAST(:demote_after AS INTEGER) THEN NULL
        ELSE merchant_purchasability.positive_until END,
    checked_at = EXCLUDED.checked_at,
    verdict = EXCLUDED.verdict,
    card_available = EXCLUDED.card_available,
    payment_methods = EXCLUDED.payment_methods,
    landed_price_minor = EXCLUDED.landed_price_minor,
    landed_currency = EXCLUDED.landed_currency,
    expected_price_minor = EXCLUDED.expected_price_minor,
    price_drift_minor = EXCLUDED.price_drift_minor,
    variant_id = EXCLUDED.variant_id,
    evidence = EXCLUDED.evidence
RETURNING *
"""

# ── reads ───────────────────────────────────────────────────────────────────────────────────

# The FRESHEST POSITIVE fact across every vantage, for a human or an ops read. `positive_until`
# is compared to the SERVER's clock. This is NOT what the door asks — see `is_purchasable`.
_SELECT_FACT_SQL = """
SELECT * FROM merchant_purchasability
 WHERE merchant_domain = :merchant_domain
   AND market_country = :market_country
   AND positive_until IS NOT NULL
   AND positive_until > clock_timestamp()
 ORDER BY checked_at DESC
 LIMIT 1
"""

_SELECT_FACT_SQL_SQLITE = """
SELECT * FROM merchant_purchasability
 WHERE merchant_domain = :merchant_domain
   AND market_country = :market_country
   AND positive_until IS NOT NULL
   AND positive_until > datetime('now')
 ORDER BY checked_at DESC
 LIMIT 1
"""

# Every row for one merchant x market, whatever its state. The ops route reads this: an operator
# asking "why is this merchant not purchasable" needs the NEGATIVE rows, which the two statements
# above cannot return by construction.
_SELECT_ALL_SQL = """
SELECT * FROM merchant_purchasability
 WHERE merchant_domain = :merchant_domain
   AND market_country = :market_country
 ORDER BY vantage
"""

# THE DOOR'S QUESTION, and the only statement that may answer it: a positive window that has not
# expired, FROM THE NAMED VANTAGE. The vantage conjunct is the whole point — a fact gathered from
# an egress the buyer does not pay from describes our reachability, not theirs.
_SELECT_PURCHASABLE_SQL = """
SELECT 1 AS ok FROM merchant_purchasability
 WHERE merchant_domain = :merchant_domain
   AND market_country = :market_country
   AND vantage = :vantage
   AND positive_until IS NOT NULL
   AND positive_until > clock_timestamp()
"""

_SELECT_PURCHASABLE_SQL_SQLITE = """
SELECT 1 AS ok FROM merchant_purchasability
 WHERE merchant_domain = :merchant_domain
   AND market_country = :market_country
   AND vantage = :vantage
   AND positive_until IS NOT NULL
   AND positive_until > datetime('now')
"""

# The sweep's due list, for ONE vantage: rows never checked first (NULLs sort first here by the
# explicit ordering column), then oldest first.
_SELECT_DUE_SQL = """
SELECT merchant_domain, market_country, checked_at FROM merchant_purchasability
 WHERE vantage = :vantage
 ORDER BY checked_at ASC NULLS FIRST
 LIMIT :limit
"""

_SELECT_DUE_SQL_SQLITE = """
SELECT merchant_domain, market_country, checked_at FROM merchant_purchasability
 WHERE vantage = :vantage
 ORDER BY checked_at IS NOT NULL, checked_at ASC
 LIMIT :limit
"""


# ── helpers ─────────────────────────────────────────────────────────────────────────────────


def _fit(column: str, value: Any) -> Optional[str]:
    if value is None:
        return None
    width = _WIDTHS.get(column)
    text = str(value)
    return text[:width] if width else text


def _decode_dt(value: Any) -> Any:
    """Postgres hands back a datetime, SQLite the text it stored; both leave here as a
    timezone-aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip().replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return value


def _decode_json(value: Any) -> Any:
    """Postgres hands back the decoded object (or a string, depending on the codec); SQLite hands
    back the text this module wrote. Never raises: an undecodable evidence blob must not sink a
    read of the fact it hangs off."""
    if value is None or isinstance(value, (list, dict)):
        return value
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return None


def is_positive(result: PreflightResult) -> bool:
    """THE POSITIVE TEST, in one place so that every caller and every test asks it the same way.

    BOTH conjuncts are required, and `card_available is True` is written out rather than tested
    for truthiness so that None — "could not determine" — can never satisfy it. ELIGIBLE on its
    own is exactly what flowerbeauty.com had.
    """
    return result.verdict is Verdict.ELIGIBLE and result.card_available is True


def is_negative(result: PreflightResult) -> bool:
    """A CONFIRMED negative: the store answered about itself. Membership of `NEGATIVE_VERDICTS`
    and nothing else — in particular, `card_available is False` does not make an otherwise
    unverifiable result negative, because the preflight only ever answers False off a page it
    actually read, and that page's verdict is already NO_CARD_PAYMENT."""
    return result.verdict.value in NEGATIVE_VERDICTS


def _evidence(result: PreflightResult) -> Optional[str]:
    """The bounded, PII-free evidence blob.

    WHAT IS DELIBERATELY NOT IN HERE: any page body (it would carry the merchant's own tokens and
    dwarf every other column), and any buyer field. The sweep runs with `buyer=None`, so there is
    no buyer to leak — but an allow-list rather than a deny-list is what keeps that true when
    somebody later passes one. Only the named keys below are ever written.
    """
    chain = [[int(status), str(url)] for status, url in (result.chain or ())][:_MAX_CHAIN_HOPS]
    blob: Dict[str, Any] = {
        "verdict": result.verdict.value,
        "retryable": bool(result.retryable),
        "detail": (str(result.detail)[:255] if result.detail else None),
        "final_status": result.final_status,
        "final_host": result.final_host,
        "checkout_country": result.checkout_country,
        "variant_source": result.variant_source,
        "chain": chain,
    }
    try:
        text = json.dumps(blob, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    if len(text) > _MAX_EVIDENCE_CHARS:
        # Drop the chain first: it is the only unbounded-ish part, and the verdict and detail are
        # what an operator reads. Truncating the JSON text itself would store invalid JSON.
        blob["chain"] = []
        blob["detail"] = "evidence_truncated"
        try:
            text = json.dumps(blob, separators=(",", ":"))
        except (TypeError, ValueError):
            return None
    return text


def _decode_bool(value: Any) -> Optional[bool]:
    """SQLite has no boolean type and hands back 0/1; Postgres hands back True/False. Both leave
    here as True, False or None — and NONE STAYS NONE. `bool(0)` and `bool(None)` are both False,
    so a careless `bool(...)` here would turn "could not determine" into "no card", which is the
    one conversion this whole rail exists to refuse."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "t", "yes")
    return None


def _row(record: Any) -> Dict[str, Any]:
    row = dict(record)
    for column in ("checked_at", "positive_until"):
        if column in row:
            row[column] = _decode_dt(row[column])
    for column in ("payment_methods", "evidence"):
        if column in row:
            row[column] = _decode_json(row[column])
    if "card_available" in row:
        row["card_available"] = _decode_bool(row["card_available"])
    return row


# ── public API ──────────────────────────────────────────────────────────────────────────────


async def record_check(
    domain: Any,
    market: Any,
    result: PreflightResult,
    *,
    vantage: str = WORKER_VANTAGE,
    expected_price_minor: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Record one purchasability check. Returns the written row, or None if the write failed.

    The three rules in the module header are applied by the ONE upsert this issues; the caller
    supplies no counters and no window, only the preflight's result. See `is_positive` /
    `is_negative` for the two booleans that drive it.
    """
    key_domain = normalize_domain(domain)
    key_market = normalize_market(market)
    if not key_domain or key_market is None:
        logger.warning("merchant_purchasability: refusing a write with an unusable key")
        return None

    positive = is_positive(result)
    negative = is_negative(result)
    methods = list(result.payment_methods or ())[:_MAX_PAYMENT_METHODS]
    values = {
        "merchant_domain": key_domain,
        "market_country": key_market,
        "vantage": _fit("vantage", vantage) or WORKER_VANTAGE,
        "verdict": _fit("verdict", result.verdict.value),
        "card_available": result.card_available,
        "payment_methods": json.dumps(methods, separators=(",", ":")),
        "landed_price_minor": result.landed_price_minor,
        "landed_currency": _fit("landed_currency", result.landed_currency),
        "expected_price_minor": expected_price_minor,
        "price_drift_minor": result.price_drift_minor,
        "variant_id": _fit("variant_id", result.variant_id),
        "evidence": _evidence(result),
        "positive": positive,
        "negative": negative,
        "ttl_hours": ttl_hours(),
        "demote_after": DEMOTE_AFTER_FAILURES,
    }
    statement = _UPSERT_SQL if IS_POSTGRES else _UPSERT_SQL_SQLITE
    try:
        record = await database.fetch_one(statement, values)
    except Exception:  # noqa: BLE001 — one merchant's write must not end a sweep
        logger.exception("merchant_purchasability: write failed for a merchant x market row")
        return None
    return _row(record) if record is not None else None


async def get_fact(domain: Any, market: Any) -> Optional[Dict[str, Any]]:
    """The freshest POSITIVE fact for this merchant x market, across every vantage, or None.

    Across vantages ON PURPOSE: this is the human-facing read ("does anything say this merchant
    can be paid?"). The door must not use it — see `is_purchasable`, which pins the vantage.
    """
    key_market = normalize_market(market)
    if key_market is None:
        # No market, no fact: nothing is ever written under an unknown market.
        return None
    statement = _SELECT_FACT_SQL if IS_POSTGRES else _SELECT_FACT_SQL_SQLITE
    try:
        record = await database.fetch_one(
            statement,
            {"merchant_domain": normalize_domain(domain), "market_country": key_market},
        )
    except Exception:  # noqa: BLE001
        logger.exception("merchant_purchasability: get_fact failed")
        return None
    return _row(record) if record is not None else None


async def list_facts(domain: Any, market: Any) -> List[Dict[str, Any]]:
    """Every row for this merchant x market, positive or not, one per vantage. The ops read.
    An unknown market has no rows by definition — nothing is ever written under one."""
    key_market = normalize_market(market)
    if key_market is None:
        return []
    try:
        records = await database.fetch_all(
            _SELECT_ALL_SQL,
            {"merchant_domain": normalize_domain(domain), "market_country": key_market},
        )
    except Exception:  # noqa: BLE001
        logger.exception("merchant_purchasability: list_facts failed")
        return []
    return [_row(r) for r in records]


async def is_purchasable(domain: Any, market: Any, *, now: Optional[datetime] = None) -> bool:
    """MAY THE DOOR OFFER A PURCHASE FOR THIS MERCHANT x MARKET?

    True only for a positive fact whose window has not closed, FROM THE BUYER VANTAGE. Every
    other state — no row, an expired window, a demoted row, a positive row from a different
    vantage, a database that will not answer — is False.

    FAIL-CLOSED ON ERROR, and that is the opposite of what a liveness check should do. The two
    are different questions: a liveness sweep that cannot reach a store must not delete it, but a
    PURCHASE gate that cannot prove payment must not permit it. This function is the second kind.

    `now` is accepted for tests and is compared in PYTHON against the row's `positive_until`,
    ON TOP OF the server-side comparison the statement already makes — never instead of it. A
    caller-supplied clock may narrow the answer; it can never widen it.

    AN UNKNOWN MARKET IS NO CLAIM. A market `normalize_market` cannot key (None, blank, not ISO-2)
    answers False BEFORE the database is asked and WITHOUT a log line: it is an expected input (a
    click whose market was never known), not an error, and a gate that logged it would storm. It
    is never substituted with "US" — that would gate a non-US buyer against the US fact. The ops
    route reports this state as `reason == MARKET_UNKNOWN`.
    """
    key_market = normalize_market(market)
    if key_market is None:
        return False
    statement = _SELECT_PURCHASABLE_SQL if IS_POSTGRES else _SELECT_PURCHASABLE_SQL_SQLITE
    values = {
        "merchant_domain": normalize_domain(domain),
        "market_country": key_market,
        "vantage": buyer_vantage(),
    }
    try:
        record = await database.fetch_one(statement, values)
    except Exception:  # noqa: BLE001
        logger.exception("merchant_purchasability: is_purchasable failed; refusing the purchase")
        return False
    if record is None:
        return False
    if now is None:
        return True
    # The row cleared the server's clock. Re-check it against the caller's, and only narrow.
    try:
        rows = await database.fetch_all(_SELECT_ALL_SQL, {
            "merchant_domain": values["merchant_domain"], "market_country": values["market_country"]
        })
    except Exception:  # noqa: BLE001
        logger.exception("merchant_purchasability: is_purchasable re-read failed; refusing")
        return False
    for raw in rows:
        row = _row(raw)
        if str(row.get("vantage") or "") != values["vantage"]:
            continue
        until = row.get("positive_until")
        if isinstance(until, datetime):
            reference = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
            return until > reference
    return False


async def list_due(limit: int = 50, *, vantage: str = WORKER_VANTAGE) -> List[Dict[str, Any]]:
    """Existing rows for one vantage, least-recently-checked first. Bounded by `limit`.

    This lists what we ALREADY HOLD; it is not the population. The sweep's population comes from
    the two Reap eligibility allowlists (see jobs/merchant_purchasability_sweep.py), because a
    merchant that has never been checked has no row here to be listed.
    """
    try:
        bound = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        bound = 50
    statement = _SELECT_DUE_SQL if IS_POSTGRES else _SELECT_DUE_SQL_SQLITE
    try:
        records = await database.fetch_all(statement, {"vantage": str(vantage)[:32], "limit": bound})
    except Exception:  # noqa: BLE001
        logger.exception("merchant_purchasability: list_due failed")
        return []
    return [_row(r) for r in records]
