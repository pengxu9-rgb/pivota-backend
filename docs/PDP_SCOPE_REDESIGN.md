# pdp_scope redesign — the classifier owns the column

Replaces three point fixes that were each undone by an unfixed neighbor:
backend #1667 (promotion predicate — kept, becomes P2), Agent #1897 (COALESCE
guard — wrong, replaced by P1), and plan item S1.5 (backfill term — becomes P3).

## The invariant

**`services/pdp_scope_classifier.classify` is the only authority on
`pdp_scope`.** Ingest lanes may seed the column only with the DB default
(`'unverified'`, NOT NULL — migration 070). Promotion and demotion happen
exclusively through classifier-backed writers. No lane ever asserts
`'multi_merchant_canonical'` as a literal.

Why it matters: the label carries a +200 rank term in THREE backend sites
(`pivot_query_service.py:1048/:1090/:1476`) and, in the Node repo, +200 plus a
market-filter exemption (`canonicalCatalogSearch.js:581/:396`) — documented as
"large enough to dominate every other term".

## Measured state (prod, 2026-08-04)

Provenance of `pdp_scope` on `merchant_id='external_seed'` rows:

| scope | source | n | verdict under the corrected rule |
|---|---|---|---|
| canonical | `external_product_seeds_mirror_v1` (Node sync) | 2,690 | mostly WRONG — see below |
| canonical | `enrichment_agent_v1` (Path C) | 2,162 | LEGITIMATE (classifier rule 1: agent-authored is canonical by intent) |
| canonical | `pdp_identity_recovery` | 63 | re-evaluate (old buggy predicate) |
| merchant_owned | `backfill_2026_05` / `external_brand_crawl` / `kbeauty` | 5,357 | historical one-off scripts — confirms **no live writer demotes** |
| unverified | (null) | 877 | correct as-is |

- **Of 3,400 mirror-lane canonical rows, only 218 qualify** under the corrected
  rule (sum of other offer merchants + active seed domains ≥ 2, OR a
  cross-merchant group peer, OR an ext-cluster spanning ≥ 2 domains).
  **3,182 are demotion candidates.**
- **Of the 284 unsuppressed `unverified` bucket rows, 0 qualify for promotion**
  — so lanes seeding `'unverified'` loses no legitimate promotion today.

## The plan

### P1 — Node stops originating the label — ✅ MERGED (PIVOTA-Agent #1897, 9792caf6)
`scripts/sync-external-seeds-to-catalog.cjs` AND
`scripts/sync-ulta-external-seeds-to-catalog.cjs` (the byte-identical twin
found by review): remove `pdp_scope`, `pdp_scope_source`, `pdp_scope_set_at`
from both the INSERT payload and the ON CONFLICT SET list entirely. New rows
land on the DB default `'unverified'`; existing rows are untouched by this lane.

Why not COALESCE (#1897's approach): the column is `NOT NULL DEFAULT
'unverified'`, so `IS NULL` never fires — COALESCE is a no-op that also freezes
the 877 at `unverified` forever from this lane's side. Measured: 0 of them
qualify anyway, but the correct shape is "don't write", not "write once".

Effect on NEW mirror rows: they serve as `unverified` (= merchant_owned
semantics per migration 070) until P3 promotes the genuine multi-sellers.
Measured lag cost today: zero rows.

### P2 — backend promotion predicate — ✅ MERGED (#1667, 0096b7a1)
Already rebuilt from the classifier's definition, three reviews couldn't break
it, gated in CI. Land after P1.

### P3 — the backfill becomes the promotion path — ✅ MERGED (#1676, 8a163645)
Done as `own_merchant_seller_term_sql` in `services/pdp_scope_classifier` —
ONE spelling, THREE writers: the recovery predicate, `backfill_pdp_scope.py`
(the live defect: bucket + one seed = "2 sellers"), and
`onboard_external_brand_from_crawl.py` (same bare `1 +`, guarded only by a
docstring claim). An AST test pins all three call sites; the bucket literal is
pinned by test to `seller_identity.BANNED_BUCKET_MERCHANT_ID`.

The formerly pinned divergence — the backfill matched seeds attached by BOTH
the product_key and the pipe-composite form, the recovery predicate only by
product_key — is CLOSED (2026-08-04): both sides now render
`seed_attachment_keys_sql` from the classifier module, one spelling. Measured
at closure: prod cohort 0 (no row's demotion or promotion verdict changed;
demotion set 2,201 under both predicates, first-tick promotion set 350 under
both).

SCOPE NOTE (post-#1680-review): the CLI backfill is INITIAL RESOLUTION for
real-merchant rows only — `_fetch_batch` excludes bucket rows. `classify()`
never returns 'unverified', and 'merchant_owned' is an AFFIRMED state
(brand_verified_graduation: "resolved, NOT canonical") that exits the
`WHERE pdp_scope='unverified'` promotion gate forever — a state a bucket row
must never receive. Bucket lifecycle belongs to the P4 demote / D3 promote
pair. Cadence for THIS script stays manual; the scheduled path is D3 below.

### P4 — demotion backfill — MEASURED 2026-08-04, script in THIS PR

| metric | measured |
|---|---|
| demotion set (whole bucket, rule-1 excluded, NULL-safe rule) | **2,201** |
| sampled queries affected (37: top-20 brands, top-10 types, 8 generic) | 23 |
| top-10 slots vacated | 164 (worst: `shampoo` 10/10, overlap 0) |
| replacements (spot-checked `shampoo`/`serum`) | `merch_obs_*` observed-seller rows — correct attribution, same products |
| Node market-filter recall lost | **0 of 2,201** (every row has a US-market seed) |

`scripts/demote_unqualified_canonical_scopes.py`: dry-run by default,
reversibility record of every previous value, CAS-guarded writes (`RETURNING`
— `databases.execute()` returns None on this stack, finding F4), selection
interpolates `CANONICAL_SCOPE_PREDICATE` inside `NOT(...)` — one rule, one
spelling. Execution against prod (`--apply`) is the remaining operator action.
Re-measured 2026-08-04 after the composite-arm closure: demotion set unchanged
at 2,201.

### D3 — promote-only cron — REDESIGNED after #1680 review (findings F1–F4)

The first D3 shipped the CLI backfill's classify() loop on a 6-hour schedule.
Review drove it end-to-end on Postgres and found the design self-defeating:

- **F1 (fatal):** classify() never returns 'unverified' — every P4-demoted
  row (single-seller by construction, all 2,201) would be resolved to
  'merchant_owned' at the FIRST tick, exiting the
  `WHERE pdp_scope='unverified'` promotion gate forever: demote-to-
  merchant_owned with a 6-hour delay, the exact outcome D1 rejected.
- **F2:** "first ticks are no-ops" was false — the 0-of-284 measurement
  covered unsuppressed bucket rows; the loop covers ALL unverified rows.
  First tick on prod: ~1,253 writes, including ~905 merchant_owned
  conversions parking every future not-yet-multi-seller mirror row.
- **F3:** the backfill's seller-count sum cannot see the predicate's
  product-group-peer and ext-cluster arms (110 prod canonical rows qualify
  only via those); and the predicate lacked the backfill's composite-
  attachment arm (prod cohort 0) — closed, see P3.
- **F4:** `databases.execute()` returns None always — rowcounts must come
  from `RETURNING`.

The root error: the promotion gate treats classification as ONE-SHOT
RESOLUTION; the redesign needs CONTINUOUS RE-QUALIFICATION. The redone cron
(`jobs/pdp_scope_backfill_cron.py`, registered every 6h at :31) is therefore
PROMOTE-ONLY and predicate-driven: ONE UPDATE — promote 'unverified',
unsuppressed rows satisfying `CANONICAL_SCOPE_PREDICATE`
(`pdp_scope_source='d3_promotion_cron'`). No classify() loop, no second
spelling of the rule; unqualified rows STAY 'unverified' and stay
re-checkable. There is no "no-op first ticks" claim and no live re-promotion
from day one: the cron ships DISABLED
(`PDP_SCOPE_BACKFILL_ENABLED` defaults false — a stated deviation from house
style), because the first enabled tick is serving-visible.

Measured 2026-08-04: the first enabled tick promotes **350** rows (349
real-merchant, 1 bucket), each gaining the +200 rank term in three backend
sites plus the Node ranker's +200 and market-filter exemption — a vetted rule,
an unmeasured batch impact.

**Operator enable sequence** (either order relative to P4 --apply is safe —
the cron never demotes and never touches suppressed rows):
1. dry-measure the 350-row cohort (top-N diff over the sampled query set, as
   for P4) if ranking impact matters to the release;
2. set `PDP_SCOPE_BACKFILL_ENABLED=true`;
3. watch the first tick's `pdp_scope_promotion` log line and the promoted
   count; the steady state per tick is near zero.

Original plan text follows.

#### (original) **[MEASURE FIRST — serving-visible]**
One-off pass over ALL canonical bucket rows (not just mirror-sourced — the 63
recovery-promoted rows re-evaluate too): demote rows failing the corrected rule
and not rule-1. ~3,182 rows each lose +200 and the Node market-filter
exemption. **Per D1: demote to `'unverified'`** — NOT `merchant_owned` — so the
rows stay inside the backfill's re-promotion gate. Before executing:
1. top-N diff over a sampled query set, BOTH rankers (backend pivot_query,
   Node canonicalCatalogSearch);
2. ship the D3 cron in the same PR set, so demoted rows have a live
   re-promotion path from day one.

### P5 — make the class unwritable (S3.3 applied here)
A cross-repo writers tripwire: scan both repos for any write of
`'multi_merchant_canonical'` as a literal outside the allow-list
(classifier-backed writers only). The Node side needs its own scan — the
existing tripwire globs `*.py` and is structurally blind to `.cjs`.

## Open decisions

**D1 — the attachment ratchet: DECIDED 2026-08-04. Keep the matcher gate
reading the stored label; the ratchet is dissolved in P4's spec and D3, not in
the matcher.**

The gate is load-bearing identity-safety, not an accident — its own docstring:
the 0.7-trigram matcher must never glue a cross-merchant seed onto a
single-merchant PDP "just because trigrams of 'brush' titles overlap" (the
MOYU-pollution defense). And post P1–P3 the label IS the classifier's verdict,
maintained only by classifier-backed writers — so option (a), gating on
computed qualification, is equivalent to gating on the label except during
promotion LAG. Rewriting a hot heuristic path to close a lag is the wrong tool.

The REAL one-way door was hiding in P4's spec, not the matcher: every
promotion writer (backfill, onboard) is gated `WHERE pdp_scope='unverified'`.
Demoting the 3,182 to `merchant_owned` would park them OUTSIDE every promotion
path permanently. Hence:

1. **P4 demotes to `'unverified'`, not `'merchant_owned'`.** Read-side
   equivalent (migration 070: unverified is "treated as merchant_owned for
   matcher safety") and semantically honest — the classifier has not AFFIRMED
   anything about these rows, whereas `merchant_owned` is documented as an
   affirmed state (`brand_verified_graduation`: "resolved, NOT canonical").
   Demoted rows stay inside the backfill's re-promotion gate.
2. **D3 is therefore CRON, by consequence.** With the gate label-based,
   promotion lag is the only residual ratchet. A scheduled backfill run closes
   the loop: a row gains its second seller through any of the three non-matcher
   routes — a foreign-merchant offer, a product-group peer, an ext-cluster
   spanning >= 2 domains — the next run promotes it, and the matcher opens for
   it. Cadence detail (nightly vs post-sync) is an implementation choice for
   the P4/D3 PR.

Net: no permanent freeze anywhere, no matcher change, one line of P4's spec
carries the whole decision.

**D2 — Path C keeps stamping.** `catalog_enrichment_agent` writes canonical at
ingest with `category_label_source='enrichment_agent_v1'` — consistent with
classifier rule 1, so it is NOT a violation of the invariant in substance, but
it is in form (a lane asserting the literal). Either exempt it in P5's
allow-list with the rule-1 justification, or route it through the classifier
too. Low urgency; it agrees with the rule today.

**D3 — cadence for P3** — RESOLVED (see the D3 section under P4): a
promote-only, predicate-driven cron, disabled by default; the CLI backfill
stays manual and excludes bucket rows.

## Sequencing

```
P1 (Node, both scripts)  →  P2 (#1667)  →  P3 (backfill CASE + cadence)
                                              ↓
                         D1 decided → P4 [MEASURE, then demote 3,182]
                                              ↓
                                       P5 (cross-repo tripwire)
```

P1 before P2 so the origination stops before the promotion rules tighten;
P4 last because it is the only serving-visible data change and everything
before it narrows what P4 must touch.
