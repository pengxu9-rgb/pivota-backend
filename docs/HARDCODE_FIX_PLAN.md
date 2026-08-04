# Hardcode fix plan

Companion to `HARDCODE_AUDIT_2026-08-03.md`. Sequenced, with the reason each step
must precede the next. **Nothing here is started.**

## Rule 0 — re-verify before acting

Every file:line in the audit is an agent's citation. The one I checked myself had
the right mechanism and the wrong lines. **Each step below begins by confirming
its own citation**, and is abandoned if the citation does not hold.

## Rule 1 — measurement precedes any change to ranking or serving

Steps marked **[MEASURE]** change what users see. The rule this codebase learned
the hard way (720 seeds → 364 PDPs at HTTP 500) is: measure the *source* side of
a pointer, not just the destination. For ranking, that means diffing top-N over a
sampled query set before and after — not reasoning about the formula.

---

# Stage 0 — stop the bleed (blocks everything else)

### S0.1 — Node re-stamps `pdp_scope`, erasing PR #1667 ⚠️ VERIFIED BY ME
`PIVOTA-Agent/scripts/sync-external-seeds-to-catalog.cjs`
- `:1200` `pdp_scope: 'multi_merchant_canonical'` — stamped on every mirrored row
- `:1604` `pdp_scope = EXCLUDED.pdp_scope` — plain re-assert on conflict
- `:1607` the next line uses `COALESCE(catalog_products.pivota_signature_id, …)`

The author applied write-once preservation to the sig and not to the scope three
lines above. **Until this is fixed, #1667 is cosmetic** — the next sync restores
the label on every row it just corrected.

Fix: the Node sync must not assert `pdp_scope` at all on conflict (leave it to
the classifier-backed writer), or must `COALESCE` it. Decide which — they differ
for rows that legitimately need re-classification.

This is the **paired PR** #1667 needs. Same dual-write rule as `catalog_row_trust`.

### S0.2 — land #1667
Only meaningful after S0.1. Currently a draft; predicate rebuilt from the
classifier's definition, 11/11 mutations caught, now actually runs in CI.

---

# Stage 1 — confirmed and safe (no measurement, no decision)

Order within the stage does not matter. Each is behaviour-preserving or strictly
narrowing.

| # | Fix | Note |
|---|---|---|
| S1.1 | All-zero GTIN reject at `intake_identity.py` + `agent_pdp_view_assembler.py` | **Do NOT touch `normalize_gtin` itself** — `make_content_key` folds it, so any row already keyed with `gtin='0'` would silently re-key. Guard at the consumers. |
| S1.2 | Thread the validator's real currency through Path C write sites; drop the `or "USD"` tails | Writer only. The **backfill is NOT safe** — existing rows may have been priced correctly by coincidence. |
| S1.3 | Availability tri-state at Path C ingest | Update the two tests that currently pin the drifted values — they encode the bug. |
| S1.4 | Add `text_score`/`structure_score` to the citable SELECT | Flag-off behaviour byte-identical. **Land before any v2 rollout**, or the rollout looks correct while demoting the one lane that was fixed. |
| S1.5 | `scripts/backfill_pdp_scope.py` — replace the bare `1` with the shipped `CASE` | Only narrows; only touches `unverified` rows. |
| S1.6 | Import the shared `price_confidence` constant | Behaviour-preserving by construction. |

Each needs: citation re-verified, a test that fails without the fix, mutation
check, full-suite diff vs `main`.

---

# Stage 2 — needs a decision or a measurement

### S2.1 — `candidate_score` saturation **[MEASURE]**
The +200 scope bonus exceeds `candidate_score`'s own 140-point cap, so every
canonical row normalizes identically and is indistinguishable from a perfect
match on every non-exact search.

Minimal fix is reportedly to delete the flag branch rather than flip its default.
**Do not ship on that reasoning alone.** Required first: diff top-N over a
sampled query set. This reorders every non-exact result set in the product.

### S2.2 — `source_system_ref` is inert in BOTH repos
Both read `product["source_system_ref"]`; neither upserter emits it (the upserter
provides `source_ref`). Symmetric, which is exactly why every parity test passes
over it. Operator quarantine reports success and does nothing.

Decision needed: fixing it *activates* a control that has never actually run.
Whatever it would have been blocking is currently public. Measure the delta
before enabling.

### S2.3 — Node trust-chain divergence
`??` vs `or`, and a bare `toLowerCase()` against Python's normalizing comparator.
An empty-string `source_domain` flips a row `blocked` ↔ `public`; a `www.` prefix
flips the quarantine verdict. Paired-PR territory; rows flap if only one side
lands.

### S2.4 — Path C off the `external_seed` bucket
The root cause behind several findings. Ties into A9-4 phase 3, which is
**already blocked** for a different reason (per-row seller resolution from
`offers[0]`; 289 genuinely two-retailer rows). Do not start this before phase 3's
own blocker is resolved.

### S2.5 — `truth_tier='primary'` at Path C
Three sibling lanes write `observed`. Freezes graduation and inflates BD trust.
Changing it moves trust decisions → measure.

### S2.6 — pipe-format key still minted at `routes/employee_products.py`
Readers were converted to `prod::` in July; this writer was not, and the alarm
built to catch exactly this cannot parse pipe keys. Fix the writer *and* the
alarm's parser, or the next occurrence is silent again.

---

# Stage 3 — make the class unwritable

The audit's five root causes are all one shape: **a signal is erased, the correct
test becomes unsatisfiable, someone substitutes a weaker proxy, and the proxy
becomes load-bearing.** Point fixes do not stop the next instance.

- **S3.1** — extend the writer tripwire (`tests/test_catalog_products_writer_tripwire.py`)
  to the Node repo. It globs `*.py` only, so the two Node inserters are
  structurally invisible to it.
- **S3.2** — a namespace registry: every minted identity namespace declared in one
  place **with its grain** (content / listing / seller), plus a test asserting the
  prefixes present in code and in the DB equal the registry. This is what would
  have caught the `ext:` vs `prod::` grain split before the backfill.
- **S3.3** — an invariant that a stamped classification agrees with its classifier,
  for every `*_scope` / `*_tier` / `*_state` column that has one.
- **S3.4** — a rule stating a two-sided pointer must be conformed on both sides or
  neither. This is the actual lesson of the 364-PDP outage and it is written down
  nowhere; the guard from #1663 is hard-coded into one script's phase 2.

---

# Sequencing summary

```
S0.1 Node pdp_scope  ──►  S0.2 land #1667
                            │
Stage 1 (6 safe fixes) ─────┼──► independent, any order
                            │
S2.1 ranking [MEASURE] ─────┤
S2.2 quarantine [MEASURE] ──┤
S2.3 trust twin (paired) ───┤
S2.4 bucket ──── blocked on A9-4 phase 3
                            │
Stage 3 (prevention) ───────┘  after the class is understood, before the next lane
```

**Do first:** S0.1. Everything about `pdp_scope` is undone without it.
