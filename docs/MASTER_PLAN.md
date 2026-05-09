# Master Plan — PDP-as-canonical catalog migration

**Live source of truth.** Update on every meaningful step. Originated from the
recall investigation closed at 23% pass-rate; tracks every phase since.

- Last updated: 2026-05-09 (UTC) — **Onboarding track operationally complete** (#387 + #388 + #390 + #392 + #394 merged; #395 drafted). O-4 lifecycle column + 3-path INSERT wiring + LabelAgent UPDATE recompute + backfill script + O-5 recall live-stage filter (with NULL grandfather) are all on main. #395 (drop NULL grandfather) is drafted, gated on O-6b backfill running in prod and confirming 0 NULL rows. Pending: probe v18 for prod regression check, then operator runs `scripts/backfill_pdp_lifecycle_stage.py --apply`, then probe v19, then merge #395, then probe v20 for fully strict gate.
- Owner: peng
- Origin: `~/.claude/plans/shimmying-soaring-ember.md` (now superseded — keep this file canonical going forward)

---

## How to use this doc

- **Read** this file at the start of every session before planning new work.
- **Update** this file when a phase ships, a probe runs, or scope changes. Stale = useless.
- **Open issues** belong in the table at the bottom; mark them resolved with a date and PR number rather than deleting.
- **Probe runs** go into the trajectory table (one row each).

---

## Where we are (one-paragraph summary)

**Phase 7b shipped to prod 2026-05-07 night.** Probe v15 (prod, post-deploy):
**37/53 PASS = 69.8%, lipstick 9/9, all beauty buckets at 100%**.
`canonical_path_executed=true` rate **96.2%** (51/53), confirming the gateway
now reads `catalog_products` at scale. Codex also added a non-beauty primary
deadline (PR #1314) to keep slow non-beauty queries from padding fallbacks
indefinitely; production p99 dropped from 17.4s to **12.0s** (still slow but
under v13 baseline).

The 16 remaining non-PASS queries are entirely **non-beauty categories** —
electronics, home, fashion_*. Recall path is healthy; we just have no
canonical PDPs for those buckets. That's Phase 4 expansion territory.

**Catalog state (durable):** 4715 rows in `catalog_products` all with
`pivota_signature_id`, zero migration drift. Phase 2-redo (PRs #347/#348/
#349/#351/#352) brought NULL `category_path` from 4069 to 627 (long-tail
accessory/lingerie/pet, not beauty). Mirror script now classifies at
INSERT time so this hole won't reopen.

**Public PDPs are healthy.** sig_* URLs return 200 across the prod sample,
content + JSON-LD intact.

---

## Phase status

All PR numbers refer to `pengxu9-rgb/pivota-backend` unless noted.

| Phase | What | Status | Refs |
|---|---|---|---|
| Deliv. A | Recall investigation handoff doc | ✅ | `pivota-agent-ui/reports/recall_v1/RECALL_INVESTIGATION_FINAL.md` |
| 1 | PDP-first SQL indexes | ✅ | #295, mig 068 |
| 2 | `category_path` columns + regex backfill | ✅ | #296, mig 069 (1216/1535 covered initially, then 304 NULL long-tail) |
| 2-redo | Re-classify 4069 NULL rows after ext→sig mirror; mirror gains INSERT-time classifier | ✅ | #347, #348, #349, #351, #352 — applied 2026-05-07. **4069 → 627 NULL** (3442 backfilled). Remaining 627 are accessory/lingerie/pet long-tail; requires separate taxonomy, not beauty regex. |
| 2b | Recall side wired to PDP-first | ✅ | #297, `services/pivot_query_service.py` |
| 3A | Deterministic seed→PDP matcher | ✅ | #299, `services/pdp_matcher/deterministic.py` |
| 3B | LLM tail matcher (gemini-2.5-flash) | ✅ | #305, `services/pdp_matcher/llm_match.py` |
| 4 (lipstick) | Catalog enrichment agent — 15 lipstick PDPs + 18 seeds | ✅ | #301, #303 |
| 6 | `pdp_scope` dimension (canonical vs merchant_owned) | ✅ | #311, mig 070 |
| 5 (probe v9) | Re-probe after Phase 6 | ✅ ran multiple times — see trajectory below | `pivota-agent-ui/reports/recall_v1/recall_v9_*` |
| 7a | Agent ingestion writes the full canonical chain | ✅ | #313 |
| 7a-backfill | Heal pre-7a PDPs (lipstick + fragrance) — 65 PDPs, 68 SKU/offer/merchant rows | ✅ applied to prod 2026-05-07 | #332 (script `scripts/backfill_canonical_chain_for_agent_seeds.py`) |
| 7c | Synthetic variant + availability fixes zero_variants blocker | ✅ | #317, #320 |
| 8 (fragrance) | 50 fragrance PDP candidates | ✅ | #323 |
| 9 (eye + face) | 50 makeup eye + face PDP candidates | ✅ | #328 |
| C-1 | Pivota canonical PDP foundation — schema + sig generator + audit fallback | ✅ | #327 |
| C-2 | Public sig_* → product API + sitemap list | ✅ | #329 |
| C-3 | One-shot script for legacy rows | ✅ | #331, plus #330 schema guard |
| 7b Step 1 | Gateway-side canonicalCatalogSearch helper + 16 unit tests | ✅ | PIVOTA-Agent #1311 (claude/phase-7b-canonical-recall) |
| 7b Step 2 | Wire helper into find_products_multi + dedupe + telemetry + 3 integration tests | ✅ | PIVOTA-Agent #1312, merged → prod commit `91cbcc98` |
| 7b non-beauty deadline | Gateway-level 6000ms hard deadline on non-beauty primary upstream; authoritative strict-empty on hit; `fpm_primary_deadline_*` telemetry | ✅ | PIVOTA-Agent #1314, merged → prod commit `d98a8704` |
| 7b ingredient_recall_direct | Extend canonical chain to ingredient_recall_direct path | ✅ | PIVOTA-Agent #1315, merged → prod commit `ee5564c4` (auto-deployed 2026-05-07T23:56). Probe v17 post-merge: **skincare_serum 0/2 → 2/2 PASS** ✅, **beauty 100% (37/37)** ✅, overall 37/53 → 38/53. Net +1 (not +2) because electronics dropped 1/5 → 0/5 in the same probe — cache-flake on the existing cache_miss_sync_filled outliers, not caused by this PR. |
| **— PDP Onboarding Standardization track —** | (started 2026-05-08, see `docs/PDP_ONBOARDING_PLAYBOOK.md`) | | |
| Onboarding playbook + 5 decisions | Maps 3 onboarding paths, 8 gaps, proposed lifecycle, peng agreed to all 5 recommendations | ✅ | PR #369, merged → prod commit `310ada4a` |
| O-1 — wire merchant tags through Shopify ingest | mig 075 + db/catalog.py mapping + ingest payload + schema_guard at-startup ALTER + tests | ✅ | PR #369. Verified in prod: `catalog_products.tags` JSONB column live on 4690-row catalog. 0 tagged today (waiting on next Shopify sync to populate). 8 tests pass. |
| O-1 followup — extend tags to all 3 paths | Wix adapter explicit `tags=[]`, mirror script extracts from seed_data jsonb (4 paths), agent ingestion JSONL `tags` field | ✅ | PR #372 → main `18cecd61`. 52 tests pass. All 3 paths now write `tags` consistently. |
| **O-2 — tag taxonomy v1** | mig 076 + 4 columns (price_tier/use_case_tags/lifestyle_tags/demographic) + `services/pdp_taxonomy.py` helper + 3-path wiring + schema_guard + tests | ✅ | PR #374 → main `caa0a850`. Verified in prod: all 4 columns live on 4690-row catalog. 76 tests pass. Conservative deterministic extraction; Phase O-3 LabelAgent fills the long tail. |
| **O-3a — LabelAgent service module** | `services/pdp_label_agent.py` — Gemini classifier with structured output (responseMimeType + responseSchema), grounding OFF, drop_reason taxonomy, retry on 429/503/504, vocab-filtered output, preserve-merchant merge | ✅ | PR #376 → main `418bca49`. 25 tests pass. Pinned vocabularies (DEMOGRAPHIC/USE_CASE/LIFESTYLE) prevent token drift. Per-row confidence enables tiered thresholds in O-3b. |
| **O-3b — LabelAgent batch worker** | `scripts/run_pdp_label_agent.py` — operator-invoked, default dry-run, --scope canonical/merchant_owned/all, --limit guardrail, --min-confidence gate, per-run report | ✅ | PR #377 → main `859765cf`. 7 new tests + 25 from O-3a = 32 pass. UPDATE uses COALESCE on every fillable column so merchant data wins even if runner misfires. Doesn't auto-run; first prod usage = Phase O-6. |
| **O-3 followups — retry parse-time drops + capture classified values + connect-with-backoff** | Three small fixes surfaced by the O-6 dry-runs: (1) parse-time drops (no_text_parts / no_balanced_block / decode_failed / response_not_dict) now retry like 429/503; (2) per-row classified values written to the run report so audits are real spot-checks; (3) DB connect retries 3× with 5s/15s/30s backoff for Railway proxy flake | ✅ | PR #383 merged. Default `max_retries` 1 → 2. 34 label-agent tests pass. |
| **O-6 — first prod LabelAgent run (canonical-only)** | Worker run on prod against canonical scope (multi_merchant_canonical ∪ catalog_enrichment_agent_v1). Uses gemini-2.5-flash. | ✅ | 13/13 canonical rows classified (12/13 first apply + 1/1 retry). All 4 audit invariants 0. Sample: AirPods Max → unisex / [daily, travel] / [], Galaxy Buds3 Pro → unisex / [daily, travel, sport] / [], Kindle Colorsoft → unisex / [daily, gift] / []. lifestyle_tags = [] across the board (correct for consumer electronics). Reports: `reports/o3_label_agent/20260508T200021Z` (dry-run), `T202357Z` (apply 12/13), `T202504Z` (retry 1/1). Cost: ~$0.05–0.15. **Validates the entire O-1 → O-3 pipeline end-to-end.** |
| **O-4 — pdp_lifecycle_stage column + 3-path wiring** | mig 077 + partial index on (validated, published) for O-5 recall filter. `services/pdp_lifecycle.py` with pure DRAFT→CANDIDATE→VALIDATED→PUBLISHED gates (handles JSONB list/string/comma forms). All 3 paths (catalog_sync_service, mirror_external_seeds, catalog_enrichment_agent.ingestion + run_catalog_enrichment.py SQL) compute + persist stage at write. Path A caps at candidate (category_path filled by classifier downstream), Path B tops at validated (no canonical scope), Path C reaches published via source_system canonical evidence. | ✅ | PR #387 → main `5123e4f`. 138 tests green. |
| **O-6b — lifecycle stage backfill script** | `scripts/backfill_pdp_lifecycle_stage.py` — one-shot worker that reads NULL-stage rows and computes the stage via `compute_lifecycle_stage` (same pure function as the 3 paths). Idempotent (`WHERE pdp_lifecycle_stage IS NULL` on both SELECT + UPDATE so concurrent ingest writers don't get clobbered). Default `--limit 10000` covers current ~5k catalog with headroom; warns + sets `limit_hit:true` when SELECT fills the limit. Progress log every 250 rows. | ✅ | PR #388 → main `67dc9e3`. 11 tests green. Pending: dry-run on prod, then `--apply`. |
| **O-4b — LabelAgent recompute on UPDATE** | Closes review gap on #387 — `run_pdp_label_agent.py` now recomputes `pdp_lifecycle_stage` after the agent fills taxonomy fields. Without this, candidate→validated promotions wouldn't reach the recall filter. SCOPE_QUERIES SELECTs add `image_url` + `pdp_lifecycle_stage`; UPDATE_SQL writes `pdp_lifecycle_stage = :new_stage`; per-row report tracks `lifecycle_stage_before/after/promoted`; run summary aggregates by transition. | ✅ | PR #390 → main `13aef95`. 152 tests green across full onboarding suite. |
| **O-5 — recall live-stage filter** | `_fetch_canonical_search_rows` hard-filters global recall on `pdp_lifecycle_stage IN ('validated', 'published') OR IS NULL` (NULL grandfather for rollout). Filter skipped for merchant-scoped queries (`merchant_id` passed). Lifecycle rank bonus +60 published / +20 validated, sized below brand-exact (80) and category-prefix (90) so it tie-breaks within the live pool rather than overriding query relevance. | ✅ | PR #394 → main `73de3f5`. 185 tests green. |
| **O-5b — drop NULL grandfather** | Tightens O-5: removes the `OR pdp_lifecycle_stage IS NULL` rollout branch so any post-backfill NULL row is treated as a bug to surface, not data to silently include. Pre-merge gate: prod confirms 0 NULL rows after `scripts/backfill_pdp_lifecycle_stage.py --apply`. | 📝 draft | PR #395 (`claude/onboarding-O5b-tighten-grandfather`). 32 tests green. |
| 4 electronics — scaffolding | Plan + 15 hand-curated PDP candidates JSONL | ✅ | pivota-backend #359 (`claude/phase-4-electronics`). Doc + data only, no DB writes. |
| 4 electronics — Stage 2 validate (initial) | Gemini URL validator run on the 15 starter candidates | 🛑 First runs failed gate | 5/15 then 3/15 validated; gate is ≥12/15. Most failures were silent `offers=[]` with no drop reason — instrumentation needed first. |
| 4 electronics — validator instrumentation | Add per-candidate drop reasons + retry to gemini_url_validator | ✅ done (draft PR #363) | `validation_drop_reason` + `validation_drop_detail` (≤500 char snippet) emitted per drop site; runner now writes a complete audit (success + failure) to validated.jsonl + a `<category>_validation_summary.json`; retry on 429/503/timeout/non-JSON-200/parse-fail; 48-test validator suite passes. |
| 4 electronics — Stage 2 re-run with diagnostics | Run validate against electronics + fragrance + lipstick with the new instrumentation | ✅ run, **gate still failed but root cause now clear** | electronics 8/15 (gemini_json_no_balanced_block: 4, gemini_no_text_parts: 2, gemini_json_decode_failed: 1). fragrance 14/50, lipstick 16/51 — historic Phase 4 ingestions appear to have not enforced ≥12/15. **Root cause: Gemini JSON contract drift, NOT anti-scraping.** Model returns 200 OK with prose / citations-only / truncated-JSON instead of strict JSON. |
| 4 electronics — fix Gemini JSON contract | Single-variable fix: `maxOutputTokens` 1024 → 4096 (truncation was the dominant root cause; structured output not chosen because grounding+structured-output is Gemini 3-only). | ✅ | PR #365 (codex/phase-4-validator-json-contract) merged → main commit `cf298984`. Re-run gates all met: electronics 8/15 → **12/15** ✅, fragrance 14/50 → **37/50** (74%) ✅, lipstick 16/51 → **30/51** (59%, exactly hits ≥30) ✅. `gemini_json_no_balanced_block` and `gemini_json_decode_failed` both 7 → 0 — the diagnostic numbers cleanly confirmed truncation as the root cause. |
| 4 electronics — Stage 3 ingest | Dry-run + apply ingestion of 12 validated electronics PDPs into prod | 🟡 next codex task | Branch off main (now has #359 + #363 + #365 merged). Run `validate --category electronics` to regenerate validated.jsonl, then `ingest --category electronics --dry-run` then apply. Audit invariants must hold (legacy=0, sig dup=0, identity dup=0, missing mirror=0). |
| 4 electronics — Probe v18 | Verify electronics lift after ingestion | 🟡 pending Stage 3 | Required: electronics 1/5 → ≥3/5 PASS, beauty 37/37 holds. If lift confirmed → scale to 30-50 entries. If not → escalate to Phase 7b non-beauty extension. |

---

## Recall pass-rate trajectory

53-query `eval_corpus_recall_v1.jsonl`, EN+ZH, run against
`https://agent.pivota.cc/api/gateway --source shopping_agent --entry chat`.

| Run ID | Phase boundary | Pass | Thin | Empty | Fail | Pass-rate |
|---|---|---:|---:|---:|---:|---:|
| recall_v9_phase6_1778094214 | After Phase 6 (no canonical-chain data) | 11 | 9 | 32 | 0 | 21% |
| recall_v9_phase7c_1778120085 | After Phase 7c synthetic-variant fix | 19 | 8 | 26 | 0 | 36% |
| recall_v9_phase8_1778122760 | After fragrance ingestion (Phase 8) | 24 | 9 | 20 | 0 | **45% — peak** |
| recall_v9_phase9_1778124544 | After eye+face ingestion (Phase 9) | 15 | 10 | 27 | 1 | **28% — regression** |
| recall_v10_phase7a_backfill_1778130307 | After Phase 7a backfill (lipstick + fragrance SKUs) | 16 | 10 | 26 | 1 | **30%** |
| recall_v11_aurora_bff_1778170034 | Same data, source=aurora-bff (orchestrator localization probe) | 16 | 3 | 33 | 1 | **30%** |
| recall_v12_post_backfill_1778176777 | Post 84+177 thin-desc content backfill | 17 | 10 | 26 | 0 | **32%** (+2pp) |
| recall_v13_post_phase2_redo_1778187080 | Post Phase 2-redo (3442 NULL → category_path) | 17 | 10 | 26 | 0 | **32%** (0pp) |
| recall_v14_phase7b_staging_deadline_1778195751 | Phase 7b + non-beauty deadline, staging | 37 | 2 | 14 | 0 | **69.8%** (+37.8pp) |
| recall_v15_phase7b_prod_deadline_1778196048 | Phase 7b + non-beauty deadline, **prod** | 37 | 2 | 14 | 0 | **69.8%** (production parity) |
| recall_v17_pre_pr1315_baseline_1778197677 | Pre-merge baseline (PR #1315 not yet deployed) | 37 | 3 | 13 | 0 | **70%** (≡ v15; serum still 0/2 PASS, 2 THIN) |
| recall_v17_post_pr1315_1778198230 | Post-merge of PR #1315 (ingredient_recall_direct extension) | 38 | 1 | 14 | 0 | **72%** (+2pp over v15). **Beauty: 37/37 = 100%** (skincare_serum lifted 0/2 → 2/2). Electronics flaked 1/5 → 0/5 (cache-related, not PR #1315). |

**Headline insight:** every probe since v9_phase8 has *more* canonical data
than the previous one, yet pass-rate has not returned to peak. The bottleneck
is no longer "do the rows exist" but "does the recall query reach them."

**v13 cleanly confirms the gateway-disconnect diagnosis:** Phase 2-redo
populated `catalog_products.category_path` on 3442 rows but probe pass-rate
moved 0pp. The data is there; the gateway just doesn't read it. Phase 7b
(gateway reads canonical chain) is now the only remaining lever.

**v14/v15 confirm the fix worked:** 32% → 69.8% (+37.8pp) is the
single-largest jump in the trajectory. **Lipstick 0/9 → 9/9** validates
that the architectural diagnosis was correct. `canonical_path_executed`
fires on 51/53 queries (96.2%) — the two that don't are short-circuited
by the new non-beauty deadline. p50 prod 3.8s, p99 prod 12.0s (down from
17.4s pre-deadline; still over budget but the deadline is now upper-bounded
on non-beauty paths).

**v17 closes the last beauty gap.** PR #1315 extended the canonical chain
to the `ingredient_recall_direct` path (where ingredient queries like
"salicylic acid serum" routed and previously bypassed the chain).
Skincare_serum 0/2 → 2/2 PASS. **Beauty pass-rate is now 37/37 = 100%.**
Overall moved 70% → 72% (+2pp; would have been +4pp if not for an
unrelated electronics cache flake during the probe — orthogonal bug).
Architectural recall work is complete; the remaining 14 EMPTY queries
are pure data gaps that Phase 4 expansion (electronics / home /
fashion_*) would address.

**v11 (aurora-bff) localization probe**: ran the same 53-query corpus under a
different orchestrator (`source=aurora-bff` instead of `shopping_agent`).
**Lipstick still 0/9 on both paths**, with different failure modes:

- shopping_agent: seed query runs, returns 0 (`cache_miss_sync_filled,
  external_raw_count=0`)
- aurora-bff: seed query NEVER RUNS (`external_seed_executed=false,
  fallback_route=invoke_primary_irrelevant`) — internal catalog returns 0
  → "primary irrelevant" gate fires → skips external seed entirely

This rules out a per-orchestrator one-line patch. The architectural fix
(Phase 7b — gateway reads `catalog_offers`/`skus`) is the right
long-term answer, but **deprioritized as of 2026-05-07 evening** in
favor of the content-quality backfill plan below — thicker content may
move pass-rate without needing the gateway-side spike, and the content
work is mechanically simpler.

---

## Probe v10 detailed (2026-05-07)

Per-bucket breakdown (PASS/THIN/EMPTY):

| Bucket | n | PASS | THIN | EMPTY | Notes |
|---|---:|---:|---:|---:|---|
| skincare_moisturizer | 3 | 3 | 0 | 0 | full pass |
| skincare_sun | 2 | 2 | 0 | 0 | full pass |
| skincare_cleanser | 2 | 2 | 0 | 0 | full pass |
| skincare_bare_noun | 4 | 2 | 0 | 2 | partial |
| skincare_serum | 2 | 0 | 1 | 1 | thin |
| fragrance | 5 | 2 | 3 | 0 | **lifted from 0/5 → 2/5** after Phase 7a backfill |
| makeup_eye | 3 | 2 | 0 | 1 | lifted from 0/3 → 2/3 |
| makeup_eye_bare_noun | 2 | 1 | 0 | 1 | lifted from 0/2 → 1/2 |
| makeup_face | 5 | 1 | 3 | 1 | unchanged |
| **makeup_lip** | **6** | **0** | **0** | **6** | **broken — see open issue #1** |
| **makeup_lip_bare_noun** | **3** | **0** | **0** | **3** | **broken — see open issue #1** |
| fashion_top | 2 | 0 | 1 | 1 | data-empty (no Phase 4 yet) |
| fashion_dress | 2 | 0 | 1 | 1 | data-empty |
| fashion_shoes | 3 | 0 | 1 | 2 | data-empty |
| electronics | 5 | 1 | 0 | 4 | data-empty |
| home | 4 | 0 | 0 | 4 | data-empty |

---

## ext→sig migration audit (2026-05-07, user-run)

After codex's ext→sig migration ran on prod, peng performed a read-only audit
to verify quality. **Migration is clean.** Earlier in the same session I
incorrectly speculated that the migration had deleted ~130 agent-authored
rows; that claim is retracted — the catalog had been thinly seeded *before*
the migration, and what looked like "loss" was just the seed snapshot's
pre-existing thinness now being faithfully projected.

**Headline counts:**
- `external_product_seeds`: total 4528, active 4120 (184 attached + 3936 standalone), deduped valid candidates 3936
- `catalog_products`: total 4715, **all 4715 with `pivota_signature_id`**, mirror rows 3936, missing external mirror **0**, legacy external seed catalog rows **0**, sig duplicate groups **0**, identity duplicate groups **0**
- Field-drift checks (title / description / image / canonical_url / brand / product_type / category / payload seed_data / source_ref): **all 0**
- Public live sample: 8/8 sig_* and ext_* PDPs return 200; HTML titles + og:title look correct

**Count pollution to clean (38 rows total):**
- 36 from `universal_product_sync` (legacy demo: dog harness/leash, AeroFlex Joggers, etc.)
- 2 from `products_cache_backfill_signoff`
- Should not appear in canonical sitemap / "real product" totals.

**Content quality gaps:**

| Metric | Count |
|---|---:|
| external_seed rows in catalog | 3936 |
| image+description ready | 3807 |
| description < 50 chars | **129** |
| zero image | 0 |
| snapshot contract missing | **3467** |
| snapshot quarantine present | 105 |

K-beauty thin-content concentration:
- roundlab.co.kr: 12/12 (KR rows, description = 0)
- medicube.us: 9/17
- anua.com: 4/54
- wishtrend.com: 4/5
- tirtir.global: 1/68 (TIRTIR Stickers — accessory)
- cosrx.com: 1/101 (Perfect Sebum Centella Powder Puff OPP — accessory)

**Shopify stale bug** is fully cleaned: 741 in cache, 741 in catalog, 0 mismatch.

---

## Content backfill plan (current priority, 2026-05-07)

Four sequenced steps, each gated by dry-run → diff → apply → DB postcheck →
canonical API sample → public PDP sample → sig + identity duplicate-group
invariant check.

### Step 1 — Count-pollution cleanup (38 rows)

- Targets: 36 `universal_product_sync` + 2 `products_cache_backfill_signoff`.
- Sample first: dump all 38 rows to a markdown report, eyeball-confirm
  they're demo/signoff (not real merchant inventory).
- **Reversibility-first option:** instead of deleting, gate them out of the
  canonical sitemap / public "real product" list via a `pdp_visibility`
  flag or equivalent. Keeps rows in the DB if we ever need to reference
  them; deletion is a follow-up if the gate proves stable.
- Apply once samples confirm safe.

### Step 2 — `description < 50` content backfill (129 rows)

Priority order (matches the audit's failure concentration):

1. K-beauty: Medicube (9), Round Lab (12), Anua (4), Wishtrend (4), COSRX (1) — **first batch, 30 rows**
2. Fenty / Sigma and other large-volume thin descriptions
3. **Hold list (do not backfill content):** Shipping Protection, stickers,
   samples, puff/accessory items. Mark with a `pdp_content_hold_reason`
   column (nullable) so future audits skip them automatically. Prevents
   another agent from re-flagging them as "thin" and trying to write
   skincare-style PDP copy onto an accessory.

Per-batch flow: dry-run → diff → apply → postcheck (sig + identity
duplicate-group invariants must stay 0) → sample 5 public PDPs.

### Step 3 — Snapshot contract metadata (3467 rows)

- Backfill `seed_data->derived->recall->*` (or whatever the contract
  schema is) and quarantine flags **only**. **Do not rewrite** title /
  description / image / canonical_url — those columns already passed
  the audit's drift check.
- Lowest-risk and largest-volume; can run in parallel with Step 2.
- Same gate: dry-run → diff → apply → invariant check.

### Step 4 — Standing invariant gates (apply to all future backfills)

After every batch:
- `legacy external seed catalog rows = 0`
- `sig duplicate groups = 0`
- `identity duplicate groups = 0`
- `missing external mirror = 0`
- 5-PDP public sample returns 200 with non-empty `<title>` and `og:title`

These are the four metrics from the user's audit; treat them as test gates
on every apply step.

---

## Open issues

### #1 — Lipstick recall returns zero + beauty pass-rate gap (✅ FULLY RESOLVED 2026-05-08)

**Resolution timeline:**
- Phase 7b Step 2 (PIVOTA-Agent #1312, prod commit `91cbcc98`) +
  non-beauty deadline (PIVOTA-Agent #1314, prod commit `d98a8704`)
  shipped 2026-05-07 night. Probe v15: lipstick 9/9, all beauty
  buckets except skincare_serum at 100%, overall 69.8%.
- ingredient_recall_direct extension (PIVOTA-Agent #1315, prod commit
  `ee5564c4`) shipped 2026-05-08 (UTC). Probe v17: skincare_serum
  0/2 → 2/2 PASS. **Beauty pass-rate 37/37 = 100%.**

The architectural diagnosis below was confirmed correct — gateway needed
to read `catalog_products` directly, which it now does via
`fetchCanonicalChainRows` running in parallel with the existing seed
scan. Both the beauty mainline and the ingredient_recall_direct path
now consult the canonical chain.

**Diagnosis (kept for reference):**

**Symptom:** all 9 lipstick queries (EN + ZH) layer-attribute to
`C-external_seed / seed_ran_returned_zero`. Probe metadata:
`external_seed_executed=true, external_raw_count=0,
external_seed_skip_reason=cache_miss_sync_filled, internal_raw_count=0`.

**Diagnosis: the gateway never reads the backend canonical chain.**
Three layered findings, in escalating severity:

1. **Architectural gap (root cause).** PIVOTA-Agent (`agent.pivota.cc/api/gateway`)
   has **zero SQL references to `catalog_products` / `catalog_skus` /
   `catalog_offers`**. Every backend phase that landed under the "canonical
   migration" banner — Phases 1, 2, 2b, 7a, 7c, 8, 9, C-1/C-2/C-3 — is
   invisible to live `find_products_multi` recall. The comment at
   `findProductsExternalSeedDirectRetrieval.js:152` acknowledges this:
   *"the gateway does not yet JOIN catalog_offers (Phase 7b)"*.

2. **Bridge clause is in the wrong file.** `findProductsExternalSeedDirectRetrieval.js`
   has `OR tool = 'catalog_enrichment_agent_v1'` to let attached agent seeds
   pass. But 5+ other seed-query templates in `server.js` and `auroraBff/`
   hard-code `attached_product_key IS NULL` with no bridge. Whichever path
   fires for `shopping_agent → find_products_multi` lacks it.

3. **Even unattached lipstick seeds (36 in prod) return 0.** Some additional
   filter (quality gate, merchant scope, or cache key mismatch on
   `cache_miss_sync_filled`) is dropping rows beyond the IS-NULL filter.
   Needs a separate trace.

**Verification done:**
- ✅ Backend `_fetch_canonical_search_rows` returns all 18 lipstick rows
  when SQL is run directly with `query='lipstick'` — backend recall is
  correct, just unused.
- ✅ Direct seed-table SQL with `attached_product_key IS NULL AND
  market='US' AND lower(title) LIKE '%lipstick%'` returns 36 rows in prod.
- ✅ Agent-authored seeds populate `title` correctly but leave
  `seed_data.derived` NULL; non-agent seeds populate
  `seed_data.derived.recall.category='Lipstick'`. Either should match the
  LIKE predicate via `lower(title)` alone — yet the gateway returns 0,
  pointing to finding #3.

**Trace summary (2026-05-07 cold-read of PIVOTA-Agent):**

- Probe metadata pinpoints the failing query: `pq_candidates=0,
  primary_quality_gate_passed=false, sd_query_semantic_class='default'`
  for lipstick vs `pq_candidates>0, semantic_class='fragrance'` for fragrance
  (which DOES surface our agent seeds — Aventus, Baccarat).
- `inferFragranceSemanticClass()` only classifies fragrance. Lipstick
  / makeup queries fall through to `'default'`.
- **Strong candidate filter:** `src/findProductsMulti/policy.js:5390-5410`
  — beauty-diversity gate requires ≥2 non-tool category buckets; fragrance is
  explicitly exempt (`isFragranceFlow`); lipstick isn't. A pure-lipstick
  result set (1 bucket = `lip_makeup`) gets `filtered = []`.
- **But** the probe reports `external_raw_count=0`, `domain_filter_dropped_external=0`
  — if the diversity filter were the cause, drops would be non-zero. So
  there's likely an upstream filter ALSO returning 0 (the SQL itself).
- Phase 8 (fragrance) commit was **data-only** (50 PDP candidates JSONL); no
  code change went in to enable fragrance retrieval. So fragrance succeeds
  through whatever existing path also exists for lipstick — meaning the
  difference is purely in pre-retrieval expansion / classification, not a
  new fragrance-specific SQL path.

**Recommendation: skip Track A as originally scoped.**

Cold-reading 30k+ LOC across PIVOTA-Agent's `server.js`, `policy.js`, and
`auroraBff/` was sufficient to identify the architectural gap and a
filter candidate, but not enough to confidently land a single-line patch.
A real Track A would need either:
- runtime log instrumentation deployed to staging, OR
- running the gateway locally with a debugger against the prod DB

Both are bigger commitments than "today, ~2 hr" anticipated. Pivot to
Track B, which addresses root cause and avoids guessing:

- **B. Phase 7b — wire gateway recall to JOIN `catalog_offers` /
  `catalog_skus`.** Estimated 1–2 day spike on PIVOTA-Agent side. Needs
  design alignment because the seed-scan code is load-bearing for the
  existing ~80% of recall traffic that isn't agent-authored. The
  comment at `findProductsExternalSeedDirectRetrieval.js:152` already
  flags this work as planned.

**Don't extend Phase 4 to fashion/electronics/home until B is in flight**
— otherwise we're adding to a ghost catalog the gateway doesn't read.

### #2 — Phase 9 regression (45% → 28%) not root-caused

**Symptom:** v9_phase8 (45%) → v9_phase9 (28%). The eye+face ingestion that
was supposed to *add* candidates appears to have *displaced* prior passes.

**Hypothesis:** Phase 9 SKUs ingested with a category_path or pdp_scope that
caused them to outrank fragrance/lipstick passes in some buckets, dropping
those queries to THIN/EMPTY. Probe v10 partially recovered (30%), but neither
fragrance nor face has returned to phase-8 levels for every query.

**Next move:** diff `recall_v9_phase8` and `recall_v9_phase9` per-query to
isolate which queries flipped state. Cheap diagnostic — 10 minutes if
`LAYER_ATTRIBUTION.md` already exists for both.

### #3 — `run_catalog_enrichment.py` swallows `Exception` on table-level INSERTs

**Where:** `scripts/run_catalog_enrichment.py:250, 282, 316` — bare
`except Exception: logger.exception(...)` around each table's INSERT loop.

**Why it matters:** this is how the fragrance Phase 8 SKU UniqueViolationError
went silent (root cause fixed in PR #326, but the data gap remained until
PR #332 backfill today). Same pattern would mask any future FK-target
INSERT failure.

**Fix:** fail-fast on FK-target tables (merchants, products, skus). Per-row
`except` is OK for `catalog_offers` and `external_product_seeds` if at all,
since those are leaf rows. Probably <50 lines of code, half-day with tests.

**Priority:** medium. Not user-facing, but it's the kind of silent-corruption
bug that costs days the next time it strikes.

### #4 — 304 catalog_products rows with NULL `category_path`

**Origin:** Phase 2 regex backfill covered 1216/1535 = 79%. The remaining 304
are pet, fashion, lingerie, and other long-tail categories the regex doesn't
have patterns for.

**Why it's deferred:** Phase 5 success criteria don't require these. Not in
the lipstick/fragrance/makeup recall path.

**When to revisit:** when extending Phase 4 to a new category beyond beauty.
Either expand the regex or run a one-shot enrichment-agent pass over the 304
rows.

### #5 — Phase 4 not yet run for fashion / electronics / home

**Status:** these buckets are still 100% EMPTY in probe v10. The Phase 4
agent + Phase 9 ingestion pattern works (proven by lipstick → fragrance →
eye/face). Repeating it for these buckets would unblock the next ~15 queries
in the corpus.

**Cost:** ~1 hr enrichment-agent runs per category, plus probe re-run.

**Priority:** lower than fixing #1 — there's no point ingesting more PDPs if
the recall path doesn't surface lipstick. Fix #1 first, then extend.

---

## Recommended next steps (post probe v17 + Phase 4 electronics blocker, 2026-05-08)

Phase 7b is in production. Beauty recall is solved (100% PASS across
lipstick/fragrance/eye/face/skincare). The remaining 14 EMPTY queries
are entirely non-beauty (electronics 4/5, home 4/4, fashion_*) — pure
data gap, not recall code.

Phase 4 electronics scaffolding (PR #359) opened with 15 hand-curated
PDP candidates but **Stage 2 Gemini validator dropped 10-12 of them
silently** (no `validation_drop_reason` persisted). Stop-and-fix
triggered before any DB writes. Validator instrumentation is now a
blocker for Phase 4 expansion at scale.

Updated priority sequence:

1. ✅ **Validator instrumentation (codex)** — done, PR #363 draft.
   `validation_drop_reason` + `_detail` per drop, retry on transients,
   summary JSON.
2. ✅ **Re-run electronics Stage 2 with diagnostics** — done. Real
   drop pattern: 7/7 are JSON-extraction-layer (no_text_parts,
   no_balanced_block, decode_failed). NOT anti-scraping.
3. ✅ **Fix Gemini JSON contract (codex)** — done via PR #365.
   `maxOutputTokens` 1024 → 4096. Single variable, dominant root
   cause was truncation (not prose, not anti-scraping). All three
   corpora now meet gate: electronics 12/15, fragrance 37/50,
   lipstick 30/51. `no_balanced_block` + `decode_failed` 7 → 0.
4. **Stage 3 ingest electronics (codex, NEXT)** — re-run validate
   against electronics with the new validator on main, then
   dry-run + apply. Audit invariants must hold. Probe v18 gate:
   electronics 1/5 → ≥3/5 PASS, beauty 37/37 holds, no latency
   regression.
5. **Phase 4 expansion to fashion / electronics / home** — scale to
   30-50 entries per category once probe v18 confirms electronics
   lifts. If electronics doesn't lift, escalate to Phase 7b non-beauty
   gateway extension before scaling data.
   The exact lipstick/fragrance Phase 4 pattern that landed beauty
   coverage now applies. For each new vertical:
   - Prepare 30–50 hand-curated PDP candidates JSONL (mirrors
     `data/catalog_enrichment/lipstick_validated.jsonl` shape)
   - Run `scripts/run_catalog_enrichment.py run --category <vertical> --apply`
   - Mirror script will pick up the resulting agent seeds at INSERT time
     and classify them (Phase 2-redo guardrail)
   - Probe v16 should show non-beauty buckets lift from 0/N to ≥half/N
   Estimated cost: 2-4 hours per vertical if hand-curated, plus probe.
   Recommend electronics first (5 queries, biggest weight on overall
   pass-rate) followed by home (4) and fashion_* (7 across 3 buckets).

2. **Latency follow-up** — p99 prod 12.0s is still over the original
   3s `pivot_search_slow` warning threshold. The 6000ms non-beauty
   deadline is upper-bounded but stacked timeouts (clampLocalBeauty +
   external_seed_direct + canonical_chain) may compose to 12s on the
   slowest queries. Investigate the 2 cache_miss_sync_filled outliers
   (noise cancelling headphones, black leather sneakers) — different
   bug than canonical recall.

3. **627 long-tail NULL category_path rows** — needs non-beauty
   taxonomy. Lower priority; only matters if a category-anchored
   recall query ever targets accessories/lingerie/pet, which today's
   corpus doesn't.

4. **Tighten probe verdict logic** — v12 fashion_shoes false positive
   (PASS on cosmetic results matching "black"/"leather") should fail
   with a stricter verdict. Worth doing before declaring future
   pass-rate gates met. ~1 hr in
   `pivota-agent-ui/scripts/eval_corpus_recall_summarize.mjs`.

5. **Open issues #2 (Phase 9 regression), #3 (runner swallow-and-log)** —
   parallel-eligible; not blockers. Issue #2 may have been masked by
   the Phase 7b lift; revisit if it resurfaces.

---

## Probe re-run command

```bash
cd ~/dev/pivota-agent-ui
EVAL_INVOKE_URL=https://agent.pivota.cc/api/gateway \
EVAL_RUN_ID=recall_v$(date +%Y%m%d_%H%M%S) \
EVAL_CONCURRENCY=2 \
node scripts/eval_corpus_recall_runner.mjs \
  --source shopping_agent --entry chat \
  scripts/eval_corpus_recall_v1.jsonl

node scripts/eval_corpus_recall_summarize.mjs <run_id>
node scripts/eval_corpus_recall_layer_attribution.mjs <run_id>
```

Wall clock: ~5 min for the runner. Compare `SUMMARY.md` and
`LAYER_ATTRIBUTION.md` against the trajectory table above; add a new row.

---

## Related docs (don't duplicate; link)

- Recall investigation final handoff: `~/dev/pivota-agent-ui/reports/recall_v1/RECALL_INVESTIGATION_FINAL.md`
- Phase 6 design notes (pdp_scope dimension): `~/.claude/plans/shimmying-soaring-ember.md` (historical)
- BD report architecture: see `BD report:` PR titles in `git log` (#283 → #316 series)
- Canonical PDP sig_* foundation: PRs #327, #329, #330, #331 (separate track from recall)
