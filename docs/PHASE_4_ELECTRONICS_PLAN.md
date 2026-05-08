# Phase 4 Electronics Expansion

**Goal**: lift the 5 electronics queries in the recall corpus from current
1/5 PASS to ≥3/5 PASS by hand-curating electronics PDP candidates and
running the existing Phase 4 enrichment pipeline (validate → ingest →
mirror).

- Status: **Step 1 PROPOSED — starter JSONL drafted, full curation pending**
- Created: 2026-05-08
- Owner: TBD (codex execution + peng review)
- Estimated cost: 2–4 hours hand-curation + 30 min run + 30 min probe

---

## Why electronics first

After Phase 7b shipped (probe v17), beauty pass-rate is **37/37 = 100%**.
The remaining 16 EMPTY queries are entirely non-beauty:

| Bucket | n | EMPTY | weight |
|---|---:|---:|---|
| **electronics** | **5** | **5** | largest single non-beauty bucket |
| home | 4 | 4 | |
| fashion_dress | 2 | 2 | |
| fashion_top | 2 | 2 | |
| fashion_shoes | 3 | 2 | |

Electronics is the largest weight (5 queries) and has clean, well-known
products that are easy to hand-curate (Apple AirPods, Sony WH-1000XM5,
Kindle Paperwhite, etc.) with stable URLs across multiple retailers.

## Target queries

```
electronics/en  bluetooth earbuds
electronics/en  noise cancelling headphones under $200
electronics/en  kindle alternative e-reader
electronics/zh  蓝牙耳机                                # bluetooth earbuds
electronics/zh  电子阅读器                              # e-reader
```

ZH→EN aliases already cover `蓝牙耳机 → bluetooth earbuds` and
`电子阅读器 → e-reader` (in `pivota-agent/src/findProductsMulti/zhEnQueryAliases.js`).
So 5 EN-equivalent product surfaces cover all 5 queries.

## Open architectural question (verify before scaling curation)

**Does the gateway's non-beauty recall path actually consult
`catalog_products`?** Phase 7b Step 2 (PR #1312) wired the canonical
chain into the **beauty mainline**. The non-beauty path uses primary
upstream HTTP to `pivota-backend.search_pivot_catalog` which DOES
JOIN catalog_products+skus+offers. So in principle:

- Add electronics PDPs → backend's SQL finds them → returns rows fast
  → 6000ms non-beauty deadline (PR #1314) doesn't fire → query passes.

**But** v17 probe showed all 5 electronics queries hit either
`shopping_mainline_non_beauty_primary_deadline` (3) or
`query_timeout` (2). That means primary upstream is currently slow on
electronics — likely because the SQL is doing a wide text-LIKE scan
with no good index hit.

**Hypothesis**: with electronics PDPs in catalog with
`category_path='electronics/audio/earbuds_wireless'` (etc.), the
backend's category-anchored WHERE will use the indexes (`mig 068
idx_catalog_products_canonical_active`) and complete in <500ms.

**De-risk via small starter batch**: ingest just 10-15 electronics
candidates first, run probe v18, observe whether the deadline still
trips. If lift happens → expand to 30-50. If no lift → escalate to
**Phase 7b non-beauty extension** (port the canonical-chain reading
into the non-beauty gateway path, similar to PR #1315 for the
ingredient_recall_direct path).

## JSONL schema (existing, no changes)

`data/catalog_enrichment/electronics_pdp_candidates.jsonl`:

```json
{"brand":"<Brand>","product_name":"<Product Name>","category_path":"<path>","attribute_summary":"<3-5 attribute keywords>","expected_url_domains":["<retailer>.com"]}
```

The Phase 4 Stage 2 validator (Gemini-based, already shipped via PR #303
in pivota-backend) takes each candidate, finds a real product URL across
the expected domains, validates that the URL exists + price + image,
and writes `electronics_validated.jsonl`.

Stage 3 (`scripts/run_catalog_enrichment.py ingest --category electronics`)
INSERTs the validated rows into `catalog_products` directly with
`category_path` populated from the JSONL — no classifier inference needed.

## Category paths (new, electronics namespace)

All under `electronics/`:

```
electronics/audio/earbuds_wireless          # bluetooth earbuds
electronics/audio/headphones_wireless       # over-ear, no NC
electronics/audio/headphones_noise_cancelling
electronics/reading/ereader                 # Kindle, Kobo, Boox
```

Existing classifier in `services/pdp_category_classifier.py` is
beauty-only — that's fine because Phase 4 ingestion uses the
JSONL-supplied path directly, bypassing the classifier. Future seed-side
mirror flow into electronics is out of scope of this PR (would need
classifier extension; tracked as a follow-up).

## Starter JSONL (this PR — 15 entries, easy-to-verify products)

See `data/catalog_enrichment/electronics_pdp_candidates.jsonl` in this
PR. Each row is a major-brand consumer electronics product with stable
identity:

| Category | Count | Brands |
|---|---:|---|
| earbuds_wireless | 5 | Apple AirPods Pro 2, Sony WF-1000XM5, Bose QuietComfort Earbuds II, Samsung Galaxy Buds3 Pro, Beats Studio Buds |
| headphones_noise_cancelling | 4 | Sony WH-1000XM5, Bose QuietComfort Headphones, Sennheiser MOMENTUM 4, Apple AirPods Max |
| headphones_wireless (non-NC) | 2 | Beats Solo 4, JBL Live 770NC |
| reading/ereader | 4 | Kindle Paperwhite, Kindle Colorsoft, Kobo Libra Colour, Onyx Boox Page |

Designed to:
- Cover all 5 target query intents directly
- Span $50 to $549 price points so "under $200" filter has hits
- Multi-retailer expected_url_domains (Amazon, BestBuy, brand store, Apple) for validator robustness
- Avoid bleeding-edge / region-specific SKUs that might 404 mid-validation

## Sequencing (one PR for the starter, codex completes follow-up)

Land in this order:

1. **Starter JSONL (this PR)** — 15 entries. Doc-only + data file. No
   ingestion, no DB writes. Codex / peng review the curation quality
   before any prod call.
2. **Stage 2 validation run** (codex):
   ```bash
   GEMINI_API_KEY=... python scripts/run_catalog_enrichment.py validate \
     --category electronics
   ```
   Writes `data/catalog_enrichment/electronics_validated.jsonl`.
3. **Stage 3 ingestion** (codex, dry-run first):
   ```bash
   DATABASE_URL=... python scripts/run_catalog_enrichment.py ingest \
     --category electronics --dry-run
   ```
   Then apply.
4. **Probe v18 staging + prod**, gates:
   - Electronics: 1/5 → ≥3/5 PASS
   - Beauty buckets: 37/37 hold (no regression)
   - `canonical_path_executed` and `external_seed_executed` rates
     stable on the electronics queries
5. **If lift confirmed** → codex curates 20-35 more entries (total ~30-50)
   and re-runs. If not → escalate to Phase 7b non-beauty extension before
   adding more data.

## Verification gates

- [ ] Each starter JSONL entry's brand + product_name corresponds to a
      real, currently-sold product (codex / peng visual sanity check)
- [ ] Stage 2 validator validates all 15 entries (≥80% should hit a
      real URL; entries that don't validate get dropped, not hand-fixed)
- [ ] Stage 3 dry-run shows expected row counts: 15 PDPs, 15 SKUs,
      15-30 offers, ~5-10 unique merchants
- [ ] Audit invariants hold: `legacy external seed catalog rows = 0`,
      `sig duplicate groups = 0`, `identity duplicate groups = 0`,
      `missing external mirror = 0`
- [ ] Probe v18 shows electronics lift; no beauty regression; no
      latency regression (p99 within v17 ±1s)

## Out of scope

- Home / fashion expansions — separate Phase 4 PRs after electronics
  proves the path works end-to-end
- Classifier extension to support electronics regex patterns — needed
  if future seed mirror flow includes electronics seeds, but not for
  this hand-curated path
- Phase 7b non-beauty extension — only if probe v18 shows the data
  doesn't reach the gateway

---

## References

- Phase 4 lipstick playbook (the original): `docs/MASTER_PLAN.md` Phase 4 row + PRs #301, #303
- Phase 4 fragrance follow-up: PR #323 (50 SKUs, data-only)
- Phase 4 makeup eye+face: PR #328 (50 SKUs, data-only)
- Phase 7b architecture: `docs/PHASE_7B_PLAN.md`
- Probe v17 (current baseline): `pivota-agent-ui/reports/recall_v1/recall_v17_post_pr1315_1778198230/`
- Mirror INSERT-time classifier (Phase 2-redo): PRs #347–#352
