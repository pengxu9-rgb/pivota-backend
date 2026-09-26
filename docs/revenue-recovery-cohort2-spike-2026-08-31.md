# Second-cohort spike — Flower Knows, and a model-generation test

**Date:** 2026-08-31 · **Brand:** Flower Knows (`flowerknows.co`) · **Engine:** Gemini, grounded, Vertex + ADC on `pivota-prod`
**Basis:** the Joy run's Tier B/C queries **byte-identical**; Tier A/D brand-substituted. 40 queries × 3 runs = 120 responses per condition.
**Improvement over the Joy run:** Vertex grounding redirectors are resolved before classification, and every host is recorded (`all_hosts`), not just one.

Three conditions were run, each varying exactly one thing:

| Dir | Model | Temp | Isolates |
|---|---|---|---|
| `flower_knows_geo_2026-09-01` | 2.5-flash | 1 | (confounded first pass) |
| `flower_knows_geo_temp0` | 2.5-flash | **0** | brand, matched to Joy |
| `flower_knows_geo_g3` | **3-flash-preview** | 0 | **model generation** |

---

## 1. The headline finding is category-structural, and survives a model generation

Brand mention rate, temperature 0, identical unbranded queries:

| Tier | Flower Knows 2.5 | Flower Knows **3.0** | Judydoll 2.5 | Joocyee 2.5 |
|---|---|---|---|---|
| A branded navigational | **100.0%** (24/24) | **100.0%** (24/24) | **100.0%** (24/24) | **100.0%** (24/24) |
| B unbranded category | 14.6% (7/48) | 6.2% (3/48) | 6.2% (3/48) | 4.2% (2/48) |
| C dupe / alternative | 2.8% (1/36) | 8.3% (3/36) | 0.0% (0/36) | 0.0% (0/36) |
| D brand comparison | 91.7% (11/12) | 100.0% (12/12) | 100.0% (12/12) | 41.7% (5/12) |

**Branded-to-unbranded ratio:** Judydoll 16.0× · Joocyee 24.0× · Flower Knows 6.9× (2.5) · Flower Knows 16.0× (3.0).

No model-generation effect on any tier reaches significance (Tier B z=−1.34, C z=+1.03, D z=+1.02, A z=0.00).
**The product thesis holds across two brands' worth of competitors and two model generations.**

### Brand differences are not resolvable at this basis size
Flower Knows Tier B 14.6% vs Joy pooled 5.2% gives **z=+1.92** — just under the 1.96 threshold. That is
*underpowered*, not *equal*: 48 observations against 96 cannot resolve an effect this size. Same n-problem
that makes a +15 pt before/after claim unsupportable.

---

## 2. Two corrections to earlier analysis in this workstream

**Retracted — the Tier C "significant" result was a temperature artifact.** At temp 1 Flower Knows scored
8.3% (3/36) against Joy's 0.0%, z=+2.48. Temperature-matched at 0 it is 2.8% (1/36), z=+1.42. Not significant.

**Corrected — the Joy confidence interval was understated.** The Joy run uses `temperature: 0`
(`run_geo_baseline.js`). Its three runs are near-replicates, not independent samples, so pooling 131
brand-mentioned responses overstates the effective n. The true 95% interval lies between **±8.5 and
±14.6 pts**, not at ±8.5. Live web-search movement keeps effective n above one run's 44 — cross-run mention
stability measured 100% (Judydoll), 95% (Joocyee), 92.5% (Flower Knows @ t=0), 100% (Flower Knows @ 3.0).
This makes Rule 2 in the P0 cut **more** binding: §36's "+15 pts" may sit entirely inside the noise band.

---

## 3. The model generation changed destinations sharply — and it inverts one P0 priority

Flower Knows, temperature 0, identical queries. D3 denominator (brand mentioned): n=43 on 2.5, n=42 on 3.0.

| Measure | 2.5-flash | 3-flash-preview | z | |
|---|---|---|---|---|
| official | 58.1% (25) | 69.0% (29) | +1.04 | not significant |
| **no destination** | **20.9% (9)** | **0.0% (0)** | **−3.14** | **SIGNIFICANT** |
| retailer | 20.9% (9) | 26.2% (11) | +0.57 | not significant |
| non-official | 0.0% (0) | 4.8% (2) | +1.45 | not significant |
| **multi-host responses** (of 120) | **50.0% (60)** | **85.8% (103)** | **+5.95** | **SIGNIFICANT** |
| **zero-host responses** (of 120) | **16.7% (20)** | **0.0% (0)** | **−4.67** | **SIGNIFICANT** |
| mean resolvable hosts / response | 1.77 | 2.91 | | |

**Gemini 3 always gives a destination, and usually gives several.**

Two direct consequences for the P0 cut:

- **"No Destination Share" is a dying bucket.** It was 30.5% in the Joy corpus and 20.9% on Flower Knows @ 2.5
  — I called it the second-largest bucket and the highest-value cheapest gap. On the current model
  generation it is **0.0%**. Building the response-level row primarily to represent it is building for a
  behaviour that is disappearing.
- **Primary-destination resolution matters far more than measured.** Multi-host went 49.8% (Joy) → 85.8%
  (Gemini 3), at 2.91 hosts per response. Without an ordinal, 86% of responses are ambiguous about where
  intent actually lands.

---

## 4. Model pinning is drifting across the stack, unversioned

Reachable on Vertex, grounded, confirmed live: `gemini-2.5-flash`, `gemini-2.5-pro`, `gemini-2.5-flash-lite`,
`gemini-3-flash-preview`, `gemini-3.1-pro-preview`, `gemini-flash-latest`. Not reachable: `gemini-3-flash`,
`gemini-3-pro`, `gemini-3.0-*`, `gemini-pro-latest`.

| Where | Pinned to |
|---|---|
| `config/settings.py:295` `GEMINI_SYNTHESIS_MODEL` default | `gemini-2.5-flash` |
| `provider_registry.py` pricing/rates | `gemini-2.5-flash` |
| PIVOTA-Agent `GEMINI_PRIMARY_MODEL` | **`gemini-3-flash-preview`** |
| PIVOTA-Agent `GEMINI_UPGRADE_MODEL` | **`gemini-3.1-pro-preview`** |
| PIVOTA-Agent `TEMPORARY_UNIFIED_GEMINI_MODEL` | `gemini-2.5-flash` |

`prompt_basis` pins and versions the prompts; **nothing pins the provider model.** A model swap silently
rewrites every historical number and every before/after diff — precisely what §12 forbids. Section 3 above
is the proof: the same brand, same queries, one model generation apart, produces a materially different
destination distribution. The cost model is also attached to the wrong thing: the registry prices 2.5-flash
while the prober may call a Gemini 3 preview.

---

## 5. Caveats

One brand in the model-generation arm; `gemini-3-flash-preview` is a preview SKU whose behaviour may change
before GA. Destination classes are assigned by the runner's deterministic `classify()`, not reviewed by hand.
`n=42–48` per cell — adequate for the large effects reported as significant, inadequate for the brand
comparison, which is why that one is reported as unresolved rather than null.

## Reproduce

```bash
BRAND="Flower Knows" DOMAIN=flowerknows.co COLLECTION=best-sellers JOY_BASIS=1 \
RUNS=3 SKUS=8 TEMP=0 MODEL=gemini-3-flash-preview \
VERTEX_AI_ENABLED=true GOOGLE_CLOUD_PROJECT=pivota-prod GOOGLE_CLOUD_LOCATION=global \
node run_cohort2.js
```
