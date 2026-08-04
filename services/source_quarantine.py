from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)


MATCH_TYPE_DOMAIN = "domain"
MATCH_TYPE_MERCHANT_PLATFORM = "merchant_platform"
MATCH_TYPE_SOURCE_SYSTEM_REF = "source_system_ref"
VALID_MATCH_TYPES = {
    MATCH_TYPE_DOMAIN,
    MATCH_TYPE_MERCHANT_PLATFORM,
    MATCH_TYPE_SOURCE_SYSTEM_REF,
}
VALID_STATES = {"active", "revoked", "expired"}

QUARANTINE_COLUMNS = """
  quarantine_id,
  match_type,
  match_value,
  state,
  reason,
  expires_at,
  created_by,
  created_at,
  revoked_at,
  revoked_by,
  metadata
"""


@dataclass(frozen=True)
class Quarantine:
    quarantine_id: int
    match_type: str
    match_value: str
    state: str
    reason: Optional[str]
    expires_at: Optional[datetime]
    created_by: str
    created_at: Optional[datetime]
    revoked_at: Optional[datetime]
    revoked_by: Optional[str]
    metadata: Optional[dict[str, Any]]


def sql_bare_domain(expr: str) -> str:
    """Lowercase, `www.`-stripped, empty-as-NULL — applied to BOTH sides.

    Stripping only the ROW side is an under-block that also splits this gate
    from the Python one in `services/offer_currency_policy`, which normalises
    both. Measured divergence before this was symmetric:

        seed www.mintree.us + quarantine mintree.us      -> SQL blocked, Python did not
        seed www.mintree.us + quarantine www.mintree.us  -> Python blocked, SQL did not

    `create_quarantine` only `.strip()`s, so an operator can enter either form
    and the API accepts it verbatim. A quarantine that reports success and
    silently blocks nothing is the failure mode this whole workstream exists to
    end, so normalise here rather than trusting the writer.

    NULL-safe by construction: `nullif(...,'')` makes a blank match_value or a
    domain-less row compare NULL, so neither can match everything. The migration
    declares `match_value TEXT NOT NULL` with no non-empty CHECK and these rows
    come from direct SQL ops, so that guard is load-bearing.

    PORTABLE: CASE/LIKE/substr/lower/nullif behave identically on Postgres and
    SQLite. `regexp_replace` would be Postgres-only, and this fragment is now
    embedded in a code path whose suite runs on SQLite.
    """
    # trim() as well as lower(): the Python twin does `.strip().lower()`, and
    # without it the two implementations of this one rule disagree on any padded
    # value — `'  mintree.us  '` normalised to itself in SQL and to
    # `'mintree.us'` in Python. `create_quarantine` only `.strip()`s its input,
    # but a value written by a direct SQL op is not stripped at all.
    # Portable: trim/lower/substr/nullif behave identically on PG and SQLite.
    lowered = f"trim(lower(coalesce({expr}, '')))"
    return (
        f"nullif(CASE WHEN {lowered} LIKE 'www.%' "
        f"THEN substr({lowered}, 5) ELSE {lowered} END, '')"
    )


# ---------------------------------------------------------------------------
# The domain chain for a `catalog_products` row.
#
# #1643: the comparators over this table agreed on how to SPELL a domain (#1641)
# but not on WHICH COLUMNS to read. The assembler and the reconciler cron used
# `cp.source_domain` alone; `catalog_trust_policy` and
# `catalog_row_trust_upserter` used the full chain. On a row with a NULL
# `cp.source_domain` — 4,007 of 14,124 in prod — a quarantined storefront kept
# its canonical pick while the trust layer marked it blocked.
#
# ⚠️ THE TIE-BREAKERS ARE THE CONTRACT, NOT AN IMPLEMENTATION DETAIL.
# A first version of this chain replicated the *shape* of
# `catalog_row_trust_upserter`'s `DISTINCT ON` CTEs but not their ORDER BY legs,
# and review measured the two resolving DIFFERENT domains on 6 of 11 shapes —
# producing opposite quarantine verdicts in BOTH directions:
#
#   * merchant with stores on two platforms, the `is_primary` row on the other
#     platform (the uniqueness constraint is per MERCHANT, not per
#     (merchant, platform)), one store `deleted` with a newer `last_sync`:
#     dropping `(status='active') DESC` picked the deleted store's domain.
#     Quarantine the deleted domain -> the row is dropped from the canonical
#     pick while trust says `public`: a public product with no PDP.
#     Quarantine the live domain -> the row is KEPT: bug #1643 verbatim.
#   * Path-C minted canonical with two attached seeds (one per retailer, which
#     is what `minted_seed_one`'s own docstring says the lane produces):
#     dropping `(spl.product_id IS NOT NULL) DESC` picked the identity-less
#     sibling.
#
# So the ORDER BY legs below are defined ONCE and consumed by both shapes — the
# lateral form here and the `DISTINCT ON` CTEs in catalog_row_trust_upserter.
# Two shapes are unavoidable (a selective per-content_key lookup wants laterals,
# a bulk scan wants CTEs); two RULES are not.
#
# `nullif(..., '')` on every leg, not just the outer wrapper: `coalesce` treats
# an EMPTY STRING as present, so `cp.source_domain = ''` short-circuited the
# whole chain and the fallback legs never ran.
#
# COST, measured on a prod-shaped corpus WITH prod's existing indexes:
# assembler `fetch_products_for_key` 0.214 -> 0.811 ms; reconciler
# `candidates_sql` 46.2 -> 97.5 ms (2.1x). Both are far inside the 800 ms
# statement timeout, and the reconciler is still below its pre-#1640 265 ms.
#
# ⚠️ No new index is needed. A first version of this change shipped one, on the
# claim that `attached_product_key` was unindexed — false: migration 044 creates
# `idx_external_product_seeds_attached (attached_product_key, attached_variant_id)`
# and nothing drops it. The "21x regression without an index" that justified it
# reproduced only on a benchmark corpus missing that index. Do not re-add it
# without re-measuring against the real schema.
#
# The real cost driver is textual: `sql_bare_domain` inlines its argument 3x and
# the anti-join uses the row expression twice, so this chain appears SIX times in
# the emitted predicate. Binding it once (a `LATERAL (SELECT ...) d(domain)` in
# the FROM) would collapse ~18 subplans to 3 — worth doing, not done here.
#
# ⚠️ MEASURED BLAST RADIUS ON PROD, 2026-07-30: widening changes **0 rows**
# today — of the 4,007 NULL-`source_domain` rows, none resolve to a quarantined
# domain via seed or store. This is convergence, not a live unblock. Do not read
# a zero delta after deploy as the change having failed.
# ---------------------------------------------------------------------------

# Which seed wins for one `external_product_id` / `attached_product_key`.
SEED_PICK_ORDER = (
    "(s.status = 'active') DESC, "
    "s.updated_at DESC NULLS LAST, "
    "s.created_at DESC NULLS LAST, "
    "s.id DESC"
)

# Path-C minted seeds additionally prefer a seed that CARRIES an identity
# listing over a merely-newer sibling: the winner's external_product_id is the
# identity join key downstream, so picking an identity-less sibling re-derives
# IDENTITY_CONFIDENCE_NULL nondeterministically as updated_at values move.
#
# THIS IS A FUNCTION, NOT A CONSTANT (issue #1665). It was a constant matching on
# `spl.product_id = s.external_product_id` ALONE. Per ADR-008 `source_listing_ref`
# is merchant_id:product_id, so product_id is explicitly NOT unique across
# merchants — and a seed whose listing lives at ANOTHER merchant then satisfied
# the leg. Being inside EXISTS it could not fan out; what it did instead was pick
# the WRONG SEED. The outer lateral (which does carry the merchant conjunct) then
# found nothing, the group read NULL, `count(DISTINCT)` skipped it, and a
# genuinely fragmented cross-domain content_key reported CLEAN. Measured: a
# fragmented key reads 1 when the rival seed has no listing anywhere, and 0 once
# it has one at a different merchant.
#
# Requiring `merchant_expr` rather than defaulting it is the point: every caller
# must name the merchant this seed is being picked FOR, and there is no spelling
# of this leg that silently omits it.
def minted_seed_identity_leg_sql(merchant_expr: str) -> str:
    """ORDER BY leg preferring a seed whose listing exists AT `merchant_expr`.

    `merchant_expr` is a literal SQL expression naming the merchant the seed is
    being picked for — e.g. ``"cp.merchant_id"``. It must be in scope at the
    point the leg is interpolated; a CTE over `external_product_seeds` alone has
    no merchant and must join the attached catalog row to get one.
    """
    if not merchant_expr or not merchant_expr.strip():
        raise ValueError(
            "minted_seed_identity_leg_sql requires a merchant expression; a "
            "merchant-less identity leg matches listings across merchants and "
            "picks the wrong seed (issue #1665)")
    return (
        "(EXISTS (SELECT 1 FROM pdp_identity_listing spl "
        "WHERE spl.product_id = s.external_product_id "
        f"AND spl.merchant_id = {merchant_expr})) DESC"
    )

# Which store wins for one (merchant_id, platform).
# `(m.status = 'active') DESC` is FIRST and load-bearing — see the comment above.
# `m.store_id DESC` is the deterministic final: without it two stores with equal
# is_primary/last_sync resolve to whatever the plan yields, so a quarantine can
# start and stop applying to a row with no data change at all.
STORE_PICK_ORDER = (
    "(m.status = 'active') DESC, "
    "m.is_primary DESC NULLS LAST, "
    "m.last_sync DESC NULLS LAST, "
    "m.created_at DESC NULLS LAST, "
    "m.store_id DESC"
)

# The seed's merchant, resolved from the catalog row it is ATTACHED to rather
# than from the enclosing query. This is deliberately scope-independent: the
# obvious `cp.merchant_id` would be TWO levels of correlation out from the
# `spl` inside the leg's EXISTS (spl -> seeds subquery -> cp), and while
# Postgres resolves that, SQLite does not — `no such column: cp.merchant_id`.
# The chain below is executed under SQLite by the agent_pdp_view_assembler
# comparator tests, so the leg here must not depend on what encloses it.
_ATTACHED_ROW_MERCHANT_SQL = (
    "(SELECT cpx.merchant_id FROM catalog_products cpx "
    "WHERE cpx.product_key = s.attached_product_key)"
)

CATALOG_PRODUCT_DOMAIN_SQL = f"""coalesce(
      nullif(cp.source_domain, ''),
      (SELECT nullif(s.domain, '') FROM external_product_seeds s
        WHERE cp.source_system = 'external_product_seeds_mirror_v1'
          AND s.external_product_id = cp.source_product_id
        ORDER BY {SEED_PICK_ORDER}
        LIMIT 1),
      (SELECT nullif(s.domain, '') FROM external_product_seeds s
        WHERE cp.source_system = 'catalog_enrichment_agent_v1'
          AND s.attached_product_key = cp.product_key
        ORDER BY {minted_seed_identity_leg_sql(_ATTACHED_ROW_MERCHANT_SQL)}, {SEED_PICK_ORDER}
        LIMIT 1),
      (SELECT nullif(m.domain, '') FROM merchant_stores m
        WHERE m.merchant_id = cp.merchant_id AND m.platform = cp.platform
        ORDER BY {STORE_PICK_ORDER}
        LIMIT 1),
      ''
    )"""


def build_quarantine_anti_join_sql(
    row_domain_expr: str,
    row_merchant_expr: str,
    row_platform_expr: str,
    row_source_system_expr: str,
    row_source_ref_expr: str,
) -> str:
    """
    Return an inline SQL anti-join fragment for opt-in source quarantine.

    Match value conventions:
    - domain: bare domain, compared case-insensitively to source_domain.
    - merchant_platform: <merchant_id>:<platform>.
    - source_system_ref: <source_system>:<source_ref>.

    The expression arguments are trusted SQL snippets supplied by the caller
    for the row being filtered, for example "p.source_domain".
    """
    expressions = [
        row_domain_expr,
        row_merchant_expr,
        row_platform_expr,
        row_source_system_expr,
        row_source_ref_expr,
    ]
    if any(not str(expr or "").strip() for expr in expressions):
        raise ValueError("all row SQL expressions are required")

    # CURRENT_TIMESTAMP, not now(): identical on Postgres (both give the
    # transaction timestamp), but `now()` does not exist on SQLite — and this
    # fragment is embedded in `external_seed_search`, whose suite runs on
    # SQLite. With now() that suite cannot EXECUTE the predicate at all, so the
    # gate would be green-but-unexercised in the only place it is tested. That
    # is the exact shape that cost a 16-minute prod feed outage in #1588.
    live = (
        "q.state = 'active' "
        "AND (q.expires_at IS NULL OR q.expires_at > CURRENT_TIMESTAMP)"
    )

    # UNCORRELATED, one arm per match type. The previous form was a single
    # correlated `NOT EXISTS` whose subquery referenced the outer row.
    #
    # WHERE THE WIN ACTUALLY IS — measured on PG 15, and NOT where the first
    # version of this comment claimed. It is not the seed seam. At that call
    # site four of the five row expressions are literal NULL, so Postgres folds
    # the merchant/source arms away and the legacy form reduced to one hashable
    # equality — it planned as a Hash Anti Join, not a rescan. On that shape the
    # rewrite is a small REGRESSION (~1–1.4 ms absolute, +17% to +47%), against
    # an 800 ms statement timeout.
    #
    # The win is on the `catalog_products` consumers, where all three arms are
    # live columns and the OR-chain is not hashable:
    #
    #   agent_pdp_view_reconciler truth CTE (60k rows, GROUP BY)  265 ms -> 42 ms
    #   agent_pdp_view_assembler (selective content_key, 4 rows)   0.042 -> 0.041 ms
    #
    # And it removes a scaling landmine: at 2,000 quarantine rows the legacy
    # reconciler query took 42.5 SECONDS versus 37 ms here; at 50,000 it ran past
    # eleven minutes. Prod has 16 rows today, so that is headroom, not a fix.
    #
    # Recorded precisely because the seam regression is real and small: someone
    # measuring only the seam will conclude this rewrite was a mistake and undo
    # it, reintroducing the reconciler cliff.
    #
    # Splitting into three subqueries that reference NO outer column lets each
    # be evaluated once and hashed. `NOT (A OR B OR C)` ≡ `NOT A AND NOT B AND
    # NOT C`, so this is a pure re-association — proven by differential test, not
    # by this comment (see tests/test_quarantine_anti_join_equivalence.py).
    #
    # 🚨 THE TRAP, and why each arm looks the way it does. `x NOT IN (S)` is NULL
    # — not TRUE — whenever S contains a NULL, so a single NULL in the subquery
    # silently excludes EVERY row. `sql_bare_domain` deliberately maps a blank
    # match_value to NULL, so S *will* contain NULLs unless filtered. Hence the
    # `IS NOT NULL` guard inside every subquery. And `{expr} IS NULL OR ...` on
    # the outside preserves the old behaviour for a row with no domain: it
    # matched nothing under NOT EXISTS, so it must be KEPT here.
    #
    # `IS NULL OR NOT IN` rather than `COALESCE(... , TRUE)`: COALESCE over a
    # boolean is where SQLite and Postgres are least alike, and portability is
    # what makes this fragment testable at all.
    domain_row = sql_bare_domain(row_domain_expr)
    domain_val = sql_bare_domain("q.match_value")
    merchant_row = f"{row_merchant_expr} || ':' || {row_platform_expr}"
    source_row = f"{row_source_system_expr} || ':' || {row_source_ref_expr}"

    return f"""
AND ({domain_row} IS NULL OR {domain_row} NOT IN (
  SELECT {domain_val} FROM catalog_source_quarantine q
  WHERE {live} AND q.match_type = 'domain' AND {domain_val} IS NOT NULL
))
AND ({merchant_row} IS NULL OR {merchant_row} NOT IN (
  SELECT q.match_value FROM catalog_source_quarantine q
  WHERE {live} AND q.match_type = 'merchant_platform' AND q.match_value IS NOT NULL
))
AND ({source_row} IS NULL OR {source_row} NOT IN (
  SELECT q.match_value FROM catalog_source_quarantine q
  WHERE {live} AND q.match_type = 'source_system_ref' AND q.match_value IS NOT NULL
))""".strip()


async def load_active_quarantines(*, db: Any) -> list[Quarantine]:
    rows = await _fetch_all(
        db,
        f"""
        SELECT {QUARANTINE_COLUMNS}
        FROM catalog_source_quarantine
        WHERE state = 'active'
          AND (expires_at IS NULL OR expires_at > now())
        ORDER BY created_at DESC, quarantine_id DESC
        """,
    )
    return [_quarantine_from_row(row) for row in rows]


async def is_source_quarantined(
    domain: Optional[str],
    merchant_id: Optional[str],
    platform: Optional[str],
    source_system: Optional[str],
    source_ref: Optional[str],
    *,
    db: Any,
) -> bool:
    quarantines = await load_active_quarantines(db=db)
    return any(
        quarantine_matches_source(
            quarantine,
            domain=domain,
            merchant_id=merchant_id,
            platform=platform,
            source_system=source_system,
            source_ref=source_ref,
        )
        for quarantine in quarantines
    )


async def create_quarantine(
    *,
    match_type: str,
    match_value: str,
    reason: Optional[str],
    created_by: str,
    expires_at: Optional[datetime] = None,
    metadata: Optional[dict[str, Any]] = None,
    db: Any,
) -> Quarantine:
    _validate_match_type(match_type)
    normalized_match_value = _required_text(match_value, "match_value")
    # CANONICALISE a domain at WRITE time. Read-side normalisation is applied in
    # all four comparators, so this is belt-and-braces rather than the fix — but
    # it means the stored value is what an operator would expect to see, and it
    # removes the whole class rather than requiring four call sites to keep
    # agreeing. An operator typing `www.mintree.us` or `MINTREE.US` previously
    # stored it verbatim and got a quarantine that reported success while some
    # comparators honoured it and others did not (#1639).
    if match_type == MATCH_TYPE_DOMAIN:
        # Reject anything that is not a bare hostname BEFORE normalising.
        # `bare_domain` is a strip/lower/www-prefix rule, not a URL parser, so
        # `https://www.mintree.us/products/x` would be stored verbatim
        # (lowercased) and then match nothing on any comparator — a quarantine
        # that reports success and blocks nobody, which is the exact failure
        # mode this workstream exists to end. Reject loudly instead.
        if any(ch in normalized_match_value for ch in "/:") or normalized_match_value.split() != [
            normalized_match_value
        ]:
            raise ValueError(
                f"match_value {match_value!r} is not a bare hostname — pass "
                "'mintree.us', not a URL, port or path"
            )
        normalized_match_value = bare_domain(normalized_match_value)
        if not normalized_match_value:
            raise ValueError(
                f"match_value {match_value!r} does not normalise to a hostname"
            )
    normalized_created_by = _required_text(created_by, "created_by")
    row = await _fetch_one(
        db,
        f"""
        INSERT INTO catalog_source_quarantine (
          match_type,
          match_value,
          reason,
          expires_at,
          created_by,
          metadata
        )
        VALUES (
          :match_type,
          :match_value,
          :reason,
          :expires_at,
          :created_by,
          :metadata
        )
        RETURNING {QUARANTINE_COLUMNS}
        """,
        {
            "match_type": match_type,
            "match_value": normalized_match_value,
            "reason": reason,
            "expires_at": expires_at,
            "created_by": normalized_created_by,
            "metadata": metadata,
        },
    )
    if row is None:
        raise RuntimeError("create quarantine did not return a row")
    result = _quarantine_from_row(row)

    # C1 Phase 2: recompute trust for all catalog rows matching this quarantine
    # so serving_decision flips to 'blocked' immediately.
    await _trust_refresh_for_quarantine(result, db=db)

    return result


async def revoke_quarantine(
    *,
    quarantine_id: int,
    revoked_by: str,
    db: Any,
) -> Quarantine:
    normalized_revoked_by = _required_text(revoked_by, "revoked_by")
    row = await _fetch_one(
        db,
        f"""
        UPDATE catalog_source_quarantine
        SET state = 'revoked',
            revoked_at = now(),
            revoked_by = :revoked_by
        WHERE quarantine_id = :quarantine_id
        RETURNING {QUARANTINE_COLUMNS}
        """,
        {"quarantine_id": int(quarantine_id), "revoked_by": normalized_revoked_by},
    )
    if row is None:
        raise LookupError(f"catalog_source_quarantine row not found: {quarantine_id}")
    result = _quarantine_from_row(row)

    # C1 Phase 2: recompute trust for catalog rows that were blocked by this
    # quarantine; now revoked, they may become public again.
    await _trust_refresh_for_quarantine(result, db=db)

    return result


def quarantine_matches_source(
    quarantine: Quarantine,
    *,
    domain: Optional[str],
    merchant_id: Optional[str],
    platform: Optional[str],
    source_system: Optional[str],
    source_ref: Optional[str],
    now: Optional[datetime] = None,
) -> bool:
    if quarantine.state != "active":
        return False
    if _is_expired(quarantine.expires_at, now=now):
        return False

    if quarantine.match_type == MATCH_TYPE_DOMAIN:
        # A blank match_value must match NOTHING, not every domain-less row.
        # Without this guard `bare_domain('') == bare_domain(None)` is
        # `'' == ''` -> True, so one blank quarantine row silently quarantines
        # every row whose domain sources are all empty. `match_value` is
        # `TEXT NOT NULL` with no non-empty CHECK and these rows come from direct
        # SQL ops, so a blank is reachable.
        #
        # The SQL twin gets this from `nullif(..., '')`; this is the Python side
        # of the same guard, and it is why they now agree on blanks. Callers that
        # already guard locally (services/offer_currency_policy) keep doing so as
        # belt-and-braces — this is the authoritative one.
        normalized = bare_domain(quarantine.match_value)
        if not normalized:
            return False
        return normalized == bare_domain(domain)

    if quarantine.match_type == MATCH_TYPE_MERCHANT_PLATFORM:
        return quarantine.match_value == _join_match_value(merchant_id, platform)

    if quarantine.match_type == MATCH_TYPE_SOURCE_SYSTEM_REF:
        return quarantine.match_value == _join_match_value(source_system, source_ref)

    return False


def _validate_match_type(match_type: str) -> None:
    if match_type not in VALID_MATCH_TYPES:
        allowed = ", ".join(sorted(VALID_MATCH_TYPES))
        raise ValueError(f"invalid match_type {match_type!r}; expected one of: {allowed}")


def _required_text(value: Optional[str], name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} is required")
    return normalized


def bare_domain(value: Optional[str]) -> str:
    """Bare host: lowercase, no leading `www.`.

    Applied to BOTH the quarantine's match_value and the row's domain, and kept
    byte-equivalent to `sql_bare_domain` above so the Python matcher and the
    SQL anti-join can never disagree about the same pair. They did before:
    a `www.mintree.us` quarantine matched nothing on either side while a
    `mintree.us` one matched only on the side that happened to strip.

    `create_quarantine` accepts whatever the operator types, so normalising at
    read time covers rows already in the table as well as new ones.
    """
    text = str(value or "").strip().lower()
    return text[4:] if text.startswith("www.") else text


def _join_match_value(left: Optional[str], right: Optional[str]) -> Optional[str]:
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if not left_text or not right_text:
        return None
    return f"{left_text}:{right_text}"


def _is_expired(expires_at: Optional[datetime], *, now: Optional[datetime]) -> bool:
    if expires_at is None:
        return False
    current = now or datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        current = current.replace(tzinfo=None)
    else:
        current = current.astimezone(expires_at.tzinfo)
    return expires_at <= current


async def _fetch_all(db: Any, query: str, values: Optional[dict[str, Any]] = None) -> Sequence[Any]:
    if hasattr(db, "fetch_all"):
        return await _maybe_await(db.fetch_all(query, values or {}))
    if hasattr(db, "fetch"):
        return await _maybe_await(db.fetch(query, values or {}))
    raise TypeError("db must expose fetch_all(query, values) or fetch(query, values)")


async def _fetch_one(db: Any, query: str, values: Optional[dict[str, Any]] = None) -> Any:
    if hasattr(db, "fetch_one"):
        return await _maybe_await(db.fetch_one(query, values or {}))
    rows = await _fetch_all(db, query, values)
    return rows[0] if rows else None


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _quarantine_from_row(row: Any) -> Quarantine:
    return Quarantine(
        quarantine_id=int(_row_value(row, "quarantine_id")),
        match_type=str(_row_value(row, "match_type")),
        match_value=str(_row_value(row, "match_value")),
        state=str(_row_value(row, "state")),
        reason=_row_value(row, "reason"),
        expires_at=_row_value(row, "expires_at"),
        created_by=str(_row_value(row, "created_by")),
        created_at=_row_value(row, "created_at"),
        revoked_at=_row_value(row, "revoked_at"),
        revoked_by=_row_value(row, "revoked_by"),
        metadata=_row_value(row, "metadata"),
    )


def _row_value(row: Any, key: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    return getattr(row, key)


async def _trust_refresh_for_quarantine(quarantine: Quarantine, *, db: Any) -> None:
    """Recompute catalog_row_trust for all rows matching a quarantine predicate.

    Fire-and-forget: called after create_quarantine and revoke_quarantine so
    affected rows flip serving_decision without waiting for the cron. Errors
    are logged and swallowed so a trust failure can never break quarantine ops.
    """
    try:
        from services.catalog_row_trust_upserter import (
            upsert_catalog_row_trust_for_quarantine_match,
        )

        wrote = await upsert_catalog_row_trust_for_quarantine_match(
            db=db,
            match_type=quarantine.match_type,
            match_value=quarantine.match_value,
        )
        logger.info(
            "catalog_row_trust refreshed after quarantine op "
            "quarantine_id=%s match_type=%s match_value=%s wrote=%s",
            quarantine.quarantine_id,
            quarantine.match_type,
            quarantine.match_value,
            wrote,
        )
    except Exception:  # pragma: no cover
        logger.exception(
            "catalog_row_trust refresh failed after quarantine op "
            "quarantine_id=%s match_type=%s match_value=%s",
            quarantine.quarantine_id,
            quarantine.match_type,
            quarantine.match_value,
        )
