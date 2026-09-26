# Third cohort — Anua and Pixi (skincare), and the first real capture leakage

**Date:** 2026-08-31 · **Engine:** `gemini-3-flash-preview`, grounded, Vertex+ADC, temperature 0, 3 runs
**Brands:** Anua (`anua.com`, K-beauty) · Pixi (`pixibeauty.com`, London-founded, Target/Ulta-led)
**Basis:** a new fixed **skincare** Tier B/C set, byte-identical between the two brands. The Joy makeup
queries would have been a category mismatch for a toner/serum brand and would have manufactured a false
~0% "confirmation". Cross-basis comparison therefore uses the branded/unbranded **ratio**, not raw rates.

Cumulative: **6 cohorts, 720 responses.**

---

## 1. Full comparison

| Brand | Model | Basis | A branded | B raw | **B neutral** | C dupe | D compare |
|---|---|---|---|---|---|---|---|
| Judydoll | 2.5-flash | makeup | 100.0% | 6.2% | **6.2%** | 0.0% | 100.0% |
| Joocyee | 2.5-flash | makeup | 100.0% | 4.2% | **4.2%** | 0.0% | 41.7% |
| Flower Knows | 2.5-flash | makeup | 100.0% | 14.6% | **8.9%** | 2.8% | 91.7% |
| Flower Knows | 3-flash-prev | makeup | 100.0% | 6.2% | **0.0%** | 8.3% | 100.0% |
| **Anua** | 3-flash-prev | skincare | 100.0% | 39.6% | **25.0%** | 0.0% | 75.0% |
| **Pixi** | 3-flash-prev | skincare | 100.0% | 0.0% | **0.0%** | 0.0% | 75.0% |

"B neutral" removes self-referential category queries ("best affordable K-beauty skincare in the US",
"best affordable C-beauty makeup in the US") — see §4.

**Tier A is 100% for all six cohorts.** Branded navigational demand is universally satisfied.

**The thesis holds for five of six.** Neutral unbranded mention is 0.0%–8.9% for every cohort except Anua.
Anua at 25.0% is a genuine exception, not noise.

### Anua vs Pixi — byte-identical queries, same model, same temperature

| Tier | Anua | Pixi | z | |
|---|---|---|---|---|
| A branded | 100.0% (24/24) | 100.0% (24/24) | 0.00 | not significant |
| **B unbranded** | **39.6% (19/48)** | **0.0% (0/48)** | **+4.87** | **SIGNIFICANT** |
| B neutral | 25.0% | 0.0% | +3.21 | SIGNIFICANT |
| C dupe | 0.0% (0/36) | 0.0% (0/36) | 0.00 | not significant |
| D compare | 75.0% (9/12) | 75.0% (9/12) | 0.00 | not significant |

**Pixi is absent from all 48 unbranded skincare responses.** Its queries are won by `ulta.com` (35×),
`target.com` (33×), `walmart.com` (19×), `sephora.com` (16×) — and by `anua.us` (3×). Pixi is a Target
hero brand that AI does not surface in the category its own products define.

This is the largest brand-level effect in the workstream, and it is the first evidence that
**selection performance is a brand property, not only a category property.**

---

## 2. The first real Intent-Capture leakage anyone here has measured

Destination class by tier, denominator = brand mentioned in that tier:

| | Anua | Pixi |
|---|---|---|
| A branded | **retailer 54% · official 46%** | **official 100%** |
| B unbranded | retailer 95% · official 5% | (no mentions) |
| D compare | retailer 78% · official 11% · non-official 11% | retailer 100% |

**On its own branded queries — "where to buy Anua Heartleaf 77 Soothing Toner" — Anua's buyer is sent to
a retailer 54% of the time.** Pixi, on the identical query shape, goes to its own site 100% of the time.

That is exactly what Official Destination Share is designed to catch, and it is the first instance in six
cohorts where the metric earns its place. It appears only once a brand has real US retail distribution —
which the C-beauty cohort did not.

### But the pooled metric is confounded by tier mix

Anua's pooled D3 official share is 25%; on branded queries alone it is 46%. The gap is entirely
composition: Tier B mentions route 95% to retailers, so a brand that *wins more category queries* scores
*worse* on Official Destination Share. **Official Destination Share must be reported per tier, or with the
tier mix pinned in the basis.** Pooled, it punishes selection success.

### And it is wrong by 13 points from one missing domain

Anua operates `anua.com` **and** `anua.us`. The classifier knew only `anua.com`, so 7 responses citing
`anua.us` were scored `retailer` (6) or `non_official` (1).

| | official | retailer | non-official |
|---|---|---|---|
| as classified | 25% | 73% | 2% |
| corrected for `anua.us` | **38%** | 62% | 0% |

A 13-point error from a single unlisted official domain, on the headline metric. This is P0 item 5
demonstrated on live data, not argued in the abstract.

---

## 3. What Anua wins and loses — the most actionable finding so far

At temperature 0 every neutral Tier B query is **3/3 or 0/3**. The brand either owns a query or is absent.
A percentage is a lossy summary of a discrete, nameable set.

**Wins (3/3):** best cleansing oil for blackheads under $25 · best double cleanse products for oily skin ·
TikTok viral skincare that's actually good

**Loses (0/3) — and sells a matching product in 8 of 9 cases:**

| Lost query | Product Anua sells |
|---|---|
| best affordable niacinamide serum | Niacinamide 10 TXA 4 Serum |
| best gentle exfoliating toner for sensitive skin | BHA 2% Gentle Exfoliating Toner |
| best budget ceramide moisturizer | 3 Ceramide Panthenol Moisture Barrier Cream |
| best affordable retinol alternative | Nano Retinol™ 0.3% + Niacin Renewing Serum |
| best hydrating toner for dehydrated skin | Azelaic 3 Cica Skin Clarifying Toner |
| best drugstore-price hyaluronic acid serum | 8 Hyaluronic Acid range |
| best pore minimizing toner under $25 | Azelaic 3 Cica Skin Clarifying Toner |
| best moisturizer for dry skin under $30 | 3 Ceramide Panthenol Moisture Barrier Cream |
| best vitamin C serum that isn't sticky | *(no matching product — a fair loss)* |

**Anua is known for formats and routines — cleansing oil, double cleanse, the K-beauty routine — and not
for ingredients.** It sells the ingredient products and is named for none of them. The queries are won by
`ulta.com`, `target.com`, `sephora.com`, `paulaschoice.com`, and by ingredient-led DTC brands whose
identity *is* the ingredient: `naturium.com`, `maelove.com`, `goodmolecules.com`, `theordinary.com`,
`theinkeylist.com`.

**This join — *products you sell* × *queries you lose* — produced eight concrete, evidenced, actionable
gaps in one pass.** Both inputs already exist: the catalog is in the Commerce Index, the queries are in the
basis. It is more useful than Official Destination Share has been in any cohort.

---

## 4. Query framing is a confound as strong as model or temperature

My skincare basis had **4 of 16** Tier B queries explicitly framed "korean"/"K-beauty"; the Joy makeup
basis had 1 of 16 framed "C-beauty". That handed Anua a home-field advantage:

| Query group | Anua mention |
|---|---|
| Korea-framed (4/16) | **83.3%** (10/12) |
| Neutral (12/16) | **25.0%** (9/36) |

z=+3.58, significant. The framing choice alone moved the headline **15 points**. Flower Knows shows the
same effect in reverse: 6.2% raw drops to **0.0%** once its one C-beauty-framed query is removed.

`prompt_basis` v3 already caps the *branded* share of a prompt set. It does not pin *category framing*.
Both belong in the versioned basis, alongside model id and temperature.

---

## 5. Destination shape confirms item 9

| Cohort | multi-host | zero-host | mean hosts |
|---|---|---|---|
| Flower Knows 2.5-flash | 50.0% | 16.7% | 1.77 |
| Flower Knows 3-flash-prev | 85.8% | 0.0% | 2.91 |
| Anua 3-flash-prev | **91.7%** | 0.0% | **3.27** |
| Pixi 3-flash-prev | **92.5%** | 0.0% | **3.23** |

Every current-generation cohort is ~86–93% multi-host at >2.9 destinations per response, and zero-host is
extinct. Primary-destination resolution is now the single highest-value unbuilt item; No-Destination is
confirmed dead across three independent brands.

---

## 6. Caveats

Two brands in this cohort; the skincare basis is new and unvalidated against a third. `anua.us` was found
by inspection, so other cohorts may carry the same unlisted-domain error in the other direction — the
Judydoll/Joocyee/Flower Knows official shares should be re-checked against a verified domain set before
they are quoted. Destination classes are assigned deterministically by the runner, not reviewed by hand.
Pixi's Tier B is a true zero (0/48), which makes its ratio formally infinite; treat "Pixi is absent" as the
finding rather than the ratio as a number.
