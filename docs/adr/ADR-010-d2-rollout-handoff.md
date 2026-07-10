# ADR-010 D-2 / Step-5 — Rollout Handoff (2026-07-10)

Status snapshot for review / fresh-session pickup. Bookend to
`docs/adr/ADR-011-rollout-handoff.md` (written this morning): that doc ended
with "new fragmentation is now stopped at every door; the catalog fix is
deliberately not started." This doc records the day that followed: the
catalog fix was executed AND turned into a standing machine. Companions:
`docs/plans/adr011_step5_catalog_identity_reconciliation.md` (step-5 plan),
`docs/plans/adr010_d2_catalog_reconciliation_at_scale.md` (the scale plan).

## 1. What shipped (17 merged PRs, one day)

**Step-5 reconciliation (the discovery phase, hand-cut + human-reviewed):**

| PR | What |
|---|---|
| #1283 | ADR-011 handoff doc + mirror flag in the GH workflow env |
| #1287 | Step-5 plan: 4 suppression lanes, invariants, no D-2 build |
| #1289 / #1291 | Lane-0 working-set classifier; orphan sweep + the **bidirectional seed-linkage fix** |
| #1292 / #1294 | Lane 2 same-URL keep-one (600 groups applied) + the **keeper-seed guard** |
| #1298 / #1300 | Lane 3 campaign clones (reviewer-editable proposals; 81 applied, 50 spared) |
| #1302 | Lane 1: 92sfrj duplicate store connection collapsed (bbd side) |
| #1304 / #1307 | Lane-4 review worksheet (170 verdicts = proto gold labels) + ownist twin cut |

**D-2 scale machine (founder directive: stop one-by-one, go bigger):**

| PR | What |
|---|---|
| #1311 | The phase plan (A: spine, B: sweep, C: judge) |
| #1318 | A1 schema (mig 179: proposals/events/pg-provenance) + A2 generic apply engine |
| #1319 | A3 strategy plugins (propose-only) — replay gate: 0 re-proposals vs cleaned prod |
| #1322 | Phase B weekly sweep (allowlist auto-apply, review batches, rise alert) |
| #1325 | Phase C Tier-3 judge + eval gate (PASSED: 0 mis-merges / 252 gold groups) |
| #1327 / #1329 | Judge wired into the sweep (proposal rights only) + the **clean-keeper fix** |
| #1332 / #1334 | efbc test-rig retirement; demo-store exclusion predicate for the gauges |

## 2. Prod state — LIVE and autonomous

- **Gauges (production-only, demo excluded): same-merchant dup keys 138,
  cross-merchant 1** (the anuko audit twin, a deliberate KEEP). Journey
  today: **1,076 / 374 → 138 / 1**. Orphan mirrors: 0.
- **Flags on Railway `web`:** the five ADR-011 door flags,
  `ENABLE_IDENTITY_RECONCILE_SWEEP=1`, `ENABLE_TIER3_JUDGE=1` — all `1`.
- **Weekly sweep**: Mon 04:30 UTC (`identity_reconcile_sweep` in
  audit_scheduler, prod-worker-gated + flag-gated). Flow per tick: gauges →
  classify → propose (dedupe on proposal_key) → judge the ambiguous tail
  (bounded, proposal-rights-only) → auto-approve+apply ONLY
  `AUTO_APPROVE_STRATEGIES = (same_url_dup, junk_url)` (test-pinned code
  constant) → review batches into `pdp_review_tasks` (module `identity`,
  deterministic ids) → **ERROR alert if any gauge rose** week-over-week →
  sweep event recorded (next week's baseline).
- **Proposal ledger (`identity_resolution_proposals`) is CLOSED**: 28
  tier3_judge applied / 2 rejected-as-gold-labels / 0 pending; 164 label_only
  (multi-seller + ambiguity annotations). Every apply carries
  `{proposal_id, run_id}`; `revert_run(run_id)` restores rows AND exactly the
  seeds that run deactivated.
- **Judge**: tier3.v2, eval-gated (252-group gold fixture,
  `reports/step5/tier3_eval_fixture_2026-07-10*.json`): 0 confident
  mis-merges, 100% collapse coverage, 98.3% keep coverage. It only ever
  emits proposals; `tier3_judge` is NOT in the auto-approve allowlist;
  ~10% deterministic `spot_check` pre-marking exists for the eventual
  per-strategy earn-in.

## 3. What remains in the catalog, and why it stays

| Population | Size | Why it stays |
|---|---|---|
| Multi-seller observations (brand+retailer: theordinary+ulta, apple+bestbuy…) | ~54 keys | CORRECT family-key sharing; the future ADR-010 resolver / buy-box inputs (labeled `multi_seller_observation`) |
| Regional storefronts (arencia.jp/.us) | ~60 keys | Same product, separate storefront domains — keep |
| Title collisions / size variants | small | Distinct products behind one normalized brand+title; GTIN discriminates as barcoded intake accrues |
| Demo stores (pivota-review-demo pair) | 9 keys | The LIVE Shopify app-review rig — excluded from gauges via `DEMO_EXCLUSION_SQL`, serving untouched |
| anuko audit twin | 1 key | url_audit rows never win serving; deliberate KEEP |

## 4. Incidents caught by the machinery's own checks (all repaired same-day)

1. **482 false orphans** — one-directional seed join; enrichment-door rows
   link via `attached_product_key`, not `source_ref`. Dry-run caught it.
2. **411 orphaned keepers** (lane-2 apply) — seed↔row linkage is many-to-many;
   deactivating a loser's seed can strip the keeper's backing. Repaired in
   ~15 min; now an in-statement guard + failing post-check.
3. **2 live mis-merges** (Merit SPF-45/50) — the `-N` clone-counter strip ate
   SPF numbers. Caught by the tier3.v1 eval; rows restored; `_UNIT_NUMBER`
   guard (unit-prefixed numbers are product identity).
4. **8 junk keepers** — `pick_canonical` chose signed `-copy` pages as
   keepers. Caught by the human review; clean-keeper preference shipped.
5. **Rise alert true-positive** on its first live opportunity (the deliberate
   SPF unmerge, +2).

Standing guard set (every strategy/apply inherits; tests pin each):
bidirectional seed linkage, keeper-backing exclusion, serving-aligned
clean-slug keepers, drift fingerprints, exact-scope aborts, empty-group +
orphaned-keeper post-checks, reversible run-id tombstones, never hard-delete.

## 5. Decisions of record

- **92sfrj-bi.myshopify.com is test data** (founder). The bbd side collapsed
  in Lane 1; the efbc side — which recon revealed to be the live checkout/ACP
  canary (549 test orders) — was deliberately kept, then **fully retired**
  later the same day (run `20260710T112048Z`): 3 connections →
  `retired_test_rig`, 763 rows tombstoned, orders untouched. The wix
  keyless-MINT source died with it.
- **`catalog_track` is NOT the demo label** — it is a serving-classification
  enum (`internal_merchant` drives first-party offer typing) and the demo
  pair must keep serving for Shopify app review. Demo-ness =
  `DEMO_EXCLUSION_SQL` in `scripts/step5_working_set.py`, one place.
- **Judge disagreements with gold labels get re-adjudicated with recorded
  rationale** — 3 worksheet-heuristic label errors were corrected that way;
  2 judge errors (mediheal refill vs bundle; arencia 404 pair) were rejected
  and stand as `keep_separate` gold labels.

## 6. How to operate / verify

- **Manual sweep** (propose-only default):
  `railway run bash -c 'DATABASE_URL="$DATABASE_PUBLIC_URL" ENABLE_TIER3_JUDGE=1 PYTHONPATH="$PWD" python3.11 scripts/run_identity_reconcile_sweep.py'`
  (`--apply` adds the mechanical allowlist).
- **Apply approved judge proposals** (they are NEVER auto-applied):
  `services.identity_resolution.apply_approved(conn, strategies=("tier3_judge",))`.
- **Revert any engine run**: `revert_run(conn, run_id)`; ad-hoc step-5 cuts
  revert by `suppression_metadata->>'run_id'` (recipes in each script's
  docstring).
- **Watch**: `identity_resolution_events` (action='sweep' weekly; 'applied'/
  'reverted' per run); `pdp_review_tasks` module='identity' (the human
  queue); the two gauges (they must only fall).
- Ops notes: the public proxy flakes — single asyncpg conn + retry loop,
  never pooled `database.connect()` from local; **this checkout is shared
  with concurrent sessions that switch branches mid-command — do all work in
  a dedicated worktree and verify `git rev-parse --abbrev-ref HEAD` after
  every commit**; `pdp_review_tasks.qa_sample` is NOT NULL with an ORM-only
  default (raw INSERTs must supply it).

## 7. Open follow-ups (none urgent)

1. **Monday's sweep** (first fully autonomous run) — expect labels only,
   gauges ≤ 138/1, no alert. Worth a glance at the sweep event.
2. **August `measure_identity_duplication.py`** — the ADR-011 monthly gauge.
   Note it does NOT exclude demo rows; the sweep gauges are now the
   authoritative scoreboard.
3. **Dead-URL cleanup class** — the arencia pair 404s (rejected proposal
   documents it). A natural future sweep strategy; likely overlaps the
   source-lifecycle/freshness machinery.
4. **Judge earn-in** — after a few weeks of review outcomes: refresh the
   eval fixture with accrued gold labels, and if precision holds, consider
   per-strategy auto-approval with the standing 10% spot-check.
5. **ADR-010 item 6** (threshold auto-merge, cross-merchant canonical cards
   at variant grain) — still explicitly out of scope, own co-gate.
6. GTIN accrual: catalog remains ~GTIN-less; the attribute populates as
   barcoded intake arrives (step-4 backfill stays a near no-op).

## 8. Key files

`services/identity_resolution.py` (engine) ·
`services/identity_resolution_strategies.py` (plugins) ·
`services/identity_reconcile_sweep.py` (weekly tick + judge wiring) ·
`services/identity_tier3_judge.py` (judge + eval gate) ·
`scripts/step5_working_set.py` (classifier, shared SQL, demo predicate) ·
`scripts/run_identity_reconcile_sweep.py` / `run_identity_tier3_eval.py` ·
`db/migrations/179_identity_resolution_d2.sql` (+ schema_guard mirror) ·
`reports/step5/*` (proposals-as-applied, gold labels, eval reports) ·
tests: `tests/services/test_identity_*.py`, `test_step5_*.py` (~200 tests).

## TL;DR

This morning the catalog had 1,076/374 duplicate keys and a plan. Tonight it
has **138/1** — the residue all labeled and deliberate — and the fixing is no
longer a project but a machine: intake contract at five doors, a weekly sweep
that auto-applies only what step-5 proved mechanical, an eval-gated LLM judge
with proposal rights only, a human review rail accruing gold labels, and an
alert that fires if duplication ever rises. Five defects were caught by the
machinery's own guards before they could compound. Everything is reversible
by run id. First autonomous run: Monday 04:30 UTC.
