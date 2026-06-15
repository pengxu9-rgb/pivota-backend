# ADR-004: Per-SKU "Win-Plan" Layer for the Merchant AI-Readiness Audit

**Status:** Proposed · **Date:** 2026-06-15 · **Scope:** `pivota-backend` audit reporting layer (consumes Fix 1/2/3)

---

## Context

The audit now delivers an honest **diagnosis** — where a merchant stands with AI shopping
agents — via three shipped fixes:

- **Fix 1** resolves the real cited hosts behind Gemini's Vertex redirector
  (`_grounding_source_host`, `services/agent_center_bd_report_service.py:389`).
- **Fix 2** classifies every cited host *relative to the merchant* and splits **findability**
  (own site + own marketplace listing) from **endorsement** (independent recommendation),
  surfaced via `host_attribution_summary` (`build_authority_map`, line 5603).
- **Fix 3** narrates it (`build_merchant_narrative`, `services/merchant_narrative_builder.py:422`),
  incl. `where_youre_losing` + `who_ai_cites_instead`.

The causal model the audit must encode to be *actionable*:

> AI agents answer "best X" by **grounding** in independent web sources they retrieve, and
> recommend the SKUs that appear in those grounded sources — **never from the merchant's own
> listing**. Winning the recommendation = getting into the exact independent sources AI grounds
> on for the specific category queries you're losing, and being citable there.

The diagnosis holds all the raw pieces but **scatters them across three structures at two
aggregation levels**, and never synthesizes the recognizable per-SKU instruction:
*"To win 'best collagen cream': AI grounds that answer in byrdie + forbes + goodhousekeeping —
you're not in any of them. Get reviewed there (Forbes draft ready; the other two are
submission-only). Competitors Vital Proteins / Ancient Nutrition win today because they are."*

There is **no `win_plan` structure in the codebase today** (`grep -rn "win_plan" services/` →
empty). This is greenfield synthesis over existing signal, not a rebuild.

---

## Signal inventory — what already exists (have vs. must build)

### A. Losing category/discovery queries (per SKU) — **EXISTS, well-formed**
- `query_class_coverage` splits `branded_navigational` vs `category_discovery` off the probe
  axis (`_query_class_coverage`, line 4292; emitted at line 5021).
- `failing_prompts` (`_failing_prompts`, line 4334) gives the **exact failed query text**, its
  `axis`, the **raw `grounding_sources`**, and `competitors_named` (emitted at line 5022).
- `opportunity.per_prompt[]` + `build_where_you_can_win` (line 5110) already rank *winnable*
  lanes vs. structurally-lost head terms.

### B. Exact independent hosts AI grounds on **per category query** — **THE CRUX: lost at brand level, recoverable at SKU level**
- `build_authority_map` resolves each grounded host and tags `citation_role`,
  `recommendation_class`, `cited_on_category_query` (lines 5664–5728).
- **But host↔*specific-query* linkage is destroyed:** the per-host row accumulates query text
  into `row["_queries"]` only to dedup a count, then **pops it** (`row.pop("_queries", None)`,
  line 5746). What survives is the boolean "cited on *some* category query," not *which* one.
- **Recovery path exists at SKU level:** `failing_prompts[].grounding_sources` (line 4355)
  preserves per-query sources. A win-plan can reconstruct "for THIS query, AI cited THESE hosts"
  — but only from the SKU-level prompt rows, **not** from `authority_map`.
- **Constraint:** `failing_prompts[].grounding_sources` is the **raw redirector list**
  (`{uri: vertexaisearch…}`). The win-plan **must apply `_grounding_source_host()` itself**
  (line 389) or every host collapses onto `vertexaisearch.cloud.google.com`.

### C. The competitor who wins a given query (the benchmark) — **EXISTS at SKU/query level**
- `failing_prompts[].competitors_named` (line 4356) ties competitors to a **specific query** —
  the benchmark we need. `authority_map` is the *wrong* source (it fans competitors onto every
  host in the run, line 5707; Fix 3 already works around this at `merchant_narrative_builder.py:142`).

### D. Outreach machinery to reach a target host — **EXISTS but registry coverage is the binding constraint**
- `cited_host_classifier.classify_host` (line 374) returns `pitch_recipient`, `outreach_hint`,
  `editorial_cadence`, `expected_outreach_cycle_weeks`, `tier`, `ai_grounding_weight`.
- `audit_playbook_engine.select_playbooks` (line 604) + `_build_pitch_draft` (line 239) render a
  one-click `pitch_draft` — **only when** the playbook has a `pitch_template` AND the host has a
  `pitch_recipient.email`.
- **Measured registry coverage** (`data/cited_host_registry.json`, `data/playbooks.json`):
  63 hosts; `outreach_hint` on all 63; **only 6 have any `pitch_recipient`, only 2 a usable
  email** (`vetted@forbes.com`, `tips@nymag.com`); 25 playbooks, **4 carry a `pitch_template`**.

**Verdict:** the machinery is real and one-click for a small curated subset; for the *typical*
cited endorsement host we have a target + `outreach_hint` + cycle estimate but NOT a draft-ready
recipient. **This is surfaced as a stated limit, never hidden.**

---

## Gaps — what must be built

1. **Per-query host/competitor linkage must be re-derived** (B, C). Genuinely lost in
   `build_authority_map`. The win-plan re-derives `(query → resolved grounded endorsement hosts
   + competitors)` from per-SKU `failing_prompts` (applying `_grounding_source_host`, filtering
   to endorsement roles via the Fix-2 classifier — reuse `merchant_relative_role` /
   `is_endorsement_role`, never re-implement). No fabrication: all inputs are real grounded sources.
2. **Endorsement-host outreach is curated-subset only** (D). Degrade per-host to one of three
   honest states: `draft_ready` (email) → `submission_only` (submission_url) → `target_only`
   (host known, no recipient yet). A content/registry gap (BD-owned), expressed honestly.
3. **No signal produced without grounding.** Competitor benchmarking fires only when
   `competitors_named` is non-empty for that query; otherwise state the limit. `is_competitor`
   (brand-storefront) rarely fires (no `brand`-typed hosts in registry) → benchmark comes from
   grounded `competitors_named` text, not competitor-role hosts.
4. **Identity-dependence.** Build on a run with populated `merchant_host`/`merchant_brand`
   (orchestrator passes these, line 8451) or own-site findability under-detects.
5. **Findability is never a win** (inherited no-inflation). `findability_hosts` (own site, own
   marketplace listing) are excluded from "where AI grounds" — same exclusion
   `who_ai_cites_instead` enforces (`merchant_narrative_builder.py:120`).

---

## Proposed design

A new pure, DB-free `services/win_plan_builder.py` (mirroring `merchant_narrative_builder.py`'s
"no probes, no DB" contract), consuming `per_sku_reports` + `authority_map`, called from the
orchestrator right after `build_merchant_narrative` (line 8505), wrapped best-effort
(`try/except → None`), passed `merchant_host`/`merchant_brand`.

Produces a `win_plan` section, one entry per losing **category** query per SKU:

```
win_plan: {
  available, note,
  sku_plans: [{
    sku_key, sku_title,
    losing_queries: [{
      query: "best collagen cream", axis: "category",
      grounds_in: [{ host, role, tier,
                     outreach: { state: draft_ready|submission_only|target_only,
                                 hint, cycle_weeks, pitch_draft|submission_url } }],
      competitor_benchmark: ["Vital Proteins", "Ancient Nutrition"],   # grounded, this query
      win_condition: "Get cited in byrdie.com / forbes.com … for \"best collagen cream\".",
      limit: null | "no draft-ready recipient for byrdie.com yet",
    }],
    coverage: { queries_total, queries_with_grounded_target, queries_draft_ready }
  }]
}
```

**Signals consumed (all existing):** `failing_prompts` (losing queries + raw sources +
competitors), `_grounding_source_host` (host resolution), `merchant_relative_role` /
`is_endorsement_role` (role filter), `classify_host` (tier/cadence/outreach), `select_playbooks`
(reuse the host's existing `pitch_draft`, keyed by query — do not re-generate).

**New logic:** the `(query → grounded endorsement hosts + competitors)` re-derivation; a per-host
`outreach.state` resolver; a `win_condition`/`limit` assembler.

**Surfacing (both):** a top-level `win_plan` section + a `where_youre_losing.win_plan_summary`
rollup in Fix 3 ("losing N category queries; AI grounds them in M independent hosts; K have
draft-ready outreach"), keeping the narrative the single entry point.

**Guardrails inherited:** no fabrication (unknown recipient → explicit state + limit, never an
invented email); no inflation (findability excluded; a SKU surfacing only via own listing yields
no target and says so).

---

## Real-data example (reproduced through deployed `build_authority_map` + `classify_host`)

Aruen tofu collagen jelly cream — category query "best collagen", grounded in
goodhousekeeping/byrdie/forbes, competitors Vital Proteins / Ancient Nutrition:

```
findability_hosts:          [aruen.us, ebay.com]            # own site + own listing — NOT targets
endorsement_category_hosts: [goodhousekeeping.com, byrdie.com, forbes.com]

registry coverage of the cited hosts (category=beauty):
  forbes.com           tier=1  email=vetted@forbes.com  cycle=[4,8]   → draft_ready    ✅ one-click
  goodhousekeeping.com tier=3  email=None, submission_url=present       → submission_only ⚠️ limit
  byrdie.com           tier=1  pitch_recipient=None                     → target_only     ⚠️ limit
```

**What the win-plan says:** *"To win 'best collagen cream' (you're losing it): AI grounds that
answer in byrdie.com, forbes.com, goodhousekeeping.com — you're cited in none. Competitors Vital
Proteins / Ancient Nutrition win because they are. Forbes: draft ready (one-click, 4–8 wks).
Byrdie: highest-tier target, **no recipient yet**. Good Housekeeping: submission form only."*

**Where the data falls short (honest):** the specific query↔host pairing had to be re-derived
(`authority_map` only knows "some category query"); outreach is one-click for **1 of 3** cited
hosts, and the *most valuable* target (byrdie, tier-1) is the *least* actionable — a registry/BD
gap the win-plan exposes rather than papers over.

---

## Phased build plan

1. **Re-derivation core (no new content):** `win_plan_builder.py` produces per-SKU
   `losing_queries` with redirector-resolved `grounds_in` (endorsement only) +
   `competitor_benchmark`, from `failing_prompts`. Pure, best-effort, no orchestrator change yet.
2. **Outreach state + drafts + surfacing:** wire each `grounds_in` host to its `select_playbooks`
   draft/recipient, resolve `outreach.state`, add `coverage`, surface `win_plan` +
   `where_youre_losing.win_plan_summary` from the orchestrator.
3. **Registry coverage uplift (BD-owned, parallel):** backfill `pitch_recipient` + cadence for the
   high-tier hosts that actually get cited (byrdie, allure, elle, …). `coverage.queries_draft_ready`
   becomes the metric tracking this.
4. **Movement (optional):** tie won/lost-per-query via the existing re-audit machinery
   (`attach_niche_movement`, line 5230) so the merchant sees whether a pitched host started
   grounding their SKU.

---

## Open questions

1. **Query-grain in `authority_map`, or keep re-derivation?** Cleaner long-term: stop popping
   `_queries` (line 5746), emit `cited_on_queries: [...]` per host. Small additive change, but
   touches the Fix-2 contract Fix 3 depends on — defer unless re-derivation proves lossy.
2. **Host ranking in `grounds_in`:** tier-then-cited (highest-leverage target leads even at 1
   cite) vs. raw `prompts_cited_count`. Recommend tier-then-cited.
3. **`target_only` honesty vs. usefulness:** naming the right target with no recipient is honest
   but half-actionable. Recommend keep — pairs with the Phase-3 coverage metric.
4. **Winnable-lane cross-reference:** intersect losing queries with `build_where_you_can_win`
   (demand + attribute fit + no owner) to prioritize *winnable* losses over structurally-lost
   head terms. Likely yes — the difference between a plan and a list.
