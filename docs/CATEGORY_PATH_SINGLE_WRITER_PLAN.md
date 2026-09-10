# `category_path`: one writer, overrides as data

**Status:** proposed, 2026-09-10 · **Scope:** pivota-backend + PIVOTA-Agent, one shared column

## The problem in one line

`catalog_products.category_path` has **13 writers across two repositories, validating against six
different vocabularies or none at all**, and nine of those writers exist only to repair what the
other four wrote.

## Why patching a lane cannot work

A repair lane never becomes unnecessary, because it does not fix what produced the bad value. So
it runs forever — and a permanent writer has exactly the same standing as the correct one. There
is no precedence and no ordering: the winner is whichever webhook fired or operator ran last.

Observed, not theorised:

* `PIVOTA-Agent/scripts/reconcile-catalog-category-taxonomy.cjs` moves a row to its canonical path.
  The next `sync-external-seeds-to-catalog.cjs --apply` on those ids writes it back and re-stamps
  it. Both scripts live in the same repo.
* `sync-external-seeds-to-catalog.cjs:673-745` hardcodes 8 paths that its own sibling
  `src/services/beautyTaxonomy.js` lists as **non-canonical alias keys**. One script writes what
  its neighbour is built to undo.
* `services/catalog_sync_service.py:1506` overwrites the whole row on any product webhook,
  including writing `category_path = NULL` when its classifier misses — erasing whatever a repair
  lane put there.
* Re-onboarding a brand through `services/catalog_enrichment_agent/apply.py:87` re-stamps one
  operator-supplied string across every product of that brand.

Between 2026-09-09 and 2026-09-10 we hardened one lane at a time — the LLM validator's membership
check, the alias map, the invariant's predicate — and each improvement remained true only until
another lane touched the row. The 245-row backfill of 2026-09-10 will be undone by the next seed
re-mirror or brand re-onboard.

## The 13 writers

| kind | writers | what they are |
|---|---|---|
| **ingest** | `catalog_sync_service.py:1506` (merchant webhook), `mirror_external_seeds_to_catalog_products.py:1599` (15-min, insert-only) | the only two lanes every product actually arrives through |
| **ingest, unvalidated** | `catalog_enrichment_agent/apply.py:87` | one operator string per brand, free-form, overwrites on conflict |
| **duplicate ingest, other language** | `sync-external-seeds-to-catalog.cjs:1863`, `sync-ulta-external-seeds-to-catalog.cjs:641` | re-implement the mirror in Node, with a sixth vocabulary of 25 hardcoded literals |
| **repair** | `backfill_pdp_category_path.py`, `..._llm.py`, `run_pdp_label_agent.py`, `standardize_off_taxonomy_category_paths.py`, `reconcile-catalog-category-taxonomy.cjs`, `apply-reviewed-external-seed-category-patch.cjs`, one 2026-05 one-off | nine lanes that exist because the four above write wrong values |

Six of the thirteen validate against **nothing**; three of those six overwrite on conflict. The six
that do validate use four different in-repo constant sets, plus the Node sync's undeclared 25.

Stamps with **no writer anywhere in either repo, on any branch**: `codex_review_v1` (225 serving
rows), `regex_backfill_v2` (106), every `manual_*` (18). A writer-side fence cannot reach these.

## What we built that does not help yet

`category_taxonomy` (migration 221) was added as "one shared vocabulary". Today it is read by
**exactly one function** — the drift detector in `services/catalog_invariant_checks.py`. No writer
consults it and PIVOTA-Agent has no SQL against it at all. It is a monitored copy of one repo's
constants, not a contract.

## Target

**`category_path` is derived, not authored. Overrides are data, not writes.**

1. **One derivation.** Both ingest lanes call the same classifier over the same vocabulary. No
   other code writes the column.
2. **`category_path_overrides`** — `(product_key, path, reason, author, created_at)`. The served
   value is `COALESCE(override, derived)`. An operator or reviewer decision then *survives* a
   webhook instead of being clobbered by it, and two conflicting repairs become two visible rows
   rather than a silent last-write-wins.
3. **A foreign key** from the column to `category_taxonomy(path)`. This is the only point all
   thirteen writers pass through — including the ones nobody can find — so it is the only fence
   that actually fences.

The nine repair lanes stop being writers. Their logic becomes either a better classifier or an
override row.

## Migration

| phase | change | gate |
|---|---|---|
| **0** | linter in CI; `error_count` on the invariant runner, asserted zero over all of `_CHECKS` | none — do first, everything after is unverifiable without it |
| **1** | `ALTER TABLE … ADD CONSTRAINT … FOREIGN KEY (category_path) REFERENCES category_taxonomy(path) NOT VALID` | new writes gated immediately; existing rows untouched |
| **2** | drain the backlog: 84 declared gaps, 21 gateway spellings with no backend entry, every unknown string → NULL, alias, or promoted leaf. Then `VALIDATE CONSTRAINT` | the drift invariant is the progress meter |
| **3** | `category_path_overrides`; repoint the repair lanes at it; make the served value a COALESCE | |
| **4** | delete the repair lanes; collapse the Node syncs to seed-writers only | |

Phase 1 before phase 2 is deliberate: `NOT VALID` stops the bleeding on the day it ships, while the
cleanup takes as long as it takes.

## Costs, honestly

* **A new category becomes a row insert before either repo can write it** — a deploy-ordering
  dependency across two repos and one data change. The existing drift invariant is the detector
  for getting that wrong.
* **Writers that emit a rejected path start failing loudly.** That is the point, but
  `sync-external-seeds-to-catalog.cjs:1806` wraps ~500 rows in one `BEGIN`, so one bad path aborts
  the batch until it handles the error per row.
* **`_upsert_by_pk`'s NULL-on-miss survives a FK** (NULL passes). Separate one-line fix at
  `catalog_sync_service.py:1532`.
* **`INTENTIONALLY_DISTINCT` stops being a code review** and becomes a row-level decision: those
  seven paths get either leaf rows or alias rows, and the table records which.

## Two things this also settles

* **`agent_pdp_view.category_path` is not this column.** `agent_pdp_view_assembler.py:888` fills it
  from `category`/`product_type`. Two different values under one name, refreshed hourly by a job
  that faithfully copies the wrong source. Rename it.
* **The backend's recall is not the served lane for beauty category queries.**
  `PIVOTA-Agent/src/server.js:30574` self-invokes the gateway's own pipeline — "NOT the Python
  kernel backend". Every reachability number computed against backend recall describes a path the
  agent door does not use. That is a separate note; it changes what our invariants should measure,
  not what this plan does.

## What this does not propose

A fourteenth lane. If the answer to a bad `category_path` is a new script that rewrites it, the
answer is wrong.
