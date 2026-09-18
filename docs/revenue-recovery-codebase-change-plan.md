# Revenue Recovery — codebase change plan

**Date:** 2026-09-01 · Supersedes §L of [the judgment doc](revenue-recovery-migration-judgment-2026-08-31.md).
Grounded in [the 7-cohort evidence base](revenue-recovery-geo-evidence-base.md) (840 grounded responses)
and the [P0 cut, rev 4](revenue-recovery-p0-cut-revised-2026-08-31.md).

Every change below is justified by a measurement, not by the PRD alone.

---

## 0. The one-paragraph version

The handoff asks for "one authoritative evidence basis, multiple product projections." **That
architecture already exists** — `report_projections(audit_run_id, audience, payload_jsonb,
builder_version)` with five registered audiences. Revenue Recovery is a sixth audience, not a sixth
system. What does *not* exist is measurement integrity underneath it: the query classifier that the
headline metric depends on is **two incompatible implementations with a fail-branded default**, the
provider model is unpinned, and the official-domain set is inferred rather than verified — an error
measured at 21 points on live data. Fix the measurement, add one projection, and most of the product
falls out of code that is already written.

---

## A. Measurement integrity — nothing ships before this

### A1. Unify the query classifier and kill the fail-branded default `[MEASURED 2026-09-01 — sprint check 1]`

**Files:** [services/audit_facts.py:334](../services/audit_facts.py) ·
[services/agent_center_bd_report_service.py:5274](../services/agent_center_bd_report_service.py) (`_query_class_coverage`) ·
`:5300` (`_intent_axis_for`) · `:10668` (`_scan_mode_for_query_spec`) · `:10779` · `:10950` ·
[services/prompt_basis.py:145](../services/prompt_basis.py) ·
[services/sku_opportunity.py:1872](../services/sku_opportunity.py)
**Proof:** [tests/services/test_query_class_unbranded_axes.py](../tests/services/test_query_class_unbranded_axes.py)
— **17 red, 5 positive counterparts green** against current code. Untracked; do not commit red.

**The default fires — and it is worse than "defaults branded on a missing axis".**
`query_class_for_axis()` is a one-value whitelist: only the literal `"category"` returns
`category_discovery`; **every other value** returns `branded_navigational`, including `sidewalk`,
`attribute`, `head`, `problem`, `dupe`, `custom`, `unknown`, `""` and `None`. Measured at the consumer,
`_query_class_coverage` — whose docstring says it exists "so the report never conflates 'found when
shoppers name you' with 'found when shoppers ask the category question'" — counts all of those as
branded. It excludes `comparison` because "the coarse classifier would miscount them as branded",
patched one miscount, and left the rest. That count reaches `RunFacts.prompt_count_by_class`
(`audit_facts.py:581`) and the stamped report.

**The pipeline probes under one partition and reports under another.** `_scan_mode_for_query_spec`
routes each spec at run time via `_intent_axis_for(query, axis)` against
`_BRANDED_INTENTS = {navigational, trust}` — only `intent`/`identity`/`review` are branded; `sidewalk`,
`attribute`, `custom`, `category` and the default all run under `category_visibility_test`. The probe
is honest. `_query_class_coverage` then describes those same runs as branded. And `_citation_by_intent`
(`:5312`) already groups citation rate by the *good* partition — so the report carries the right
per-intent numbers in one function and the wrong headline split in another, from identical runs.

**Three `"intent"` stamp defaults mis-probe, not just mis-report.** A record with no axis is stamped
`"intent"` at `:10779`, `:10950` and `prompt_basis.py:145` *before* partitioning, and
`_scan_mode_for_query_spec(q, "intent")` selects the **branded** scan mode. So a missing-axis query is
probed as branded and reported as branded — consistent, and wrong if the query was unbranded.
Whether any production spec actually reaches those sites without an axis is **unmeasurable without DB
access**; every in-repo spec builder inspected sets one.

**Six classifiers, five vocabularies:**

| Function | Shape | Default |
|---|---|---|
| `audit_facts.query_class_for_axis` | binary | **branded** |
| `agent_center._intent_axis_for` (run-time partition) | 6 intents | `category_head` (unbranded) |
| `agent_center._sku_intelligence_ladder_layer` | 6 layers | `None` |
| `sku_opportunity._query_class` | 6 classes, text-aware | `attribute`/`category` |
| `sku_opportunity` `:297` | — | `"unknown"` |
| stamp sites `:10779` `:10950` `prompt_basis:145` | — | `"intent"` |

On the 280 real spike queries with axis unknown, the ladder files all 12 "cheaper alternative to X"
as `branded_consideration`, all 12 "affordable dupe for X" as `None`, and 31 of 32 unbranded category
queries as `None`. In the evidence, dupe queries are **0.0% mention in 6 of 7 cohorts**.

**Status: IMPLEMENTED 2026-09-01 — in the working tree, uncommitted.** Independently verified after
the implementing agent reported: 22/22 new tests green; 209 must-pass tests green; full suite
2 failed / 12,989 passed with both failures pre-existing and outside the diff
(`pdp_renderability.py:240`, untracked `scripts/audit_sig_pdp_force_fill.py:408`); my own mutant
check reproduced exactly 17 red / 5 green with the old body and restored byte-identical. Coverage
class now agrees with `_scan_mode_for_query_spec` for every axis tested, including `comparison`,
`price`, `brand`, `trust`, `navigational`, `unclassified` and `None`. Diff: `audit_facts.py` (+91/-?),
`agent_center_bd_report_service.py` (relocation + 2 stamp defaults), `prompt_basis.py` (1 default),
`tests/services/test_audit_facts.py` (2 fixtures gained the axis the pipeline really stamps; no
assertion text changed — they had been vacuous under the fail-branded default).

*Prod-visible effect:* no live generator emits `price`/`brand` or a bare `comparison` axis, so probe
behaviour is unchanged for current audits. Legacy stored reports carrying unstamped or bare-comparison
runs will show a lower branded count and higher category count **on re-render only** —
`report_projections` never silently re-renders, so this surfaces on explicit re-render or fresh runs.

*Follow-ups deliberately left (from the implementation):* (1) `dupe` still lands in `category_head`,
so the 0.0%-in-6-of-7 failure mode is pooled with genuine head terms in `_citation_by_intent` — a
distinct `dupe_alternative` class is the next classifier change; (2) `sku_opportunity._query_class`
treats `comparison` as branded while `audit_facts` now treats it as category — reconcile the three
remaining copies (`sku_opportunity`, `report_summary_builder._SOV_BRANDED_AXES`, the ladder) onto
`BRANDED_INTENTS`; (3) `_sku_intelligence_ladder_layer` untouched.

**Fix shape (as implemented):** the run-time partition is the truth — it is what ran. Move
`_intent_axis_for` + `_BRANDED_INTENTS` into `audit_facts` (the same relocation already done for
`QUERY_CLASS_*`, `:5264`; import direction verified safe) and make `run_query_class` derive from it,
so coverage and RunFacts agree with the scan mode by construction. Replace the three `"intent"` stamp
defaults with an explicit `unclassified` that partitions to discovery — the failure mode that does not
inflate the brand's own numbers. Extend `_CATEGORY_DEMAND_CLASSES`-style classes to name
`dupe_alternative` distinctly, since it is the most complete failure measured and currently shares a
bucket with comparison queries running 41.7–100%. Existing tests (38) pass unchanged; the locking
test at `test_agent_center_bd_per_sku.py:1912` feeds only `intent`/`review`/`brand`/`category`, so it
does not lock in the miscount.

### A2. Evidence states — add the four missing values

**File:** [db/audit_evidence.py:199](../db/audit_evidence.py)

`verification_runs.status` is `pending|claimed|succeeded|failed|exhausted_retries|blocked` — a work-queue
lifecycle. It has no `unverified`, `skipped`, `provider_failed` or `unparseable`. With the browser
commerce lane disarmed, "never ran" and "no row" are indistinguishable, which is exactly how a
projection reads absence as a pass.

### A3. Pin the basis: model, temperature, framing, tier mix

**Files:** [services/prompt_basis.py](../services/prompt_basis.py) · new `audit_basis` migration ·
[config/settings.py:295](../config/settings.py) ·
[services/llm_providers/provider_registry.py](../services/llm_providers/provider_registry.py)

`prompt_basis` pins and versions the *prompts*. Nothing pins the *model*, *temperature* or *query
framing* — and each was measured to move the headline:

| Unpinned variable | Measured effect |
|---|---|
| model generation | No-Destination 20.9% → **0.0%**; multi-host 50% → **85.8%** |
| temperature | produced a **false-positive** "significant" brand difference (z=+2.48 → +1.42) |
| query framing | **15 points** on Anua's headline (83.3% framed vs 25.0% neutral, z=+3.58) |
| tier mix | Anua official share 25% pooled vs **46%** branded-only, pure composition |

Model pinning is also drifting *across* the stack today: `config/settings.py:295` and
`provider_registry.py` say `gemini-2.5-flash`; PIVOTA-Agent's `GEMINI_PRIMARY_MODEL` says
`gemini-3-flash-preview` and `GEMINI_UPGRADE_MODEL` says `gemini-3.1-pro-preview`. The registry is
therefore **pricing the wrong model**.

---

## B. Destination truth

### B1. Verified, liveness-checked official-domain set `[proven wrong twice on live data]`

**File:** [services/brand_claim_service.py](../services/brand_claim_service.py) `merchant_owned_domains()`
· new migration · reuse [services/external_seed_destination_liveness.py](../services/external_seed_destination_liveness.py)

`merchant_owned_domains()` *infers* from onboarding `store_url`/`website` plus catalog
`source_domain`/`canonical_url`. Measured consequences, in both directions:

- **Understated:** Anua runs `anua.com` **and** `anua.us` (byte-identical, 904,225 bytes). Registering
  only one put branded official share at 46% instead of **67%** — a 21-point error on the headline.
- **Overstated:** `us.judydoll.com` was scored *official* and has **no DNS record**. Judydoll's 50% is
  too high.

The set must be (a) merchant-asserted or verified, not inferred, (b) multi-domain, (c) subdomain-aware,
and (d) **liveness-checked**. The liveness module already exists and already encodes the right rule —
*"`unverifiable` is a first-class outcome and it must never buy a retirement"* (213 of 286 hosts in its
own audit answered with Cloudflare challenges).

### B2. Wire the redirect resolver into the destination lane `[VERIFIED 2026-09-02 — ALREADY DONE; this item was wrong]`

**Files:** [services/grounding_redirect_resolver.py](../services/grounding_redirect_resolver.py) ·
[services/agent_center_bd_report_service.py:5212](../services/agent_center_bd_report_service.py) ·
[services/audit_facts.py:100](../services/audit_facts.py)

**Do not build this. It is already wired, and the number that motivated it was mine, not the pipeline's.**

`resolve_grounding_redirects_in_runs` is called at `load_per_sku_probe_runs:5212` — exactly what feeds
`build_authority_map` (`:13394` → `:13522`) → `authority_map.hosts[].evidence_urls` →
`citation_observations.evidence_url`. `load_per_sku_probe_runs` is the only source of runs on that path
(call sites `:7289`, `:13394`), `audit_evidence_builder` is the only writer of `citation_observations`, and
`AUDIT_RESOLVE_GROUNDING_REDIRECTS` defaults to `true`. Verified end to end against a stubbed 302: both
`grounding_sources[].uri` and `grounding_chunks[]` are rewritten in place.

**Two corrections to what this document previously claimed:**

1. *"100% of Gemini responses carry an unresolved redirector, so classifying before resolving discards the
   entire Gemini destination signal."* That 100% came from `scripts/geo_cohort_spike.js` — the scratch
   runner built for the cohort measurements, which does not resolve. It says nothing about Pivota's
   pipeline, which does. Conflating the measurement instrument with the system under measurement is
   precisely the error this document warns about elsewhere.
2. *"the module exists; this is wiring."* It was wired in the commit that introduced it.

**The host was never at risk regardless.** `_grounding_source_host` (`audit_facts.py:100`) derives the
publisher host from the chunk `title` when the URI is a redirector, so `cited_host` is correct with or
without HTTP resolution. Verified: a still-redirected source titled `oliveyoung.com` yields
`oliveyoung.com`; a redirector with **no** title yields `None`, so the citation is dropped rather than
mis-attributed to `vertexaisearch.cloud.google.com`.

**Residual — real, small, and degradation rather than a wiring gap.** When a redirect cannot be resolved
(network failure, or Google expiring the token, which the module's own docstring says happens "after a
while"), `evidence_url` keeps the opaque redirector while `cited_host` stays correct. A merchant clicking
that evidence link gets nothing. Worth a follow-up that drops an unresolvable redirector from
`evidence_urls` or records the failure. It does not block B3.

### B3. Primary commerce destination `[highest-value unbuilt item]`

**New:** `services/primary_destination.py` · columns on `citation_observations`

Current-generation cohorts are **85.8%–92.5% multi-host** at 2.91–3.28 hosts per response. Without an
ordinal, ~90% of responses are ambiguous about where intent lands. Deterministic and versioned — §12
forbids silent methodology change.

### B4. Destination class + claim on `citation_observations`

**Files:** [db/audit_evidence.py:437](../db/audit_evidence.py) ·
[services/audit_evidence_builder.py](../services/audit_evidence_builder.py) · new
`services/destination_classifier.py`

Add `destination_class`, `claimed_relationship`, `is_primary_destination`, `destination_confidence`.

**Do not modify `citation_role`.** `services/cited_host_classifier.py`'s findability-vs-endorsement axis
is consumed by `win_plan_builder`, `merchant_narrative_builder`, `outreach_outcomes` and
`independent_signals`. Repurposing it silently changes four live surfaces. The commerce axis is
orthogonal — a new column, not a redefinition.

### B5. Hallucinated-domain detector → Authority Gap, not the legal lane

**Files:** new detector · [services/bd_brand_signals.py](../services/bd_brand_signals.py) ·
[services/external_seed_destination_liveness.py](../services/external_seed_destination_liveness.py)

`judydoll.shop`, `joocyee.co`, `judydoll-joygroup.com` and `us.judydoll.com` have **no A record and no
CNAME** — engines invented them ("The official website for Judydoll is judydoll.shop"). That is weak
canonical identity, not impersonation: §18 Authority Gap and §19A **Generate Fix**, which is
auto-fixable, rather than §19B external action, which is not. A **live** brand-token domain is
impersonation; a **dead** one is hallucination — liveness is the discriminator.

`bd_brand_signals.py` already extracts Organization schema, `sameAs`, social handles, robots and
sitemap. Authority Gap is a rules-and-rendering job over signals that exist, not an extraction build.

---

## C. Product surface

### C1. Catalog × lost-query join `[ship first in this group]`

**New:** `services/selection_gap.py` — joins Commerce Index catalog against per-class lost queries.

For Anua this produced **eight concrete gaps in one pass**: it sells a Niacinamide 10 TXA 4 Serum and is
named in **0/3** responses for "best affordable niacinamide serum"; likewise its BHA exfoliating toner,
ceramide cream, retinol serum and HA range. Anua is known for formats and routines, not ingredients.

Both inputs exist today. This is more merchant-actionable than Official Destination Share was in any of
the seven cohorts.

**Report won/lost query lists, not a percentage.** At temperature 0 every neutral unbranded query
resolves **3/3 or 0/3** — a brand owns a query or is absent. "You lose *best affordable niacinamide
serum*" is actionable; "your unbranded visibility is 25%" is not.

### C2. Two new projection audiences

**Files:** [db/audit_evidence.py:250](../db/audit_evidence.py) `VALID_AUDIENCES` ·
[services/audit_projection_builder.py](../services/audit_projection_builder.py)

Add `revenue_recovery` (three stages, GET SELECTED leading, CONVERT SALES `UNVERIFIED`) and
`public_anonymous` (deterministic tier only). This is the whole of §24 — a builder, not a system.

### C3. Anonymous audit-run claim

**File:** [db/merchant_audit_runs.py:120](../db/merchant_audit_runs.py)

`merchant_id` is `nullable=False`. Make it nullable with `claimed_at`, copying migration 196's
claim-by-one-UPDATE pattern exactly — never duplicate the run.

### C4. Recovery Action lifecycle

**File:** [db/merchant_tasks.py:42](../db/merchant_tasks.py)

Add `ready_for_retest`, `verifying`, `verified`, `regressed` to `VALID_STATUSES`.

### C5. Copilot onto canonical evidence

**File:** [routes/merchant_audit_routes.py:3985](../routes/merchant_audit_routes.py)

`_build_ask_context` reads `report_jsonb`'s narrative. Point it at `readiness_findings` +
`evidence_items` + the new projection; add `selected_stage`, `probe_coverage`, `available_actions[]`.
The endpoint's guardrails (cross-tenant check, anti-invention prompt, metering, refund-on-failure) are
already right — keep them.

---

## D. What the evidence says NOT to build

| Item | Original plan | Evidence |
|---|---|---|
| Response-level row **for No-Destination** | "second-largest bucket, highest-value cheapest gap" | **0.0%** on current-generation models across three brands (was 30.5% on 2.5-flash). Keep the row for "brand never mentioned"; do not size work around a dead class. |
| Suspicious / impersonation surface | P0 detector + P1 surface | The domains **don't exist**. Reframed to B5 (Authority Gap). No case management, no takedown, no legal workflow. |
| GEO before/after diff | P0 | 95% CI is **±8.5–14.6 pts**; §36's "+15 pts" may sit entirely inside noise. Ship deterministic retest only, or budget ~1,384 responses/audit. |
| Anonymous GEO audit | implied P0 by §27 | ~1,384 grounded responses per credible audit. Deterministic tier instead. |
| Authorized / Trusted Destination Share | P0/P1 | **Zero** authorized destinations observed in 840 responses. |
| Editing `cited_host_classifier` in place | — | Four live surfaces depend on its current axis. |

---

## E. Sequence

```
A1 classifier unification ──┐  strictly sequential, backend-only
A2 evidence states ─────────┤
A3 basis pinning ───────────┘
        │
B1 official domains ──┬── B2 redirect wiring (independent, do early)
        │             │
B3 primary dest ──────┴── B4 destination class ── B5 hallucination detector
        │
C1 selection gap ── C2 projections ── C3 claim ── C4 actions ── C5 copilot
```

A1–A3 gate everything: each downstream number is computed through them. B2 is independent and can land
immediately. C1 is the earliest point at which a merchant sees something they can act on.

## F. Still unmeasured

All seven cohorts are beauty; no non-beauty vertical has been run (`fromourplace.com`,
`greatjonesgoods.com`, `marinelayer.com`, `everlane.com` are verified usable). Tier C's uniform 0.0%
across six of seven cohorts is unexplained. The browser commerce lane has produced zero production
observations, so every CONVERT SALES claim remains `UNVERIFIED` by construction.
