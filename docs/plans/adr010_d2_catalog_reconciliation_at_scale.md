# Catalog reconciliation at scale — the D-2 phase plan (2026-07-10)

Founder directive (2026-07-10, after the step-5 lane cuts): stop fixing the
product catalog one-by-one; build the machinery that does it at scale. This
plan is the execution path for **ADR-010's committed-but-unbuilt Option D
increment** (action items 3–6), sequenced against what step-5 already built
and learned. Companions: `docs/adr/ADR-010-canonical-product-identity.md`,
`docs/plans/adr011_step5_catalog_identity_reconciliation.md` (the completed
discovery phase), `reports/step5/lane4_review_2026-07-10.*` (the gold-label
seed).

## 0. Where step-5 left the catalog, and why scale is now the right call

Step-5 (2026-07-10) took the backlog from **1,076 → 280** same-merchant dup
keys and **374 → 10** cross-merchant, via four hand-cut, human-reviewed lanes.
That was the correct *discovery* mode: it established that catalog noise is
**systematic** — same-PDP re-seeding, campaign-slug clones, querystring/UTM
noise, regional storefronts, brand+retailer multi-seller overlap, demo waves
— and it hardened the apply mechanics the hard way. What remains, and what
will keep arriving, should be eaten by standing machinery, not by lane
scripts:

- 41 bounded REVIEW groups (ambiguous clones/shades) + whatever new crawls add;
- 115 no-URL families + the wix null-brand keyless rows, unblocking on the
  92sfrj/efbc test-rig retirement;
- 54 multi-seller observations — not duplicates, but the resolver's future
  cross-merchant input;
- the monthly D-1 gauge as the standing scoreboard.

## 1. Invariants (unchanged, now load-bearing at scale)

1. **`content_key` is never re-minted or dropped** for an existing row; all
   reconciliation is row suppression (reversible tombstone) and
   pg-membership/canonical-card selection.
2. **Propose → review → apply**, with review evidence persisted. Auto-apply
   is allowed ONLY for lanes proven mechanical in step-5, under the full
   guard set (§3).
3. **Auto-MERGE above a confidence threshold stays gated** behind ADR-010
   action item 6's co-gate (eval set + convergence pivot). This plan builds
   the machinery and the eval set; it does not flip that switch.
4. Money never keys on pg/content_key (safe by construction); the blast
   radius is serving + publish, which is where guards concentrate.
5. Mis-merge is worse than fragmentation. Sparing an ambiguous group is
   always acceptable; silently collapsing distinct products never is.

## 2. Phase A — the D-2 resolver spine (the build)

The schema + engine that turns lane scripts into strategy plugins.

**A1. Schema (migrations + schema_guard self-heal, Railway skips
`db/migrations/`):**
- `identity_resolution_proposals` — `{proposal_id, kind
  (suppress_dup | flip_canonical | attach_membership | unmerge), subject rows,
  keeper/target, strategy, confidence, evidence jsonb, status
  (proposed | approved | applied | rejected | reverted), run_id,
  resolver_version, created/decided/applied timestamps, decided_by}`.
  Competing proposals per subject are allowed (the current
  `product_group_members` PK forbids them — proposals live in their own
  table precisely for this).
- Provenance columns on `product_group_members`:
  `{match_tier, confidence, evidence, resolver_version, resolved_at}`
  (ADR-010's exact list).
- `identity_resolution_events` — append-only audit of every apply/revert,
  superset of what step-5 stored in `suppression_metadata`.
- Unmerge: `reverted` status + the inverse-apply path (step-5's
  revert-by-run_id, made first-class).
- Move `pdp_review_tasks` into migrations (ADR-010 D-2 footnote).

**A2. Generic apply engine** — extracted from the step-5 lane scripts
(they already share SUPPRESS/DEACTIVATE/post-check SQL): takes approved
proposals, applies in a transaction, writes events, runs the full guard set
(§3), exits non-zero on any post-check failure. One engine, N strategies.

**A3. Strategy plugins v1** (each emits proposals, never applies):
- `same_url_dup` (step-5 lane 2, serving-aligned keeper) — auto-approvable;
- `campaign_clone` (lane 3, slug evidence) — review-approvable;
- `seed_first_party_twin` (migration-139 predicate, audit-sibling exclusion);
- `junk_url` (redirect/tracking canonical_urls — the JBL class);
- `multi_seller_observation` (labels only — feeds the future buy-box, no
  suppression).

Deliverable gate: replaying step-5's four cuts through the engine on a
staging snapshot reproduces today's prod state byte-for-byte (same rows
suppressed, same keepers).

## 3. The guard set (step-5 lessons, now hard requirements)

Every strategy/apply inherits these; a new strategy cannot ship without them:
- **Bidirectional seed linkage** everywhere a seed is read or deactivated
  (`source_ref` OR `attached_product_key` — the 482-false-orphan lesson).
- **Keeper-backing guard**: seed deactivation excludes keeper-linked seeds
  in-statement; post-apply asserts no keeper lost active-seed backing
  (the 411-orphaned-keepers incident).
- **Serving-aligned keeper by default** (`pick_canonical`) — suppression must
  not change what serves unless the proposal explicitly says so
  (`flip_canonical` kind).
- **Drift fingerprints**: apply only what was reviewed; member-set changes
  since review skip the group.
- **Exact-scope abort guards** on targeted cuts; post-apply checks for
  empty groups; reversible tombstones with run_id, never hard deletes.
- **Working-set hygiene**: suppressed rows, deactivated-seed orphans, demo/
  test-track rows excluded before any classification (and demo/test stores
  get a durable `catalog_track` label instead of ad-hoc domain matching).

## 4. Phase B — the standing reconcile sweep (the cadence)

A scheduled job (weekly; APScheduler tick or workflow_dispatch + cron GH
workflow, same env-flag discipline as the mirror):
1. Run the working-set classifier (exists) + D-1 gauge (exists); persist both
   as the run's report.
2. Feed each lane's groups to its strategy plugin → proposals.
3. **Auto-approve + apply only `same_url_dup` and `junk_url`** (the proven
   mechanical lanes) under the §3 guards.
4. Everything else lands as review batches in `pdp_review_tasks`
   (module='identity'), with the slug/evidence payloads the lane-3 worksheet
   carried.
5. Emit deltas; **alert if any gauge rises** week-over-week (intake contract
   regression signal — the ADR-011 flags should make rises impossible).

Success bar: new catalog noise is cleaned or queued within one sweep cycle
(< 1 week) instead of accumulating; D-1 falls monotonically toward the
labeled-KEEP floor (~124 groups today, mostly multi-seller/regional).

## 5. Phase C — Tier-3 batch adjudication (the ambiguous tail)

ADR-010 deferred embeddings/LLM "until scale demands them"; the founder
directive is that signal. Scope stays adjudication-only (propose, never
apply):
- Batch LLM judge over review-lane groups (the `pdp_matcher/llm_match.py`
  pattern: structured evidence in, verdict + confidence out): same-product
  clone vs distinct product vs variant listing.
- **Eval gate before it approves anything**: today's 170-group adjudicated
  worksheet + accumulated `pdp_review_tasks` outcomes are the gold labels.
  Required precision on the KEEP/SUPPRESS boundary ≥ the human baseline on a
  held-out slice; measured per resolver_version, recorded in the proposals.
- Human spot-check sampling (e.g. 10% of LLM-approved) forever — the judge
  earns auto-approval per-strategy, never globally.
- Only after C is stable does ADR-010 action item 6 (threshold auto-merge +
  cross-merchant canonical cards at variant grain) come up for its own
  co-gated decision — explicitly OUT of this plan.

## 6. Sequencing & effort

| Order | Item | Size | Depends on |
|---|---|---|---|
| A1 | D-2 schema migrations + schema_guard | S | — |
| A2 | Generic apply engine (refactor lane scripts) | M | A1 |
| A3 | Strategy plugins v1 + staging replay gate | M | A2 |
| B | Standing weekly sweep + alerting | S–M | A3 |
| C | Tier-3 judge + eval gate | M | B (labels accruing) |
| — | efbc rig retirement cleanup (115 families + wix) | S | founder timing |

A1+A2 are the first PR-able unit. Each phase lands dark/propose-only first,
exactly like the ADR-011 door flags did.

## 7. Success metrics

- D-1 monthly: same-merchant dup keys → labeled-KEEP floor; cross-merchant →
  demo-only; **no unexplained rise, ever** (alert wired in Phase B).
- 100% of mutations carry proposal_id + run_id (auditable, revertible);
  unmerge SLO: any mis-merge reversible within one working day.
- Review queue: bounded and burning down; LLM judge precision vs gold labels
  reported per version.
- Zero serving regressions: keeper-backing and serving-aligned-keeper checks
  green on every sweep.
