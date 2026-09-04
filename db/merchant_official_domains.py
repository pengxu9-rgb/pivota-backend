"""B1 — a merchant's official storefront domains (db layer).

The set that decides `first_party` on every cited host in the BD report used to
be INFERRED, in one shape, from onboarding + catalog. This table makes it
ASSERTED or VERIFIED, MULTI-DOMAIN, and LIVENESS-CHECKED. See
db/migrations/207_merchant_official_domains.sql for the measurement that forced
it (anua.us understated by 21 points; us.judydoll.com counted official with no
DNS record at all).

Two invariants live here rather than in the caller:

* THE DOMAIN SHAPE IS A DATABASE CONSTRAINT, not a convention. A host stored
  with a scheme, a port, a path, a trailing dot or a leading `www.` is a host
  nothing downstream will ever match — it would silently subtract from the
  official set instead of adding to it. `ck_merchant_official_domains_domain`
  rejects it, so a caller that skips `normalize_host` fails loudly.

* ONLY `dead` EXCLUDES. `unverifiable` (a Cloudflare challenge, a 429, a
  timeout, a TLS error) and `unchecked` are first-class outcomes that keep the
  domain in the set. This is the same discipline
  services/external_seed_destination_liveness.py applies to product URLs, and
  for the same measured reason: 213 of 286 brand hosts in its audit answered
  every request with a bot challenge, so a rule that folded "cannot verify" into
  "gone" would have eaten most of the corpus on its first run.

Best-effort accessors (DB failures log + return None/False/empty) + an inline
DDL backstop — the db/brand_claims.py + db/audit_evidence.py pattern.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import (
    Boolean, CheckConstraint, Column, DateTime, Index, Table, Text,
    text as sa_text,
)
from sqlalchemy.sql import expression

from db._ddl_guard import apply_ddl_statements
from db.database import database, metadata

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- vocabulary

SOURCE_ASSERTED = "asserted"
SOURCE_VERIFIED = "verified"
SOURCE_INFERRED = "inferred"
VALID_SOURCES = frozenset({SOURCE_ASSERTED, SOURCE_VERIFIED, SOURCE_INFERRED})

# The two tiers a merchant (or a proven claim) put there on purpose. Membership
# of the official set is granted by these regardless of what inference found.
OFFICIAL_SOURCES = frozenset({SOURCE_ASSERTED, SOURCE_VERIFIED})

LIVENESS_LIVE = "live"
LIVENESS_DEAD = "dead"
LIVENESS_UNVERIFIABLE = "unverifiable"
LIVENESS_UNCHECKED = "unchecked"
VALID_LIVENESS = frozenset({
    LIVENESS_LIVE, LIVENESS_DEAD, LIVENESS_UNVERIFIABLE, LIVENESS_UNCHECKED,
})

# THE ONLY VERDICT THAT REMOVES A DOMAIN. Named as a set of one so the exclusion
# is a single, greppable fact rather than an `!= "dead"` scattered over three
# call sites — and so widening it is a deliberate edit, not a typo.
EXCLUDING_LIVENESS = frozenset({LIVENESS_DEAD})

# brand_claims' vocabulary, reused verbatim (db/brand_claims.py).
VERIFICATION_PENDING = "pending"
VERIFICATION_VERIFIED = "verified"
VERIFICATION_FAILED = "failed"


# --------------------------------------------------------------------------- model

merchant_official_domains = Table(
    "merchant_official_domains",
    metadata,
    Column("merchant_id", Text, primary_key=True, nullable=False),
    Column("domain", Text, primary_key=True, nullable=False),
    Column("source", Text, nullable=False),
    Column("verification_status", Text, nullable=True),
    Column("liveness_status", Text, nullable=False, server_default=LIVENESS_UNCHECKED),
    Column("last_checked_at", DateTime(timezone=True), nullable=True),
    # expression.false(), never the STRING "false": SQLAlchemy renders a string
    # Boolean default QUOTED, so SQLite would store the five-character word and
    # every `IS TRUE`/`IS FALSE` downstream would read it wrong. See the trap
    # documented in tests/model_schema.py.
    Column("is_primary", Boolean, nullable=False, server_default=expression.false()),
    # server_default mirrors migration 207's DEFAULT CURRENT_TIMESTAMP so the
    # create_all path and the migration path produce the SAME shape. Every
    # writer here sets both explicitly anyway — the default is the backstop.
    Column(
        "created_at", DateTime(timezone=True), nullable=False,
        server_default=sa_text("CURRENT_TIMESTAMP"),
    ),
    Column(
        "updated_at", DateTime(timezone=True), nullable=False,
        server_default=sa_text("CURRENT_TIMESTAMP"),
    ),
    # Mirrors migration 207 verbatim. If you change one, change both — the
    # repo's drift rule is that migrations + the schema backstop are the truth.
    CheckConstraint(
        "domain = lower(domain) "
        "AND domain <> '' "
        "AND domain LIKE '%.%' "
        "AND domain NOT LIKE '% %' "
        "AND domain NOT LIKE '%/%' "
        "AND domain NOT LIKE '%:%' "
        "AND domain NOT LIKE '%.' "
        "AND domain NOT LIKE 'www.%'",
        name="ck_merchant_official_domains_domain",
    ),
    CheckConstraint(
        "source IN ('asserted', 'verified', 'inferred')",
        name="ck_merchant_official_domains_source",
    ),
    CheckConstraint(
        "verification_status IS NULL "
        "OR verification_status IN ('pending', 'verified', 'failed')",
        name="ck_merchant_official_domains_verification",
    ),
    CheckConstraint(
        "liveness_status IN ('live', 'dead', 'unverifiable', 'unchecked')",
        name="ck_merchant_official_domains_liveness",
    ),
    CheckConstraint(
        "liveness_status = 'unchecked' OR last_checked_at IS NOT NULL",
        name="ck_merchant_official_domains_checked_at",
    ),
    Index("idx_merchant_official_domains_merchant", "merchant_id", "source"),
    Index("idx_merchant_official_domains_liveness_due", "last_checked_at"),
    extend_existing=True,
)


# --------------------------------------------------------------------------- DDL backstop

# Byte-for-byte the shape migration 207 creates (minus its COMMENT ON lines,
# which Postgres-only metadata and change no shape). Deliberately portable to
# SQLite too — CURRENT_TIMESTAMP rather than NOW(), LIKE predicates rather than
# btrim/regex — so the hermetic test suite exercises the REAL constraint instead
# of a hand-written fixture that is free to be laxer than production.
_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS merchant_official_domains (
        merchant_id         TEXT NOT NULL,
        domain              TEXT NOT NULL,
        source              TEXT NOT NULL,
        verification_status TEXT NULL,
        liveness_status     TEXT NOT NULL DEFAULT 'unchecked',
        last_checked_at     TIMESTAMPTZ NULL,
        is_primary          BOOLEAN NOT NULL DEFAULT FALSE,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (merchant_id, domain),
        CONSTRAINT ck_merchant_official_domains_domain
          CHECK (
            domain = lower(domain)
            AND domain <> ''
            AND domain LIKE '%.%'
            AND domain NOT LIKE '% %'
            AND domain NOT LIKE '%/%'
            AND domain NOT LIKE '%:%'
            AND domain NOT LIKE '%.'
            AND domain NOT LIKE 'www.%'
          ),
        CONSTRAINT ck_merchant_official_domains_source
          CHECK (source IN ('asserted', 'verified', 'inferred')),
        CONSTRAINT ck_merchant_official_domains_verification
          CHECK (
            verification_status IS NULL
            OR verification_status IN ('pending', 'verified', 'failed')
          ),
        CONSTRAINT ck_merchant_official_domains_liveness
          CHECK (liveness_status IN ('live', 'dead', 'unverifiable', 'unchecked')),
        CONSTRAINT ck_merchant_official_domains_checked_at
          CHECK (liveness_status = 'unchecked' OR last_checked_at IS NOT NULL)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_merchant_official_domains_merchant "
    "ON merchant_official_domains (merchant_id, source);",
    "CREATE INDEX IF NOT EXISTS idx_merchant_official_domains_liveness_due "
    "ON merchant_official_domains (last_checked_at);",
]

_DDL_LABEL = "ensure_merchant_official_domains_table"
_DDL_READY = False


async def ensure_merchant_official_domains_table() -> None:
    """Idempotent schema backstop for envs where migration 207 has not run.

    Memoization goes through db/_ddl_guard so a pass that left a statement
    outstanding genuinely retries instead of marking the module ready with the
    object still missing.
    """
    global _DDL_READY
    if _DDL_READY:
        return
    try:
        _DDL_READY = await apply_ddl_statements(
            _DDL_STATEMENTS,
            label=_DDL_LABEL,
            logger=logger,
            execute=database.execute,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("%s failed: %s", _DDL_LABEL, str(exc)[:200])


def reset_ddl_ready_for_tests() -> None:
    """Test hook: forget the memoized pass so a fresh DB gets its DDL again."""
    global _DDL_READY
    from db._ddl_guard import reset_ddl_state

    _DDL_READY = False
    reset_ddl_state(_DDL_LABEL)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- SQL

# ON CONFLICT (merchant_id, domain) DO UPDATE is written once, as a module-level
# constant, and is valid on BOTH engines: Postgres and SQLite share this
# `excluded.`-qualified upsert syntax. tests/test_repo_sql_prepare_postgres.py
# collects module-level constants, so this statement is PREPARE-checked there.
#
# COALESCE on the verdict columns is what makes the upsert composable: a writer
# that only knows the SOURCE (the brand-claim backfill) passes
# liveness_status=None and must not blank the liveness the sweep recorded, and a
# writer that only knows the LIVENESS must not blank the verification status. A
# fresh INSERT falls back to 'unchecked'. Without this a later claim silently
# resurrected a domain the sweep had measured DEAD, for a whole TTL.
UPSERT_OFFICIAL_DOMAIN_SQL = """
INSERT INTO merchant_official_domains (
    merchant_id, domain, source, verification_status,
    liveness_status, last_checked_at, is_primary, created_at, updated_at
) VALUES (
    :merchant_id, :domain, :source, :verification_status,
    COALESCE(:liveness_status, 'unchecked'), :last_checked_at,
    :is_primary, :now, :now
)
ON CONFLICT (merchant_id, domain) DO UPDATE SET
    source              = excluded.source,
    verification_status = COALESCE(excluded.verification_status,
                                   merchant_official_domains.verification_status),
    liveness_status     = COALESCE(:liveness_status,
                                   merchant_official_domains.liveness_status),
    last_checked_at     = COALESCE(:last_checked_at,
                                   merchant_official_domains.last_checked_at),
    is_primary          = excluded.is_primary,
    updated_at          = excluded.updated_at
"""

# The liveness sweep's write. It touches ONLY the liveness columns: a probe
# knows nothing about who asserted the domain, and an observation must never
# rewrite provenance.
RECORD_LIVENESS_SQL = """
UPDATE merchant_official_domains
   SET liveness_status = :liveness_status,
       last_checked_at = :checked_at,
       updated_at      = :checked_at
 WHERE merchant_id = :merchant_id
   AND domain = :domain
"""

# The one predicate for "this domain is provably this merchant's storefront",
# shared by both readers below. They MUST agree: if the resolver admits a row the
# counter does not, a merchant resolves on its domain and then counts zero
# storefronts, and the caller silently skips forever.
#
#   verification_status='verified' — proven, not pending/failed/NULL.
#   source='verified'             — brand-BOUND. record_official_domain writes
#     SOURCE_ASSERTED when merchant_owns_domain FAILED: domain control was shown
#     but Pivota does not associate the domain with this merchant. That gap is
#     load-bearing here, because the caller POSTs a create_checkout built from
#     the merchant's catalogue at whatever storefront answers on this domain.
#     The email claim method accepts any mailbox at the exact host, so on a
#     shared or multi-tenant domain an employee could otherwise point our probe
#     at a stranger's store. (That method is default-off today; this does not
#     rely on it staying that way.)
#   liveness                      — `dead` is the module's one excluding verdict
#     (is_excluded); counting dead rows would let a merchant who MIGRATED
#     domains look like two storefronts forever.
_PROVEN_STOREFRONT_WHERE = """
       verification_status = :verified
   AND source = :verified_source
   AND (liveness_status IS NULL OR liveness_status <> :dead)
"""

RESOLVE_VERIFIED_MERCHANT_SQL = """
SELECT merchant_id
  FROM merchant_official_domains
 WHERE lower(domain) = :domain
   AND """ + _PROVEN_STOREFRONT_WHERE + """
 ORDER BY merchant_id ASC
 LIMIT 2
"""

LIST_OFFICIAL_DOMAINS_SQL = """
SELECT merchant_id, domain, source, verification_status,
       liveness_status, last_checked_at, is_primary
  FROM merchant_official_domains
 WHERE merchant_id = :merchant_id
"""

# Stalest first, and NEVER-CHECKED first of all. The ordering is written as an
# explicit `IS NULL DESC` rather than relying on NULL ordering, because the two
# engines disagree: Postgres sorts NULLS LAST on ASC, SQLite sorts them first.
DUE_FOR_LIVENESS_SQL = """
SELECT merchant_id, domain, source, liveness_status, last_checked_at
  FROM merchant_official_domains
 WHERE (last_checked_at IS NULL OR last_checked_at < :cutoff)
 ORDER BY (last_checked_at IS NULL) DESC, last_checked_at ASC
 LIMIT :limit
"""

DUE_FOR_LIVENESS_FOR_MERCHANT_SQL = """
SELECT merchant_id, domain, source, liveness_status, last_checked_at
  FROM merchant_official_domains
 WHERE merchant_id = :merchant_id
   AND (last_checked_at IS NULL OR last_checked_at < :cutoff)
 ORDER BY (last_checked_at IS NULL) DESC, last_checked_at ASC
 LIMIT :limit
"""


# --------------------------------------------------------------------------- accessors

async def upsert_official_domain(
    *,
    merchant_id: str,
    domain: str,
    source: str,
    verification_status: Optional[str] = None,
    liveness_status: Optional[str] = None,
    last_checked_at: Optional[datetime] = None,
    is_primary: bool = False,
    now: Optional[datetime] = None,
) -> bool:
    """Record one official domain. Best-effort: returns False and logs on any
    DB error, including a CHECK violation from a caller that skipped
    normalization — the row is refused rather than planted unmatched.

    `domain` is passed through UNCHANGED. Normalizing here would hide exactly
    the bug the CHECK constraint exists to surface.
    """
    if not merchant_id or not domain:
        return False
    if source not in VALID_SOURCES:
        logger.warning("upsert_official_domain: bad source %r", source)
        return False
    # None is meaningful here: "I only know the SOURCE, leave the sweep's
    # verdict alone". The SQL COALESCEs it against the stored value (and against
    # 'unchecked' on a fresh INSERT), so it is not a missing value to reject.
    if liveness_status is not None and liveness_status not in VALID_LIVENESS:
        logger.warning("upsert_official_domain: bad liveness %r", liveness_status)
        return False
    await ensure_merchant_official_domains_table()
    stamp = now or _now_utc()
    if (
        liveness_status is not None
        and liveness_status != LIVENESS_UNCHECKED
        and last_checked_at is None
    ):
        # Keep ck_..._checked_at satisfiable for a caller that supplies a
        # verdict without a clock: the verdict IS the observation, so its time
        # is this write.
        last_checked_at = stamp
    try:
        await database.execute(
            UPSERT_OFFICIAL_DOMAIN_SQL,
            {
                "merchant_id": merchant_id,
                "domain": domain,
                "source": source,
                "verification_status": verification_status,
                "liveness_status": liveness_status,
                "last_checked_at": last_checked_at,
                "is_primary": bool(is_primary),
                "now": stamp,
            },
        )
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning(
            "upsert_official_domain failed for %s/%s: %s",
            merchant_id, domain, str(exc)[:200],
        )
        return False


async def resolve_verified_merchant_for_domain(domain: str) -> Optional[str]:
    """The merchant that has PROVEN this domain is theirs, or None.

    `execution_routes.merchant_id` looks like the natural answer to "whose store
    is this route?" and is the wrong one twice over: nothing in the tree writes
    it (`claim_execution_route` has no callers), and the association it was
    designed to hold comes from a self-declared `store_url`. This asks the one
    table that records a domain association someone had to prove.

    FAILS CLOSED, and every branch matters to a caller that will act on the
    answer against a live storefront:
      * the row must satisfy _PROVEN_STOREFRONT_WHERE — proven status, a
        brand-BOUND source, and a liveness verdict that is not `dead`. Read
        that constant's comment before loosening any of the three; each one is
        load-bearing for a caller that transacts against the resulting store.
      * two merchants verified on one domain is ambiguity, not a tie to break.
        LIMIT 2 exists to SEE the second row rather than silently take the first.
      * a lookup failure is not an absence; it returns None either way, but the
        caller must treat None as "we do not know", never as "not a merchant".
    """
    normalized = str(domain or "").strip().lower().lstrip(".")
    if not normalized:
        return None
    await ensure_merchant_official_domains_table()
    try:
        rows = await database.fetch_all(
            RESOLVE_VERIFIED_MERCHANT_SQL,
            {
                "domain": normalized,
                "verified": VERIFICATION_VERIFIED,
                "verified_source": SOURCE_VERIFIED,
                "dead": LIVENESS_DEAD,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "resolve_verified_merchant_for_domain failed for %s: %s",
            normalized, str(exc)[:200],
        )
        return None
    rows = list(rows or [])
    if len(rows) != 1:
        if len(rows) > 1:
            logger.warning(
                "resolve_verified_merchant_for_domain: %s is verified by %d "
                "merchants; refusing to pick one", normalized, len(rows),
            )
        return None
    return str(rows[0]["merchant_id"] or "").strip() or None


LIST_VERIFIED_DOMAINS_SQL = """
SELECT domain
  FROM merchant_official_domains
 WHERE merchant_id = :merchant_id
   AND """ + _PROVEN_STOREFRONT_WHERE


async def list_verified_domains(merchant_id: str) -> List[str]:
    """Every domain this merchant has proven, so a caller can tell whether the
    merchant is one storefront or several.

    Exists because `canonical_variants` carries `merchant_id` but no store key,
    while Shopify variant ids are per-STORE. A merchant with two proven domains
    that are two different Shopify stores (anua.com alongside anua.us, a pairing
    this codebase has already met) cannot have its catalogue attributed to one
    of them, and a caller that guesses will hand storefront A a variant only
    storefront B sells. Returns [] on failure, which callers must read as "we do
    not know", never as "none".
    """
    if not merchant_id:
        return []
    await ensure_merchant_official_domains_table()
    try:
        rows = await database.fetch_all(
            LIST_VERIFIED_DOMAINS_SQL,
            {
                "merchant_id": merchant_id,
                "verified": VERIFICATION_VERIFIED,
                "verified_source": SOURCE_VERIFIED,
                "dead": LIVENESS_DEAD,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "list_verified_domains failed for %s: %s", merchant_id, str(exc)[:200]
        )
        return []
    return [str(r["domain"] or "").strip().lower() for r in rows or [] if r["domain"]]


async def list_official_domains(merchant_id: str) -> List[Dict[str, Any]]:
    """Every stored row for the merchant — including `dead` ones, which the
    caller filters. Returning them lets a report say WHY a host it once counted
    is gone, which dropping them here would make impossible."""
    if not merchant_id:
        return []
    await ensure_merchant_official_domains_table()
    try:
        rows = await database.fetch_all(
            LIST_OFFICIAL_DOMAINS_SQL, {"merchant_id": merchant_id}
        )
        return [dict(r) for r in rows or []]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "list_official_domains failed for %s: %s", merchant_id, str(exc)[:200]
        )
        return []


async def list_domains_due_for_liveness(
    *,
    ttl: timedelta,
    limit: int = 100,
    merchant_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Rows whose liveness verdict is older than `ttl` (never-checked first)."""
    await ensure_merchant_official_domains_table()
    cutoff = (now or _now_utc()) - ttl
    params: Dict[str, Any] = {"cutoff": cutoff, "limit": max(1, int(limit))}
    try:
        # Both branches name their constant AT THE CALL SITE rather than
        # assigning one to a local first. tests/test_repo_sql_prepare_postgres
        # resolves module-level constants but does NOT follow a function-local,
        # so the tidier `sql = ...` form would quietly drop one of these
        # statements out of the Postgres PREPARE sweep.
        if merchant_id:
            params["merchant_id"] = merchant_id
            rows = await database.fetch_all(DUE_FOR_LIVENESS_FOR_MERCHANT_SQL, params)
        else:
            rows = await database.fetch_all(DUE_FOR_LIVENESS_SQL, params)
        return [dict(r) for r in rows or []]
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_domains_due_for_liveness failed: %s", str(exc)[:200])
        return []


async def record_liveness(
    *,
    merchant_id: str,
    domain: str,
    liveness_status: str,
    checked_at: Optional[datetime] = None,
) -> bool:
    """Persist one liveness observation against an existing row.

    Unlike services/external_seed_destination_liveness.record_destination_
    observation, an `unverifiable` IS written here — because it is written to a
    column whose ONLY excluding value is `dead`, so recording "we could not
    look" cannot cost the domain its place in the set. What it buys is the
    clock: without stamping last_checked_at, an unreachable host would be
    re-probed on every single sweep run forever.
    """
    if not merchant_id or not domain:
        return False
    if liveness_status not in VALID_LIVENESS:
        logger.warning("record_liveness: bad liveness %r", liveness_status)
        return False
    await ensure_merchant_official_domains_table()
    try:
        await database.execute(
            RECORD_LIVENESS_SQL,
            {
                "merchant_id": merchant_id,
                "domain": domain,
                "liveness_status": liveness_status,
                "checked_at": checked_at or _now_utc(),
            },
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "record_liveness failed for %s/%s: %s",
            merchant_id, domain, str(exc)[:200],
        )
        return False


def is_excluded(liveness_status: Optional[str]) -> bool:
    """The single place that answers "does this verdict remove the domain?".

    `unverifiable` and `unchecked` answer False, on purpose and by measurement.
    """
    return (liveness_status or LIVENESS_UNCHECKED) in EXCLUDING_LIVENESS


__all__: Sequence[str] = (
    "EXCLUDING_LIVENESS",
    "LIVENESS_DEAD",
    "LIVENESS_LIVE",
    "LIVENESS_UNCHECKED",
    "LIVENESS_UNVERIFIABLE",
    "OFFICIAL_SOURCES",
    "SOURCE_ASSERTED",
    "SOURCE_INFERRED",
    "SOURCE_VERIFIED",
    "VALID_LIVENESS",
    "VALID_SOURCES",
    "VERIFICATION_FAILED",
    "VERIFICATION_PENDING",
    "VERIFICATION_VERIFIED",
    "ensure_merchant_official_domains_table",
    "is_excluded",
    "list_domains_due_for_liveness",
    "list_official_domains",
    "list_verified_domains",
    "resolve_verified_merchant_for_domain",
    "merchant_official_domains",
    "record_liveness",
    "reset_ddl_ready_for_tests",
    "upsert_official_domain",
)
