# Fix Plan G — Full T1 run + T2 full-cohort re-snapshot: run report

**Date:** 2026-07-13 · **Founder GO:** received 2026-07-13 (full run of the
pilot-scoped T1 enrichment) · **Runner:** `scripts/backfill_llm_attributes.py`
(`--i-understand-full-cost`, batch 250, concurrency 5, kill-switches: parse-fail
> 5% after ≥50 calls, cost > safety ceiling) · **Model:** gemini-2.5-flash via
`services/llm_synthesis` · **Writes:** additive `llm_attributes` envelopes
(`schema_version=structural_depth.beauty.v1`), guarded
`WHERE llm_attributes IS NULL OR '{}'::jsonb`, live/non-demo/beauty cohort,
set-based unnest batches.

## T1 enrichment — final state (prod-verified)

| metric | value |
|---|---|
| live non-demo catalog rows | 9,378 |
| beauty rows (T1 scope) | 9,249 |
| rows enveloped | **9,249 / 9,249 (100%)** |
| rows with ≥1 populated attribute field | **7,387 (79.9%)** |
| rows with ≥3 fields | 5,658 (61.2%) |
| honest-empty envelopes (thin/non-product PDPs: bundles, gift cards, accessories) | 1,862 (20.1%) |
| non-beauty live rows (out of T1 scope, `llm_attributes` still NULL) | 129 |

### Field fill (rows carrying each field)
texture 5,770 · concerns 5,359 · key_ingredients 4,716 · format 4,250 ·
finish 3,378 · skin_type 2,407 · volume 2,138 · vegan_status 733 ·
cruelty_free_status 500 · spf 301 · fragrance_free 168 · sulfate_free 70 ·
silicone_free 54.

Fields/product distribution: 0→1,862 · 1→850 · 2→879 · 3→1,065 · 4→1,455 ·
5→1,528 · 6→1,001 · 7→385 · 8→149 · 9→63 · 10→11 · 11→1.

## Run legs, cost, parse failures

| leg | rows written | LLM outcomes | parse-fail | actual cost (USD) |
|---|---|---|---|---|
| pilot keyset 100 (2026-07-12) | 100 | 16 ok / 84 empty | 0 | 0.0141 |
| pilot random 100 (2026-07-12) | 100 | 67 ok / 33 empty | 0 | 0.0198 |
| full run leg 1 (killed externally at batch 28; keyset-resumable by design) | 7,000 | 4,860 ok / 2,112 empty / 28 parse_fail | 28 | 1.4230 |
| full run leg 2 (resume; drained the cohort) | 2,049 | 1,523 ok / 526 empty | 0 | 0.4388 |
| **total** | **9,249** | 6,466 ok / 2,755 empty / 28 parse_fail | **28 (0.30%)** | **$1.896** |

- Kill-switches never tripped (0.30% ≪ 5%; $1.90 ≪ ceiling). Projection was
  ~$1.80 — actual within 6%.
- Parse failures write the row with its deterministic fields only (never
  fabricated); truncation is detected via `finish_reason` — none observed at
  `max_tokens=512`.

## T2 — model_readiness re-snapshot (full live cohort, set-based)

`scripts/resnapshot_quality_bulk.py` — 9,378/9,378 rows re-snapshotted in 19
unnest batches, append-only rows tagged `model_version=structural_depth.g1`
(prior snapshot history untouched; the earlier per-key pilot re-snapshot of the
100 random-pilot keys also completed 100/100, avg 4.56 → 73.51).

| | before (latest pre-g1 snapshots, n=12,551) | after (g1, n=9,378) |
|---|---|---|
| avg | **2.48** | **74.29** |
| median | 0.0 | 76.1 |
| p25 / p75 | — | 68.6 / 83.7 |
| min / max | — | 24.0 / 90.0 |
| rows > 0 | 570 (4.5%) | **9,378 (100%)** |

## Reversibility

- Envelopes: `UPDATE catalog_products SET llm_attributes = NULL WHERE
  llm_attributes->>'schema_version' = 'structural_depth.beauty.v1'` (only ever
  written where NULL/'{}' — nothing pre-existing is affected).
- Snapshots: append-only; `DELETE FROM product_quality_snapshot WHERE
  model_version = 'structural_depth.g1'` restores the prior latest rows.

## Residual gaps

- 129 non-beauty live rows (74 other / 46 fashion / 9 electronics) have no
  attribute pipeline — beauty-only was the T1 scope; fashion/electronics need
  their own field sets + vocabularies.
- 1,862 honest-empty envelopes are largely thin or non-product PDPs (bundles,
  gift cards, candles, washcloths); depth there needs richer crawl copy (refresh
  loop) or cohort hygiene, not a better extractor.
- Deterministic concern lexicon can fire on non-product rows (a gift card
  matched "dullness"); harmless for serving but worth a non-product filter if
  these rows ever gate on concerns.
- Go-forward: new intake rows arrive with `llm_attributes` NULL — re-running
  `backfill_llm_attributes` periodically (it is cohort-guarded + resumable) or
  wiring it into the mirror lane keeps coverage from decaying.
