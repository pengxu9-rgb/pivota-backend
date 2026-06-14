# Pivota Merchant Growth System — Holistic Build Plan

**Date:** 2026-06-14 · **Status:** Draft for joint review (pre-implementation) ·
**Deciders:** Founder (peng), Claude

Synthesizes the last 24h of decisions into **one system**. Companion docs:
ADR-001 (canonical record vs supplier), ADR-002 (decision-intelligence),
`docs/AGENT_PRODUCT_RECORD.md`, the K-beauty data contract
(`/tmp/pivota-kbeauty-data-contract.md`), and `docs/SUPPLIER_EVIDENCE_INTAKE.md`.

---

## 0. The frame — what we are actually building

A system that takes a **long-tail K-beauty startup** from *"a product and almost
nothing else"* to *"agent-recommended, agent-trusted, winning a defensible
niche."* It is the **supply-side decision layer for agentic commerce** — the
merchant-facing complement to the agent-facing canonical record. Not an audit
tool that scores you; a **growth system** that positions you, builds your verified
record, creates your presence, gets you served to agents, and compounds.

Anchored in ADR-001: **Pivota owns the canonical record; the merchant is a
supplier of inventory + evidence**, and need not author agent-grade data.

---

## 0.5 Corrections from the pre-implementation gap-check (2026-06-14)

A rigorous review found the strategy + reuse inventory ~85% accurate, with these
load-bearing fixes (folded into the sections below):

1. **Outcomes / moat is BLOCKED, not built.** `aggregated_outcomes` ships only in the
   unmerged PR **#815** (OPEN). Phase 4 + "compounding moat" depend on merging it.
   Reclassified ◐ blocked-on-#815.
2. **Merchant SKU → canonical `content_key` resolver is NOT built** —
   `matched_content_key` is hardcoded `None` (`catalog_trust_policy.py:208-213`,
   "resolved in Phase 2 dual-write"). It is the **hidden dependency under all of Phase 1**
   and is **net-new**. Elevated to a first-class Phase-1 line item.
3. **Serving by `content_key` is a net-new, two-repo read-path rewrite**, not a clean
   fold. Confirmed disjoint: merge hook fires only for `external_seed`
   (`PIVOTA-Agent src/server.js:34640`); `/contributions` keys to the authed merchant
   (`routes/merchant_pdp.py:71,124`). Keep the overlay path as the **bridge**; cut over,
   don't retire. Writes in pivota-backend (Py) + reads in PIVOTA-Agent (Node) = coordinated.
4. **Niche candidate-generation is net-new**, not a thin wrapper over `sku_opportunity`
   (which scores *probed* prompts for ownership/demand/density — it does not invent
   winnable niches from attributes). Reclassified ❌ net-new.
5. **Risks added (Section 5.5):** demand-proxy back-test, multi-merchant conflict on a
   shared `content_key`, resolution-confidence gating, Reddit *participation* ToS,
   supplement efficacy hard-block until lab-ingest, cold-start (no-sources) sequencing.
6. **Bonus — already resolved:** `category_kind` is now a durable shipped column
   (migration 151 + `services/category_kind.py`, never guesses) — that data-contract
   dependency is done.

Verified-as-claimed (reuse is real): `sku_opportunity`, `build_sideways_wedge`,
`sku_lane_priority`, `decision_grade_eval`, `claim_screening`, `claim_safety`,
`canonical_inci_intake.may_write` precedence, `beauty_evidence`, `crawled_inci_ingest`,
`ProductClaim`, `EvidenceProfile`.

---

## 1. The system as one loop

```
        ┌──────────────────────────────────────────────────────────────┐
        │                                                              │
        ▼                                                              │
  ① DIAGNOSE ───▶ ② STRATEGIZE ───▶ ③ BUILD THE ───▶ ④ CREATE & ───▶ ⑤ SERVE ─┐
   (audit:        (where can you      RECORD          DISTRIBUTE      (canonical │
   where are      WIN? fit×open×      (verified,      (presence for   content_key│
   you (in)visible demand — don't     graded,         the targets:    record →   │
   to agents)     fight head-on)      claim-safe      content,        agents     │
                                      evidence)       community, KOL) read it)   │
        ▲                                                                        │
        │                                                                        ▼
        └──────────────────── ⑥ MEASURE & DEFEND ◀───────────────────────────────┘
                              (outcomes accrue: reviews / repurchase /
                               share-gained → the compounding moat;
                               re-audit validates; defend won niches)
```

Each stage is a subsystem (Section 3). The loop is the product: diagnose →
position → build → create → serve → measure → re-diagnose.

---

## 2. The architecture spine (shared by every stage — reuse-heavy)

| Spine element | Role | Where |
|---|---|---|
| **Canonical record** (`content_key`) | one verified product record, shared across all merchant offers | `catalog_products`, `pivota_signature_id` |
| **Data contract** | the field-by-field definition of "agent-decision-grade" (common core + category modules) | `/tmp/pivota-kbeauty-data-contract.md` |
| **Graded claims** | `ProductClaim {claim_text, source_ref, source_type, evidence_grade, substantiation_status}` + `required_disclaimers` | `models/catalog.py:134`, `EvidenceProfile` |
| **Source precedence + provenance** | brand-official > supplier > reseller; every field traces to a source | `canonical_inci_intake.may_write()` |
| **Decision-grade scoring** | 5 dims (find/justify/trust/buy/compare) | `decision_grade_eval.py` |
| **Serving gate** | admits a SKU to the agent surface only when decision-grade | `index_pipeline_state` (11 gates) |
| **Agent serving** | what an agent reads | `agent_pdp_view` → `/agent/shop/v1/invoke get_pdp_v2` |
| **Opportunity engine** | per-prompt ownership / demand / density / open-lane / sideways-wedge | `sku_opportunity.py`, `sku_lane_priority.py` |
| **Social-intel** | search-then-extract, Reddit monitor, authority map | audit social-intel callers (SerpAPI) |
| **Action ladder** | content briefs, creator match, outreach drafts | `audit_playbook_engine`, `next_best_action`, `content_brief` |
| **Outcomes** | rail-transacted / refund / repurchase per content_key | `aggregated_outcomes` — **◐ in unmerged PR #815, NOT in codebase yet** |

> The headline: **most of the machinery already exists.** The build is mostly
> *generation, packaging, intake, and wiring* on top of this spine.

---

## 3. The subsystems

### ① DIAGNOSE — the readable audit  *(STATUS: SHIPPED)*
- **Purpose:** show the merchant where they are (in)visible to agents, readably.
- **Shipped:** band+meaning copy layer + executable next-step + scorecard + de-jargoned
  queue + typed evidence + content_brief de-dup. PRs: pivota-backend **#869 (merged)**,
  pivota-merchants-portal **#46 (open, preview green)**, **#45 Wave 1 (open)**.
- **Net-new remaining:** none (this is the entry-point UI for the loop).

### ② STRATEGIZE — winnable-niche targeting  *(the keystone)*
- **Purpose:** tell the merchant *where they can win* (fit × openness × demand) and
  *where not to fight* (flagship-owned head terms). Reframes the audit from diagnosis
  to strategy.
- **Reuse:** `sku_opportunity` (ownership_state / demand_state / density / `_is_open_lane`),
  `build_sideways_wedge`, `sku_lane_priority`, the audit probes + citation concentration.
- **Net-new:** (a) **candidate niche-prompt generation** from *verified* attributes
  (concern × active × format × price × audience × use-case), claim-screened; (b) **demand
  proxy** for un-probed candidates (bounded probe of top-N + community-mention frequency +
  search-volume band + cross-merchant prompt recurrence — *estimated, transparent, never
  promised*); (c) the **"Where you can win" deliverable** (head-terms to abandon + ranked
  winnable niches + per-target action → ③/④).
- **Depends on:** ③ (verified attributes drive candidate quality).
- **Guardrails:** cost-bounded probing (generate → cheap pre-filter → probe top-N only);
  winnable *and* has-demand *and* defensible.

### ③ BUILD THE RECORD — evidence intake + verify + grade + serve  *(the foundation)*
- **Purpose:** turn supplier-supplied **evidence** (not prose) into a verified, graded,
  claim-safe canonical record agents trust. (`docs/SUPPLIER_EVIDENCE_INTAKE.md`.)
- **Reuse (almost all of it):** `ProductClaim`, `canonical_inci_intake` (precedence),
  `crawled_inci_ingest` (URL→INCI), `beauty_evidence` (ingredient→claim), `claim_screening`
  + `claim_safety` (cosmetic-vs-drug + FDA disclaimers), `decision_grade_eval`, GPT-5.5
  `source_grounded` gate.
- **Net-new:** typed **supplier-evidence intake** endpoint/service; the **evidence-locker
  form** (multi-source: brand URL · lab report upload/scan · cert · INCI/label · Reddit/
  social/retailer review links digested **neutrally**); lab-report ingest + OCR (later);
  serve graded claims via the **canonical `content_key` record** (resolves the
  external_seed/authed-merchant overlay disjoint).
- **Replaces:** the #46 v0 free-text form (ADR-001 Option C, rejected).
- **Guardrails:** community = trust/decision signal, NOT claim-substantiation (efficacy
  needs a lab report); claim-safety enforced; provenance on every field.

### ④ CREATE & DISTRIBUTE — the presence engine
- **Purpose:** for startups with nothing to collect, **create** the presence the niche
  needs: content, community answers, KOL/social distribution, authentic UGC.
- **Reuse:** `content_brief` (drafts), `MatchedCreatorCard`/creator-match (KOL — gated on a
  creator API = the known `no_data` gap), `authority_map` (the threads to engage),
  `DraftPitchButton` (outreach). *Most of the "create" side already exists as drafts.*
- **Net-new:** graduate drafts → **executed, tracked distribution**; the creator API; feed
  resulting real signal back into ③'s evidence locker.
- **Depends on:** ② (which niches) + ③ (the record to promote).
- **Guardrail (non-negotiable):** **authentic only** — real/disclosed/FTC+ToS-compliant.
  No fake reviews, no astroturfing. (Pivota digests neutrally anyway.)

### ⑤ SERVE — canonical record → agents
- **Purpose:** the graded record reaches AI agents at read time.
- **Reuse:** `agent_pdp_view`, `/agent/shop/v1/invoke get_pdp_v2`, the overlay merge hook.
- **Net-new / decision:** serve graded claims via the **canonical `content_key` record**
  (not the per-merchant single-field overlay). Resolves: overlay hook fires only for
  `external_seed`; `/contributions` keys to authed-merchant — disjoint. Folds in the old
  tasks #9/#10 (SKU_OPT_OVERLAY_V1) into "serve the canonical record."
- **Guardrail:** serving gate admits only decision-grade SKUs.

### ⑥ MEASURE & DEFEND — the outcomes moat
- **Purpose:** track share gained on the targeted niches; accrue reviews/repurchase/return
  per `content_key` → the compounding moat; re-audit validates; defend won niches.
- **Reuse:** `aggregated_outcomes` (#815), the re-audit/delta loop.
- **Net-new:** target-level share tracking (did we win the niches from ②?); defense signal
  (is a won niche being contested?).
- **Depends on:** transactions/presence (accrues with volume).

---

## 4. Dependency-ordered build sequence

| Phase | Ships | Proves | Depends on |
|---|---|---|---|
| **0 — Diagnosis UI** *(done)* | readable audit (#869/#46/#45) | merchant can read where they stand | — |
| **1 — Foundation: record + serving** | evidence-locker form + typed intake (URL→INCI/claims + neutral social digest) → graded claims; **+ NET-NEW (gate the proof): (a) merchant SKU→`content_key` resolver with confidence-gating; (b) `content_key`-keyed agent read path in PIVOTA-Agent (overlay stays the bridge); (c) brand-official URL discovery; (d) cold-start degradation (Pivota-side crawl seeds the record when the merchant has no sources)** | a wedge SKU reaches decision-grade from supplied evidence; an agent reads it via `get_pdp_v2` | spine + **(a)–(d) are net-new, two-repo, and IN Phase-1 scope** |
| **2 — Strategy: winnable niches** | candidate generation + demand proxy + "Where you can win" view | merchant gets ranked winnable targets, head-terms to skip | Phase 1 (verified attributes) |
| **3 — Presence: create & distribute** | graduate content/community/KOL drafts → executed + tracked; creator API | presence created for the targets; real signal flows back to ③ | Phases 1+2 |
| **4 — Measure & defend** | target share-tracking + defense + outcomes accrual | won niches measured + held; moat compounds | Phases 1–3 + volume |

Each phase ends by **running the Pivota-vs-native eval** on the wedge cohort
(One by Zero/Aruen → Anuko → BB Lab/Ownist), per the data contract's proof gate.

---

## 5. Cross-cutting guardrails (non-negotiable)

1. **Authenticity** — never fabricate signal (reviews, presence, demand). Real, disclosed,
   FTC/ToS-compliant. Fabrication kills the trust moat and Pivota digests neutrally anyway.
2. **Claim-safety** — cosmetic-vs-drug screening + FDA/DSHEA disclaimers, per category,
   enforced in the evidence gate. Pivota owns this liability centrally (a feature).
3. **Cost** — no LLM-call multipliers without bounding (generate→pre-filter→probe top-N;
   worker isolation/quotas; staging load test before any fan-out flip).
4. **Honesty / provenance** — every served fact traces to a source; demand/competition are
   *estimated + transparent*, never promised numbers; no mock data to merchant-facing prose.
5. **Preview-gate UI + canonical-first** — visual changes verified on preview before prod;
   data sourced canonical-first by precedence.

---

## 5.5 Risks surfaced by the gap-check (must-address)

1. **Demand-proxy validity** — the niche proxy (community-frequency + search-volume band +
   cross-merchant recurrence) is unvalidated. **Back-test it against the wedge cohort's real
   probe results before surfacing scores.** Honesty guardrail already says "estimated"; this
   adds calibration.
2. **Multi-merchant conflict on one `content_key`** — precedence handles brand-vs-reseller,
   but two suppliers at the *same* tier (e.g., two resellers) is undefined. Define a tie-break
   (recency? brand-official-only writes? flag-for-review).
3. **Resolution-confidence gating** — a mis-matched SKU→`content_key` pollutes a *shared*
   record (blast radius = every offer). Gate canonical writes on `identity_confidence`;
   low-confidence → human review, never auto-write.
4. **Reddit *participation* ToS** — facilitating *posting* (not just digesting) is far
   stricter than read-only; brand-affiliated/automated posts can break subreddit rules even
   when disclosed. Phase 3 must require **manual human-in-the-loop**, not just "FTC-compliant."
5. **Supplement efficacy hard-block** — supplements (FDA-strict) are built last, behind the
   unbuilt lab-ingest. Until lab ingest exists, **hard-block supplement efficacy claims** —
   don't surface a structure-function claim with only a disclaimer and no substantiation.
6. **Cold-start (merchant with no sources)** — **RESOLVED (decision §7.5):** route the
   merchant straight to **Phase 3 (create presence)** first; then Pivota **crawls the created
   presence** (the new content/community/listing) to verify and build the record. The create
   step *generates* the sources the intake then verifies. Flow: create → crawl → grade.

---

## 6. Build delta — shipped / reuse / net-new (the whole system)

| Capability | Status |
|---|---|
| Audit readability (names, band scorecard, two-path next-step, queue, typed evidence) | ✅ **shipped** (#869/#46/#45) |
| Claim/grade schema, source precedence, INCI authority, claim-screening, disclaimers, decision-grade scoring, GPT-5.5 source-grounded gate, opportunity engine (ownership/demand/density/open-lane/sideways-wedge), social-intel, action-ladder drafts, outcomes table | ✅ **EXISTS — reuse** |
| Typed supplier-evidence intake + evidence-locker form (multi-source) | ❌ net-new (Phase 1) |
| **Merchant SKU → `content_key` resolver (+ confidence gating)** | ❌ **net-new (Phase 1, hidden dep — `matched_content_key` is `None` today)** |
| **`content_key`-keyed agent read path (PIVOTA-Agent) + brand-URL discovery + cold-start crawl** | ❌ **net-new, two-repo (Phase 1); overlay stays the bridge** |
| Lab-report ingest + OCR | ❌ net-new (Phase 1→2) |
| Niche candidate generation + demand proxy (+ back-test) + "Where you can win" view | ❌ net-new (Phase 2) |
| Executed/tracked create-distribute + creator API + Reddit human-in-loop | ◐ partial (Phase 3) |
| Outcomes accrual (`aggregated_outcomes`) | ◐ **blocked on #815 merge** |
| Target share-tracking + defense | ❌ net-new (Phase 4) |

---

## 7. Decisions — LOCKED (founder, 2026-06-14)

1. **Evidence-intake §10** — defaults accepted (brand-official authoritative + flag conflict;
   draft-claim → require source → grade; cert-issuer registry TBD; serve via `content_key`).
2. **Niche targeting** — **focus: surface ~3–5 winnable targets** (not breadth). The merchant
   is **given the option to choose the demand-proxy priority** (probe top-N vs
   community-frequency vs cross-merchant recurrence) — operator controls how demand is weighted.
3. **Create/distribute** — **build the creator API now** (Phase 3), not manual-first.
4. **Serving** — adopt canonical-`content_key` serving as the target; keep the external_seed
   overlay as the **bridge** — **cut over, don't retire**. Tasks #9/#10 become "build the
   cutover," not "flip a flag." ✓
5. **Cold-start (no sources)** — **route the merchant straight to Phase 3 (create presence)
   first; then Pivota crawls the created presence to verify and build the record.** So for
   cold-start, Phase 3 precedes Phase 1's verify step (create → crawl → grade).
6. **#815 (outcomes)** — **merge it** to unblock the Phase-4 moat.

---

## 8. Proof gates (how we know it works)

- **Phase 1:** a wedge SKU (Aruen) goes from thin → decision-grade *from supplied evidence*,
  and `/agent/shop/v1/invoke get_pdp_v2` returns the graded record.
- **Phase 2:** for that SKU, the system names ≥3 winnable niches (open + fit + demand-proxy)
  and the head terms to skip — validated against a manual read.
- **Phase 3:** presence created for ≥1 target (real content/community/creator), tracked.
- **Phase 4:** re-audit shows share gained on a targeted niche; outcomes begin accruing.
- **Throughout:** Pivota-vs-native eval per category shows Pivota's record beats native
  retrieval on find/justify/trust/buy.
