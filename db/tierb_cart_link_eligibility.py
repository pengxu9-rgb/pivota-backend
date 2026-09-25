"""Persistence for Tier B cart-link eligibility (migration 228): one row per (shop, market).

Written once a day by jobs/tierb_cart_link_eligibility.py; read by whoever decides whether a
buyer may be sent down the cart-permalink path (`is_cart_link_eligible`). This module is the
only writer.

── THE RULES `record_result` ENFORCES ───────────────────────────────────────────────────────

  DEFINITE verdicts (`DEFINITE_VERDICTS`) describe the store. One overwrites the row: verdict,
  evidence and `checked_at`. `consecutive_same` counts up while the verdict repeats and resets
  to 1 when it changes; a change also stamps `verdict_changed_at` and moves the old verdict into
  `previous_verdict`.

  INDEFINITE results (`INDEFINITE_VERDICTS`) describe the ATTEMPT — a proxy flake, a storefront
  that would not confirm the variant, a landing nobody has classified, an input the preflight
  refused. They NEVER overwrite a verdict. They set `last_attempt_at` and `last_error_code` and
  nothing else, so a merchant that was ELIGIBLE yesterday and answered TRANSPORT_ERROR today is
  still ELIGIBLE with yesterday's `checked_at` — and ages out through `max_age_hours` if the
  errors persist, instead of being flipped by a non-answer. With no prior row, an indefinite
  result inserts one with verdict NULL (which reads as not eligible).

  The two sets partition `Verdict` exactly; a verdict in neither is refused (ValueError), so a
  member added to the enum later cannot be written until someone decides which kind it is.

── WHY THE KEY IS NORMALISED HERE ───────────────────────────────────────────────────────────

`normalize_domain` (lower case, one leading `www.` stripped) runs on EVERY write and EVERY read,
so `www.Judydoll.com` and `judydoll.com` are one row. A result whose host is a different store
than the domain it is being recorded under, or whose market is not the market it is recorded
under, is refused: that is a caller mixing up two merchants, and the row would be wrong forever.

── DRIVER NOTES (the same ones db/reap_agentic_ledger.py documents at length) ──────────────

  * `execute()` of an INSERT/UPDATE returns no rowcount on Postgres, so each write is
    `fetch_one(... RETURNING *)` and the written row comes back to the caller.
  * Timestamps are written SERVER-SIDE (`CURRENT_TIMESTAMP`) and the freshness cutoff is
    computed server-side too — a naive client datetime binds as local wall time under asyncpg.
    Everything read back is a timezone-aware UTC datetime on both dialects.
  * Every statement is a MODULE-LEVEL constant (tests/test_repo_sql_prepare_postgres.py prepares
    those), split by dialect only where the dialects genuinely differ.
  * This module opens no transactions. Each write is ONE statement (an upsert), which is what
    makes a concurrent second run safe without a lock.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, Optional
from urllib.parse import urlsplit

from db.database import IS_POSTGRES, database
from db.tierb_cart_link_eligibility_schema import ensure_schema
from services.shopify_cart_link_preflight import PreflightResult, Verdict
from services.tierb_cart_link_merchants import normalize_domain, normalize_market

__all__ = [
    "DEFINITE_VERDICTS",
    "INDEFINITE_VERDICTS",
    "DEFAULT_MAX_AGE_HOURS",
    "unclassified_verdicts",
    "ensure_schema",
    "normalize_domain",
    "record_result",
    "get_eligibility",
    "is_cart_link_eligible",
]

TABLE = "tierb_cart_link_eligibility"
DEFAULT_MAX_AGE_HOURS = 48
MIN_MAX_AGE_HOURS = 1
MAX_MAX_AGE_HOURS = 8760  # a year: beyond that "fresh" means nothing

logger = logging.getLogger(__name__)

DEFINITE_VERDICTS: FrozenSet[Verdict] = frozenset({
    Verdict.ELIGIBLE,
    Verdict.LOGIN_REQUIRED,
    Verdict.NOT_ACCEPTING_ORDERS,
    Verdict.VARIANT_GONE,
    Verdict.VARIANT_UNAVAILABLE,
    Verdict.PASSWORD_PAGE,
    Verdict.BLOCKED_UNKNOWN,
    Verdict.CHECKOUT_PREFILL_MISSING,
    # A checkout that landed in another market than the buyer's, or whose market the page did
    # not state: the store answered, and the answer is "not for this market". Fail-closed.
    Verdict.CHECKOUT_MARKET_MISMATCH,
    # The checkout's own accept-list was READ and holds no card method, and the checkout's line
    # price differs from ours. Both are the store answering about itself, so both are definite —
    # and both mean a card-paying agent must not be sent down this path. Added with the
    # purchasability gate; `unclassified_verdicts()` is the test that forced this decision to be
    # made here rather than letting the two members be written by default.
    Verdict.NO_CARD_PAYMENT,
    Verdict.PRICE_DRIFT,
})
INDEFINITE_VERDICTS: FrozenSet[Verdict] = frozenset({
    Verdict.TRANSPORT_ERROR,
    Verdict.VARIANT_UNVERIFIED,
    Verdict.UNCLASSIFIED,
    Verdict.INVALID_INPUT,
})

def unclassified_verdicts(verdicts: Any = Verdict) -> list:
    """Members of the preflight's verdict enum that neither set classifies, by value. Empty today.
    A new member upstream makes `record_result` refuse it (ValueError) — this is the check that
    names it, so the failure is a decision to make rather than a mystery."""
    known = {v.value for v in DEFINITE_VERDICTS | INDEFINITE_VERDICTS}
    return sorted(v.value for v in verdicts if v.value not in known)


_TS_COLUMNS = ("checked_at", "last_attempt_at", "verdict_changed_at", "created_at")

# Column widths from the migration. Values are the storefront's and the preflight's own; an
# overlong one is truncated here rather than allowed to fail the write on Postgres (SQLite would
# not notice, so the two dialects would disagree about whether the row exists).
_WIDTHS = {
    "variant_id": 32,
    "variant_source": 16,
    "price_text": 32,
    "detail": 255,
    "last_error_code": 128,
}


# ── writes ──────────────────────────────────────────────────────────────────────────────────
#
# THE DEFINITE UPSERT. Every SET expression on the right reads the row's OLD values (both
# engines evaluate an UPDATE's assignments against the pre-update row), so the three history
# columns are computed from the verdict being replaced, not from the one being written.
# `IS NOT DISTINCT FROM` on Postgres, `IS` on SQLite: the old verdict may be NULL (a row an
# indefinite attempt created), and NULL -> ELIGIBLE is a change, which plain `=` would call
# unknown and route to the ELSE branch by accident rather than by rule.

_UPSERT_DEFINITE_SQL = """
INSERT INTO tierb_cart_link_eligibility (
    shop_domain, market, verdict, retryable, variant_id, variant_source, product_title,
    price_text, detail, checkout_country, checked_at, last_attempt_at, last_error_code,
    verdict_changed_at, previous_verdict, consecutive_same
) VALUES (
    :shop_domain, :market, :verdict, :retryable, :variant_id, :variant_source, :product_title,
    :price_text, :detail, :checkout_country, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL,
    CURRENT_TIMESTAMP, NULL, 1
)
ON CONFLICT (shop_domain, market) DO UPDATE SET
    previous_verdict = CASE
        WHEN tierb_cart_link_eligibility.verdict IS NOT DISTINCT FROM EXCLUDED.verdict
        THEN tierb_cart_link_eligibility.previous_verdict
        ELSE tierb_cart_link_eligibility.verdict END,
    verdict_changed_at = CASE
        WHEN tierb_cart_link_eligibility.verdict IS NOT DISTINCT FROM EXCLUDED.verdict
        THEN tierb_cart_link_eligibility.verdict_changed_at
        ELSE EXCLUDED.checked_at END,
    consecutive_same = CASE
        WHEN tierb_cart_link_eligibility.verdict IS NOT DISTINCT FROM EXCLUDED.verdict
        THEN tierb_cart_link_eligibility.consecutive_same + 1
        ELSE 1 END,
    verdict = EXCLUDED.verdict,
    retryable = EXCLUDED.retryable,
    variant_id = EXCLUDED.variant_id,
    variant_source = EXCLUDED.variant_source,
    product_title = EXCLUDED.product_title,
    price_text = EXCLUDED.price_text,
    detail = EXCLUDED.detail,
    checkout_country = EXCLUDED.checkout_country,
    checked_at = EXCLUDED.checked_at,
    last_attempt_at = EXCLUDED.last_attempt_at,
    last_error_code = NULL
RETURNING *
"""

_UPSERT_DEFINITE_SQL_SQLITE = """
INSERT INTO tierb_cart_link_eligibility (
    shop_domain, market, verdict, retryable, variant_id, variant_source, product_title,
    price_text, detail, checkout_country, checked_at, last_attempt_at, last_error_code,
    verdict_changed_at, previous_verdict, consecutive_same
) VALUES (
    :shop_domain, :market, :verdict, :retryable, :variant_id, :variant_source, :product_title,
    :price_text, :detail, :checkout_country, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL,
    CURRENT_TIMESTAMP, NULL, 1
)
ON CONFLICT (shop_domain, market) DO UPDATE SET
    previous_verdict = CASE
        WHEN tierb_cart_link_eligibility.verdict IS EXCLUDED.verdict
        THEN tierb_cart_link_eligibility.previous_verdict
        ELSE tierb_cart_link_eligibility.verdict END,
    verdict_changed_at = CASE
        WHEN tierb_cart_link_eligibility.verdict IS EXCLUDED.verdict
        THEN tierb_cart_link_eligibility.verdict_changed_at
        ELSE EXCLUDED.checked_at END,
    consecutive_same = CASE
        WHEN tierb_cart_link_eligibility.verdict IS EXCLUDED.verdict
        THEN tierb_cart_link_eligibility.consecutive_same + 1
        ELSE 1 END,
    verdict = EXCLUDED.verdict,
    retryable = EXCLUDED.retryable,
    variant_id = EXCLUDED.variant_id,
    variant_source = EXCLUDED.variant_source,
    product_title = EXCLUDED.product_title,
    price_text = EXCLUDED.price_text,
    detail = EXCLUDED.detail,
    checkout_country = EXCLUDED.checkout_country,
    checked_at = EXCLUDED.checked_at,
    last_attempt_at = EXCLUDED.last_attempt_at,
    last_error_code = NULL
RETURNING *
"""

# THE INDEFINITE UPSERT touches exactly two columns of an existing row. On insert (no prior row)
# every verdict column stays NULL — the CHECK `(verdict IS NULL) = (checked_at IS NULL)` holds.
# Portable as written; one constant serves both dialects.
_UPSERT_INDEFINITE_SQL = """
INSERT INTO tierb_cart_link_eligibility (
    shop_domain, market, last_attempt_at, last_error_code, consecutive_same
) VALUES (
    :shop_domain, :market, CURRENT_TIMESTAMP, :last_error_code, 0
)
ON CONFLICT (shop_domain, market) DO UPDATE SET
    last_attempt_at = EXCLUDED.last_attempt_at,
    last_error_code = EXCLUDED.last_error_code
RETURNING *
"""

# ── reads ───────────────────────────────────────────────────────────────────────────────────

_SELECT_ROW_SQL = """
SELECT * FROM tierb_cart_link_eligibility
 WHERE shop_domain = :shop_domain AND market = :market
"""

# The freshness cutoff is computed by the SERVER, from `checked_at` (the definite-verdict
# clock), never from `last_attempt_at`. `clock_timestamp()` on Postgres so a caller's long
# transaction cannot freeze it; SQLite's datetime('now', ...) is statement time already and
# produces the same text format CURRENT_TIMESTAMP wrote.
_SELECT_ELIGIBLE_SQL = """
SELECT 1 AS eligible FROM tierb_cart_link_eligibility
 WHERE shop_domain = :shop_domain
   AND market = :market
   AND verdict = 'ELIGIBLE'
   AND checked_at IS NOT NULL
   AND checked_at >= clock_timestamp() - (CAST(:max_age_seconds AS INTEGER) * INTERVAL '1 second')
"""

_SELECT_ELIGIBLE_SQL_SQLITE = """
SELECT 1 AS eligible FROM tierb_cart_link_eligibility
 WHERE shop_domain = :shop_domain
   AND market = :market
   AND verdict = 'ELIGIBLE'
   AND checked_at IS NOT NULL
   AND checked_at >= datetime('now', '-' || CAST(:max_age_seconds AS INTEGER) || ' seconds')
"""


# ── helpers ─────────────────────────────────────────────────────────────────────────────────


def _fit(column: str, value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    width = _WIDTHS.get(column)
    return text[:width] if width else text


def _country_or_none(value: Any) -> Optional[str]:
    """The checkout's buyer country, kept only in its ISO-2 shape. Anything else is dropped to
    NULL rather than truncated into a different country."""
    if isinstance(value, str) and len(value) == 2 and value.isascii() and value.isalpha() and value.isupper():
        return value
    return None


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


def _normalize_row(row: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    out = dict(row)
    for column in _TS_COLUMNS:
        if column in out:
            out[column] = _decode_dt(out[column])
    if out.get("retryable") is not None:
        out["retryable"] = bool(out["retryable"])
    return out


def _key(domain: Any, market: Any) -> Dict[str, str]:
    return {"shop_domain": normalize_domain(domain), "market": normalize_market(market)}


def error_code_for(result: PreflightResult) -> str:
    """`last_error_code` for an indefinite result: the verdict, plus the preflight's own detail
    code when it has one (e.g. `TRANSPORT_ERROR:resolve:ConnectError`). The detail is a code
    the preflight builds, never a URL."""
    code = result.verdict.value
    if result.detail:
        code = f"{code}:{result.detail}"
    return code[: _WIDTHS["last_error_code"]]


# ── public API ──────────────────────────────────────────────────────────────────────────────


async def record_result(domain: str, market: str, result: PreflightResult) -> Dict[str, Any]:
    """Record one preflight outcome under (domain, market) and return the row as written.

    DEFINITE -> the row is overwritten (history columns updated). INDEFINITE -> only
    `last_attempt_at` / `last_error_code` move; a prior verdict is never touched. See the module
    docstring. Raises ValueError for a malformed key, a verdict in neither set, or a result that
    belongs to a different store or market than the one named."""
    key = _key(domain, market)
    if not isinstance(result, PreflightResult):
        raise ValueError("result must be a PreflightResult")
    if result.host and normalize_domain(result.host) != key["shop_domain"]:
        raise ValueError(
            f"result is for {normalize_domain(result.host)!r}, not {key['shop_domain']!r}"
        )
    if result.market is not None and normalize_market(result.market) != key["market"]:
        raise ValueError(f"result was read for market {result.market!r}, not {key['market']!r}")

    verdict = result.verdict
    if verdict in DEFINITE_VERDICTS:
        params = {
            **key,
            "verdict": verdict.value,
            "retryable": bool(result.retryable),
            "variant_id": _fit("variant_id", result.variant_id),
            "variant_source": _fit("variant_source", result.variant_source),
            "product_title": _fit("product_title", result.product_title),
            "price_text": _fit("price_text", result.price),
            "detail": _fit("detail", result.detail),
            "checkout_country": _country_or_none(result.checkout_country),
        }
        if IS_POSTGRES:
            row = await database.fetch_one(_UPSERT_DEFINITE_SQL, params)
        else:
            row = await database.fetch_one(_UPSERT_DEFINITE_SQL_SQLITE, params)
    elif verdict in INDEFINITE_VERDICTS:
        row = await database.fetch_one(
            _UPSERT_INDEFINITE_SQL, {**key, "last_error_code": error_code_for(result)}
        )
    else:
        raise ValueError(f"verdict {verdict!r} is neither definite nor indefinite")
    written = _normalize_row(row)
    if written is None:
        # An upsert with RETURNING always returns its row; None means the driver lost it.
        raise RuntimeError("the eligibility upsert returned no row")
    return written


async def get_eligibility(domain: str, market: str) -> Optional[Dict[str, Any]]:
    """The row for (domain, market), or None. Timestamps are aware UTC datetimes."""
    row = await database.fetch_one(_SELECT_ROW_SQL, _key(domain, market))
    return _normalize_row(row)


def _host_for_log(value: Any) -> str:
    """The HOST of whatever a caller passed, and nothing more: a caller that hands this gate a
    full URL must not get its path or query into our logs."""
    if not isinstance(value, str):
        return f"<{type(value).__name__}>"
    text = value.strip()
    try:
        host = urlsplit(text if "//" in text else f"//{text}").hostname
    except ValueError:
        host = None
    return (host or "<unparseable>")[:253]


async def is_cart_link_eligible(
    domain: str, market: str, *, max_age_hours: int = DEFAULT_MAX_AGE_HOURS
) -> bool:
    """True ONLY when the last definite verdict is ELIGIBLE and it was observed within
    `max_age_hours` (measured on `checked_at`, by the database clock). No row, a NULL verdict,
    any other verdict, or a stale ELIGIBLE -> False.

    A domain or market it cannot normalise (e.g. `https://judydoll.com/`) is NOT an error for a
    gate: it returns False, and logs at WARNING naming only the host. `max_age_hours` outside
    1..8760 (an int) IS a programming error and raises ValueError, before any SQL.

    Database errors propagate: whether an outage should close Tier B or fail the request is the
    caller's decision, and a swallowed error here would be indistinguishable from "no".
    """
    if isinstance(max_age_hours, bool) or not isinstance(max_age_hours, int):
        raise ValueError("max_age_hours must be an int")
    if not MIN_MAX_AGE_HOURS <= max_age_hours <= MAX_MAX_AGE_HOURS:
        raise ValueError(f"max_age_hours must be in {MIN_MAX_AGE_HOURS}..{MAX_MAX_AGE_HOURS}")
    try:
        key = _key(domain, market)
    except ValueError:
        logger.warning(
            "tierb eligibility: refused an un-normalisable key (host=%s market=%.8r); not eligible",
            _host_for_log(domain), market,
        )
        return False
    params = {**key, "max_age_seconds": max_age_hours * 3600}
    if IS_POSTGRES:
        row = await database.fetch_one(_SELECT_ELIGIBLE_SQL, params)
    else:
        row = await database.fetch_one(_SELECT_ELIGIBLE_SQL_SQLITE, params)
    return row is not None
