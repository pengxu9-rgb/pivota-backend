# ADR-012 — Catalog Convergence Architecture: pokes for speed, sweeps for truth

Status: **accepted** (founder, 2026-07-24)
Owners: catalog/serving pipeline (pivota-backend + PIVOTA-Agent)
Related: ADR-001 (canonical record), ADR-010 (canonical product identity),
ADR-011 (intake identity contract / writer chokepoints),
`docs/CATALOG_ROW_TRUST_CONTRACT.md` (PIVOTA-Agent repo)

## Context — why small patches keep leaking

The catalog is a chain of derived stores:

```
seed_data → catalog_products/skus/offers → product_quality_snapshot
  → index_pipeline_state → catalog_row_trust → agent_pdp_view / serving docs / sitemap
```

Correctness today depends on **imperative pokes**: whoever writes at hop N must
remember to trigger the recompute at hop N+1 (`recompute_serving_eligibility`,
`upsert_catalog_row_trust`, view refresh, IndexNow, …). Every poke is
fire-and-forget by contract ("never break ingest"), while downstream readers
gate **fail-closed** on the derived state. A missed or failed poke therefore
converts silently into a wrong serving decision — a hidden product, or worse, a
stale-served one.

The 2026-06/07 defect record is one class wearing many faces:

| Defect | Hop that silently diverged |
|---|---|
| #1570 sections never reached scoring payload | seed_data → quality |
| #1571 rescore flipped eligibility, never trust | IPS → trust |
| trust cron `interval` never fired (restart starvation) | reconciler itself dead |
| #1578 bulk trust upsert truncated at 5,000 keys | reconciler silently partial |
| #1574 one timeout poisoned the run; `ok` counted "didn't raise" | outcome counters lie |
| fabricated seed_id → view refresh silent no-op | catalog → agent_pdp_view |
| manual "PBA delta gap-fill after each backfill" runbook | agent_pdp_view has NO reconciler |

With ~5 ingest paths (Shopify/Wix sync, external-seed mirror, crawl onboard,
minted canonicals, url_audit) × ~5 hops, there are dozens of poke edges. Each
patch fixes one edge; the class survives. Meanwhile the completeness-style
quality score never catches the defects that matter (INR-served-as-USD,
inventory=999, shell PDPs, quarantined store still serving) — those were all
found by manual sweeps.

Evidence the alternative works: the one hop with a true reconciler —
`catalog_row_trust_backfill` after #1578 — converged the full 14.1k-row table
in two passes, closed a 1,349-row coverage hole, and surfaced ~1,700 rows that
had been serving on stale verdicts, all without a human in the loop.

## Decision

1. **Every derived store MUST have a convergent reconciler**: a periodic job
   that recomputes the store from upstream truth — full-coverage over time,
   stalest-first, chunked (no silent caps — the #1578 pattern is the
   template), with outcome counters that count **writes that landed**, never
   attempts.
2. **Every derived store MUST have a drift metric and an alarm**: a cheap
   "rows where derived state disagrees with (or is missing vs) upstream truth"
   count, exposed on a health endpoint and ERROR-logged above a threshold
   (prod filters INFO). `catalog_rows_without_trust` +
   `CATALOG_TRUST_DRIFT_ALERT_THRESHOLD` is the template.
3. **Event pokes are latency optimizers only.** They stay, but no correctness
   argument may depend on a poke firing. "Pokes for speed, sweeps for truth."
4. **Correctness is measured directly, not proxied by completeness.** A
   recurring invariant sweep checks the serving surface for contradictions
   (public ⇒ renderable, public ⇒ not tombstoned/quarantined, offer currency
   sane for market, view freshness), independent of the quality score.
5. **Quality metrics are reported per cohort**, never as one aggregate:
   retired-by-design rows (tombstoned / quarantined / judged rejects) are a
   separate denominator from the serving corpus. The 24%-public headline mixes
   dead-on-purpose rows with real gaps and is unactionable.
6. **Writers converge on chokepoints** (extends ADR-011): ingest paths call the
   shared write seam; per-path key-shape special cases in downstream joins
   (e.g. the trust upserter's per-source CTEs) are debt to be collapsed, not a
   pattern to extend.

Explicitly **out of scope** (rejected as part of this decision):
- Re-keying `product_key` / `store_id` / public `sig_*` URLs (Layer A1) — zero
  live multi-store collisions; bundling it would turn weeks into a quarter.
- Rewriting the identity graph or the data model — ADR-001/010/011 layers have
  survived their audits; the stores are fine, the arrows between them are not.
- New infrastructure (queues, event bus, CDC). A 14k-row catalog needs
  reconcilers and counters, not Kafka.
- Big-bang rewrite. Prod is live and mid-wedge; each phase below ships as
  independent, individually-safe slices.

## Reconciler + drift-metric inventory (the contract)

| Derived store | Upstream truth | Reconciler today | Drift metric today | Required action |
|---|---|---|---|---|
| `product_quality_snapshot` | seed_data / catalog fields | rescore backfills (manual, scripts) | none | scheduled reconciler + "stale payload-version" metric |
| `index_pipeline_state` | quality + offers + APV + identity | `nightly_index_health` (04:00 UTC, full-coverage) ✅ | none | add drift metric (rows whose inputs changed after last IPS write) |
| `catalog_row_trust` | IPS + identity + lifecycle | 6h cron, fixed by #1578 ✅ | `catalog_rows_without_trust` + threshold alarm ✅ | template — done |
| `agent_pdp_view` | catalog_products/offers/seeds | **none** (event refresh + manual PBA gap-fill runbook) | none | build reconciler; retire the manual runbook |
| serving docs / search index (PIVOTA-Agent) | trust + catalog | backfill script (manual) | none | scheduled reconciler + doc-vs-trust drift count |
| sitemap / canonical feed | IPS + identity (`renderable`) | regenerated on read ✅ | dead-PDP count (post-#1575) | keep |

## Phases

**Phase 0 — measurement safety net (no behavior change).**
- 0a: per-cohort catalog health endpoint: partition every catalog row into
  `retired` (tombstoned/quarantined/inactive-source), `quality_blocked`,
  `identity_gated`, `lifecycle_blocked`, `public`, `shadow` from
  `catalog_row_trust` reason codes; report counts overall and per
  `source_system`. Acceptance: one GET returns the funnel; the "serving %"
  for the wedge corpus is readable directly.
- 0b: internal-consistency invariant sweep (daily job + on-demand endpoint):
  public ⇒ renderable identity row; public ⇒ suppression_reason IS NULL and
  no active quarantine match; public ⇒ has a live offer with price > 0;
  trust rows orphaned from catalog rows; catalog rows missing trust
  (existing). Violations ERROR-log with counts + sample keys. Acceptance:
  seeded contradiction is caught within one run.
- 0c (follow-up): point the merchant-audit prober inward for external-truth
  checks (currency vs storefront, availability truth) — the AEO-for-ourselves
  shape; separate epic, needs probe budget.

**Phase 1 — convergence everywhere.** Extract the #1578 reconciler shape
(stalest-first, chunked, outcome-counted, drift-alarmed) into one helper;
apply to `agent_pdp_view` (highest value — retires manual ops), then quality
snapshots, then serving docs (Agent repo). Acceptance per store: kill its
event pokes in a staging test and the store still converges within one cycle;
drift metric visible on a health endpoint.

**Phase 2 — one write seam per repo.** All ingest paths route
`catalog_products`/offer writes through the ADR-011 chokepoints; downstream
per-path key-shape CTEs collapse. CI tripwire extended to pin the writer set.

**Phase 3 — one policy owner (last, optional).** Retire the Python/Node
byte-aligned trust twin: Node-side producers call a backend internal endpoint
(or enqueue a backend job) instead of deriving policy locally. Parity tests
make deferral safe; do not start until Phases 0–1 have soaked.

## Consequences

- Worst-case staleness becomes bounded-and-known per store (reconciler cadence)
  instead of unbounded-and-invisible; fail-closed reads then over-hide for at
  most one cycle rather than forever.
- Reconcilers add periodic read load; all sweeps are chunked and
  drift-proportional on write (conditional upserts). At 14k rows this is noise;
  revisit cadences at 10× scale.
- Truth-revealing fixes will keep making aggregate numbers *drop* before they
  rise. Per-cohort reporting (Phase 0a) is what makes that legible instead of
  alarming.
- The manual runbooks (PBA delta gap-fill, ad-hoc rescore-then-trust flushes)
  are retired as each reconciler lands — their existence is the checklist.
