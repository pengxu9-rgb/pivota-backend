# `category_path`: one writer, overrides as data

**Status:** proposed, 2026-09-10 · **Scope:** pivota-backend + PIVOTA-Agent, one shared column

## The problem in one line

`catalog_products.category_path` is written from **two repositories, by more lanes than anyone
can hold in their head, validating against several different vocabularies or against nothing at
all** — and most of those lanes exist only to repair what the others wrote.

The exact counts are in [the table below](#the-writers), which is the only place they appear.
That is deliberate, and it is the fifth version of this sentence: every `file:line` in this
document has survived four review passes, while the numbers over them were wrong four times
(13 writers over a table of 12; "nine repair lanes" listing seven; "three of six overwrite"
that is four; a vocabulary line totalling five under a headline of six). The enumeration was
never the problem. Re-typing an aggregate by hand, with nothing linking it to the list, was.
A number that is not written twice cannot disagree with itself.

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

## The writers
<a id="the-writers"></a>

| kind | writers | what they are |
|---|---|---|
| **ingest** | `catalog_sync_service.py:1506` (merchant webhook), `mirror_external_seeds_to_catalog_products.py:1599` (15-min, insert-only) | the only two lanes every product actually arrives through |
| **ingest, unvalidated** | `catalog_enrichment_agent/apply.py:87` | one operator string per brand, free-form, overwrites on conflict |
| **duplicate ingest, other language** | `sync-external-seeds-to-catalog.cjs:1863`, `sync-ulta-external-seeds-to-catalog.cjs:641` | re-implement the mirror in Node, with a sixth vocabulary of 25 hardcoded literals |
| **repair** | `backfill_pdp_category_path.py`, `..._llm.py`, `run_pdp_label_agent.py`, `standardize_off_taxonomy_category_paths.py`, `reconcile-catalog-category-taxonomy.cjs`, `apply-reviewed-external-seed-category-patch.cjs`, `reports/markato_expansion_status_20260524/wave12_apiceuticals_batch_20260525/apply_wave12_category_path_repair.cjs:299` (a 2026-05 one-off, in the Node repo) | lanes that exist because the writers above write wrong values |

Six validate against **nothing**; **four** of those six overwrite on conflict —
`apply.py:87`, both Node syncs, and the 2026-05 one-off, which was missed on the first count
because it was the one writer left unnamed. The six that do validate use four different in-repo
constant sets, plus TWO more in the Node repo: 25 undeclared inline literals in
`sync-external-seeds-to-catalog.cjs` (28 occurrences, 25 distinct — `sync-ulta-…` has NONE, and
an earlier version of this line said "the syncs'", attributing to both), and
`beautyTaxonomy.js:37`'s `CANONICAL_CATEGORY_PATHS`, 25 entries, which `reconcile-catalog-category-taxonomy.cjs`
validates against and `src/services/externalSeedProducts.js:90` also reads at serve time — the
true statement is the narrower one, that neither SYNC requires the module. Four plus two is the
"six vocabularies" above; a version of this line listed five and contradicted its own headline.

> **THE COUNTS USED TO BE WRITTEN TWICE, AND THE SECOND COPY WAS WRONG FOUR TIMES.** The table
> enumerated twelve while the prose said thirteen; the repair row listed seven lanes and called
> itself nine; "the four above" was five; "three of those six overwrite" is four; and a
> vocabulary line totalled five under a headline of six. Every `file:line` survived all of it.
>
> The counts now live in the table and nowhere else, so there is no second copy to disagree.
> Re-derived by reading the **non-test Python files that touch both `catalog_products` and
> `category_path`** (215 touch the table at all; 50 touch both; 3 of those 50 are under
> `reports/`), plus the Node repo. An earlier version of this sentence quoted this note's own
> writers-plus-named-readers as if it were the search space.

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
   every writer passes through — including the ones nobody can find — so it is the only fence
   that actually fences.

The repair lanes stop being writers. Their logic becomes either a better classifier or an
override row.

## Migration

| phase | change | gate |
|---|---|---|
| **0** | linter in CI (+ a compile check no config can disable); `errored_count` on the invariant runner and in the sweep summary | none — do first, everything after is unverifiable without it |
| **0b** | make the unit fake exercise every check, so `errored_count` is asserted zero over all of `_CHECKS`. It is **5 today** against `tests/test_catalog_invariant_checks.py`'s `FakeDb`, and that file is green | needs one shared DB fake; see the safety-net audit |
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
  `sync-external-seeds-to-catalog.cjs:1803` wraps a whole batch in one `BEGIN`, so one bad path
  aborts the batch until it handles the error per row. And the batch is normally EVERYTHING:
  `normalizeBatchSize:90` returns 0 unless `--batch-size` is passed, and `chunkArray:96` then
  returns a single chunk of all rows. 500 is the cap when a size IS given, not the default —
  an earlier version of this line said "~500 rows" and understated the blast radius.
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
