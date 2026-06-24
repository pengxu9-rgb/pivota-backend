# Spec: gateway brand detection — dynamic brand recognition from the catalog

**Status:** proposal (scoped, not built) · **Date:** 2026-06-24 · **Repo:** `pivota-backend` (`routes/agent_api.py`)
**Severity:** core agent-shopping recall quality — the path agents actually call (`/api/gateway` → `find_products_multi`).
**Found via:** the RECALL_RELEVANCE_V2 harness eval. Cross-ref: [recall-relevance-saturation-fix.md](recall-relevance-saturation-fix.md), `commerce-index-storeless-brand-decision-layer` memory.

> **TL;DR:** A branded query the agent gateway can't recognize as branded falls to ingredient/category recall + external-seed fill and returns off-brand junk. Brand detection is **7 hardcoded brands + a weak suffix rule**, so it misses essentially every real catalog brand. Fix: detect brands **dynamically from the catalog's own brand set** (cached), which lights up the already-built `brand_strict` recall path. Flag-gated, validated on the recall harness.

---

## 1. Evidence (live, 2026-06-24)

`POST agent.pivota.cc/api/gateway` `find_products_multi`:

| Query | `brand_query_detected` | `query_source` | Top results |
|---|---|---|---|
| **The Ordinary Niacinamide 10% + Zinc 1%** | **False** | `agent_products_ingredient_recall_direct` | Dokdo Toner, blotting paper, **eye patches** (8 external-seed) |
| Skin1004 Poremizing Deep Cleansing Foam | **False** | `agent_products_beauty_external_seed_mainline` | correct #1 (saved by exact title text-match) |
| Beauty of Joseon Glow Deep Serum | False | … | correct #1 (exact title) |

"The Ordinary" wasn't recognized as a brand → the query was treated as an **ingredient** query (`niacinamide`) → ingredient recall returned little → external-seed filled 8 with off-brand junk. The brands that "work" only survive because their **exact product title** happens to text-match; nothing about the brand is understood.

> Note: this is the path RECALL_RELEVANCE_V2 (#1030) does **not** touch — V2 fixes `search_pivot_catalog` (`/v1/pivot/query`), which is a *different* recall than the gateway orchestrator.

## 2. Root cause (grounded in `routes/agent_api.py`, origin/main)

`_detect_brand_query()` (`:783`) recognizes a brand only via:
1. **`_BRAND_STATIC_ALIASES`** (`:637`) — a hardcoded dict of **7 brands**: tom ford, jo malone, byredo, dior, fenty beauty, kylie cosmetics, sigma beauty.
2. **`_BRAND_SUFFIX_PATTERN`** (`:651`) — `<≤3 words> (beauty|cosmetics|fragrance|perfume|parfum)`.

Any brand not in that tiny list and not ending in those suffixes → `brand_like=False`. "The Ordinary", "Skin1004", "Beauty of Joseon", "Anuko", and the long tail of real catalog brands all miss.

**The downstream is already built and correct.** When `brand_query_detected=True`, the orchestrator already: scopes external seed to `brand_strict` (`:2948`), sets `required_terms`/`prefer_terms` to the brand (`:2958`, `:2984`), and biases the internal recall. So **only detection is broken** — the brand-aware recall path exists and works; it just rarely fires.

## 3. Fix design — dynamic brand detection from the catalog

**Principle:** the set of "brands" is the set of brands **in our own index**, not a hardcoded list.

1. **Brand dictionary, loaded from the catalog.** A cached set of normalized brand names from `catalog_products.brand` (and optionally `catalog_merchants.merchant_name` / verified `brand_claims`). Refresh on a TTL (e.g. hourly) — brands change slowly. Bounded (top-N by product count) to keep it in memory.
2. **Consult it in detection.** `_detect_brand_query` stays the fast path (static + suffix); add a dynamic pass: does the query contain a known catalog brand (longest-match, whole-token)? If yes → `brand_like=True`, `brand_terms=[that brand]`, `mode="catalog"`, scope by category-hint as today. (Detection is currently sync; load the dictionary async + cache, and have detection read the cached set — or make the gateway resolve it before calling detection.)
3. **Confidence + ambiguity.** Only treat as brand when the match is a real token span (avoid "the ordinary" matching inside unrelated text). Keep the existing `has_category_hint` → `category_scoped` vs `broad` logic. Single-word generic brands ("anua" vs the word) need a min-length / known-brand guard.
4. **Reuse the existing brand_strict path.** No new recall lane — a `True` from detection flows into the built `brand_strict` scoping (`:2948`+). That's the whole point: fix detection, the rest already works.
5. **Neutrality.** Brand detection scopes *relevance* (the shopper named a brand), not commercial ranking. No take-rate signal.

**Flag:** `GATEWAY_DYNAMIC_BRAND_DETECT` (default OFF). Off ⇒ detection = today's static+suffix only; byte-identical.

## 4. Eval plan (REQUIRED before flip — core recall)

- **Harness:** `pivota-agent-ui/scripts/eval_corpus_recall_runner.mjs` → `agent.pivota.cc/api/gateway`, `eval_corpus_recall_summarize.mjs` (breadth) + a **branded precision corpus** (`eval_corpus_recall_precision.jsonl`: "The Ordinary Niacinamide…", "Skin1004 …", "Anuko …", etc.).
- **OFF vs ON:**
  - **Breadth:** no regression (PASS-rate ≥ baseline 62%; no new EMPTY/MONOCULTURE).
  - **Precision (the win):** branded queries flip to `brand_query_detected=True`, `query_source` becomes a brand-scoped lane, and the named brand's products rank top instead of external-seed junk ("The Ordinary Niacinamide" → an actual The Ordinary product #1).
  - **No over-detection:** generic category queries ("acne cleanser", "vanilla perfume") stay `brand_query_detected=False` and their breadth is unchanged (guard against a common word being mistaken for a brand).
- **Acceptance:** breadth ≥ baseline AND the precision cases flip fail→pass AND no generic-query regression. Attach the before/after report.

## 5. Rollout
1. Build behind `GATEWAY_DYNAMIC_BRAND_DETECT` (OFF). Unit: detection recognizes catalog brands, rejects generic words, respects category-hint scoping.
2. Run the harness OFF vs ON (breadth + precision + over-detection guard).
3. Canary the flag; watch gateway recall + the precision/over-detection cases.
4. Flip on once clean.

## 6. Open questions
- **Brand source:** `catalog_products.brand` only, or also `catalog_merchants` / verified `brand_claims`? (Lean: `catalog_products.brand`, top-N by product count, since that's what's actually buyable/citable.)
- **Ambiguity:** single common-word brands (e.g. "Anua", "The Ordinary" contains "ordinary") — min-token / known-brand-span guard to avoid false positives.
- **Sync vs async detection:** `_detect_brand_query` is sync; either preload the cached brand set at request entry and pass it in, or make a small async wrapper. Keep the hot path cheap (in-memory set lookup).
- **Interaction with ingredient detection:** "The Ordinary Niacinamide" is *both* a brand (The Ordinary) and an ingredient (niacinamide). Brand should win (or brand-scope the ingredient recall) — define precedence.

> Cross-ref: #1030 (RECALL_RELEVANCE_V2 — the sibling fix on the `/v1/pivot` path), [recall-relevance-saturation-fix.md], `find-products-multi-recall-lane` memory. The gateway also does NOT surface store-less **citable** rows (the "Anuko" query is empty via gateway) — a separate gap to track.
