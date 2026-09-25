# Why Stila stopped serving — diagnosis, 2026-09-07

All numbers below are read-only SELECTs against production Postgres via
`scripts/ops/run_oneoff_job.sh` (pivota-prod / us-west1), programs in this directory:
`stila_probe.py` → `stila_diagnosis_raw.json`, `stila_fix_verify.py` →
`stila_fix_verify.json`, `stila_fix_widening.py` → `stila_fix_widening.json`.
**No writes were made to production.**

## Verdict

Stila is not out-ranked by Pixi. **Stila is never a candidate.** Of the 124 Stila sku rows in
prod, the query "Stila Stay All Day Liquid Lipstick" admits **0** through every door
`_fetch_canonical_search_rows` has:

| door | Stila sku rows admitted (of 124) |
|---|---|
| whole-phrase `LIKE '%stila stay all day liquid lipstick%'` over title/brand/merchant/sku/variant | **0** |
| category `p.category_path LIKE 'beauty/makeup/lip/%'` | **0** |
| brand-anchor ADMIT (`p.brand`/`m.merchant_name` must carry stila AND stay AND all AND day) | **0** |
| brand-anchor SCORE (same terms, title also allowed) | **0** |

The page that comes back is therefore whoever else matched: 7 Fenty Beauty rows, 4 Clarins,
2 Lord & Berry, and a tail of Tocobo/Heart Percent/Miss Nella — all at `rank_score` 350, all
of them rows that DO carry a `beauty/makeup/lip/%` path.

## What was ruled out, with numbers

**Promotion ran, and worked.** `scripts/promote_brand_official_canonicals.py` is not the
missing step:

* 124 / 124 Stila content_keys have an `index_pipeline_state` row (`content_keys_without_ips` = **0**)
* 123 are `serving_eligible = true` with `blocker_code = 'none'` — 87 `public_indexed`,
  36 `shadow_indexed` — average `content_quality_score` **83.3**
* the one non-serving row is the withdrawn promo: `extracted` / `suppressed` / false

So re-running promote would change nothing. It was not run.

**The withdraw script hit exactly one row, on all three tables.** The content_key-grain
caveat did not bite:

| table | rows stamped `script=withdraw_catalog_rows` | reason | at |
|---|---|---|---|
| catalog_products | **1** | `token_price_promo` | 2026-09-05 01:51:09.651Z |
| catalog_skus | **1** | `token_price_promo` | 2026-09-05 01:51:09.660Z |
| catalog_offers | **1** | `token_price_promo` | 2026-09-05 01:51:09.665Z |

That row is `Free Travel Stay All Day® Liquid Eye Liner in Intense Black (TikTok Shop)` —
the named $0.01 promo. Stila's other 123 rows carry `suppression_reason IS NULL` and
`suppressed_at IS NULL`.

**Inventory and merchant gates are all open.** 124 products / 124 skus (123 unsuppressed) /
125 offers, of which **124 live and 124 priced**. `Stila Cosmetics`
(`merch_obs_e65708e6dc609674`) is `status='observed'` (≠ inactive) and `indexable=true`.
`sync_status='live'` on all 124.

**The #2062 per-product SKU cap is not implicated.** Stila is 1 sku per product (124/124), so
a cap of 12 can never bite. The lip-category candidate pool is 859 sku rows over 341
products — real budget pressure against a `candidate_limit` of 80, but irrelevant when Stila
contributes 0 rows to that pool.

**brand_priority (#2063) is 0 for everyone on this query**, because it wraps the same
identity-only admit predicate that matches nothing here. It is not tilting the page.

## The actual mechanism — three independent misses

### 1. The category path stops one level short (primary)

Every Stila row's `category_path` is **exactly `beauty/makeup`**. Not one is under
`beauty/makeup/lip/`:

```
category_path | n
beauty/makeup | 124
```

`category_path_prefix_for_query("Stila Stay All Day Liquid Lipstick")` = `beauty/makeup/lip/`,
and the door asks `p.category_path LIKE 'beauty/makeup/lip/%'`. `'beauty/makeup'` fails it.

The `missing_taxonomy` escape a few lines down does not reach these rows either: it fires only
when the path **IS NULL**. So a row that asserted a coarse-but-correct ancestor was strictly
**less** recallable than a row that asserted nothing at all. That inversion is the bug.

**This is a whole-lane defect, not a Stila one.** The curated-brand lane
(`source_system = 'catalog_enrichment_agent_v1'`) writes depth-2 paths for entire brands,
while the seed-mirror lane writes depth-4:

| brand | source_system | products | with category_path | of those, `beauty/makeup/lip/%` |
|---|---|---|---|---|
| Stila Cosmetics | catalog_enrichment_agent_v1 | 124 | 124 | **0** |
| Tarte (+ Tarte Cosmetics) | catalog_enrichment_agent_v1 | 249 | 249 | **0** |
| FLOWER Beauty | catalog_enrichment_agent_v1 | 49 | 49 | **0** |
| Clarins | catalog_enrichment_agent_v1 | 4 | 4 | 4 |
| Lord & Berry | catalog_enrichment_agent_v1 | 3 | 3 | 3 |
| fenty beauty | external_product_seeds_mirror_v1 | 862 | 862 | 132 |
| pixi beauty | external_product_seeds_mirror_v1 | 351 | 351 | 5 |
| kylie cosmetics | external_product_seeds_mirror_v1 | 226 | 226 | 24 |

Clarins and Lord & Berry serve this query because they got depth-4 paths. Tarte and Flower
Beauty did not, and are shut out of a lip-category query for exactly the same reason Stila is
— they serve today only for queries that open a different door.

A second, quieter victim: **73 rows sit at `beauty/makeup/lip` exactly** (no trailing
segment), and `LIKE 'beauty/makeup/lip/%'` excludes those too.

### 2. The brand-anchor ADMIT branch cannot rescue it

`_category_brand_anchor_terms("Stila Stay All Day Liquid Lipstick")` = `['stila', 'stay',
'all', 'day']`. The ADMIT branch ANDs all four against **`p.brand` and `m.merchant_name` only**
— never the title. No brand on earth is named "Stila Stay All Day", so the admit is false for
every row in the catalog, and `brand_priority` is 0 everywhere. The score-side anchor DOES
read the title, but a score cannot admit a row the WHERE excluded — the file already says so
in its own comments.

### 3. `®` eats the word it is glued to (measured: 0 of 124)

The score-side anchor should still have found Stila once admitted — and it did not. Stila's
titles are `Stay All Day® Liquid Lipstick`, and `_BRAND_IDENTITY_SEPARATORS` folded
`. , - ' + / & ( )` to spaces but not `®`. The space-padded predicate asks for `% day %` and
the column offers `" day® "`. Measured over all 124 rows: the four-term anchor matched
**0**. With `®` folded, it matches **30, and all 30 are Stila** — no other brand in the
catalog is touched.

## Fix

The fix is code, not data, so per instruction nothing was written to prod and no script was
re-run. Branch `fix/recall-ancestor-taxonomy-and-trademark-glyphs`, in
`services/pivot_query_service.py`:

1. **Ancestor-taxonomy door.** A `category_path` that is a strict ANCESTOR of the query's
   prefix is admitted, *provided the row's own title / product_type / sku title carries a
   whole-word category phrase from the query* — the same evidence standard `missing_taxonomy`
   already applies to NULL paths. The phrase derivation was hoisted to
   `_category_evidence_phrases()` so both escapes share one rule. `SUBSTR`/`LENGTH`, not
   `LIKE`, for the prefix test: `p.category_path` would be the LIKE *pattern* there, and a
   stored `_` (real: `beauty/makeup/lip/lip_oil`) would silently become a wildcard. The
   **score is unchanged** — an ancestor-only row is admitted so it can compete, not promoted.
2. **`®`, `™`, `©`, `·` added to `_BRAND_IDENTITY_SEPARATORS`.**

### Verified on the production dialect, against production data

The ancestor predicate on real stored paths (target `beauty/makeup/lip/`):

| stored path | rows | is_ancestor |
|---|---|---|
| `beauty` | 1385 | true |
| `beauty/makeup` | 1746 | true |
| `beauty/makeup/lip` | 73 | true |
| `beauty/makeup/eye` | 34 | false |
| `beauty/skincare` | 1694 | false |
| `beauty/makeup/lip/lipstick` | 249 | false (already admitted by the existing door) |
| `beauty/makeup/lip/lip_oil` | 1 | false — the `_` did **not** act as a wildcard |

The served page for the live query, after the fix, in the lane's own order:

| rank | brand | title | category_path | rank_score |
|---|---|---|---|---|
| 1–6 | **Stila Cosmetics** | Stay All Day® Liquid Lipstick (+ Mini, Shimmer & Sheer, Fiery, Patina, Kisses Make Me Happy duo) | `beauty/makeup` | **440** |
| 7–10 | Clarins | Joli Rouge / Joli Rouge Shine / Lip Comfort Oil / Lip Perfector | `beauty/makeup/lip/…` | 350 |
| 11–12 | Lord & Berry | Timeless Kissproof / Vogue Matte Lipstick | `beauty/makeup/lip/lipstick` | 350 |
| 13+ | OILUJ, Fenty Beauty… | | | 350 |

**Blast radius of the widening**, on this query, over rows the old category door did not
already admit: **140 sku rows / 32 products / 15 brands** — every one a real lip product of a
real beauty brand, no junk:

```
MAC Cosmetics      beauty/makeup      111 rows /  7 products   <- MAC was shut out too
Stila Cosmetics    beauty/makeup        6 rows /  6 products
fenty beauty       beauty/makeup/lip    6 rows /  3 products   <- the depth-3 case
Charlotte Tilbury  beauty               3 / 3      Dior   beauty  2 / 2
NARS               beauty               2 / 2      kylie  beauty/makeup/lip  2 / 1
Bobbi Brown, Tarte, Clinique, FLOWER Beauty, ILIA, L'Oreal Paris, MAC, Merit — 1 each
```

Tests: `tests/test_recall_ancestor_taxonomy_and_glyphs.py`, 8 cases, executing the real
`_fetch_canonical_search_rows` against a real DB. Each clause was verified load-bearing by
mutating **one at a time**:

| mutant | tests that fail |
|---|---|
| ancestor door removed (`if False`) | the 2 positive tests only |
| evidence conjunct made vacuous (`1=1 OR …`, binds still referenced) | the mascara bound only |
| ancestor prefix test replaced by a tautology | the sibling-path bound only |
| `®™©·` removed from the separator list | the glyph test + its unit pin |

The first mutation attempt also surfaced a real hazard now written into the code: on this
stack an **unused bind is a hard `ArgumentError` from `text()`**, not a no-op, so the params
and the SQL that references them are built inside the same branch.

## Left open (not fixed here)

* **The data defect stands.** `catalog_enrichment_agent_v1` is still writing `beauty/makeup`
  for Stila, Tarte, Flower Beauty and MAC. The code fix makes them recallable; it does not
  give them taxonomy, and they still forgo the +90 category score. A backfill of
  `category_path` for the curated cohort is the real repair.
* **`catalog_row_trust.serving_decision = 'shadow'` with `serving_reason_codes =
  {IDENTITY_LIVE_READ_DISABLED}` on 123 of 124 Stila rows.** Not the cause of this symptom —
  recall reads neither column — but it means any public reader that gates on trust still sees
  Stila as shadow.
* **36 of 124 Stila rows are `pdp_lifecycle_stage = 'candidate'`** and are excluded by the
  lifecycle clause independently of everything above (`gate_lifecycle` = 88 of 124). Even
  with this fix, those 36 do not serve cross-merchant.
* **The ADMIT branch remains identity-only.** Widening it to the title is a much larger
  ranking change and is not attempted here.
