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

### P3 — the backfill becomes the promotion path — THIS PR
Done as `own_merchant_seller_term_sql` in `services/pdp_scope_classifier` —
ONE spelling, THREE writers: the recovery predicate, `backfill_pdp_scope.py`
(the live defect: bucket + one seed = "2 sellers"), and
`onboard_external_brand_from_crawl.py` (same bare `1 +`, guarded only by a
docstring claim). An AST test pins all three call sites; the bucket literal is
pinned by test to `seller_identity.BANNED_BUCKET_MERCHANT_ID`.

Known, deliberate divergence: the backfill matches seeds attached by BOTH the
product_key and the pipe-composite form; the recovery predicate matches only
product_key. Pre-existing, pinned by test so it cannot silently vanish, and
unifying it needs its own measurement.

Cadence (D3) still undecided: the backfill remains manual. Until a cron runs
it, a new genuinely-multi-seller mirror row stays `unverified`.

### P4 — demotion backfill **[MEASURE FIRST — serving-visible]**
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

**D3 — cadence: RESOLVED TO CRON by D1** (see above). The backfill must run on
a schedule or demoted/unverified rows have no timely promotion path. Nightly vs
post-sync is chosen in the P4/D3 implementation PR.

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
