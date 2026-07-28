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
🚨 THE COLUMN IS WRITTEN BUT DELIBERATELY UNREAD. Do not add a consumer yet.
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


async def _persist(rows: List[Any], *, database) -> int:
    """Write the computed values, stamping computed_at so staleness is visible.

    SET-BASED on purpose. The earlier per-row loop issued one sequentially
    awaited UPDATE per row; with the app and database in different regions
    (~140ms RTT) that is N round trips, and the caller that was going to use it
    sits inside a for-loop over products. One statement with a VALUES list is
    one round trip regardless of row count.

    ``computed_at`` is stamped in the SAME statement as the boolean — never a
    second pass — because a value whose freshness marker can be missing or
    stale relative to it is worse than no marker at all: the whole
    read-gating story rests on that timestamp.
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

    values_sql = ", ".join(
        f"(:pk_{i}, CAST(:wr_{i} AS BOOLEAN))" for i in range(len(pairs))
    )
    params: Dict[str, Any] = {}
    for i, (pk, wr) in enumerate(pairs):
        params[f"pk_{i}"] = pk
        params[f"wr_{i}"] = wr

    # CAST on the first tuple's parameter is load-bearing: Postgres cannot infer
    # a bare bind's type inside a VALUES list and refuses to PREPARE the
    # statement (IndeterminateDatatypeError) — the exact class that took this
    # repo's canonical feed down in #1588.
    await database.execute(
        sa.text(
            f"""
            UPDATE catalog_products AS tgt
               SET {COLUMN_WILL_RENDER} = src.wr,
                   {COLUMN_COMPUTED_AT} = NOW()
              FROM (VALUES {values_sql}) AS src(pk, wr)
             WHERE tgt.product_key = src.pk
            """
        ),
        params,
    )
    return len(pairs)


__all__ = [
    "COLUMN_WILL_RENDER",
    "COLUMN_COMPUTED_AT",
    "READ_FLAG_ENV",
    "persisted_read_enabled",
    "persisted_will_render_predicate",
    "refresh_for_content_key",
    "refresh_for_product_keys",
]
