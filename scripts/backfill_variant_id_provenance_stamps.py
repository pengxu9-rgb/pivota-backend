"""Write down, on every catalog_skus row, where its variant id came from.

WHAT THIS FIXES. `services/variant_identity.variant_id_provenance` is the repo's one answer to
"is this variant id the merchant's, or one we minted?", and three writers already record that
answer into `sku_payload.variant_id_provenance` at write time —
`services/catalog_enrichment_agent/ingestion.py` (since #2113), `services/catalog_variant_promoter`,
and `scripts/backfill_variant_identity_skus.py` (#2118). Everything written before those, and
everything written by the OTHER derivation sites `services/variant_identity`'s own docstring
names, carries no stamp at all.

Measured 2026-09-08. Over all 30,256 catalog_skus rows the classifier places them at
**14,320 merchant_issued / 11,926 product_derived / 4,010 unverifiable / 0 absent** — that is the
whole table, not a sample. The stamp coverage is the weaker measurement and is stated as such: on
a SAMPLE, about **75% of rows carry no `variant_id_provenance` key**, and among the sampled rows
that do, the stored value never disagreed with the classifier. "Never disagreed" is a positive
claim with a detection floor, so this script does not assume it: it re-checks the comparison on
EVERY row and reports the count, which is the first census of it over the full table.

So the stamp is not a second opinion competing with the predicate; it is the predicate's answer,
cached at the row. This script fills in the rows that never got one.

WHY A STAMP AT ALL, when the predicate is pure and can be called any time? Because the id is a
string and the classification depends on TWO MORE strings — `source_product_id` and `product_key`.
A restated id is only recognisable next to the product it restates. Any reader that has the SKU row
but not its product row (a checkout preflight handed a sku_key, an export, a SQL-side cohort query)
cannot re-derive the answer, and a SQL-side re-derivation would be a second implementation of
`_is_restatement_of` — the thing this module's own docstring exists to prevent. Stamping puts the
answer where the row is.

── WHAT IT DELIBERATELY REFUSES TO DO ──────────────────────────────────────────────────────────

**It never overwrites an existing stamp, not even one it disagrees with.** The `WHERE` clause on
the UPDATE is `(sku_payload->>'variant_id_provenance') IS NULL`, so a row another writer stamped is
untouchable by this script — including a row whose stamp contradicts today's classifier. A
disagreement means either the classifier moved or a writer stamped something it should not have,
and both of those are findings for a human, not something a backfill should quietly launder. Every
disagreement is COUNTED and a bounded sample is carried in the report.

**It stamps suppressed rows too.** Suppression is a serving decision; provenance is a fact about
the string. A suppressed row that is later un-suppressed must not come back unlabelled, and a
suppressed row is exactly the kind of thing an audit reads. There is therefore no `suppressed_at`
filter anywhere in this file, on purpose.

── THREE THINGS THAT WOULD HAVE BEEN WRONG, AND WHY THEY ARE NOT ───────────────────────────────

1. **THE KEYSET CURSOR IS `sku_key`, NOT `product_key`.** The obvious cursor to copy from
   `scripts/backfill_variant_identity_skus.py` is `product_key`, because that is what that script
   pages on — but there `product_key` is catalog_products' PRIMARY KEY and therefore unique. In
   catalog_skus it is not: a product with 40 shades has 40 rows sharing it. Paging on a non-unique
   column with `WHERE product_key > :after` SILENTLY DROPS every remaining row of whichever product
   straddles the page boundary; using `>=` instead loops forever on it. Neither failure raises,
   and the report would still say "scanned N, stamped N" — the rows would just never have been
   seen. The cursor here is the primary key, so a page boundary can fall anywhere.
   `test_a_product_whose_rows_straddle_a_page_boundary_loses_none` executes exactly that case.

2. **THE BINDS INSIDE `jsonb_build_object` ARE CAST.** `jsonb_build_object` is variadic `"any"`,
   so Postgres cannot infer a bind's type from its position and rejects the statement at PREPARE —
   `could not determine data type of parameter $2` — on every row, having written nothing. That is
   #1703, verbatim, and it killed the first production `--apply` of
   `scripts/remediate_unpublished_crawl_rows.py`. Both binds are wrapped in `CAST(... AS text)`,
   and both statements below are registered in `tests/test_ops_script_sql_prepare_postgres.py` so
   the next statement added here is planned too, not just today's.

3. **A NON-OBJECT `sku_payload` IS REFUSED, NOT ERRORED THROUGH.** `jsonb || jsonb` requires both
   sides to be objects; `'[1,2]'::jsonb || '{"a":1}'::jsonb` raises. The column is jsonb, not
   "jsonb object", so nothing in the schema forbids an array or a scalar landing there. Such rows
   are filtered in Python from `jsonb_typeof`, counted as `skipped_payload_not_object`, and the
   UPDATE carries the same condition so a row that changes shape between the SELECT and the UPDATE
   is refused rather than aborting its page.

── THE ONE KNOWN DIVERGENCE FROM INGESTION, STATED RATHER THAN HIDDEN ──────────────────────────

`ingestion.py` classifies with a fourth argument — `handle=v["source_handle"]` (ingestion.py:325).
This script does not pass it. It COULD: catalog_skus has no handle column, but the handle survives
in `sku_payload.source_handle`, which the page SELECT already reads.

An earlier version of this note justified the omission by saying the handle exists only on rows
ingestion wrote, "which are already stamped". THAT IS FALSE, and the dates say so: ingestion began
writing `source_handle` on 2026-09-04 (`8c9c1f26e`) and `variant_id_provenance` on 2026-09-08
(`d466bc6ee`). Every row ingested in those four days carries a handle and NO stamp — it is exactly
this script's population. The divergence is real and reaches rows this run writes.

The convention stands anyway, for a better reason: **the stamp must answer the question the way the
money reader asks it.** `services/checkout_preflight.preflight()` — the gate that decides whether
an offer may be handed to a buyer — calls (at :189, inside the `try` that opens at :185)

    variant_id_provenance(variant_id, product_id=..., product_key=...)

with no handle. A cached answer that used a fourth input the reader does not have would be a
DIFFERENT predicate wearing the same key, and the first time the two disagreed the stamp would be
the wrong one. Reproducibility from the row's own columns is the property that makes the cache
sound; that is what `classify()` refuses to give up.

Passing a handle can only move a row TOWARDS `product_derived`, never away (the handle is just a
third candidate parent in `_is_restatement_of`), so the omission is the LESS conservative call and
is measured rather than assumed, on both halves of the table:

  * on rows already stamped — `already_stamped_disagree_explained_by_handle`. If it equals
    `already_stamped_disagree`, every disagreement is this divergence and nothing else.
  * on the rows THIS RUN WRITES — `stamped_would_differ_with_handle`, and its subset
    `stamped_would_differ_with_handle_from_merchant_issued`.

**The second is a stop condition.** `merchant_issued` is the only verdict any money path acts on,
so a row we stamp `merchant_issued` that the handle would call `product_derived` is the only kind
of difference that could over-promise. If that counter is non-zero in the dry run, do not apply:
either the handle belongs in the classification — in which case `checkout_preflight` is asking the
question wrong too, and that is the bug to fix — or those rows need looking at individually.
(There is deliberately no `..._to_merchant_issued` counter: the handle can only add a restatement
parent, so no row can move INTO `merchant_issued` by gaining one. Such a counter could never leave
zero, and a permanently-zero counter reads as evidence when it is not.)

    python3 scripts/backfill_variant_id_provenance_stamps.py                # dry run, counts only
    python3 scripts/backfill_variant_id_provenance_stamps.py --report       # identity table
    python3 scripts/backfill_variant_id_provenance_stamps.py --apply --limit 500 \
        --expect-contract stamp-v1-sku-key-cursor
    python3 scripts/backfill_variant_id_provenance_stamps.py --apply \
        --expect-contract stamp-v1-sku-key-cursor

STOP CONDITION, read off the dry run before applying: if
`stamped_would_differ_with_handle_from_merchant_issued` is non-zero, DO NOT APPLY. Those are rows
this script would stamp `merchant_issued` that `ingestion.py`'s four-argument call would call
`product_derived`, and `merchant_issued` is the verdict the checkout preflight spends money on.
See the handle section above for what to do about it.

If a run dies mid-scan ON AN EXCEPTION it still prints a fenced report — `mode: "failed"` — and
still writes its audit row; both carry `resume_after`. Committed pages are durable, so resume from
that cursor rather than re-running the table. A report carrying `resume_after_to_recover_rollbacks`
means a page was LOST: resume from that key instead, because `resume_after` is already past it.

THAT PROMISE IS NARROWER THAN IT SOUNDS. Both `run()` and `main()` catch `Exception`, not
`BaseException`, and a SIGTERM — which is what a Cloud Run task timeout sends, and what
`gcloud run jobs executions cancel` sends — raises nothing at all in a process with the default
handler: it just ends. So a run killed that way writes NO audit row and prints NOTHING; the
last thing in Cloud Logging is the page it was on. (MEASURED, not reasoned: a SIGTERM delivered
while page 2 was in flight left no `STAMPREPORT` line on stdout, zero `writer_audit_log` rows,
and exit 143 — while page 1's three rows stayed committed and correctly stamped.) Nothing is LOST
by it, because the UPDATE's own `IS NULL` predicate is the idempotency: a rescan from `--after ""`
stamps only the rows the killed run did not reach, and reports the rest as `rows_already_stamped`.
That rescan, not a cursor, is the recovery from a timeout. Size `--limit` so a run fits inside the
task timeout with room, and the exception path above stays the only one that needs a cursor.

Running it twice stamps nothing the second time: the UPDATE's own `IS NULL` predicate is the
idempotency, not a flag we carry.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.catalog_offer_writer_guard import (  # noqa: E402
    WriterAuditAccumulator,
    make_batch_id,
    write_writer_audit_log,
)
from services.variant_identity import variant_id_provenance  # noqa: E402

logger = logging.getLogger("backfill_variant_id_provenance_stamps")

SOURCE_SYSTEM = "variant_id_provenance_stamp_v1"
WRITER_NAME = "backfill_variant_id_provenance_stamps"

#: Required with --apply. Same purpose as the token in
#: `scripts/backfill_variant_identity_skus.py`: `scripts/ops/run_oneoff_job.sh` runs
#: `backend:latest`, and nothing about the command an operator types says WHICH build answered it.
#: A future edit that changes what `--apply` means bumps this token, so an in-flight command
#: written against the old meaning fails on the argument instead of doing something else.
CONTRACT = "stamp-v1-sku-key-cursor"

#: Sentinels around the one-line report. `run_oneoff_job.sh` retrieves job output from Cloud
#: Logging, which DROPS LINES — a pretty-printed report arrives with arbitrary keys missing and no
#: indication anything is gone. One line cannot be partially dropped. DISTINCT tokens and an
#: ANCHORED strip, so a report whose data contains the token is not silently corrupted:
#:
#:     ... | grep -o 'STAMPREPORT>>>{.*}<<<STAMPREPORT' \
#:         | sed 's/^STAMPREPORT>>>//; s/<<<STAMPREPORT$//' | python3 -m json.tool
REPORT_BEGIN = "STAMPREPORT>>>"
REPORT_END = "<<<STAMPREPORT"

#: How many disagreeing rows to carry in the report. Bounded because the report must stay ONE LINE
#: and Cloud Logging truncates a long one; the count is exact regardless of how many are sampled.
DISAGREEMENT_SAMPLE_CAP = 25

#: Keyset page over catalog_skus, ordered by the PRIMARY KEY — see note 1 in the module docstring.
#: `jsonb_typeof` rather than a Python `isinstance` on the parsed blob, because the decision the
#: UPDATE makes is a SQL one and both must agree on the same definition of "object".
#:
#: No `suppressed_at` filter, deliberately: provenance is a property of the string, not of whether
#: we are currently willing to serve the row.
SELECT_PAGE_SQL = """
    SELECT sku_key,
           product_key,
           platform,
           source_product_id,
           source_variant_id,
           jsonb_typeof(sku_payload)              AS payload_type,
           sku_payload->>'variant_id_provenance'  AS stamped,
           sku_payload->>'source_handle'          AS source_handle
    FROM catalog_skus
    WHERE sku_key > :after
    ORDER BY sku_key
    LIMIT :page
"""

#: CAST on BOTH binds — `jsonb_build_object` is variadic "any" and Postgres cannot infer a bind's
#: type from its position there (note 2 in the module docstring).
#:
#: The `IS NULL` in the WHERE is the whole safety argument AND the idempotency: it is why a second
#: run stamps nothing, and why a stamp another writer left — agreeing or not — is never overwritten.
#: It is re-checked here rather than trusted from the SELECT, so a row stamped by a concurrent
#: ingest between the read and the write is skipped rather than clobbered.
#:
#: `databases` returns no rowcount from `execute()` on asyncpg, so "did this land" is spelled
#: RETURNING and read with `fetch_val`.
#:
#: ── `updated_at` IS DELIBERATELY NOT BUMPED ────────────────────────────────────────────────────
#: The obvious `updated_at = NOW()` — every sibling backfill has one, including
#: `scripts/backfill_variant_identity_skus.py:246` — is WRONG here, and it is wrong precisely
#: BECAUSE readers depend on this column. It is not decoration this script may set freely; two
#: live readers treat it as "when did this row's commercial content last change", and a stamp is
#: metadata ABOUT the variant id string, not a change to the thing being sold:
#:
#:   1. `services/merchant_catalog_listing_fallback_service.py:55-67` emits
#:      `GREATEST(o.updated_at, s.updated_at, p.updated_at)` as a listing's `updated_at`, which
#:      `services/merchant_commerce_readiness_service.py:173-181` turns into a SEVEN-DAY
#:      freshness clock and `:232` into the `catalog_freshness_stale` blocker. Touching ~22,000
#:      rows would clear that blocker for every affected merchant and restart the clock at the
#:      backfill date — a stale catalog reported fresh, which is the dangerous direction.
#:      The same GREATEST is the ORDER BY of a `ROW_NUMBER() PARTITION BY s.sku_key` at :60-68,
#:      so a uniform bump also collapses "newest offer represents this SKU" into "lowest
#:      offer_id does".
#:   2. `services/pivot_query_service.py:1541-1551` sorts recall candidates by
#:      `sku_updated_at DESC` under BOTH a per-product cap (`RECALL_MAX_SKUS_PER_PRODUCT`, 12)
#:      and a `LIMIT`. Under a truncation the sort key decides which SKUs are served at all, so
#:      flattening it is not a reordering.
#:
#: Checked and NOT affected: the agent_pdp_view reconciler watermark
#: (`jobs/agent_pdp_view_reconciler_cron.py:121-141`) joins only catalog_products/catalog_offers,
#: the stale-row reaper (`services/catalog_sync_service.py:2319-2331`) selects by membership not
#: timestamps, and `services/catalog_trust_policy.py:687-733` reads none of this.
#: `tests/...::test_a_stamp_does_not_touch_updated_at` pins the omission.
STAMP_SQL = """
    UPDATE catalog_skus
       SET sku_payload = COALESCE(sku_payload, '{}'::jsonb) || jsonb_build_object(
               'variant_id_provenance', CAST(:provenance AS text),
               'provenance_stamped_by', CAST(:source_system AS text)
           )
     WHERE sku_key = :sku_key
       AND (sku_payload->>'variant_id_provenance') IS NULL
       AND (sku_payload IS NULL OR jsonb_typeof(sku_payload) = 'object')
    RETURNING sku_key
"""


def classify(row: Dict[str, Any], *, with_handle: bool = False) -> str:
    """The repo's classifier, called — never re-implemented.

    `with_handle` re-asks the question the way `ingestion.py:325` asks it, using the handle that
    survives in `sku_payload.source_handle`. Used ONLY to MEASURE the divergence — to explain a
    disagreement on an already-stamped row, and to count it on rows this run is about to write.
    Never to decide what to stamp: the stamp must be reproducible from the row's own columns, and
    must match how `services/checkout_preflight.preflight()` asks the question (:189). See
    the module docstring's section on this.
    """
    return variant_id_provenance(
        row.get("source_variant_id"),
        product_id=row.get("source_product_id"),
        product_key=row.get("product_key"),
        handle=row.get("source_handle") if with_handle else None,
    )


def _new_counts() -> collections.Counter:
    """Every outcome pre-seeded to zero.

    A bare Counter omits keys that never incremented, which makes a run that stamped nothing look
    like a run that never checked — and `record_info` drops <= 0 for the same reason, so the same
    ambiguity would reach `writer_audit_log.reasons`.
    """
    counts: collections.Counter = collections.Counter()
    for key in (
        "rows_scanned", "rows_already_stamped", "rows_unstamped",
        "already_stamped_agree", "already_stamped_disagree",
        "already_stamped_disagree_explained_by_handle",
        "stamped_would_differ_with_handle",
        "stamped_would_differ_with_handle_from_merchant_issued",
        "skipped_payload_not_object", "stamped",
        "raced_already_stamped", "row_errors",
        "page_rollbacks", "rows_lost_to_page_rollback", "stopped_at_limit",
    ):
        counts[key] = 0
    return counts


class _Tally:
    """The distributions the report carries, kept beside the flat counters."""

    def __init__(self) -> None:
        #: classifier verdict over EVERY row scanned — the inventory's identity table.
        self.by_class: collections.Counter = collections.Counter()
        #: verdict x platform, same population.
        self.by_platform: Dict[str, collections.Counter] = collections.defaultdict(
            collections.Counter
        )
        #: verdict over the rows that carried no stamp, i.e. this run's actual work.
        self.unstamped_by_class: collections.Counter = collections.Counter()
        #: verdict over the rows this run actually wrote.
        self.stamped_by_class: collections.Counter = collections.Counter()
        self.disagreements: List[Dict[str, str]] = []

    def as_dict(self) -> Dict[str, Any]:
        return {
            "class_all": dict(self.by_class),
            "class_unstamped": dict(self.unstamped_by_class),
            "class_stamped_this_run": dict(self.stamped_by_class),
            "class_by_platform": {
                platform: dict(counter) for platform, counter in sorted(self.by_platform.items())
            },
            "disagreement_sample": self.disagreements,
        }


async def _stamp_page(
    pending: List[Tuple[Dict[str, Any], str]],
    counts: collections.Counter,
    tally: _Tally,
    *,
    page_start: str,
    state: Dict[str, Any],
) -> None:
    """Write one page's stamps inside ONE transaction, each row under its own SAVEPOINT.

    WHY BOTH LEVELS. Per-row transactions (the shape
    `scripts/backfill_variant_identity_skus.py` uses) cost BEGIN + UPDATE + COMMIT round trips on
    every one of ~22,000 rows; a page transaction with a nested `database.transaction()` per row
    costs two round trips per PAGE plus the savepoint pair per row, and still contains a single
    row's failure — a nested transaction in `databases` is a SAVEPOINT, so a bad row rolls back to
    the savepoint and its 499 neighbours commit.

    NOTHING IS COUNTED UNTIL THE PAGE COMMITS. Incrementing beside the UPDATE made a page whose
    commit failed still report its rows as stamped, which is indistinguishable from success to
    anyone reading the number this script exists to produce.
    """
    if not pending:
        return
    staged: collections.Counter = collections.Counter()
    staged_classes: collections.Counter = collections.Counter()
    try:
        async with database.transaction():
            for row, provenance in pending:
                try:
                    async with database.transaction():  # SAVEPOINT
                        written = await database.fetch_val(
                            STAMP_SQL,
                            {
                                "sku_key": row["sku_key"],
                                "provenance": provenance,
                                "source_system": SOURCE_SYSTEM,
                            },
                        )
                except Exception as exc:  # noqa: BLE001 — one bad row must not cost its page
                    staged["row_errors"] += 1
                    logger.warning(
                        "stamp failed on %s: %s", row["sku_key"], repr(exc)[:200]
                    )
                    continue
                if written is None:
                    # The WHERE refused it: something stamped this row between our SELECT and our
                    # UPDATE, or its payload stopped being an object. Either way we wrote nothing
                    # and must not claim we did.
                    staged["raced_already_stamped"] += 1
                    continue
                staged["stamped"] += 1
                staged_classes[provenance] += 1
    except Exception as exc:  # noqa: BLE001
        # The page transaction itself failed. Its rows are NOT stamped; say so rather than
        # merging counts for writes that were rolled back. The only thing marking a row done is
        # the stamp itself, so a later run re-plans them — but `resume_after` alone would skip
        # them, since the scan must move past this page to make progress. Record the cursor
        # value the LOST page began at, so an operator has an exact place to resume from
        # instead of re-running 30,000 rows to recover 500.
        counts["page_rollbacks"] += 1
        counts["rows_lost_to_page_rollback"] += len(pending)
        state.setdefault("first_rollback_after", page_start)
        logger.warning("page transaction rolled back (%d rows): %s", len(pending), repr(exc)[:200])
        return
    counts.update(staged)
    tally.stamped_by_class.update(staged_classes)


async def run(
    *,
    apply: bool,
    limit: int = 0,
    after: str = "",
    page: int = 500,
    mode: str = "",
) -> Dict[str, Any]:
    """Scan catalog_skus, classify every row, stamp the ones carrying no stamp.

    `limit` bounds the rows STAMPED (or, in a dry run, the rows that would be), not the rows
    scanned — a pilot wants N writes, and 75% of rows are unstamped so "N scanned" would be a
    different, unstable number. Zero means no bound.
    """
    counts = _new_counts()
    tally = _Tally()
    audit = WriterAuditAccumulator(
        writer_name=WRITER_NAME, batch_id=make_batch_id(SOURCE_SYSTEM)
    )
    state = {"cursor": after or "", "mode": mode or ("apply" if apply else "dry_run")}
    try:
        await _scan(counts, tally, audit, apply=apply, limit=limit, page=page, state=state)
    except Exception as exc:
        # Seal the report, the resume cursor and the audit row before the exception leaves.
        # Committed pages are already durable; losing the cursor is what makes recovery manual —
        # the operator's alternative is re-scanning 30,000 rows to find the ~500 that are left.
        #
        # The sealed report rides OUT ON THE EXCEPTION, because `raise` discards a return value
        # and `main()` is the only thing that can print it. Sealing must not be able to replace
        # the original failure: if the run died because the connection went away, the audit
        # INSERT dies too, and a bare `await _finish(...)` here would raise that instead —
        # substituting a write error for the real cause. So it is guarded, and the original
        # exception propagates either way.
        try:
            report = await _finish(
                counts, tally, audit, apply=apply, state=state, error=repr(exc)[:500]
            )
            setattr(exc, "stamp_report", report)
        except Exception:  # noqa: BLE001 — never mask the cause with a failure to report it
            logger.exception(
                "could not seal the report after a mid-run failure; resume_after was %r",
                state.get("cursor"),
            )
        raise
    return await _finish(counts, tally, audit, apply=apply, state=state)


async def _scan(counts, tally, audit, *, apply, limit, page, state) -> None:
    while True:
        page_start = str(state["cursor"])
        rows = await database.fetch_all(
            SELECT_PAGE_SQL, {"after": page_start, "page": int(page)}
        )
        if not rows:
            return

        pending: List[Tuple[Dict[str, Any], str]] = []
        # The cursor advances to the last row this loop actually PROCESSED, never to the last row
        # the page returned. Those differ whenever --limit stops us mid-page, and taking the
        # page's last key there would resume past rows nobody looked at — a pilot run's own
        # `resume_after` would silently skip the remainder of its final page.
        last_key = page_start
        stopped = False
        for record in rows:
            if limit and (counts["stamped"] + len(pending)) >= limit:
                # Checked BEFORE the row is counted, so `rows_scanned` and the class tallies
                # describe exactly the rows this run examined.
                counts["stopped_at_limit"] = 1
                stopped = True
                break
            row = dict(record)
            last_key = str(row["sku_key"])
            counts["rows_scanned"] += 1
            provenance = classify(row)
            tally.by_class[provenance] += 1
            tally.by_platform[str(row.get("platform") or "(none)")][provenance] += 1

            stamped = row.get("stamped")
            if stamped is not None:
                counts["rows_already_stamped"] += 1
                if str(stamped) == provenance:
                    counts["already_stamped_agree"] += 1
                else:
                    counts["already_stamped_disagree"] += 1
                    if classify(row, with_handle=True) == str(stamped):
                        # Explained entirely by the fourth classifier argument this script
                        # cannot pass from columns. Still not overwritten.
                        counts["already_stamped_disagree_explained_by_handle"] += 1
                    if len(tally.disagreements) < DISAGREEMENT_SAMPLE_CAP:
                        tally.disagreements.append({
                            "sku_key": str(row["sku_key"]),
                            "stored": str(stamped),
                            "classifier": provenance,
                            "with_handle": classify(row, with_handle=True),
                        })
                continue

            if row.get("payload_type") not in (None, "object"):
                # `jsonb || jsonb` demands two objects; an array or scalar payload would raise.
                # Refused and counted rather than swallowed as a row error.
                counts["skipped_payload_not_object"] += 1
                continue

            counts["rows_unstamped"] += 1
            tally.unstamped_by_class[provenance] += 1
            if row.get("source_handle"):
                # The divergence from ingestion, measured on the population it can still affect:
                # rows this run is about to STAMP that carry a handle. (The `already_stamped_*`
                # counters above measure it on rows already stamped, which is the other half.)
                with_handle = classify(row, with_handle=True)
                if with_handle != provenance:
                    counts["stamped_would_differ_with_handle"] += 1
                    if provenance == "merchant_issued":
                        # The stop condition. `merchant_issued` is the only verdict any money
                        # path acts on, so this is the only difference that could over-promise.
                        counts["stamped_would_differ_with_handle_from_merchant_issued"] += 1
            pending.append((row, provenance))

        if apply:
            await _stamp_page(pending, counts, tally, page_start=page_start, state=state)
        else:
            # A dry run reports the plan. It writes nothing, so it neither commits nor rolls back
            # and there is no per-row outcome to distinguish.
            counts["stamped"] += len(pending)
            for _row, planned in pending:
                tally.stamped_by_class[planned] += 1

        state["cursor"] = last_key
        if stopped:
            # `last_key` may still equal `page_start` here — --limit can stop on a page's FIRST
            # row, having processed nothing. That is not a stalled cursor, it is a bounded run
            # ending; the guard below must not fire on it, and we return anyway.
            return
        # PROGRESS OR RAISE. Note 1 in the module docstring says paging with `>=` "loops forever";
        # nothing made that loud. With `>=` the tail page returns the single row the cursor
        # already names, `last_key` comes back equal to `page_start`, and this `while True` spins
        # on one row until the job is killed — no error, no output, a report that never prints.
        # With the correct `>` a processed row's key is strictly greater than the cursor by the
        # SELECT's own predicate, so equality here is unreachable and this can only fire on a
        # regression to the comparison, the ORDER BY, or the cursor column.
        if last_key == page_start:
            raise RuntimeError(
                "keyset cursor did not advance past "
                f"{page_start!r} after processing {len(rows)} row(s): the scan would loop "
                "forever. Check that SELECT_PAGE_SQL still pages on `sku_key > :after` "
                "ordered by sku_key."
            )


async def _finish(
    counts, tally, audit, *, apply: bool, state, error: Optional[str] = None
) -> Dict[str, Any]:
    counts["applied"] = 1 if apply else 0
    report: Dict[str, Any] = dict(counts)
    # Which question this run answered. Without it `stamped` is ambiguous — it is rows WRITTEN
    # under --apply and rows that WOULD BE written otherwise, and `--report` produces a plan it
    # will never execute. `applied` alone does not separate --report from a plain dry run.
    #
    # `failed` overrides the mode rather than sitting beside it: a report from a run that died
    # mid-scan describes a PARTIAL table, and anything reading `mode: apply` would take its
    # counts for a census of the whole one.
    report["mode"] = "failed" if error else (
        state.get("mode") or ("apply" if apply else "dry_run")
    )
    if error:
        report["error"] = error
        report["mode_attempted"] = state.get("mode") or ("apply" if apply else "dry_run")
    report["resume_after"] = state["cursor"]
    # Present ONLY when a page was lost, and then it is the resume point that recovers those
    # rows. `resume_after` is past them by construction, so a report carrying this key must not
    # be resumed from `resume_after` alone.
    if state.get("first_rollback_after") is not None:
        report["resume_after_to_recover_rollbacks"] = state["first_rollback_after"]
    report.update(tally.as_dict())
    if apply:
        ints = {
            k: v for k, v in counts.items()
            if isinstance(v, int) and not isinstance(v, bool)
        }
        # record_info drops <= 0, so a zero counter never reaches writer_audit_log.reasons even
        # though it is in the JSON report. Send the zeros under an explicit key instead of
        # leaving "measured zero" and "never measured" indistinguishable there.
        audit.record_info({k: v for k, v in ints.items() if v > 0})
        audit.reasons["zero_counters"] = sorted(k for k, v in ints.items() if v == 0)
        audit.reasons["class_stamped_this_run"] = dict(tally.stamped_by_class)
        # THE RESUME CURSOR BELONGS IN THE AUDIT ROW, not only in the printed report. On the
        # failure path the report reaches stdout and stdout reaches Cloud Logging, which drops
        # lines; the audit row is the copy that is still there tomorrow. Assigned rather than
        # passed through `record_info`, which is numeric and drops <= 0 — a cursor is neither.
        audit.reasons["resume_after"] = state["cursor"]
        if state.get("first_rollback_after") is not None:
            audit.reasons["resume_after_to_recover_rollbacks"] = state["first_rollback_after"]
        if error:
            audit.reasons["run_failed"] = error
        audit.record_applied(int(counts["stamped"]))
        await write_writer_audit_log(audit)
        report["batch_id"] = audit.batch_id
    return report


def _render_identity_table(report: Dict[str, Any]) -> str:
    """The inventory's identity table, rebuilt from a run's own numbers.

    Printed for a human beside the machine-readable line; the JSON is the contract.
    """
    classes = sorted(report.get("class_all") or {})
    lines = ["", "variant id provenance x platform (classifier over every row scanned)", ""]
    header = f"{'platform':<28}" + "".join(f"{c:>18}" for c in classes) + f"{'total':>10}"
    lines.append(header)
    lines.append("-" * len(header))
    for platform, per_class in sorted((report.get("class_by_platform") or {}).items()):
        total = sum(per_class.values())
        lines.append(
            f"{platform:<28}"
            + "".join(f"{per_class.get(c, 0):>18,}" for c in classes)
            + f"{total:>10,}"
        )
    lines.append("-" * len(header))
    all_counts = report.get("class_all") or {}
    lines.append(
        f"{'TOTAL':<28}"
        + "".join(f"{all_counts.get(c, 0):>18,}" for c in classes)
        + f"{sum(all_counts.values()):>10,}"
    )
    lines.append("")
    lines.append(
        f"stamped already: {report.get('rows_already_stamped', 0):,} "
        f"(agree {report.get('already_stamped_agree', 0):,}, "
        f"disagree {report.get('already_stamped_disagree', 0):,}) | "
        f"unstamped: {report.get('rows_unstamped', 0):,}"
    )
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Stamp sku_payload.variant_id_provenance on catalog_skus rows lacking it."
    )
    ap.add_argument("--apply", action="store_true", help="write; default is a dry run")
    ap.add_argument(
        "--report", action="store_true",
        help="read-only: scan the whole table and print the class x platform identity table. "
             "Implies no writes and ignores --limit.",
    )
    ap.add_argument(
        "--limit", type=int, default=0,
        help="stop after N rows have been STAMPED (0 = all). Not rows scanned: 75%% of rows are "
             "unstamped, so a scan bound would be a different number every run.",
    )
    ap.add_argument("--after", default="", help="resume: only sku_key > this")
    ap.add_argument(
        "--page", type=int, default=500,
        help="keyset page size. Also the transaction size, so it is the number of catalog_skus "
             "rows this holds a row lock on at once — lower it if a concurrent ingest is running.",
    )
    ap.add_argument(
        "--expect-contract", default="",
        help=f"required with --apply; must be {CONTRACT!r}. Its purpose is to fail on a stale "
             "image: a build that predates this script's merge, or postdates a change to what "
             "--apply means, rejects the command instead of doing something else.",
    )
    args = ap.parse_args(argv)
    if args.report and args.apply:
        ap.error("--report is read-only; drop --apply or drop --report.")
    if args.apply and args.expect_contract != CONTRACT:
        ap.error(
            f"--apply requires --expect-contract {CONTRACT}. Got {args.expect_contract!r}. "
            "If you passed the right token and still see this, the image is running a different "
            "version of this script than the one you read."
        )
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    async def _go() -> Dict[str, Any]:
        await database.connect()
        try:
            return await run(
                apply=args.apply,
                limit=0 if args.report else args.limit,
                after=args.after,
                page=args.page,
                mode="report" if args.report else ("apply" if args.apply else "dry_run"),
            )
        finally:
            await database.disconnect()

    def _emit(report: Dict[str, Any]) -> None:
        # ONE LINE, fenced — see REPORT_BEGIN. Printed on every mode, including --report, so the
        # human table and the machine-readable numbers can never drift apart.
        print(
            REPORT_BEGIN + json.dumps(report, sort_keys=True, default=str) + REPORT_END,
            flush=True,
        )

    try:
        report = asyncio.run(_go())
    except Exception as exc:
        # A RUN THAT FAILS ON AN EXCEPTION STILL PRINTS A REPORT. Without this the only output of
        # a mid-run failure is a traceback in Cloud Logging, and the pages that DID commit are
        # unrecoverable except by re-scanning the table: `resume_after` — the one number that
        # makes recovery cheap — was computed, written to the audit row, and then never shown to
        # the operator who has to type it. Exit code is non-zero, so the job is still a failed
        # job. `Exception`, not `BaseException`, on purpose and with a known cost: a SIGTERM
        # (Cloud Run task timeout) ends the process without reaching here, so nothing prints —
        # the module docstring says what the recovery is in that case (a rescan; the UPDATE is
        # idempotent).
        #
        # The sealed report is read off the exception, where run() left it. The fallback below
        # is for a failure BEFORE run() (an unresolvable DATABASE_URL) or a seal that itself
        # failed; printing it when a sealed report exists would drop the cursor on the floor.
        # `tests/...::test_main_prints_the_sealed_report_with_the_cursor_when_the_run_dies_mid_scan`
        # pins that this branch reads the attribute rather than always printing the fallback.
        report = getattr(exc, "stamp_report", None)
        report = dict(report) if isinstance(report, dict) else {
            # The seal itself failed (see run()); say so rather than printing a report shaped
            # like a run that measured zero of everything.
            "resume_after": None,
            "sealed": False,
        }
        report["mode"] = "failed"
        report.setdefault("error", repr(exc)[:500])
        _emit(report)
        logger.error("run failed: %s", repr(exc)[:500])
        return 1

    if args.report:
        print(_render_identity_table(report), flush=True)
    _emit(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
