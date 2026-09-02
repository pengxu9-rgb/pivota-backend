# Revenue Recovery — consolidated GEO evidence base

**Last updated:** 2026-09-01 · **7 cohorts · 840 grounded responses · all temperature 0**
Detail lives in the per-spike docs: [Joy](revenue-recovery-joy-spike-2026-08-31.md) ·
[cohort 2 / model generation](revenue-recovery-cohort2-spike-2026-08-31.md) ·
[cohort 3 / skincare](revenue-recovery-cohort3-spike-2026-08-31.md).
Runner: [scripts/geo_cohort_spike.js](../scripts/geo_cohort_spike.js) · comparison:
[scripts/geo_cohort_compare.py](../scripts/geo_cohort_compare.py)

---

## The table

| Brand | Model | Basis | A branded | B raw | **B neutral** | C dupe | D compare | ratio A:Bn |
|---|---|---|---|---|---|---|---|---|
| Judydoll | 2.5-flash | makeup | 100.0% | 6.2% | **6.2%** | 0.0% | 100.0% | 16.0× |
| Joocyee | 2.5-flash | makeup | 100.0% | 4.2% | **4.2%** | 0.0% | 41.7% | 24.0× |
| Flower Knows | 2.5-flash | makeup | 100.0% | 14.6% | **8.9%** | 2.8% | 91.7% | 11.2× |
| Flower Knows | 3-flash-prev | makeup | 100.0% | 6.2% | **0.0%** | 8.3% | 100.0% | ∞ |
| **Anua** | 3-flash-prev | skincare | 100.0% | 39.6% | **25.0%** | 0.0% | 75.0% | 4.0× |
| Pixi | 3-flash-prev | skincare | 100.0% | 0.0% | **0.0%** | 0.0% | 75.0% | ∞ |
| Medicube | 3-flash-prev | skincare | 100.0% | 2.1% | **2.8%** | 0.0% | 75.0% | 36.0× |

"B neutral" excludes self-referential category queries ("best affordable K-beauty skincare in the US").
Raw and neutral are only comparable *within* a basis; the ratio is the cross-basis metric.

## What is established

**1. Tier A is universal — 100% in all seven cohorts.** Branded navigational demand is satisfied
everywhere, on both model generations. There is no product in "we'll make AI find you when they type
your name."

**2. The unbranded gap is real and large for six of seven.** Neutral Tier-B mention is 0.0%–8.9% for
every cohort except Anua, across two verticals, three sub-segments and two model generations.

**3. It is not purely category-structural — but Anua is the exception, not the rule.** Within one
byte-identical skincare basis: Anua 25.0%, Medicube 2.8%, Pixi 0.0%. Anua vs Medicube z=+4.52,
Anua vs Pixi z=+4.87, both significant; Pixi vs Medicube z=−1.01, not significant. Two K-beauty brands
on identical queries differ by an order of magnitude, so **selection is a brand property** — but the
achievable state is rare.

**4. Dupe queries (Tier C) are near-dead for everyone.** 0.0% in six of seven cohorts.

**5. Current-generation destination behaviour is uniform.** Every `gemini-3-flash-preview` cohort is
85.8%–92.5% multi-host at 2.91–3.28 hosts per response, with **zero-host extinct** (0.0% in all three,
vs 16.7% on 2.5-flash). No-Destination is dead; primary-destination resolution is now the highest-value
unbuilt item.

## Branded-only official destination share

Pooled official share is confounded by tier mix (unbranded mentions route overwhelmingly to retailers),
so the clean cross-cohort comparison is branded queries only:

| Cohort | branded-only official |
|---|---|
| Flower Knows 3-flash-prev · Pixi · Medicube | **100%** |
| Flower Knows 2.5-flash | 83% |
| Joocyee | 71% |
| Anua *(corrected)* | 67% |
| Judydoll | 50% *(itself overstated — see below)* |

Anua is the only cohort where a meaningful share of **branded** intent leaves for a retailer (33%).
Medicube — also K-beauty, also US-distributed — is 100%, so retail distribution alone does not cause
the leak.

## Corrections applied

| What | Effect |
|---|---|
| **Temperature mismatch.** The Joy basis runs `temperature: 0`; the first Flower Knows pass ran at 1. | Retracted a "significant" Tier C brand difference (z=+2.48 → +1.42). |
| **Effective n.** At temp 0 the three runs are near-replicates, not independent samples. | Joy 95% CI is **±8.5 to ±14.6 pts**, not ±8.5. §36's "+15 pts" may sit entirely inside noise. |
| **Query framing.** 4 of 16 skincare Tier-B queries were Korea-framed vs 1 of 16 C-beauty-framed in the makeup basis. | Moved Anua's headline **15 points** (83.3% framed vs 25.0% neutral, z=+3.58). Flower Knows drops 6.2%→0.0% on the same correction. |
| **Official domain set — understated.** Anua runs `anua.com` **and** `anua.us`, serving byte-identical pages (904,225 bytes). | Pooled official 25%→**38%**; branded-only 46%→**67%**. A 21-point error on the branded figure. |
| **Official domain set — overstated.** `us.judydoll.com` was scored official but has **no DNS record**. | Judydoll's 50% is itself too high. |

Verified single-domain, unaffected: Joocyee, Flower Knows, Pixi. Medicube was run with both
`medicube.us` and `medicube.com` registered from the start.

## The hallucinated-domain finding

Four brand-token domains an engine recommended have **no A record and no CNAME** — not blocked, not
challenged, simply nonexistent:

| Domain | What an engine said |
|---|---|
| `judydoll.shop` | Gemini: *"The official website for Judydoll is judydoll.shop"* |
| `joocyee.co` | Gemini: *"Joocyee's official website for US shoppers is joocyee.co"* |
| `judydoll-joygroup.com` | — |
| `us.judydoll.com` | scored **official** in the Joy data |

This is **not impersonation**. The brand's canonical identity is weak enough that the model invents a
plausible official domain, and every buyer who follows it is lost. That makes it §18 Authority Gap and
§19A **Generate Fix** (assert the domain in machine-readable form), not §19B external action — a
better story and an auto-fixable one.

Telling the two apart requires a liveness check. [services/external_seed_destination_liveness.py](../services/external_seed_destination_liveness.py)
already does this and already encodes the necessary discipline — *"`unverifiable` is a first-class
outcome and it must never buy a retirement"* (213 of 286 hosts in its audit answered with Cloudflare
challenges). Reuse, not build.

## The most actionable output

At temperature 0 every neutral Tier-B query resolves **3/3 or 0/3** — a brand owns a query or is absent.
A percentage is a lossy summary of a nameable set. Joining *products you sell* × *queries you lose* gave
Anua eight concrete gaps in one pass: it sells a Niacinamide 10 TXA 4 Serum and is named in 0/3
responses for "best affordable niacinamide serum"; likewise its BHA exfoliating toner, ceramide cream,
retinol serum and HA range. **Anua is known for formats and routines, not ingredients.** Both inputs
already exist — catalog in the Commerce Index, queries in the basis.

## Open

All seven cohorts are beauty. The non-beauty test is unrun; verified-usable storefronts:
`fromourplace.com`, `greatjonesgoods.com`, `marinelayer.com`, `everlane.com`. Judydoll's official share
should be re-scored against a liveness-checked domain set. Tier C's uniform 0% has not been explained.
