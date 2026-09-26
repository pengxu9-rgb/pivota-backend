# Joy-cohort spike — what Official Destination Share actually looks like

**Date:** 2026-08-31 · **Dataset:** `~/dev/judydoll_joocyee_geo_2026-08-23/per_response_rows.jsonl`
**Basis:** 80 queries × 2 brands (Judydoll, Joocyee) × 2 engines (ChatGPT chat-latest, Gemini 2.5-flash) × 3 runs, live web search, US/USD, 2026-08-23.
**Rows:** 1,142 raw → **480 deduped** on `(brand, query_id, model, run)` — matches `measurement_status.json` and `expected_rows` exactly. 68 resume-duplicates carried conflicting classifications; last-write-wins.

Destinations in this dataset were hand-classified (`link_destination` ∈ official/retailer/non_official/none), so this measures **what the metric would say**, independent of whether our classifier can produce it yet.

---

## 1. The metric is not a number — it is a choice of denominator

| Denominator | Official Destination Share |
|---|---|
| D1 every probed response | **15.4%** (74/480) |
| D2 purchase-intent tiers A+B+C | **14.4%** (62/432) |
| D3 responses that mentioned the brand | **56.5%** (74/131) |
| D4 responses that gave any buying link | **81.3%** (74/91) |
| D5 mentioned **and** linked | **81.3%** (74/91) |

**5.6× spread on identical evidence.** Every one of these is a defensible reading of
§10's *"all valid brand purchase-intent AI responses."* Publishing this metric before
freezing the predicate is not a rounding risk — it is a different product per reading.

**Recommendation: freeze D3.** D1/D2 conflate "never selected" with "selected but
leaked" — they charge selection failure to the capture stage. D4/D5 are near-unfalsifiable
(once a link exists it is usually official). D3 answers the actual question: *when AI does
create intent for this brand, where does it send it?*

---

## 2. The leak is in SELECTION, not CAPTURE

| Tier | Query shape | Brand mentioned | Official share when mentioned |
|---|---|---|---|
| A | branded navigational — "where to buy Judydoll X", "official website" | **94.8%** (91/96) | **68.1%** (62/91) |
| B | unbranded category — "best contour palette under $15" | **5.2%** (10/192) | 0.0% (0/10) |
| C | dupe / alternative — "dupe for Charlotte Tilbury contour wand" | **0.0%** (0/144) | n/a |
| D | brand comparison — "Judydoll vs Flower Knows" | 62.5% (30/48) | 40.0% (12/30) |

When a shopper names the brand, AI finds it and sends them to the official store two times
in three. **That is not a broken capture funnel.** The failure is upstream: in the
unbranded discovery and dupe queries — where purchase decisions are actually made — the
brand is mentioned 5.2% and 0.0% of the time. There is almost no intent to capture because
almost none is created.

---

## 3. Destination distribution (D3, n=131)

| Class | Share |
|---|---|
| official | **56.5%** (74) |
| **none — no actionable destination** | **30.5%** (40) |
| non_official | 7.6% (10) |
| retailer | 5.3% (7) |
| authorized | 0 — not represented |
| competitor | 0 — none observed |

No Destination is the **second-largest bucket** and is exactly the class the current
pipeline cannot represent (rows exist only per cited host). Highest-value, lowest-cost gap.

Non-official destinations, in full: `joocyeebeauty.com` ×2, `judydoll.shop`,
`judydoll-joygroup.com`, `tesolife.com`, `chicdecent.com`, `joocyee.co`, `joocyee.us`,
`aliexpress.us`, `vertexaisearch.cloud.google.com`.
Retailer: `ulta.com` ×3, `yesstyle.com` ×2, `nordstrom.com` ×2.
Official: `judydoll.com` ×37, `joocyee.com` ×36, `us.judydoll.com` ×1.

Two incidental confirmations: an unresolved Vertex redirector leaked in as a "destination"
(the repo's `grounding_redirect_resolver.py` exists precisely for this), and
`us.judydoll.com` shows the official-domain set must handle subdomains.

---

## 4. The measurement cannot detect the improvement the PRD advertises

Same merchant, same basis, no change between runs:

| Run | Official Destination Share (D3) |
|---|---|
| 1 | 63.6% (28/44) |
| 2 | 52.3% (23/44) |
| 3 | 53.5% (23/43) |

**11.3-point spread from noise alone.**

| Basis | n | 95% CI | Min detectable change |
|---|---|---|---|
| 1 run | 44 | ±14.6 pts | ~20.5 pts |
| 3 runs (as measured) | 131 | ±8.5 pts | ~11.9 pts |
| 9 runs | 393 | ±4.9 pts | ~6.9 pts |

§36's flagship example — *"Before 28% → After 43%, +15 pts"* — sits barely outside the
noise floor of a 3-run basis. To reach ±5 pts you need **~378 brand-mentioned responses**,
which at this cohort's 27.3% mention rate is **~1,384 probed responses per audit** — about
3.5× this run, or ~231 distinct queries at 2 engines × 3 runs.

**This is the true cost driver of the retest loop, and it settles the anonymous-audit
question:** ~1,400 grounded responses is not something you give a stranger.

---

## 5. Suspicious / impersonation: real, small, and cheaply detectable

Brand-token domains outside the official set:

| Domain | n | Engines |
|---|---|---|
| joocyeebeauty.com | 2 | chatgpt |
| judydoll.shop | 1 | gemini |
| judydoll-joygroup.com | 1 | gemini |
| joocyee.co | 1 | gemini |
| joocyee.us | 1 | gemini |

**4.6% exposure** (6/131), 4 of 5 domains surfaced by Gemini only. Supports P1 deferral —
and shows the P0-cheap detector is "brand token present, domain not in verified official
set", not a threat-intelligence stack.

---

## 6. The PRD's illustrative dashboard does not match reality

| Bucket | §15 example | Joy cohort (D3) |
|---|---|---|
| Official | 28% | 56.5% |
| Authorized | 19% | not represented |
| Marketplace / third-party | 17% | 5.3% |
| Unknown | 9% | — |
| Suspicious | 7% | 4.6% |
| Competitor | 6% | 0% |
| No Destination | 14% | **30.5%** |

Designing the Capture Intent UI around §15's numbers yields a dashboard whose largest bar
is missing and whose second and third bars are empty.

---

## Caveat

n = 2 brands, one vertical (C-beauty), one market (US/USD), one date. This is the cohort
the PRD names as the P0 target, not a law about all merchants. A second cohort in a
different vertical would materially strengthen or overturn §2's tier conclusion.

## Reproduce

```bash
python3 - <<'PY'
import json, collections
rows=[json.loads(l) for l in open('per_response_rows.jsonl') if l.strip()]
seen={}
for r in rows: seen[(r['brand'],r['query_id'],r['model'],r['run'])]=r
D=list(seen.values())
den=[r for r in D if r['mentioned']=='Y']
print(collections.Counter(r.get('link_destination') or 'none' for r in den))
PY
```
