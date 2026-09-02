# Revenue Recovery — Repository Judgment & Migration Plan

**Date:** 2026-08-31
**Against:** `~/Desktop/revenue_recovery_handoff.md` (Unified Handoff v3)
**Repos inspected:** `pivota-backend-quality-gate` (backend), `pivota-marketing`, `pivota-merchants-portal`
**Status:** inspection only — no code written.

---

## A. Executive Judgment

### A1. Does the three-stage merchant model fit the current architecture?

**Yes, and better than the PRD assumes.** The backend already has a first-class
*audience projection* layer: `report_projections(audit_run_id, audience, payload_jsonb,
builder_version)` with five registered audiences and a builder that reads the canonical
tables and renders per-audience shapes ([db/audit_evidence.py:416](db/audit_evidence.py#L416),
[services/audit_projection_builder.py](services/audit_projection_builder.py)).

`GET SELECTED / CAPTURE INTENT / CONVERT SALES` is exactly a sixth audience. The PRD's
"one authoritative evidence basis, multiple product projections" principle is not a
migration target here — **it is the shipped architecture**. This is the single most
important finding of the audit.

### A2. Can Destination Leakage live inside Capture Intent without a new product stack?

**Yes for the plumbing. No for the classifier.**

`citation_observations` already stores one row per `(audit_run, content_key, provider,
query, cited_host)` with `host_type`, `citation_role`, `first_party`, `is_competitor`,
`evidence_url` ([db/audit_evidence.py:437](db/audit_evidence.py#L437)). That is the
destination observation table the PRD asks for, already relational, already
content-key-keyed, already written on every audit.

But the existing classifier answers a **different question**. `merchant_relative_role()`
([services/cited_host_classifier.py:626](services/cited_host_classifier.py#L626)) sorts hosts
onto a *findability vs endorsement* axis — "who is talking about me". The PRD needs a
*commerce destination* axis — "where does purchase intent land". These are not the same
cut, and the existing roles collapse exactly the distinctions the PRD's north-star metric
depends on:

| PRD class | Today |
|---|---|
| Official | `own_domain` — usable |
| **Authorized** | collapses into `marketplace_self_listing` |
| Marketplace / Third-Party | `marketplace_self_listing` — same bucket as Authorized |
| Competitor | `competitor` — usable |
| Unknown / Unverified | `unclassified` — usable |
| **Suspicious / Impersonation** | **does not exist** |
| **No Destination** | **not representable** (rows only exist per cited host) |

Extend this classifier; do not build a second one. But be honest in planning that
"extend" here means a new orthogonal axis, not three new enum values.

### A3. Can current infrastructure support Official Destination Share?

**Numerator: yes. Denominator: no. Primary-destination selection: no.**

- **Numerator** — `first_party` on `citation_observations`, derived from
  `merchant_owned_domains()` ([services/brand_claim_service.py](services/brand_claim_service.py)),
  which folds onboarding `store_url`/`website` plus catalog `source_domain`/`canonical_url`.
- **Denominator** — the PRD's denominator is *"all valid brand purchase-intent AI
  responses."* The repo has no purchase-intent classification of a response. There is
  `query_class` and `axis` on the observation, and `services/query_semantic_class.py`,
  but no `is_purchase_intent` predicate. Without it, Official Destination Share has no
  defensible denominator and the number is not publishable.
- **Primary destination** — `citation_observations` is flat. Every cited host in a
  response is one equal row. There is no `all_destination_mentions[]` vs
  `primary_commerce_destination` distinction (§12), and no ordinal at all.

### A4. The 3–5 largest gaps

1. **No purchase-intent response classification.** Blocks the north-star metric's
   denominator, and therefore blocks §10, §11, §15, §17, §29, §36 as *numbers*.
2. **No primary-commerce-destination resolution (§12).** Without an ordinal or a
   buy-language signal, "AI sent buyers to X" is unprovable; you only know "X was cited".
3. **Anonymous audit runs are not claimable.** `execution_routes.merchant_id` is
   deliberately nullable and claimable by one UPDATE
   ([db/migrations/196_store_audit_execution_routes.sql](db/migrations/196_store_audit_execution_routes.sql)) —
   the PRD's §30 pattern, already correct. But `merchant_audit_runs.merchant_id` is
   `nullable=False` ([db/merchant_audit_runs.py:120](db/merchant_audit_runs.py#L120)).
   Only the thin UCP-probe lane can be anonymous today; the full audit cannot.
4. **The evidence-state model is a work-queue lifecycle, not the PRD's honesty model.**
   `verification_runs.status` is `pending|claimed|succeeded|failed|exhausted_retries|blocked`
   ([db/audit_evidence.py:199](db/audit_evidence.py#L199)). It has `blocked` (good) but
   **no `unverified`** — the single state the PRD's §23 leans on hardest ("payment not
   attempted → UNVERIFIED"). Today "we didn't try" and "there is no row" are the same
   thing, which is exactly how a projection silently reads absence as pass.
5. **The Convert Sales worker is out-of-repo and disarmed.** The receipt contract is
   excellent and complete — platform, checkout route status, `challenge_stage`, cart
   status/price/currency, `outcome_code: challenge`
   ([routes/store_audit_commerce_probe_internal.py:74](routes/store_audit_commerce_probe_internal.py#L74)).
   The worker that produces it is a separate node image (`store-audit-browser`,
   `scripts/run_store_audit_commerce_worker.js`) and every flag ships
   `false`/`ARMED=false` ([infra/gcp/setup_store_audit_commerce_jobs.sh:68](infra/gcp/setup_store_audit_commerce_jobs.sh#L68),
   [infra/gcp/deploy_backend.sh:32](infra/gcp/deploy_backend.sh#L32)).
   **CONVERT SALES has never produced a production observation.** No amount of backend
   work changes that.

### A5. Is the 10-week beta credible?

**Weeks 8–10 as scoped: yes. Weeks 1–7 as assumed-already-done: no.**

The PRD says "Weeks 1–7 remain substantially the existing Codex engineering plan," i.e.
treats the evidence pipeline as built. Weeks 2–3 (canonical GEO + destination path) and
weeks 4–5 (static store audit + PDP truth) are genuinely ~80% there. **Week 6–7 (safe
browser commerce + UCP) is the schedule risk**: the contract exists, the worker is
external, and the lane has never been armed. Arming a browser-based cart/checkout prober
against real merchant storefronts is a multi-week safety, SSRF, politeness, and
false-positive problem — the repo already treats it that way (dedicated crawl subnet,
dedicated SA, redaction validators, lease-based claims, disarmed by default). That
caution is correct and should not be traded away for the beta date.

**Recommendation:** hold the 10-week date, and cut CONVERT SALES in P0 to what the
already-armed lanes can prove (UCP capability + PDP/Offer truth + sitemap/robots
readiness), marking cart and checkout `UNVERIFIED` rather than shipping an unarmed lane.

### A6. What should change in the PRD based on actual code?

1. **§24 should not invent a new projection type.** Add `revenue_recovery` and
   `public_anonymous` to `VALID_AUDIENCES` and write two builders. The PRD's
   `RevenueRecoveryProjection` shape maps 1:1 onto `report_projections.payload_jsonb`.
   Delete the implication that this is new infrastructure.
2. **§22 `audit_basis_v1` is 70% built and the PRD doesn't know it.**
   [services/prompt_basis.py](services/prompt_basis.py) already implements pinned,
   versioned, replay-stable prompt sets: `PROMPT_BASIS_VERSION`, `prompt_set_id`,
   `build_selected_set_id()` over the exact probed specs, explicit-refresh-only
   regeneration, per-tier isolation ("a deep run never reuses a standard basis"). The
   PRD should say *extend `prompt_basis` into a first-class `audit_basis` row* — adding
   providers/models, market, language, currency, `official_domains[]`, probe versions —
   not "build audit basis".
3. **§23's state model must be reconciled explicitly, not assumed.** The PRD's seven
   states and the repo's six are different vocabularies at different layers
   (`evidence_items.evidence_level` = `detected|tested` is a third). The PRD should
   specify the mapping, and specifically where `UNVERIFIED` is *stored* rather than
   inferred from a missing row.
4. **§10's definition needs a denominator spec.** "Valid brand purchase-intent AI
   responses" must become a named, versioned predicate with its own test corpus, or the
   metric will silently change meaning between runs — which §12 explicitly forbids.
5. **§18 is more built than the PRD credits.** [services/bd_brand_signals.py](services/bd_brand_signals.py)
   (2,092 lines) already extracts Organization schema, `sameAs`, social handles, robots
   directives, sitemap structure, an SEO-completeness score, plus grounded retail-presence
   and corporate-identity inference. Authority Gap is largely a *rules-and-rendering*
   job over existing signals, not an extraction job.
6. **§17's guardrail is already the house style.** [routes/store_audit_public_intake.py:20](routes/store_audit_public_intake.py#L20)
   already refuses to let a blocked probe buy a negative claim ("cannot-verify must not
   buy a negative claim"). Cite this as precedent rather than introducing it as new policy.

---

## B. Current Architecture Map

```
                       ┌──────────────────── MARKETING (pivota-marketing) ────────────────────┐
                       │  /ai-readiness  →  AuditUrlCaptureForm                               │
                       │       └─ lib/store-audit-teaser.ts                                   │
                       └──────────────────────────┬──────────────────────────────────────────┘
                                                  │ POST /public/store-audit/intake
                                                  │ GET  /public/store-audit/teaser
                                                  ▼
  ┌────────────────────────────────── BACKEND (pivota-backend-quality-gate) ─────────────────────────────────┐
  │                                                                                                          │
  │  INTAKE            routes/store_audit_public_intake.py     (anonymous, domain-keyed, flag-gated)          │
  │                    routes/merchant_audit_routes.py         (authenticated: /ai-commerce-readiness,        │
  │                                                             /url-readiness, /ask, /tasks, /share)         │
  │                    routes/audit_runs_routes.py             (canonical: GET /{run_id}?audience=…)          │
  │                                                                                                          │
  │  RUN LIFECYCLE     db/merchant_audit_runs.py               queued→discovering→probing→scoring→            │
  │                                                            materializing→verifying→completed             │
  │                    services/audit_run_worker.py            services/audit_scheduler.py                   │
  │                                                                                                          │
  │  MEASUREMENT       services/prompt_basis.py                pinned prompt set, PROMPT_BASIS_VERSION=3      │
  │   BASIS                                                    prompt_set_id + selected_set_id                │
  │                                                                                                          │
  │  GEO PROBES        services/llm_providers/orchestrator.py  strategies, cost caps, health                 │
  │                    services/llm_providers/provider_registry.py  gemini · deepseek · chatgpt · claude      │
  │                    db/llm_probe_runs.py                    per-probe cost/latency telemetry               │
  │                                                                                                          │
  │  ANSWER→HOST       services/agent_center_bd_report_service.py :: build_authority_map()   [19,481 lines]   │
  │                    services/grounding_redirect_resolver.py  unwraps Vertex redirectors                    │
  │                    services/cited_host_classifier.py        registry + merchant_relative_role()           │
  │                                                                                                          │
  │  CANONICAL         evidence_items · readiness_findings · action_plan_items ·                              │
  │   EVIDENCE         verification_runs · execution_routes · report_projections ·                            │
  │   (migration 086)  citation_observations              [db/audit_evidence.py]                              │
  │                    services/audit_evidence_builder.py       synthesis + dual-write                        │
  │                                                                                                          │
  │  STORE TRUTH       services/external_offers_service.py      Product/Offer JSON-LD, price, currency,       │
  │                                                             availability, variants                        │
  │                    services/bd_brand_signals.py             Organization · sameAs · robots · sitemap      │
  │                    services/pdp_renderability.py            services/verifiers/*  (7 registered)          │
  │                                                                                                          │
  │  COMMERCE LANE     routes/store_audit_probe_internal.py     UCP claim/receipt   ── external worker        │
  │  (contract only)   routes/store_audit_commerce_probe_internal.py  browser claim/receipt ── external, DARK │
  │                                                                                                          │
  │  PROJECTION        services/audit_projection_builder.py     5 audiences → report_projections              │
  │                                                                                                          │
  │  ACTION            db/merchant_tasks.py                     pending→in_progress→done/dismissed/           │
  │                                                             failed/superseded                             │
  │                    db/executor_runs.py                      agent-executed remediation runs               │
  │                                                                                                          │
  │  COPILOT           routes/merchant_audit_routes.py :: POST /ask                                           │
  │                    bounded ctx (_build_ask_context) + ungrounded DeepSeek + anti-invention prompt         │
  └──────────────────────────────────────────────────────────────────────────────────────────────────────────┘
                                                  ▲
                       ┌──────────────────────────┴─────── MERCHANT (pivota-merchants-portal) ───────────────┐
                       │  /dashboard/agent-center/ai-readiness   (4,331 lines)                               │
                       │  /dashboard/agent-center/url-audit · /funnel · /pdp-status · /agent-chat            │
                       │  components/audit/*  (40 panels: ChannelAppearancePanel, EvidencePlayPanel,         │
                       │                       MerchantTaskQueuePanel, AskAboutThis, WinPlanPanel, …)        │
                       │  /share/r/[token]   public read-only redacted audit view                            │
                       └────────────────────────────────────────────────────────────────────────────────────┘
```

---

## C. Requirement → Capability Matrix

Statuses: `EXISTING` · `PARTIAL` · `MISSING` · `REFACTOR` · `UNKNOWN`
Decisions: `REUSE AS-IS` · `EXTEND` · `WRAP` · `REFACTOR` · `REPLACE` · `NEW BUILD` · `DEFER`

### C1. Shared / canonical

| Requirement | Status | Existing Module | Code Evidence | Decision | Gap | P0/P1 |
|---|---|---|---|---|---|---|
| Audit basis (`audit_basis_v1`) | PARTIAL | `services/prompt_basis.py` | `PROMPT_BASIS_VERSION`, `build_selected_set_id()`, per-tier pinning | EXTEND | no `basis_id` row; no providers/models, market, language, currency, `official_domains[]`, probe versions | P0 |
| Immutable replay reference | EXISTING | `services/prompt_basis.py` | `load_prior_prompt_basis()`, refresh only on version bump or explicit flag | REUSE AS-IS | — | P0 |
| Evidence store | EXISTING | `db/audit_evidence.py` | `evidence_items` + idempotency + expiry | REUSE AS-IS | — | P0 |
| Findings | EXISTING | `db/audit_evidence.py` | `readiness_findings` | EXTEND | destination finding types | P0 |
| Actions | EXISTING | `db/audit_evidence.py`, `db/merchant_tasks.py` | `action_plan_items` → `merchant_tasks` | EXTEND | lifecycle states (see F/J) | P0 |
| Verification runs | EXISTING | `db/audit_evidence.py:388` | durable lease queue, 7 registered verifiers | REUSE AS-IS | — | P0 |
| Execution routes | EXISTING | migration 196 | domain-keyed, nullable merchant, claim-by-UPDATE | REUSE AS-IS | — | P0 |
| Audience projections | EXISTING | `services/audit_projection_builder.py` | 5 audiences, cached, versioned builder | EXTEND | 2 new audiences | P0 |
| Evidence state model (§23) | PARTIAL | `db/audit_evidence.py:199` | `pending/claimed/succeeded/failed/exhausted_retries/blocked` | REFACTOR | **no `UNVERIFIED`, `SKIPPED`, `PROVIDER_FAILED`, `UNPARSEABLE`** | P0 |
| Probe coverage / confidence | PARTIAL | `evidence_items.confidence` | integer confidence column | EXTEND | no run-level coverage rollup | P0 |
| Commerce Index | EXISTING | `db/commerce_index.py`, `services/commerce_index_v2.py` | sources, field changes, publication jobs | REUSE AS-IS | — | P1 |

### C2. GET SELECTED

| Requirement | Status | Existing Module | Code Evidence | Decision | Gap | P0/P1 |
|---|---|---|---|---|---|---|
| ChatGPT visibility | EXISTING | `provider_registry.py:351` | grounded `web_search_preview`, explicit selection | REUSE AS-IS | premium; excluded from `auto` | P0 |
| Gemini visibility | EXISTING | `provider_registry.py:271` | grounded, orchestrator-selectable | REUSE AS-IS | — | P0 |
| Recommendation rank | EXISTING | `agent_center_bd_report_service.py` | authority map + per-prompt observations | REUSE AS-IS | — | P0 |
| First-recommendation share | PARTIAL | same | `citation_by_provider` medians | EXTEND | ordinal not persisted relationally | P0 |
| SKU mention rate | EXISTING | same | deterministic `sku_mention` | REUSE AS-IS | — | P0 |
| Competitive selection | EXISTING | `db/competitor_audit_runs.py`, `services/competitor_audit_orchestrator.py` | parented competitor runs | REUSE AS-IS | — | P0 |
| Product understanding | EXISTING | `services/pdp_content_depth.py`, `pdp_scope_classifier.py` | — | REUSE AS-IS | — | P0 |
| Selection Leakage findings | PARTIAL | `readiness_findings` | finding taxonomy exists | EXTEND | no stage tagging | P0 |

### C3. CAPTURE INTENT

| Requirement | Status | Existing Module | Code Evidence | Decision | Gap | P0/P1 |
|---|---|---|---|---|---|---|
| Destination extraction | EXISTING | `build_authority_map()` + `grounding_redirect_resolver.py` | grounding chunks → hosts, redirectors unwrapped | REUSE AS-IS | — | P0 |
| Multiple destinations per response | EXISTING | `citation_observations` | one row per cited host | REUSE AS-IS | — | P0 |
| **Primary commerce destination** | **MISSING** | — | flat rows, no ordinal, no buy-language signal | **NEW BUILD** | §12 entirely | **P0** |
| **Destination claim (§14)** | **MISSING** | — | no field represents "AI said this was official" | **NEW BUILD** | needs answer-text parse, not chunk metadata | **P0** |
| **Purchase-intent response predicate** | **MISSING** | `services/query_semantic_class.py` (adjacent) | `query_class`/`axis` classify the *query*, not the response | **NEW BUILD** | denominator for every §10/§11 metric | **P0** |
| Official classification | PARTIAL | `merchant_relative_role()` → `own_domain` | `first_party` from `merchant_owned_domains()` | EXTEND | domains are *derived*, never *verified* | P0 |
| **Authorized classification** | **MISSING** | — | collapses into `marketplace_self_listing` | **NEW BUILD** | no authorized-retailer relation table | P0 |
| Marketplace / third-party | PARTIAL | `cited_host_registry.json` + role map | `retailer`/`marketplace` host types | EXTEND | shares a bucket with Authorized | P0 |
| Competitor classification | EXISTING | `merchant_relative_role()` | `is_competitor` → `ROLE_COMPETITOR` | REUSE AS-IS | — | P0 |
| Unknown / unverified | EXISTING | `ROLE_RELATIVE_UNCLASSIFIED` | — | REUSE AS-IS | — | P0 |
| **Suspicious / impersonation** | **MISSING** | — | zero hits for `impersonat\|typosquat\|suspicious` in product code | **NEW BUILD** | §13 evidence set entirely | P1 |
| **No Destination Share** | **MISSING** | — | rows exist only per cited host; a response with none emits nothing | **NEW BUILD** | needs a response-level row | **P0** |
| Official Destination Share | MISSING | — | numerator derivable, denominator not | NEW BUILD | depends on the three MISSINGs above | P0 |
| Trusted Destination Share | MISSING | — | — | NEW BUILD | depends on Authorized | P1 |
| Official Presence Rate | PARTIAL | `merchant_first_party_visible` | `agent_center_bd_report_service.py:1150` | EXTEND | not response-scoped | P0 |
| Exposure rate | PARTIAL | `services/host_recurrence.py` | recurrence over runs | EXTEND | not intent-scoped | P0 |
| Revenue Leakage Case | MISSING | — | derived grouping over findings | NEW BUILD | pure projection — no new table | P1 |
| Authority Gap | PARTIAL | `services/bd_brand_signals.py` | Organization, `sameAs`, social, robots, sitemap, SEO score | EXTEND | no gap *rules*, no merchant-facing finding | P0 |
| Destination price consistency | PARTIAL | `services/live_offer_verification.py`, `external_offers_service.py` | model-quoted vs live PDP price | EXTEND | not joined to destination class | P1 |

### C4. CONVERT SALES

| Requirement | Status | Existing Module | Code Evidence | Decision | Gap | P0/P1 |
|---|---|---|---|---|---|---|
| Robots readiness | EXISTING | `bd_brand_signals.py:588` | `_extract_robots_directives` | REUSE AS-IS | — | P0 |
| Sitemap readiness | EXISTING | `bd_brand_signals.py:414`, `verifiers/pdp_in_sitemap.py` | fetch + classify + verify | REUSE AS-IS | — | P0 |
| Product JSON-LD | EXISTING | `external_offers_service.py:65` | `application/ld+json` parse | REUSE AS-IS | — | P0 |
| Offer JSON-LD | EXISTING | `external_offers_service.py:339` | price, currency, `priceSpecification`, availability | REUSE AS-IS | — | P0 |
| Live PDP truth | EXISTING | `verifiers/pdp_renders.py`, `pdp_renderability.py` | — | REUSE AS-IS | — | P0 |
| Variant selection | EXISTING | `external_offers_service.py:356` | `_offer_variants_from_node` + data-attr fallback | REUSE AS-IS | — | P0 |
| Feed readiness | PARTIAL | `db/amazon_feeds.py`, `readiness/channel_exports/` | ACP + UCP exports | EXTEND | — | P1 |
| UCP capability / priced preview | EXISTING (dark) | `routes/store_audit_probe_internal.py` | claim/receipt, external worker | REUSE AS-IS | flag default `false` | P0 |
| **Add-to-cart** | **CONTRACT ONLY** | `store_audit_commerce_probe_internal.py:79` | `CartObservation` shape defined | **DEFER/ARM** | worker out-of-repo, `ARMED=false` | **P1** |
| **Cart-line + cart price** | **CONTRACT ONLY** | same | `quantity`, `cart_price`, `currency` | **DEFER/ARM** | same | **P1** |
| **Initial checkout route** | **CONTRACT ONLY** | same:86 | `CheckoutRouteObservation` | **DEFER/ARM** | same | **P1** |
| **Challenge / WAF classification** | **CONTRACT ONLY** | same:91 | `challenge_stage`, `outcome_code:"challenge"` | **DEFER/ARM** | same | **P1** |
| Transaction readiness | PARTIAL | `services/merchant_commerce_readiness_service.py` | connected-store + PSP readiness | REUSE AS-IS | connected merchants only | P1 |

### C5. Product surfaces

| Requirement | Status | Existing Module | Code Evidence | Decision | Gap | P0/P1 |
|---|---|---|---|---|---|---|
| Safe anonymous public projection | PARTIAL | `store_audit_public_intake.py` | redacted domain teaser, binary `agent_ready` | EXTEND | single-signal; not three-stage | P0 |
| Anonymous → auth claim | PARTIAL | migration 196 | route claim by UPDATE | EXTEND | **`merchant_audit_runs.merchant_id` is NOT NULL** | P0 |
| Evidence drill-down | EXISTING | `components/audit/EvidencePlayPanel.tsx`, `PromptEvidencePanel.tsx` | — | REUSE AS-IS | — | P0 |
| Evidence-aware Copilot | EXISTING | `merchant_audit_routes.py:4041` | bounded ctx, anti-invention prompt, cross-tenant checked, metered | EXTEND | reads `report_jsonb`, not canonical tables | P0 |
| Recovery Action lifecycle | PARTIAL | `db/merchant_tasks.py:42` | `pending/in_progress/done/dismissed/failed/superseded` | EXTEND | no `ready_for_retest`/`verifying`/`verified`/`regressed` | P0 |
| Same-basis replay | EXISTING | `services/prompt_basis.py` | — | REUSE AS-IS | — | P0 |
| Before/after diff | EXISTING | `services/audit_delta.py`, `audit_tracking_series.py` | basis-aware trend | EXTEND | not stage-scoped | P0 |
| Public share of a report | EXISTING | `merchant_audit_routes.py:2646` | expiring, revocable, redacted token | REUSE AS-IS | — | P1 |

---

## D. Marketing Portal Migration Plan (`pivota-marketing`)

Next.js App Router. Today the audit lives on exactly one page and runs exactly one probe.

| Current Route | Current Purpose | Target Purpose | Decision | Required Changes |
|---|---|---|---|---|
| `/` | Positioning; no audit entry | Primary "Run Free AI Revenue Audit" CTA (§28) | REFRAME | mount `AuditUrlCaptureForm`; replace protocol-led copy with `Visible ≠ Buyable` |
| `/ai-readiness` | Only audit entry; `AuditUrlCaptureForm` → UCP teaser | Anonymous **Revenue Audit** entry + result | REFACTOR | teaser must return three stages, not one boolean |
| `src/lib/store-audit-teaser.ts` | 1 intake + 2 polls, 4s apart, binary `agent_ready` | Poll the three-stage public projection | REFACTOR | new response type; truthful progress (§40.4) instead of a 12s window |
| `src/components/AuditUrlCaptureForm.tsx` | URL capture, silent fallback to signup | Same, plus stage summary + `Unlock Full Revenue Leak Map` | EXTEND | keep the silent-fallback discipline — it is correct |
| `/merchant/signup` · `/merchant/signup/ai-readiness` | Signup; 13-line stub | Carry `domain` + `audit_run_id` through signup into the claim | EXTEND | **the §30 handoff has no client-side carrier today** |
| `/promotion-readiness`, `/merchant-gateway`, `/agent-integration`, `/developers/*` | Protocol/infra narrative | Unchanged | KEEP | do not lead the merchant homepage with these (§28) |
| `/ucp/insights` | UCP data surface | Unchanged | KEEP | — |
| `/zh/*`, `/blog/*`, `/faq`, `/about` | Content | Unchanged | KEEP | — |

**Migration risk.** The teaser lane is `STORE_AUDIT_PUBLIC_INTAKE_ENABLED` default-false and
404s while dark, and the client treats any failure as "fall back to plain signup". That is
the right shape and must survive the refactor — a three-stage teaser must degrade to signup,
never to a broken page or a fabricated result.

---

## E. Merchant Portal Migration Plan (`pivota-merchants-portal`)

Classification: `KEEP` · `REFACTOR` · `MOVE/REFRAME` · `DEPRECATE/HIDE` · `NEW` · `DEFER`

| Surface | Today | Classification | Notes |
|---|---|---|---|
| `/dashboard` (1,258 ln) | Generic merchant overview | **MOVE/REFRAME** | becomes the three-stage Revenue Recovery Overview (§25) |
| `/dashboard/agent-center/ai-readiness` (4,331 ln) | The whole audit report on one page | **REFACTOR** | split by stage; this is the single largest frontend work item |
| `/dashboard/agent-center/url-audit` (1,328 ln) | Merchant-curated URL audit | **KEEP** | genuinely a specialist Store Audit surface |
| `/dashboard/agent-center/funnel` | Funnel metrics | **REFRAME** | source of Convert Sales telemetry once connected |
| `/dashboard/pdp-status` | PDP indexing/renderability | **MOVE** | under CONVERT SALES |
| `/dashboard/product-optimization` | Per-SKU optimization | **MOVE** | under GET SELECTED |
| `/dashboard/agent-chat` (5-line shell + 26 components) | Beauty/fashion authoring assistant | **KEEP, do not reuse** | wrong runtime for the Copilot — it is a content-authoring queue, not evidence Q&A |
| `components/audit/AskAboutThis.tsx` | Client-synthesized prompt chips + `/ask` | **EXTEND** | this, not agent-chat, is the Copilot seam |
| `components/audit/ChannelAppearancePanel.tsx` | "Where does the brand appear vs retailers AI cites instead" | **REFACTOR** | direct ancestor of the Capture Intent distribution (§15) |
| `components/audit/MerchantTaskQueuePanel.tsx` | Task queue | **EXTEND** | Recovery Action list |
| `components/audit/EvidencePlayPanel.tsx`, `PromptEvidencePanel.tsx`, `PromptExplorer.tsx` | Evidence drill-down | **KEEP** | Evidence Workspace already exists |
| `components/audit/MomentumTrend.tsx`, `VisibilityTrendChart.tsx`, `BrandMomentumChart.tsx` | Trend | **EXTEND** | before/after diff per stage |
| `components/audit/CompetitorIntelPanel.tsx` | Competitor set | **KEEP** | — |
| `/dashboard/integrations`, `/billing`, `/payouts`, `/commission`, `/orders`, `/platform-orders`, `/mcp` | Transaction/ops | **KEEP** | separate from Revenue Recovery onboarding (§38) |
| `/dashboard/platform-onboarding` | KYB/store connect | **KEEP, resequence** | must not gate diagnostic value |
| `/share/r/[token]` | Public redacted report | **EXTEND** | can serve the safe public projection |
| `/dev/*` previews | Internal | **KEEP** | — |

**Reuse verdict.** Of the ~15 components §46 asks for, **11 already exist** in
`components/audit/`. The two genuinely new ones are the Intent Capture distribution bar
and the Revenue Leakage Case card. The three-stage summary is a new composition of
existing primitives (`AuditScoreStrip`, `MomentumCard`, `ReportSectionBoundary`).

---

## F. Destination Leakage / AI Channel Integrity Assessment

### Exists
- Per-`(provider, query, host)` observation rows, relational and content-key-keyed.
- Redirect unwrapping for Gemini's Vertex redirector, with a kill switch and a bounded cache.
- A BD-curated host registry with types, subtypes, categories and coverage notes.
- Merchant-relative role assignment with an explicit **no-inflation guardrail**: an unknown
  host is `unclassified`, never promoted to endorsement.
- Competitor detection pooled across SKUs within a run.
- Merchant-owned-domain folding so a second storefront is not read as a third party.
- `services/host_recurrence.py` and `competitor_recurrence.py` for cross-run exposure.
- A precedent for the PRD's honesty rule: a blocked probe answers "inconclusive", never
  "not agent-ready".

### Missing
1. **The commerce axis.** The classifier's axis is *findability vs endorsement*. Official
   Destination Share needs *where the money would go*. `marketplace_self_listing` is one
   bucket for two PRD classes (Authorized, Marketplace/Third-Party).
2. **Primary destination (§12).** No ordinal, no buy-language signal, no provider citation
   ordering retained.
3. **Destination claim vs truth (§14).** The pipeline consumes grounding-chunk *metadata*.
   "AI called example-store.com the official Judydoll store" lives in the answer *prose*,
   which is never parsed for relationship claims. This is the PRD's highest-value evidence
   and the repo cannot currently represent it.
4. **Suspicious classification (§13).** Nothing. No domain-similarity, no
   claims-to-be-official detection, no conflicting-entity signal.
5. **No-destination responses.** A purchase-intent answer that names no actionable
   destination produces zero rows and is therefore invisible.
6. **Verified official domains.** `merchant_owned_domains()` *infers* from onboarding and
   catalog. `brand_claims` verifies exactly one `brand_domain`. A metric published as
   "Official" needs the verified set, not the inferred one.

### Recommendation
Add **one orthogonal axis** to `citation_observations` — `destination_class` +
`claimed_relationship` + `is_primary_destination` + `destination_confidence` — plus one
response-level table so no-destination is representable. Do **not** fork the classifier.
Keep `citation_role` exactly as-is: it feeds the win-plan and outreach surfaces, and
repurposing it would silently change those products.

---

## G. Backend / API Plan (minimum change)

### Reuse unchanged
`POST /public/store-audit/intake` · `GET /public/store-audit/teaser` ·
`GET /api/audits/{run_id}?audience=…` · `POST /api/merchant-center/audit/url-readiness` ·
`GET /history` · `/tracking` · `/tasks` · `PATCH /tasks/{id}` · `/ask` ·
`POST /url-readiness/{run_id}/share` · internal claim/receipt endpoints.

### New (5 endpoints, all read-time projection)
| Endpoint | Purpose | Backed by |
|---|---|---|
| `GET /api/audits/{run_id}?audience=revenue_recovery` | Three-stage projection (§24) | new builder over canonical tables — **no new route** |
| `GET /public/revenue-audit/teaser?domain=` | Three-stage anonymous result (§29) | `audience=public_anonymous` |
| `POST /api/merchant-center/audit/{run_id}/claim` | §30 anonymous→auth handoff | `merchant_audit_runs` claim UPDATE, mirroring migration 196 |
| `GET /api/merchant-center/audit/{run_id}/capture-intent` | Destination distribution + drill-down | aggregate over `citation_observations` |
| `POST /api/merchant-center/audit/{run_id}/retest` | Same-basis replay + diff (§35) | existing `prompt_basis` + `audit_delta` |

Everything else is a **projection change, not an API change** — which is the whole point
of the existing `report_projections` design. Prefer read-time projection over duplication,
exactly as §47 asks.

---

## H. Schema / Data Plan

| Concept | Verdict | Rationale |
|---|---|---|
| `basis_id` | **PERSISTED (new)** | `prompt_basis` is per-SKU on the report; the PRD needs a run-level immutable row |
| `destination_class` | **PERSISTED** | new column on `citation_observations`; recomputation would break replay comparability |
| `claimed_relationship` | **PERSISTED** | extracted once from answer prose; the prose is not retained forever |
| `is_primary_destination` | **PERSISTED** | §12 forbids silent methodology change ⇒ must be stamped, not re-derived |
| Response-level row (no-destination) | **PERSISTED (new table)** | not representable today |
| `is_purchase_intent` | **PERSISTED** | versioned predicate; must be frozen per basis |
| Official Destination Share | **DERIVED, CACHED** | aggregate → `report_projections` |
| Trusted / No-Destination / Suspicious shares | **DERIVED, CACHED** | same |
| Revenue Leakage Case | **DERIVED** | grouping over findings; **no `fake_store_*` tables** |
| Authority Gap | **DERIVED** | rules over existing `bd_brand_signals` output |
| Recovery Action | **PERSISTED (extend)** | add states to `merchant_tasks.VALID_STATUSES` |
| Revenue Recovery Projection | **CACHED** | `report_projections` + `builder_version` |
| Anonymous run ownership | **PERSISTED (alter)** | `merchant_audit_runs.merchant_id` → nullable + `claimed_at` |
| Verified official domains | **PERSISTED (new)** | inferred ≠ verified; the metric depends on it |
| Authorized retailer relations | **PERSISTED (new)** | merchant-asserted, needs an approval trail |

### Answers to §48's ten questions
1. **Can existing destination resolution support the new metrics?** Partly — hosts and
   redirect unwrapping yes; primary/claim/suspicious/no-destination no.
2. **Is primary destination persisted or derived?** Neither — it does not exist. Persist it.
3. **Can claims fit existing observation models?** Only with new columns; the current
   pipeline never reads answer prose.
4. **Is Revenue Leakage Case derived?** Yes. Do not build case management.
5. **Does Recovery Action need new persistence?** No — extend `merchant_tasks`.
6. **Can Authority Gap be derived from Store Audit evidence?** Yes, almost entirely, from
   `bd_brand_signals`.
7. **Recompute or cache the projection?** Cache in `report_projections`, invalidate by
   `builder_version` — the mechanism already exists and already defaults to "never
   silently re-render".
8. **Anonymous run ownership?** Copy migration 196's pattern exactly: nullable owner,
   claim by one UPDATE, never duplicate the row.
9. **Is `basis_id` immutable?** `prompt_set_id` is content-addressed and effectively
   immutable; the *run-level* basis does not exist yet. Make it immutable by construction.
10. **Duplicated merchant/product/domain identity?** Yes — merchant domain identity is
    spread across `merchant_onboarding.store_url`, `merchant_onboarding.website`,
    `merchants.store_url`, `merchant_stores.domain`, `catalog_products.source_domain`,
    `catalog_products.canonical_url`, and `brand_claims.brand_domain`, unified only at
    read time by `merchant_owned_domains()`. **Resolve this before publishing Official
    Destination Share** — the metric is a direct function of that set.

---

## I. Copilot Integration Plan

```
Selected Stage / Finding
        ↓
Recovery Context  ← extend _build_ask_context() to read canonical tables
        ↓
answer_grounded_question()  (DeepSeek, ungrounded — cannot reach the web)
        ↓
Grounded Response  (anti-invention system prompt, <120 words, JSON)
        ↓
Allowed Action  (Explain · Show Evidence · Recommend Fix · Retest)
```

**Reuse `POST /ask`.** It is already cross-tenant checked, metered with idempotent
charging, refunds on failure, and instructed never to invent hosts, numbers, competitors
or recommendations. Do **not** reuse `/dashboard/agent-chat` — that is a content-authoring
queue with a different runtime and different guardrails.

**Extend:** `_build_ask_context()` currently reads `report_jsonb`'s narrative
([routes/merchant_audit_routes.py:3985](routes/merchant_audit_routes.py#L3985)). Point it
at `readiness_findings` + `evidence_items` + the revenue-recovery projection and add
`selected_stage`, `selected_finding`, `probe_coverage`, `confidence`, `previous_run_diff`,
`available_actions[]`, `permissions`.

**Can:** explain, show evidence, recommend a fix from `action_plan_items`, trigger retest.
**Cannot:** deploy fixes, invent evidence, claim payment verification, read unrestricted
merchant data (the bounded slice is the enforcement), run legal/takedown workflows.

---

## J. Retest Plan

| Finding | Verification | Available today? |
|---|---|---|
| Official Destination Share | Same-basis replay | **Yes** — `prompt_basis` + `audit_delta` |
| Suspicious exposure | Same-basis replay + deterministic resolver | Replay yes; resolver **no** |
| Merchant identity gap | Re-crawl + deterministic check | **Yes** — `bd_brand_signals` re-run |
| Destination claim mismatch | Same-basis replay | Replay yes; claim extraction **no** |
| Pricing mismatch | Model observation + fresh PDP truth | **Yes** — `live_offer_verification` |
| Cart / checkout regression | Commerce-route replay | **No** — worker disarmed |
| Sitemap / robots / JSON-LD | Deterministic recheck | **Yes** — registered verifiers |

`services/audit_delta.py` already exists and is basis-aware; §36's before/after view is
mostly a rendering job over it, scoped to a stage.

---

## K. Week 8–10 Sequence (dependency-aware)

The PRD's proposed order front-loads UI. The real dependency chain is measurement-first,
because three P0 metrics have no denominator until step 2.

1. **Reconcile the evidence state model** — add `unverified`/`skipped`/`provider_failed`/
   `unparseable`. Everything downstream inherits its honesty from this. *Nothing else
   should start first.*
2. **Purchase-intent predicate + response-level observation row** — versioned, with a
   frozen test corpus. Unblocks every §10/§11 metric.
3. **Primary commerce destination + destination claim extraction** — persisted, versioned.
4. **`destination_class` axis on `citation_observations`** — Official / Authorized /
   Marketplace / Competitor / Unknown / No-Destination. (Suspicious deferred to P1.)
5. **Run-level `audit_basis` row** — extend `prompt_basis`; stamp `official_domains[]`.
6. **Verified official-domain set** — collapse the six identity sources.
7. **Capture Intent aggregation** — shares and exposure rates.
8. **`revenue_recovery` projection builder** — three stages over canonical tables.
9. **`public_anonymous` projection builder** — safe teaser shape.
10. **Anonymous audit-run claim** — nullable `merchant_id` + claim endpoint.
11. **Authority Gap rules** — over existing `bd_brand_signals` output.
12. **APIs** (the five above).
13. **Marketing result migration** — three-stage teaser + signup carrier.
14. **Merchant Overview migration** — three-stage summary.
15. **Capture Intent drill-down** — refactor `ChannelAppearancePanel`.
16. **Recovery Action lifecycle** — extend `merchant_tasks` states.
17. **Recovery Context + Copilot** — repoint `_build_ask_context`.
18. **Retest / diff per stage.**
19. **Funnel instrumentation** — anonymous funnel needs a non-merchant-scoped event
    (`funnel_events.merchant_id` is NOT NULL today).
20. **Allowlist end-to-end validation.**

Steps 1–6 are backend-only and strictly sequential. Steps 7–12 parallelize. Steps 13–18
are frontend and parallelize across the two portals.

---

## L. File-Level Change Plan

### Backend — new
| File | Purpose |
|---|---|
| `db/migrations/2xx_audit_basis.sql` | run-level immutable basis |
| `db/migrations/2xx_destination_axis.sql` | `destination_class`, `claimed_relationship`, `is_primary_destination`, `destination_confidence` on `citation_observations` |
| `db/migrations/2xx_response_observations.sql` | response-level row (enables No-Destination) |
| `db/migrations/2xx_merchant_official_domains.sql` | verified official + authorized-retailer relations |
| `db/migrations/2xx_audit_runs_anonymous.sql` | `merchant_id` nullable + `claimed_at` + claim constraint |
| `services/destination_classifier.py` | the commerce axis (**separate module; do not edit `cited_host_classifier` in place**) |
| `services/primary_destination.py` | §12 deterministic, versioned selection |
| `services/purchase_intent_classifier.py` | versioned response predicate |
| `services/revenue_recovery_projection.py` | three-stage builder |
| `services/authority_gap.py` | rules over `bd_brand_signals` |
| `routes/revenue_recovery_routes.py` | the 5 new endpoints |

### Backend — modify
| File | Change |
|---|---|
| [db/audit_evidence.py](db/audit_evidence.py) | evidence states; `AUDIENCE_REVENUE_RECOVERY`, `AUDIENCE_PUBLIC_ANONYMOUS`; destination finding types |
| [db/merchant_audit_runs.py:120](db/merchant_audit_runs.py#L120) | `merchant_id` nullable; claim helper |
| [db/merchant_tasks.py:42](db/merchant_tasks.py#L42) | add `ready_for_retest`, `verifying`, `verified`, `regressed` |
| [services/prompt_basis.py](services/prompt_basis.py) | emit a run-level `basis_id` |
| [services/audit_evidence_builder.py:1010](services/audit_evidence_builder.py#L1010) | populate the new observation fields |
| [services/audit_projection_builder.py](services/audit_projection_builder.py) | register the two new audiences |
| [routes/merchant_audit_routes.py:3985](routes/merchant_audit_routes.py#L3985) | repoint `_build_ask_context` at canonical tables |
| [routes/store_audit_public_intake.py](routes/store_audit_public_intake.py) | three-stage teaser payload |

**Do not touch:** `services/cited_host_classifier.py`'s `citation_role` semantics —
`win_plan_builder`, `merchant_narrative_builder`, `outreach_outcomes` and
`independent_signals` all depend on the findability/endorsement cut.

### Marketing
`src/lib/store-audit-teaser.ts` (REFACTOR) · `src/components/AuditUrlCaptureForm.tsx`
(EXTEND) · `src/app/ai-readiness/page.tsx` (REFACTOR) · `src/app/page.tsx` (add CTA) ·
`src/app/merchant/signup/page.tsx` (carry domain + run id) · **new**
`src/components/RevenueStageSummary.tsx`.

### Merchant portal
`app/dashboard/page.tsx` (REFRAME) · `app/dashboard/agent-center/ai-readiness/page.tsx`
(split by stage — largest item) · `components/audit/ChannelAppearancePanel.tsx`
(REFACTOR) · `components/audit/AskAboutThis.tsx` (EXTEND) ·
`components/audit/MerchantTaskQueuePanel.tsx` (EXTEND) · `lib/api-client.ts` (new
endpoints) · **new** `components/audit/IntentCaptureDistribution.tsx`,
`components/audit/RevenueLeakageCaseCard.tsx`, `components/audit/ThreeStageSummary.tsx`.

---

## M. Risk Register

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| 1 | **Official Destination Share published on an inferred domain set.** `merchant_owned_domains()` guesses from onboarding + catalog; a missed domain reads as leakage that isn't real. | **Critical** | Ship the verified set (step 6) *before* the metric. Until then label it Directional. |
| 2 | **Denominator drift.** "Valid purchase-intent response" is a model judgement; an unversioned predicate silently changes every historical number. | **Critical** | Version the predicate, freeze it into the basis, refuse cross-version comparison — the same discipline `prompt_basis` already enforces for prompts. |
| 3 | **CONVERT SALES ships unarmed.** Contract completeness reads as capability; a projection built over an empty lane renders a stage that never had an observation. | **Critical** | Stage status must be `UNVERIFIED`, never a score. Requires risk 6 to be fixed first. |
| 4 | **Suspicious-domain false positive → legal exposure.** | High | P1, not P0. Never emit "Fake Store". Require multiple independent signals + a review threshold. §13's wording rules are non-negotiable. |
| 5 | **Repurposing `citation_role` breaks four live surfaces.** | High | New orthogonal column. Do not overload. |
| 6 | **Absence read as pass.** With no `unverified` state, a projection can't distinguish "checked, fine" from "never ran". | **Critical** | Step 1 first. Add a projection invariant test: every stage claim must cite a row. |
| 7 | **Anonymous → auth duplicates the run.** | High | Copy migration 196: claim by UPDATE, never insert a second row. |
| 8 | **The 4,331-line ai-readiness page.** Splitting it by stage is high-regression. | High | Extract stage containers around existing panels; do not rewrite the panels. |
| 9 | **`agent_center_bd_report_service.py` is 19,481 lines** and owns `build_authority_map`. | High | Additive only; new logic in new modules. |
| 10 | **Browser probe safety** against real storefronts. | High | The existing containment (crawl subnet, dedicated SA, redaction validators, leases, disarmed default) is correct — inherit it, don't rebuild it. |
| 11 | **Marketing overclaim.** A binary teaser becoming a three-stage score invites unbacked numbers. | High | Reuse the existing rule verbatim: cannot-verify never buys a negative claim. |
| 12 | **Provider failure semantics.** ChatGPT/Claude are excluded from `auto` selection by design; a failed premium probe must not read as absence. | Medium | `PROVIDER_FAILED` (step 1) + coverage on the projection. |
| 13 | **Copilot hallucination.** | Medium | Existing prompt is strong; extend the context, keep the constraints. |
| 14 | **Anonymous funnel unmeasurable.** `funnel_events.merchant_id` is NOT NULL. | Medium | Separate anonymous funnel table or nullable owner. |
| 15 | **Cost.** §20 targets 10,000+ observations; ChatGPT is priced at $5/$30 per 1M plus a per-call search fee. | Medium | Cost caps already exist in the orchestrator; model the beta's spend before arming. |
| 16 | **Merchant identity duplication across six sources.** | Medium | Resolve in step 6. |
| 17 | **Rollback.** New columns on `citation_observations` are additive; the projection is cached and versioned. | Low | Roll back by reverting `builder_version`; the repo's default is already "never silently re-render". |

---

## N. Recommended P0 Cut

**Ship:**
- Evidence-state reconciliation with a real `UNVERIFIED`.
- Purchase-intent predicate + response-level observations.
- Primary commerce destination + destination claim extraction.
- `destination_class`: Official · Marketplace/Third-Party · Competitor · Unknown ·
  No-Destination. *(Authorized and Suspicious deferred.)*
- Verified official-domain set.
- Run-level `audit_basis`.
- Official Destination Share + No Destination Share + Official Presence Rate.
- Three-stage `revenue_recovery` projection, with CONVERT SALES rendering as
  `Partially Verified` from PDP/Offer/sitemap/robots/UCP evidence only.
- `public_anonymous` projection + three-stage marketing teaser.
- Anonymous audit-run claim.
- Authority Gap findings from existing brand signals.
- Recovery Action lifecycle on `merchant_tasks`.
- Copilot repointed at canonical evidence, with the four bounded actions.
- Same-basis retest + per-stage before/after diff.

**This delivers §40's 21 numbered steps except #13 (full distribution — partial, no
Authorized/Suspicious split) and the cart/checkout half of #6.**

## O. Explicit P1 Deferrals

- **Suspicious / possible-impersonation classification.** Highest legal risk, lowest
  readiness. Needs its own evidence design and review threshold.
- **Trusted Destination Share + Authorized retailer relations.** Requires a
  merchant-assertion and approval surface that does not exist.
- **Arming the browser commerce lane** (add-to-cart, cart-line, cart price, checkout
  route, WAF classification). Contract is ready; arming is its own project.
- **Revenue Leakage Cases.** Pure projection — cheap once the classes exist, so defer
  without cost.
- **Destination price/offer consistency joined to destination class.**
- **`Generate Fix` → `Apply Patch`.** Guidance and schema payloads only in P0; no
  automated site edits.
- **Competitor Destination Exposure** as a published metric — detection exists, confidence
  bounds do not.
- **Anonymous funnel instrumentation.**
- **Commerce Index enrichment from destination observations.**

**Explicitly not built, at any phase:** fake-store management, Brand Protection suite,
domain security dashboard, takedown or DMCA workflow, legal case management, trademark
workflow, a second evidence store, a second destination classifier, a second
merchant/product catalog, `fake_store_*` schema.

---

## Appendix: The single most important sentence in this audit

> The PRD asks for "one authoritative evidence basis, multiple product projections."
> The repo already has it — `report_projections` keyed by `(audit_run_id, audience)` with
> a versioned builder. Revenue Recovery is **a sixth audience**, not a sixth system.
> Every plan that forgets this will cost 10x more than it should.
