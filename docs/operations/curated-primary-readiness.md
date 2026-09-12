# Curated ingestion serving handoff

The curated CLI and onboarding queue select `apply_ingest_plan(primary_readiness=True)`.
After complete product/SKU/offer/seed persistence, the same door produces the
derived artifacts that the primary search route actually reads. Both row and
batch executors share this handoff. Other ingest doors retain their existing
behavior unless they explicitly select it.

The handoff is bounded to the plan's exact persisted product keys and attached
seed IDs. It reads the actual database merchant/platform/source-product identity
after seller adoption and refuses missing identity or seed attachment. It runs:

1. `full_quality_eval` with the existing source-backed deterministic scoring
   policy, actual source fields, and only observed canonical INCI/detail sections.
2. `refresh_agent_pdp_view_for_content_key`, after every selected listing has a
   quality snapshot, once per content key. This preserves normal evidence and
   enrichment overlays; it does not assemble a stripped replacement view.
3. `recompute_serving_eligibility(strict=True)` with the same classifier as nightly
   index health, followed by authoritative index-state readback.
4. `upsert_catalog_row_trust`, followed by exact product trust readback.

The report records database identity, score/version, timestamps, index/serving
decisions and blocker codes. Snapshot/APV/index writes require fresh readbacks.
The trust writer intentionally skips unchanged policy: a successful evaluation
with identical before/after trust is reported as `unchanged_policy`, preserving
its earlier timestamp. Timestamp comparisons respect the database session's
wall clock for naive columns and absolute time for timezone-aware columns.

`status=complete` means these stages completed, not that every product is
servable. `serving_eligible_count` and `index_eligible_count` are IPS decisions;
`public_policy_count` additionally requires trust `public`. Low quality, retired
sources, missing content, or other policy blockers remain blocked. Stock and
currency remain original offer facts; caller stock/market filters still apply.
Nothing requires all offers to be in stock or invents stock for a sold-out item.

On an execution error, `PrimaryReadinessIncomplete` retains the stage report and
`persisted_counts`. Catalog writes may already exist; retry the same identities.
The queue must fail/retry this handoff rather than mark it done. There is no
fallback merchant, synthetic quality facet, forced eligibility or policy change.

Keep current identity, pricing-region, category-leaf, evidence/agent-decision,
trust and index-read flags. Source-backed scoring explicitly selects the existing
six-component policy; no environment flags or quality thresholds change.

Nightly index health (04:00 UTC), trust backfill (every six hours at :17), and APV
reconciliation (every six hours at :43) remain consistency safety nets. They
were not a complete substitute for this handoff: the curated writer previously
never scheduled its initial quality evaluation, and missing APV candidates in
the reconciler require existing public trust. The merchant-cache quality drain
does not enumerate these external curated rows.

Acceptance must query the exact new content keys in `index_pipeline_state`,
the live `pdp_will_render_expression` used by canonical routes, and actual primary
search/PDP results. The persisted `catalog_products.pdp_will_render` column is a
dormant reader feature and is not this handoff's gate. Do not turn on its reader
or another scheduled promotion job to make a canary appear to pass.

The scorer and core assembler/classifier/trust operations use the supplied DB.
The assembler's optional enrichment and seller-outcome overlays retain existing
module-global connections and best-effort semantics. PostgreSQL tests isolate
only those ancillary overlays; core scoring, APV assembly, classification and
trust run against production-shaped schemas without seeded readiness rows.
