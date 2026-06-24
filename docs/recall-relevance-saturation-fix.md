# Spec: recall relevance saturation — separate text-relevance from structural boosts

**Status:** proposal (scoped, not yet built) · **Date:** 2026-06-24 · **Repo:** `pivota-backend` (`services/pivot_query_service.py`)
**Severity:** core recall quality — affects **every** shopping/recall query, not one lane.
**Found via:** the Anuko store-less canary (verify-to-serve → citable). Cross-ref: [ADR-007](adr/ADR-007-citable-index-vs-commerce-overlay.md), `commerce-index-storeless-brand-decision-layer` memory.

> **TL;DR:** For a category/vertical query, recall ranks by **structure, not text relevance** — the `+200 multi_merchant_canonical` boost saturates `candidate_score` at its `1.4` cap, so any structurally-good canonical row in the category pins to the top regardless of how well it matches the words. Result: irrelevant same-category products bury precise matches. Fix = compute a **text-relevance score distinct from structural/scope boosts**, order by text-relevance first with structure as a *secondary* signal, behind a flag, validated on the recall probe harness.

---

## 1. Evidence (live, 2026-06-24)

Query **"Anuko Nourishing Hair Butter"** (a product's exact name) → top recall results:

| # | Result | `candidate_score` | `lane` | `text_relevance` |
|---|---|---|---|---|
| 1 | Lav **Kids Hair Clips** Duo | 1.4 | catalog_discovery | null |
| 2 | Lav Kids **Scalp Massager** | 1.4 | catalog_discovery | null |
| 3 | After Workout **Dry Shampoo** | 1.4 | catalog_discovery | null |
| 4 | Natural **Shampoo Bar** | 1.4 | catalog_discovery | null |
| 5 | Moisture Repair **Shampoo** (Fenty) | 1.4 | catalog_discovery | null |
| … | (49 rows, all 1.4) | | | |
| 50 | **Anuko Nourishing Hair Butter** (citation) | 0.90 | citable_canonical | — |

Every top row is irrelevant to "hair butter" (clips, massager, shampoos) yet maxed at `1.4`; the exact-name product is last. `candidate_score` carries **no discriminating power** — 49 rows tie at the cap.

## 2. Root cause (grounded in `services/pivot_query_service.py`)

1. **Broad candidate pool for vertical queries.** `_fetch_canonical_search_rows` builds a `candidate_skus` CTE that, for a recognized vertical/category (e.g. "hair"), pulls the whole **category** via `category_path` matching (mig 069 + the ported `BEAUTY_CATEGORY_PATTERNS`). So all hair-care products are candidates — clips and massagers included.
2. **`rank_score` is dominated by STRUCTURAL boosts, not text.** The SELECT `rank_score` adds `CASE WHEN p.pdp_scope = 'multi_merchant_canonical' THEN 200` (+ lifecycle `+60/+20`, category-prefix `+90`). Text bonuses are mostly **exact equality** (`= :query_exact`), which a real query never hits. So a structurally-good canonical row scores `≥200` on structure alone with ~0 from text.
3. **The cap erases the difference.** `_canonical_match_reason` sets `candidate_score = min(rank_score / 100, 1.4)`. Anything with the `+200` scope boost → `2.0` → **clamped to `1.4`**. Every `multi_merchant_canonical` candidate in the category saturates at `1.4`.
4. **`_sort_items` ranks by `candidate_score` first.** Key 3 is `-(relevance_boost + source_boost)`. With 49 rows tied at `1.4`, ordering collapses onto weak tiebreakers (`source_order`, price) — and any genuinely precise, lower-scope match (the brand's own product at `0.90`, citable or not) sorts beneath all of them.

**Net:** structure ("is this a good canonical PDP") is being read as relevance ("does this match the query"). They must be separate signals.

## 3. Fix design

**Principle:** order by **text relevance first**; structural/scope quality is a *secondary* signal (tiebreaker or small additive weight), never a primary that saturates.

1. **Return a distinct `text_relevance` from the SQL.** In `_fetch_canonical_search_rows`, split `rank_score` into:
   - `text_score` — only the query-vs-content match terms (exact + **partial `LIKE`** + token overlap on title/brand/category). *(Partial-match credit is the same gap fixed for the citable lane in #1027; bring it to the canonical lane too.)*
   - `structure_score` — `multi_merchant_canonical`, lifecycle, scope (kept, but separate).
2. **Rank on text first.** `_canonical_match_reason` sets `candidate_score` from **`text_score`** (normalized, *uncapped by structure*). Expose `structure_score` separately.
3. **Structure as secondary.** `_sort_items` orders by text-relevance (key) then a *small* structure weight (new lower-priority key), so a `multi_merchant_canonical` PDP wins **ties** among similarly-relevant rows but cannot leapfrog a more relevant row.
4. **Stop the saturation.** Either raise the cap or (better) cap **text-relevance** independently of structure so 49 category rows no longer collapse to one value.
5. **Neutrality preserved.** Text + structure only; no take-rate. The P0.3 firewall + invariance test still hold (extend the invariance test to assert structure can't outrank text).
6. **Side benefit — fixes citable burial for free.** Once relevance discriminates, the citable lane's `0.90` text match for a branded query naturally outranks irrelevant `~0.2`-text category rows — no special citation boost needed (supersedes the deferred "_sort_items citation boost" option).

**Flag:** `RECALL_RELEVANCE_V2` (default OFF). With it off, scoring is byte-identical to today.

## 4. Eval plan (REQUIRED before flip — this is core recall)

- **Harness:** the recall probe suite in `pivota-agent-ui/reports/recall_v1` (+ `recall_v6_*`) and `scripts/beauty_ranking_audit.py`.
- **Before/after, flag OFF vs ON:**
  - **No regression** on the existing `recall_v6` shopping-agent pass-rate.
  - **Precision cases that must improve:** (a) a product's exact-name query returns that product in the top results; (b) category-head queries don't return off-type products (clips/massager for "hair butter") above on-type ones; (c) the Anuko citable row ranks competitively for its branded query.
- **Acceptance:** pass-rate ≥ baseline AND the precision cases above flip from fail → pass. Capture the before/after report in the PR.

## 5. Rollout

1. Build behind `RECALL_RELEVANCE_V2` (OFF). Unit suites: `test_pivot_query_service_*`, `test_pivot_query_citable_recall`, `test_pivot_query_service_scope_rank`.
2. Run the recall harness OFF vs ON; attach the diff.
3. Canary the flag on one surface; watch shopping recall + the precision cases.
4. Flip globally once the harness + canary are clean. Then the citable lane's #1027 partial-match credit fully pays off.

## 6. Open questions
- Exact text vs structure weighting (how much should `multi_merchant_canonical` win *ties* without leapfrogging relevance?).
- Whether the vertical/category candidate pool should also tighten (clips shouldn't be candidates for "hair butter" at all) — a recall **precision** lever orthogonal to ranking; decide if it's in-scope or a follow-on.
- Token-overlap vs whole-phrase `LIKE` for `text_score` (whole-phrase misses "best X for Y" shapes; token overlap is better but costlier).

> Cross-ref: #1027 (citable lane partial-match credit — the same idea, scoped to the citable lane), ADR-007 (the citable surface this unblocks), `find-products-multi-recall-lane` memory.
