"""Persisted, ROW-GRAIN "will this PDP render?" — written here, computed nowhere else.

WHAT THIS IS FOR. The gateway's priced serving lane (PIVOTA-Agent
``src/services/acpFeedSource.js``) can only prove a sig is WELL-FORMED, never
that it RESOLVES. Measured on prod 2026-07-28: 9 of 600 sampled rows are
``serving_eligible: true`` AND ``renderable: false`` — well-formed sigs that pass
the lane's own ``ips.serving_eligible = TRUE`` join and are dead pages anyway
(~1.5%). The lane cannot ask the Python predicate, so the answer has to be a
column it can join.

═══════════════════════════════════════════════════════════════════════════════
WHY catalog_products AND NOT index_pipeline_state — the spec said the latter
═══════════════════════════════════════════════════════════════════════════════
``pdp_will_render_expression`` is the composite of a ROW fact and a KEY fact:

    pdp_renderable_expression   CONTENT ROUTE — row-grain. Resolves per
                                catalog_products row via external_product_seeds
                                on source_product_id / product_key.
    pdp_serving_gate_passes     index_pipeline_state.serving_eligible — content_key.

A composite of a row fact and a key fact is a ROW fact.
``index_pipeline_state``'s PRIMARY KEY is ``(content_key)`` (migration 098), so
it structurally cannot carry one value per row. Measured on prod:

    content_keys                                        11,288
    …with more than one catalog_products row             1,289
    …whose rows DISAGREE on pdp_will_render                279   (21.6% of those)

For those 279 no single per-content_key value is correct: ``ANY`` re-advertises
exactly the dead sigs this exists to close, ``ALL`` suppresses live ones.
Verified over HTTP, not merely by predicate — both sides of three disagreeing
keys, 6/6 matching the row-grain prediction (sig_817ed740… 200 vs its sibling
sig_aa918d2d… 404, and two more pairs like it).

═══════════════════════════════════════════════════════════════════════════════
🚨 THE COLUMN IS NEITHER WRITTEN NOR READ YET. It is 100% NULL in prod.
   NULL reads as do-not-advertise, so that is harmless — but say it plainly.
═══════════════════════════════════════════════════════════════════════════════
A stored composite is only as good as its invalidation, and NOTHING WRITES THIS
COLUMN YET. Both refresh entry points below exist and are called by no
production code. That is deliberate, and it is the second revision of this
decision:

An earlier cut called ``refresh_for_content_key`` inline from
``recompute_serving_eligibility``. Measured cost: ``_persist`` issues one
sequentially-awaited UPDATE per row, ``services/catalog_sync_service`` calls
``recompute_serving_eligibility`` inside a for-loop over products, and the app
and database are in different regions (~140ms RTT). That is (N+1)x140ms per
call — ~280ms typical, ~6.4s worst case — times ~3 recomputes per product, so a
1,000-product merchant sync would have paid **~14 minutes** to populate a column
nothing reads. It also produced a sparse, BIASED trickle: rows only got written
when their content_key happened to be recomputed, which is a poor basis for
"observe it against reality".

So the write belongs to a periodic **drift reconciler** (worklist P1.13),
modelled on the ``agent_pdp_view`` reconciler + its ``/__catalog_health`` drift
count. One code path, self-healing, set-based, and the drift number — not a
date — is what says the column is trustworthy enough to read.

**45 code paths INSERT/UPDATE catalog_products, 32 of them one-off scripts/
backfills.** Per-writer invalidation is not a design, and it is exactly how
``catalog_row_trust`` and the minted/mirror split became stale derivatives.

So this module WRITES the column and makes its staleness measurable, and nothing
reads it. ``routes/pivota_canonical_routes._renderable_column`` still evaluates
the live expression and is untouched. That ordering is deliberate: the column
can be observed against reality for as long as it takes to trust it, and a wrong
value costs nothing until a consumer exists.

**Before any consumer is added:** wire trigger (1), then prove freshness — not
that the column is *correct once*, but that it *stays* correct under writes.

═══════════════════════════════════════════════════════════════════════════════
ONE COMPUTATION, NOT A FOURTH TWIN
═══════════════════════════════════════════════════════════════════════════════
This module never re-derives renderability. It compiles
``services.pdp_renderability.pdp_will_render_expression`` — the same object the
canonical feed's ``renderable`` column uses — and writes its result. There is no
second implementation to drift, which is the whole reason the value is computed
here rather than hand-ported into the gateway. ``tests/test_pdp_renderability_store_postgres.py``
asserts the persisted column equals the live expression row for row on real
Postgres; if that ever fails, the column is wrong, never the expression.

Untouched on purpose: the three route twins and their parity test, and
``public_not_renderable``, whose threshold is a measured baseline for a
different question.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Iterable, List, Optional

import sqlalchemy as sa

from db.catalog import catalog_products
from services.pdp_renderability import pdp_will_render_expression

logger = logging.getLogger("pdp_renderability_store")

#: Column names, in one place so a consumer never spells them itself.
COLUMN_WILL_RENDER = "pdp_will_render"
COLUMN_COMPUTED_AT = "pdp_will_render_computed_at"

#: Default-off read flag. Reserved for the FIRST consumer, which does not exist
#: yet — see the module header. Named now so the rollout switch is not invented
#: in a hurry later, and so its default is on record as OFF.
READ_FLAG_ENV = "PDP_WILL_RENDER_PERSISTED_READ"

_ENV_TRUE = {"1", "true", "yes", "on"}


def persisted_read_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    """Is a consumer allowed to READ the persisted column? Default: no.

    Ships off, and stays off until the row-change invalidation trigger exists.
    A consumer that flips this while trigger (1) is unwired is advertising URLs
    on a signal that can silently go stale in the dangerous direction.
    """
    source = env if env is not None else os.environ
    return str(source.get(READ_FLAG_ENV, "")).strip().lower() in _ENV_TRUE


# DEPLOY-ORDER SAFEGUARD: the new columns are referenced ONLY by name, in the
# predicate below and in _persist's raw UPDATE — they are deliberately NOT added
# to db.catalog.catalog_products.
#
# This is not a style choice. Adding a column to the shared Core Table makes
# EVERY `select(catalog_products)` in the codebase emit it, so the instant this
# code deploys against a database that has not yet grown the column, every one
# of those SELECTs is an UndefinedColumn 500 — this repo's recorded "SQLAlchemy
# metadata column breaks SELECTs" failure. Verified both directions: with the
# columns absent, `select(catalog_products)` succeeds as written here and RAISES
# if the column is appended to the shared Table.
#
# (An earlier cut declared a local `sa.table()` for this. It was never
# referenced by anything, so twelve lines of comment described a safeguard that
# no code implemented — a later reader could have deleted it and concluded the
# protection was gone. The safeguard is the ABSENCE of a shared-Table column,
# which is what this comment now documents.)


def persisted_will_render_predicate(alias: str = "catalog_products"):
    """The read predicate consumers must use: ``pdp_will_render IS TRUE``.

    ``IS TRUE`` and never ``IS NOT FALSE``. The column is nullable with no
    default precisely so "never computed" is distinguishable from "computed
    false"; ``IS NOT FALSE`` collapses them and turns an uncomputed row into an
    advertisable one — which is the failure this whole item exists to prevent,
    reintroduced by a single operator.

    Takes an alias NAME rather than a Table so callers embedding this in
    hand-written SQL (the gateway lane aliases catalog_products as ``cp``) get
    the right qualifier instead of silently binding to the wrong scope.
    """
    qualifier = str(alias or "catalog_products").strip()
    if not (qualifier and (qualifier[0].isalpha() or qualifier[0] == "_")
            and all(ch.isalnum() or ch == "_" for ch in qualifier)):
        qualifier = "catalog_products"
    return sa.literal_column(f"{qualifier}.{COLUMN_WILL_RENDER}").is_(True)


def _compute_select(product_keys: Optional[Iterable[str]] = None,
                    content_keys: Optional[Iterable[str]] = None):
    """SELECT product_key, <live expression> — the ONE computation.

    Deliberately built from the shared expression object rather than a string
    copy: if ``pdp_will_render_expression`` changes, this follows with no edit.
    """
    stmt = sa.select(
        catalog_products.c.product_key,
        pdp_will_render_expression(catalog_products).label("will_render"),
    )
    if product_keys is not None:
        keys = [k for k in product_keys if k]
        stmt = stmt.where(catalog_products.c.product_key.in_(keys or [""]))
    if content_keys is not None:
        cks = [k for k in content_keys if k]
        stmt = stmt.where(catalog_products.c.content_key.in_(cks or [""]))
    return stmt


async def refresh_for_content_key(content_key: str, *, database=None) -> int:
    """Recompute + persist every row of one content_key. Trigger (2).

    Content_key scope, ROW granularity: `serving_eligible` flipping changes the
    answer for EVERY row under that key (it is one half of the composite), but
    each row still gets its own value because the other half — the content route
    — is per row. That is the grain distinction this module exists to respect,
    so it is enforced here rather than left to the caller.

    Never raises: a failed refresh must not fail the serving-eligibility
    recompute that triggered it. A stale value is bad; a serving_eligible write
    that rolls back because a bookkeeping column failed is worse, and nothing
    reads this column yet anyway.
    """
    if not str(content_key or "").strip():
        return 0
    if database is None:
        from db.database import database as _db
        database = _db
    try:
        rows = await database.fetch_all(
            _compute_select(content_keys=[content_key])
        )
        return await _persist(rows, database=database)
    except Exception as exc:  # pragma: no cover - logged, never raised
        logger.warning(
            {"event": "pdp_will_render_refresh_failed",
             "content_key": content_key, "error": str(exc)[:200]}
        )
        return 0


async def refresh_for_product_keys(product_keys: Iterable[str], *, database=None) -> int:
    """Recompute + persist specific rows. The entry point trigger (1) WILL use.

    Exists now, unwired, so the follow-up that adds row-change invalidation has
    a single place to call and does not invent a second write path.
    """
    keys = [k for k in (product_keys or []) if k]
    if not keys:
        return 0
    if database is None:
        from db.database import database as _db
        database = _db
    try:
        rows = await database.fetch_all(_compute_select(product_keys=keys))
        return await _persist(rows, database=database)
    except Exception as exc:  # pragma: no cover
        logger.warning(
            {"event": "pdp_will_render_refresh_failed",
             "product_keys": len(keys), "error": str(exc)[:200]}
        )
        return 0


#: Rows per UPDATE statement.
#:
#: NOT set by the bind-parameter ceiling, and saying so matters because a
#: correct number with a wrong reason is how the next person picks a wrong one.
#: Postgres caps binds at 65,535 (int16 Bind message) = 32,767 rows at 2/row —
#: at 1,000 rows we use 2,000, which is **32x under** the cap. The cap is not
#: what binds.
#:
#: The operative trade-off is ROUND TRIPS vs STATEMENT SIZE. App and database are
#: in different regions (~140ms RTT), so P1.13's full-table backfill of 14,104
#: rows is 15 statements ≈ 2.1s at this size. Ten times larger would be ~1.5
#: statements but a multi-megabyte statement; ten times smaller would be 141
#: round trips ≈ 20s. 1,000 sits in the flat part of that curve.
_PERSIST_CHUNK_ROWS = 1000


async def _persist(rows: List[Any], *, database) -> int:
    """Write the computed values, stamping computed_at so staleness is visible.

    ⚠️ PASSES PLAIN SQL, NOT ``sa.text(...)``, AND THAT IS LOAD-BEARING.
    Production runs ``databases.Database`` over **asyncpg**. Its ``_build_query``
    gives a ``str`` the ``text(q).bindparams(**values)`` treatment, but hands a
    non-``str`` WITH values to ``query.values(**values)`` — and ``TextClause``
    has no ``.values()``. So ``sa.text(sql)`` plus a params dict raises
    ``AttributeError: 'TextClause' object has no attribute 'values'`` on the
    production driver, every time.

    An earlier cut did exactly that. It passed 32/32 tests because the test shim
    was SQLAlchemy's SYNC engine over psycopg2, which accepts the same call
    happily — so the module would have shipped as a silent no-op in prod behind
    a fully green suite. Same SQL, same Postgres, different ADAPTER. The tests
    now drive ``databases.Database`` for exactly this reason.

    SET-BASED, and chunked. One statement per chunk rather than one per row: the
    app and database are in different regions (~140ms RTT), so a per-row loop is
    N round trips.

    ``computed_at`` is stamped in the SAME statement as the boolean — never a
    second pass — because a freshness marker that can drift from the value it
    describes is worse than none, and the whole read-gating story rests on it.
    """
    pairs = []
    for row in rows or []:
        mapping = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
        pk = mapping.get("product_key")
        if not pk:
            continue
        pairs.append((str(pk), bool(mapping.get("will_render"))))
    if not pairs:
        return 0

    written = 0
    for offset in range(0, len(pairs), _PERSIST_CHUNK_ROWS):
        chunk = pairs[offset:offset + _PERSIST_CHUNK_ROWS]
        # CAST on EVERY tuple, not just the first. Without it Postgres types the
        # VALUES column from the bind's text representation and refuses the
        # UPDATE with `column "pdp_will_render" is of type boolean but
        # expression is of type text`. Verified against asyncpg — psycopg2
        # interpolates client-side and never surfaces it, which is precisely why
        # this needs the real driver to be testable at all.
        values_sql = ", ".join(
            f"(:pk_{i}, CAST(:wr_{i} AS BOOLEAN))" for i in range(len(chunk))
        )
        params: Dict[str, Any] = {}
        for i, (pk, wr) in enumerate(chunk):
            params[f"pk_{i}"] = pk
            params[f"wr_{i}"] = wr

        # RETURNING + fetch_all, not execute + len(chunk). The return value is
        # rows WRITTEN, never rows OFFERED: offer one real key and one that does
        # not exist and the naive count says 2 having written 1.
        #
        # This matters beyond tidiness. `refresh_for_content_key` returns this
        # number, and P1.13's drift metric is the thing that decides whether the
        # column is trustworthy enough to read. A drift number counting offers
        # would make the column look healthier than it is — the precise failure
        # the metric exists to catch.
        rows_written = await database.fetch_all(
            f"""
            UPDATE catalog_products AS tgt
               SET {COLUMN_WILL_RENDER} = src.wr,
                   {COLUMN_COMPUTED_AT} = NOW()
              FROM (VALUES {values_sql}) AS src(pk, wr)
             WHERE tgt.product_key = src.pk
         RETURNING 1
            """,
            params,
        )
        written += len(rows_written or [])
    return written


__all__ = [
    "COLUMN_WILL_RENDER",
    "COLUMN_COMPUTED_AT",
    "READ_FLAG_ENV",
    "persisted_read_enabled",
    "persisted_will_render_predicate",
    "refresh_for_content_key",
    "refresh_for_product_keys",
]
