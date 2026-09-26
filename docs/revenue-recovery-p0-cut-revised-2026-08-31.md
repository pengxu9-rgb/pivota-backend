# Revised P0 Cut — Revenue Recovery

**Date:** 2026-08-31
**Supersedes:** §N and §O of [revenue-recovery-migration-judgment-2026-08-31.md](revenue-recovery-migration-judgment-2026-08-31.md)
**Evidence:** [revenue-recovery-joy-spike-2026-08-31.md](revenue-recovery-joy-spike-2026-08-31.md) plus a deeper pass over the 480 raw response bodies.


> **Revision 2 (2026-08-31, after the Flower Knows cohort + model-generation spike —
> [revenue-recovery-cohort2-spike-2026-08-31.md](revenue-recovery-cohort2-spike-2026-08-31.md)):**
> The headline holds and now has two brands' competitors and two model generations behind it.
> Four changes below: **item 7 demoted**, **item 9 promoted**, **item 4 expanded to pin provider models**,
> and **Rule 2's interval corrected upward**. One earlier claim retracted (Tier C significance was a
> temperature artifact).


> **Revision 3 (2026-08-31, after the Anua/Pixi skincare cohort —
> [revenue-recovery-cohort3-spike-2026-08-31.md](revenue-recovery-cohort3-spike-2026-08-31.md); 6 cohorts, 720 responses):**
> **New item 11a** — the catalog x lost-query join, now the highest-value finding in the workstream.
> **Item 5 proven on live data** (one unlisted official domain moved Anua's headline 13 points).
> **Item 9 reinforced** (86-93% multi-host on every current-generation cohort).
> **Item 4 expanded again** to pin query framing and tier mix.
> **New Rule 4** — Official Destination Share must be tier-stratified. And the headline is restated:
> the thesis holds for 5 of 6 brands, but selection is a *brand* property, not only a category one.


> **Revision 4 (2026-09-01, Medicube added — 7 cohorts, 840 responses; consolidated table in
> [revenue-recovery-geo-evidence-base.md](revenue-recovery-geo-evidence-base.md)):**
> Anua is confirmed the **exception, not the rule** (Medicube 2.8% neutral, z=+4.52 vs Anua) — 6 of 7
> cohorts sit at 0.0-8.9%. **Item 11 reframed**: the lookalike domains are *hallucinated*, not
> impersonations (no DNS record at all), so the detector feeds **Authority Gap / Generate Fix**, not the
> suspicious lane, and needs a liveness check to separate the two. **Item 5 proven twice more** —
> Anua's branded official share was understated 46%->67%, Judydoll's 50% is overstated by a dead domain.

---

## 0. What the numbers changed

The first P0 cut was written from code inspection. Four measurements moved items across
the P0 line in both directions.

| Measurement | Effect |
|---|---|
| **49.8%** of responses cite more than one resolvable host | **Primary destination (§12) → P0.** Was going to be deferred. Half the corpus is ambiguous without it. |
| **100%** of Gemini responses carry an unresolved Vertex redirector | **Redirect resolution → hard P0 dependency.** Half the corpus's destination data is opaque until it runs. |
| **4 live examples** of an engine calling a non-official domain "the official website" | **Destination claim (§14) → P0.** Cheapest, highest-value merchant evidence in the dataset. |
| Official Destination Share spans **14.4%–81.3%** by denominator; **±8.5 pts** noise at 3 runs | **Official Destination Share → demoted from north star to supporting distribution.** |

And the finding that reorders everything: **the merchant is found when named and invisible
when not.** Tier A branded queries mention the brand 94.8% of the time; unbranded category
queries 5.2%; dupe queries 0.0%. The capture funnel is comparatively healthy. The
selection funnel is the hole.

---

## 1. The P0 promise

> **You are found when someone names you, and invisible when they don't. Here is who is
> in the room instead — and here are the domains AI is calling your official store.**

Every clause is provable from evidence that already exists or is cheap to add. No clause
depends on the browser commerce lane, on dollar figures, or on a stage score.

---

## 2. P0 ship list

### Tier 1 — measurement integrity · nothing else ships without these

| # | Item | Why, in numbers |
|---|---|---|
| 1 | **Evidence states: add `unverified`, `skipped`, `provider_failed`, `unparseable`** | Unchanged from the first cut. With CONVERT SALES dark, "never ran" must be storable, not inferred from a missing row. |
| 2 | **Wire `grounding_redirect_resolver` into the destination lane, pre-classification** | 240/240 Gemini responses contain `vertexaisearch.cloud.google.com`. Classifying before resolving discards 50% of the corpus. Module already exists — this is wiring, not building. |
| 3 | **Freeze the D3 denominator as a versioned predicate** ("the response mentioned the brand") | The same evidence yields 14.4% or 81.3%. D1/D2 charge selection failure to the capture stage — the exact error that would have sent this roadmap the wrong way. |
| 4 | **Run-level `audit_basis` row** — providers, **exact model ids**, temperature, tier mix, D3 predicate version, `official_domains[]`, run count | The tier mix is a headline metric, so a drifting mix silently rewrites the headline. **Rev 2 — this is now demonstrated, not hypothetical:** same brand, same queries, one model generation apart changed No-Destination 20.9%→0.0% and multi-host 50%→86%. Meanwhile `config/settings.py:295` and `provider_registry.py` pin `gemini-2.5-flash` while PIVOTA-Agent pins `gemini-3-flash-preview`/`gemini-3.1-pro-preview`. `prompt_basis` versions the prompts; **nothing versions the model.** Temperature too — the Joy basis is `temperature: 0`, and a mismatch there produced a false positive in this very workstream. **Rev 3 — also pin query framing and tier mix.** Four of sixteen Korea-framed Tier-B queries moved Anua's headline **15 points** (83.3% on framed vs 25.0% on neutral, z=+3.58); Flower Knows drops 6.2% -> 0.0% when its one C-beauty-framed query is removed. `prompt_basis` v3 caps *branded* share but does not pin *category framing*. |
| 5 | **Verified official-domain set, subdomain-aware and multi-domain** | `us.judydoll.com` is official; `judydoll.shop` is not. **Rev 3 — measured, not argued:** Anua runs `anua.com` *and* `anua.us`; the classifier knew only the first, scoring 7 `anua.us` citations as retailer/non-official. Correcting one domain moved Anua's official share **25% -> 38%**. A 13-point error on the headline metric from a single unlisted domain. Items 8, 9 and 11 all depend on this. |

### Tier 2 — the product story

| # | Item | Why, in numbers |
|---|---|---|
| 6 | **Tier-stratified selection metric** — branded / unbranded / dupe mention rate | 94.8% vs 5.2% vs 0.0%. This is the headline. Largely free: `prompt_basis` v3 already generates and caps branded prompts (default 30%, floor 2), so the axis exists in the generator. |
| 7 | **Response-level observation row** — *demoted in rev 2; keep it, for "brand never mentioned", not for No-Destination* | **No-Destination is a dying bucket.** 30.5% in the Joy corpus and 20.9% on Flower Knows @ 2.5-flash, but **0.0% on `gemini-3-flash-preview`** (z=−3.14) — Gemini 3 always returns a destination. The row is still needed so "brand never mentioned" is representable (the Tier-B story lives there), but do not size the work around a class that is disappearing. |
| 8 | **Destination claim extraction (§14)** | Confirmed live: Gemini — *"The official website for Judydoll is judydoll.shop"*; ChatGPT — *"Joocyee's official website is joocyeebeauty.com"*; Gemini — *"Joocyee's official website for US shoppers is joocyee.co"*. All on Tier-A "official website" queries. 3.1% of brand-intent responses, on the single query where being wrong costs most. |
| 9 | **Primary commerce destination (§12)** — deterministic, versioned, persisted · **rev 2: highest-value item in Tier 2** | 49.8% of Joy responses cite >1 resolvable host — and on `gemini-3-flash-preview` that rises to **85.8%** (z=+5.95), at **2.91 hosts per response** vs 1.77. Without an ordinal, ~86% of current-generation responses are ambiguous about where intent lands. This item gets *more* valuable as models improve, which is the opposite of item 7. |
| 10 | **`destination_class`: official · retailer · other-brand · unknown · none** | Matches what is actually observable. `authorized` and `competitor` are **not** P0 classes — see §4. |
| 11a | **Catalog x lost-query join** — *new in rev 3; ship this first in Tier 2* | For Anua it produced **8 concrete gaps in one pass**: it sells a Niacinamide 10 TXA 4 Serum and is named in 0/3 responses for "best affordable niacinamide serum"; same for its BHA exfoliating toner, ceramide cream, retinol serum, HA range. Both inputs exist today — catalog in the Commerce Index, queries in the basis. More merchant-actionable than Official Destination Share has been in any of the six cohorts. |
| 11 | **Brand-token domain detector + liveness check** — *rev 4: reframed from "lookalike" to "hallucinated"* | `judydoll.shop`, `joocyee.co`, `judydoll-joygroup.com` and `us.judydoll.com` have **no A record and no CNAME** — the engines invented them ("The official website for Judydoll is judydoll.shop"). That is not impersonation: it is weak canonical identity, so it routes to **Authority Gap / Generate Fix** (§18/§19A), not the suspicious lane (§19B). A live lookalike is impersonation; a dead one is hallucination — the liveness check is what separates them, and [services/external_seed_destination_liveness.py](../services/external_seed_destination_liveness.py) already does it with the right `unverifiable`-is-first-class discipline. |

### Tier 3 — surfaces

| # | Item | Notes |
|---|---|---|
| 12 | **`revenue_recovery` projection**, three stages, GET SELECTED leading | New audience on the existing `report_projections`. CONVERT SALES renders `UNVERIFIED`, never a score. |
| 13 | **`public_anonymous` projection — deterministic tier only** | robots · sitemap · Product/Offer JSON-LD · PDP truth · UCP capability. All free to run, all already built, all real. No GEO for anonymous visitors (see §3). |
| 14 | **Anonymous audit-run claim** | `merchant_audit_runs.merchant_id` → nullable + claim-by-UPDATE, copying migration 196 exactly. |
| 15 | **Copilot repointed at canonical evidence** | `_build_ask_context` reads `report_jsonb` today; point it at findings + evidence + the projection. Four bounded actions. |
| 16 | **Retest — deterministic findings only** | Schema, sitemap, robots, PDP truth, lookalike domains. Binary, noise-free, re-checkable on demand. |

---

## 3. Three rules the numbers impose

**Rule 1 — no stage scores. Distributions only.**
A "CAPTURE INTENT 41%" implies a formula. Ours moves 5.6× on denominator choice. Ship
`Official 56% · No destination 31% · Other brand 8% · Retailer 5%`, which is
self-evidently honest and needs no formula defence.

**Rule 2 — every GEO number carries `n` and a confidence interval, and no improvement is
claimed below the noise floor.**
Three runs, no merchant change: 63.6% → 52.3% → 53.5%. An 11.3-point swing from nothing.
**Rev 2 correction — the interval was understated.** The Joy basis runs at `temperature: 0`, so its three
runs are near-replicates rather than independent samples and pooling 131 responses overstates the effective
n. The true 95% interval is between **±8.5 and ±14.6 pts**. §36's flagship *"+15 pts"* may sit **entirely
inside** the noise band, not just near its edge. Either budget ~1,384 probed responses
per audit (≈3.5× the Joy run) for a ±5 pt basis, or do not promise GEO before/after in P0.

**Rule 4 — Official Destination Share must be tier-stratified, never pooled.** *(new in rev 3)*
Anua's pooled official share is 25%; on branded queries alone it is 46%. The gap is pure composition —
Tier B mentions route 95% to retailers — so a brand that *wins more category queries* scores *worse*.
Pooled, the metric punishes selection success. Report per tier, or pin the tier mix in the basis.

**Rule 5 — report won/lost query lists, not just a rate.** *(new in rev 3)*
At temperature 0 every neutral Tier-B query resolves 3/3 or 0/3 — the brand owns a query or is absent.
A percentage is a lossy summary of a discrete, nameable set. "You lose *best affordable niacinamide serum*"
is actionable; "your unbranded visibility is 25%" is not.

**Rule 3 — CONVERT SALES renders as `UNVERIFIED`, never as a number.**
The receipt contract is complete; the worker is out-of-repo and ships `ARMED=false`. A
stage score over zero observations is a fabrication, and Rule 3 is only enforceable once
item 1 lands — which is why item 1 is first.

---

## 4. Moved out of P0

| Item | Was | Now | Reason |
|---|---|---|---|
| **Official Destination Share as north star** | P0 headline | P0 *supporting distribution* | 5.6× denominator spread; ±8.5 pt noise. Real, useful, not a headline. |
| **GEO before/after diff (§36)** | P0 | **P1**, or P0 labelled *Directional* with n + CI | Rule 2. Deterministic retest still ships. |
| **Authorized classification** | P1 | P1 confirmed | Zero observed in 480 responses. Needs a merchant-assertion surface that does not exist. |
| **Trusted Destination Share** | P1 | P1 confirmed | Depends on Authorized. |
| **Competitor Destination Exposure as a capture metric** | P1 | **P1, and re-framed** | Competitor hosts are pervasive corpus-wide (elf 97×, NYX 39×, Flower Knows 20×, Milani 18×) — but almost entirely in answers where the merchant is *not mentioned*. Under D3 the defensible figure is 23.7% co-presence. That is a **selection** story (item 6), not a capture metric. |
| **Revenue Leakage Case UI (§16)** | P1 | P1 confirmed | Pure projection; cheap once classes exist. |
| **Suspicious *product surface*** | P1 | P1 confirmed | Detector ships P0 (item 11); the case view, evidence pack and wording review do not. |
| **Browser commerce lane** | P1 | P1 confirmed | Arming is its own project. |
| **Anonymous GEO audit** | implied P0 by §27 | **cut** | ~1,384 grounded responses per credible audit. Not givable to a stranger. Item 13 replaces it. |

**Still not built at any phase:** fake-store management · Brand Protection suite · domain
security dashboard · takedown/DMCA · legal case management · trademark workflow · second
evidence store · second destination classifier · second catalog · `fake_store_*` schema.

---

## 5. Sequencing

Items 1–5 are backend-only and strictly sequential — each one is a precondition for the
next. Items 6–11 parallelize across two engineers once 1–5 land. Items 12–16 are frontend
and parallelize across the two portals.

```
week 1      1 evidence states ─┐
week 1      2 redirect wiring ─┤ (independent, do both)
week 2      3 D3 predicate ────┤
week 2      4 audit_basis ─────┤
week 2-3    5 official domains ┘
week 3-5    6 tier metric │ 7 response rows │ 9 primary dest │ 8 claim │ 10 class │ 11 lookalike
week 5-6    12 projection │ 13 public projection │ 14 claim endpoint
week 6-8    15 Copilot │ 16 deterministic retest │ portal surfaces
week 9-10   allowlist end-to-end
```

The 10-week date holds because the browser lane is out of scope and the anonymous tier is
deterministic. The schedule risk is item 5 — merchant domain identity is spread across six
sources today and is unified only at read time.

---

## 6. Revised definition of done

Replaces §40. For an allowlisted merchant:

**Marketing** — visit the page · enter a domain · a *deterministic* audit runs for real ·
truthful progress · a safe result naming at least one real finding · authenticate.

**Merchant portal** — open the same run · see three stages with GET SELECTED leading ·
see the branded-vs-unbranded split with `n` beside it · open CAPTURE INTENT · see the
destination distribution · open the claim contradiction if one exists · review evidence ·
see the lookalike domains · ask the Copilot to explain · receive a Recovery Action ·
mark it done · retest the deterministic findings · see a binary before/after.

**Two deliberate subtractions from §40:** no GEO before/after diff (Rule 2), and CONVERT
SALES carries no score (Rule 3).

---

## 7. What six cohorts settled, and what they did not

**Settled — Tier A is universal.** 100% branded mention in all six cohorts. Branded navigational demand is
satisfied everywhere; there is no product in "we'll make AI find you when they type your name".

**Settled — the unbranded gap is real for most brands.** Neutral Tier-B mention is 0.0%-8.9% for five of
six cohorts, across two verticals and two model generations.

**Overturned — it is not purely category-structural.** Anua reaches 25.0% neutral on the same queries where
Pixi scores 0.0% (z=+3.21). Selection is a *brand* property too. The rev-2 framing ("here is the size of
the gap, we cannot promise to close it") was too pessimistic: Anua proves the gap is closable, and §3 of
the cohort-3 doc shows exactly what closing it would look like.

**New — capture leakage is real, but only for brands with retail distribution.** Anua's own branded queries
route to a retailer 54% of the time; Pixi's go to its own site 100% of the time. The C-beauty cohort could
not have surfaced this. Official Destination Share earns its place here — tier-stratified, per Rule 4.

**Still open.** A non-beauty vertical: all six cohorts are beauty. Verified-usable storefronts for that test:
`fromourplace.com`, `greatjonesgoods.com`, `marinelayer.com`, `everlane.com`. And the makeup cohorts'
official shares should be re-checked against a verified domain set before quoting — the `anua.us` class of
error may be present there too, in either direction.
