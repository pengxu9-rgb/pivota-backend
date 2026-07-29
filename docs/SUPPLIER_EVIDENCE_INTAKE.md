# Supplier Evidence Intake & Grading

> How a merchant/supplier hands Pivota **verifiable evidence** (not agent-facing
> prose), and how Pivota **verifies, screens, substantiates, grades, and serves**
> it as part of the agent-decision-grade canonical record.
>
> Realizes **ADR-001 action items #2 (canonical-sourcing engine + supplier
> raw-input intake) and #3 (source precedence + provenance tagging)**, and is the
> merchant-facing intake for the [Agent-Decision-Grade Data Contract] Layer 3
> (`claims[] = {claim_text, source_ref, source_type, evidence_grade,
> substantiation_status}` + `required_disclaimers[]`).

**Status:** Draft for founder review · **Date:** 2026-06-14 · **Deciders:** Founder (peng), Claude

---

## 1. The principle (why a free-text form is wrong)

The merchant audit's "Add to your Pivota page" form v0 asked the operator to
**type product content** (what it is / who for / how to use). That is
**ADR-001 Option C — "require merchants to author agent-grade data" — which we
explicitly rejected**: long-tail operators won't and can't author INCI +
substantiated, claim-safe copy, and free-text merchant assertions are *exactly
as untrustworthy to an agent as the merchant's own marketing site* (which agents
already discount ~6.5× vs external authority). If Pivota's canonical record
becomes a dump of unverified merchant prose, agents stop treating it as a
reliable source — destroying the one thing the canonical layer exists to be.

**The correct model (ADR-001 Option A):** the merchant is a **supplier of
evidence and inventory, not an author**. They provide **verifiable raw inputs**
(a brand-official URL, an INCI string, a label photo, a certificate, a lab
report). Pivota **structures, screens, substantiates, grades, and distributes**
it. Every fact that drives a fit-match or backs a claim is **provenance-verified**
(the data contract's "VERIFIED vs PRESENT" rule). Nothing reaches the agent as a
bare assertion.

> The operator's job is to point Pivota at where the truth already lives — not to
> write the answer. This is **easier** for a non-technical operator (paste a link
> / upload a PDF) **and** produces trustworthy data. Lower effort, higher trust.

---

## 2. The model in one line

```
supplier raw inputs / sources
        │  (typed intake — net-new)
        ▼
verify → extract → screen → substantiate → grade   (REUSE existing pipeline)
        │
        ▼
graded claims  { claim_text, source_ref, source_type, evidence_grade,
                 substantiation_status }  +  required_disclaimers
        │  (source precedence: brand-official > supplier > reseller)
        ▼
canonical record (content_key)  →  decision-grade serving gate  →  agent
```

The entire middle band already exists (Section 5). **The only net-new work is
the typed intake + the merchant form (Section 4) and the wiring to serving
(Section 7).**

---

## 3. What the supplier may submit (the intake taxonomy)

The supplier supplies **sources**, by two ingestion mechanisms — **a URL Pivota
scans** or **a file the supplier uploads** — across two evidence purposes. The
operator never types the agent-facing answer; they point Pivota at evidence.

### 3a. Evidence that substantiates FACTS & CLAIMS (the product is what it says)

| Tier | Source | Mechanism | What Pivota does | Reuses |
|---|---|---|---|---|
| **1. Brand-official page** | official product URL | scan | crawl → INCI + facts + brand-stated claims → ingredient-substantiate | `crawled_inci_ingest`, `_is_official_brand_source`, `beauty_evidence` |
| **2. Ingredient list (INCI)** | pasted string, or any URL | scan/paste | parse → `supplier_input` precedence write → actives → substantiated claims | `canonical_inci_intake(..., "supplier_input")` |
| **2. Label / packaging photo** | image upload | upload (OCR) | OCR → INCI + dosage/format/disclaimers (**OCR net-new**) | feeds `canonical_inci_intake` |
| **3. Lab / clinical report** | PDF upload **or** URL to scan | upload/scan | extract → attach as `source_ref` so an efficacy claim can reach `substantiated` (**the only thing that legitimizes an efficacy claim**) | `ProductClaim.source_ref`, claim review |
| **3. Certificate** | upload/URL, **with issuing body** | upload/scan | record cert + issuer → `claimed`→`verified` when issuer recognized | `trust.certifications` (Layer 5) |
| **4. Structured raw fact** | dosage, mol-weight, servings — **each with a source** | typed | provenance-tagged attribute write | category attribute modules |

### 3b. Evidence that builds TRUST & DECISION SIGNAL (real people, real use)

The supplier can also point Pivota at **where this product is actually discussed**
— and Pivota **digests** it into authentic, cited community signal. This is the
highest-value-to-agents class (external authority ≫ self-claim), and the supplier
*can't fake it*: Pivota digests **neutrally** (surfaces the cons, not just praise)
and **augments with its own discovery** so a cherry-picked link can't bias the record.

| Source | Mechanism | What Pivota digests it into | Reuses |
|---|---|---|---|
| **Reddit thread(s)** | URL(s) to scan | sentiment + recurring themes + honest pros/cons, cited to the thread | audit social-intel / SerpAPI search-then-extract, Reddit monitor, `authority_map` |
| **Social posts/handles** (TikTok / IG / YouTube creators) | URL/handle | real-use feedback, creator mentions → decision signal + trust | social-intel callers (`bd_social_search`) |
| **Retailer / review-site pages** (iHerb, Amazon, Olive Young…) | URL to scan | aggregated review themes + rating, cited | retail grounded callers |

**Use differs by purpose — and grade reflects it:**
- 3a sources **substantiate facts/claims** → `substantiation_status: substantiated`,
  source_type `ingredient_mechanism` / `lab_report` / `official_page`. A **lab report
  is required** to surface any efficacy claim; an ingredient mechanism substantiates a
  *cosmetic* benefit; nothing else does.
- 3b sources are **trust & decision signal, never claim-substantiation** — a Reddit
  comment is real, but it is *opinion*, not a clinical study. They feed the dossier's
  `trust` / honest pros-cons / outcomes (ADR-002 "real reviews"), tagged
  `source_type: community` with a link, and are **digested neutrally** (pros and cons).

**Explicitly NOT accepted as authoritative:** free-text marketing prose, bare
benefit assertions with no source. A draft claim is captured, but only enters the
record once tied to a source of the right class for its purpose.

---

## 4. The merchant-facing form (evidence-first — an "evidence locker")

Replaces the v0 free-text fields. Same two-path placement (the "Add it to your
Pivota page" card); only its internals change. The operator **adds sources**; each
is ingested, graded, and shown back — a running list of evidence, not a text box.

**Flow:**
1. **Start with the strongest source.** "Where can Pivota verify this product?" →
   paste the **brand-official product URL**. Pivota scans it and shows back *what it
   verified* ("✓ 23 INCI ingredients, 4 ingredient-grounded claims, vegan claim — from
   aruen.us").
2. **Add more evidence** — a source picker, each routed to the right pipeline:
   - **Lab / clinical report** — upload a PDF *or* paste a URL to scan → substantiates an efficacy claim.
   - **Certificate** — upload (with issuing body) → `claimed`→`verified`.
   - **Ingredient list** — paste, or **upload a label photo** (OCR).
   - **Where it's reviewed** — paste **Reddit / social / retailer-review links** → Pivota
     digests real comments into cited, neutral pros/cons + sentiment. ("Add the threads
     where people actually discuss this — we read them, the good and the bad.")
3. **Each source becomes a graded line** showing what Pivota extracted + its grade/use,
   so the operator sees the trust bar fill and what's still thin.
4. **Per draft claim, require a source of the right class.** Want to assert a benefit?
   The UI asks "what backs this?" — and an **efficacy** claim needs a **lab report**, not
   an ingredient or a review. No qualifying source → kept `unverified`, **not surfaced**.
5. **Grade feedback, inline:**
   - ✓ **Verified** — official page / recognized cert / ingredient mechanism / lab report
   - ◐ **Brand-stated** — first-party, fine for usage/neutral facts, labeled as such
   - ⚑ **Community** — real third-party reviews, cited, neutral (pros *and* cons)
   - ⚠ **Needs a source** — add evidence to make this agent-trusted
   - ✗ **Can't be shown** — drug/medical claim, blocked by claim-safety

This is the supplier intake for the data contract's **common core + category
module** (`category_kind` ∈ skincare/haircare/supplement) — the form asks for the
category's verified attributes and the sources that back them, never generic prose.

---

## 5. The verification + grading pipeline (REUSE — already built)

Each existing component and what it does for an intake. **None of this is
net-new.**

| Stage | Service (file) | Role |
|---|---|---|
| Claim + source schema | `models/catalog.py:134` `ProductClaim` `{claim_text, source_ref, source_type, evidence_grade, substantiation_status}`; `EvidenceProfile.claims` | the unit of graded evidence |
| Source precedence | `services/canonical_inci_intake.py` `may_write()` / `source_rank()` — `brand_official(3) > supplier_input/inci_database(2) > reseller(1)` | a supplier input never downgrades a brand-official fact (ADR-001 #3) |
| Brand-official crawl → INCI | `services/crawled_inci_ingest.py`, `scripts/ingest_crawled_inci.py` | URL → canonical INCI, source-tagged |
| Official-source check | `OfferNode.official_source` — once `OFFICIAL_SOURCE_SELLER_DERIVED` is on, the stored per-offer seller identity (written by `services/offer_seller_identity.py` on the onboard lane, and by an inlined copy of the same rule in `services/external_offer_dual_write.py` on the mirror lane). `services/pivot_query_service.py:_is_official_brand_source()` is the legacy path — a tautology on the mirror lane; see ADR-019 | who is actually selling this offer? |
| Ingredient → substantiated claim | `services/beauty_evidence.py` (`source_type="ingredient_mechanism"`, `evidence_grade="ingredient_inference"`) | INCI active → claim-safe cited benefit (no LLM) |
| Claim-safety / screening | `services/claim_screening.py` (cosmetic-vs-drug per category), `services/claim_safety.py` (statuses `unverified\|substantiated\|flagged\|rejected`; FDA/DSHEA + per-category `required_disclaimers`) | blocks drug claims, attaches mandatory disclaimers |
| Grading | `services/decision_grade_eval.py` — 5 dims (find / justify / trust / buy / compare); "justify" requires `source_ref` + `substantiation_status=="substantiated"` | the decision-grade verdict |
| Governance source-grounding | `services/pdp_governance_service.py` GPT-5.5 gate `source_grounded = bool(source_refs)`; module versions store `source_refs` | a contribution can't publish without a source |

**The grade ladder** (already modeled — see the worked example
`agent_product_record.aruen_tofu_collagen.json`):
`substantiation_status`: `unverified → substantiated | flagged | rejected`;
certifications: `claimed → verified` (when a recognized issuer is attached).
Only `substantiated` (sourced) claims serve as facts; `flagged`/`rejected`
(drug claims) never reach the agent; `unverified` is held or labeled, not
surfaced as authority.

---

## 6. Data model & storage

- The intake writes through the **existing stores**, source-tagged:
  - INCI / actives → `canonical_inci_intake` → `beauty_sku_ingredients` (precedence-gated).
  - Claims → `EvidenceProfile.claims` (`beauty_product_profiles.evidence_profile` JSONB) + `required_disclaimers`.
  - Certs / trust → `catalog_row_trust` / Layer-5 trust.
  - Provenance → `source_refs` on the governance module + per-fact `source_ref`.
- **Net-new:** a typed `supplier_evidence_submission` payload + a record of what
  the operator supplied (URL/doc/cert) and the grade returned — so the form can
  show status and re-verification can re-run. This replaces the free-text `copy`
  module payload as the intake shape.

---

## 7. Serving to agents (and the open keying gap)

Graded claims feed the **canonical record at `content_key`**, which the
decision-grade serving gate (`index_pipeline_state`, data-contract Layer 8)
already requires to carry `≥1 provenance-backed claim + required disclaimers +
reviewed evidence`. So a well-graded intake **promotes the SKU through the gate**
— the merchant's reward for supplying evidence is the SKU becoming agent-servable.

**Carry-over from the audit work (must resolve for "agents read it"):**
- The agent-path overlay merge (`PIVOTA-Agent
  enrichProductWithCatalogPdpContentFields`) currently fires **only for
  `external_seed` products**, and the `/contributions` endpoint keys to the
  **authed merchant** — a disjoint. For the **canonical-first** model this
  resolves cleanly: a supplier's evidence targets the **`content_key` canonical
  record** (shared, often `external_seed`-sourced for crawled brands), not a
  per-merchant overlay. The intake should write to the canonical record by
  `content_key`, and serving reads the canonical record — sidestepping the
  per-merchant-overlay path entirely.
- The v1 overlay carries only `pdp_description_raw`. Graded claims are richer; the
  canonical-record path (not the single-field overlay) is the right serving
  surface. (If an overlay is still used, widen `_OVERLAY_FIELD_MAP` to graded
  claims.)

---

## 8. Build delta — reuse vs net-new

| Component | Status |
|---|---|
| Claim/source/grade schema, claim-screening, disclaimers, source precedence, INCI authority, official-source check, ingredient→claim, decision-grade scoring, governance source-grounding | ✅ **EXISTS — reuse** |
| Typed **supplier evidence intake** endpoint + service (URL / INCI / cert / lab → pipeline) | ❌ **net-new** |
| **Evidence-first form** ("evidence locker": multi-source add, per-claim source, grade feedback) | ❌ **net-new** (replaces v0 free-text) |
| **Lab-report ingest** (PDF upload / URL scan → `source_ref` for efficacy claims) | ◐ partial (claim+source_ref schema exists; PDF/text extract net-new) |
| **Social/community digestion intake** (operator points at Reddit/social/retailer URLs → neutral cited pros-cons + sentiment) | ◐ partial (audit social-intel / SerpAPI search-then-extract / Reddit monitor / `authority_map` EXIST; the merchant-supplied-source intake + per-product digest write are net-new) |
| **OCR** for label/cert photos | ❌ net-new (Tier-2/3 only — defer) |
| **Cert-issuer verification** (claimed→verified) | ◐ partial (status modeled; recognized-issuer registry net-new) |
| Serving graded claims by `content_key` (resolve the external_seed/overlay disjoint) | ◐ partial (canonical path exists; intake-write-by-content_key net-new) |

---

## 9. Phasing

1. **v1 — URL-first + community signal, the wedge (skincare):** brand-official URL
   intake → existing crawl/INCI/claim pipeline → graded claims on the `content_key`
   record → grade feedback in the form. Plus **social-source digestion** (operator adds
   Reddit / review-site URLs → cited, neutral pros/cons), since that machinery already
   exists and it's the highest-trust-to-agents addition. (One by Zero / Aruen.)
2. **v2 — no-crawlable-source + substantiation:** paste-INCI + certificate (issuer);
   **lab-report ingest** (PDF/URL → substantiate efficacy claims); label-photo OCR.
   (Anuko vegan-cert verification.)
3. **v3 — supplements (strictest):** dosage/molecular-weight/allergen structured
   intake + FDA claim-safety + mandatory disclaimer enforcement. (BB Lab / Ownist.)
4. Each: prove the SKU passes the decision-grade gate → run the Pivota-vs-native eval.

---

## 10. Open decisions for the founder

1. **Source conflict precedence** when a supplier input contradicts the
   brand-official record (ADR-001 "to revisit"): keep brand-official authoritative
   and flag the conflict? (Recommended.)
2. **Draft-claim capture:** allow the operator to type a benefit and *then* require
   a source (keeps the UX guided), vs. only accept structured sourced inputs
   (stricter, less guided). Recommended: capture draft → require source → grade.
3. **Cert-issuer registry:** which certifying bodies count as "verified" for
   claimed→verified (vegan, Leaping Bunny, GMP, 3rd-party-tested)?
4. **Serving surface:** confirm graded claims serve via the **canonical
   `content_key` record** (not the single-field per-merchant overlay), which also
   resolves the external_seed/authed-merchant disjoint from the audit work.

---

## 11. Beyond intake: collect → create → distribute (the startup reality)

Sections 3–10 are the **collect** mode: verify + grade evidence the merchant
*already has*. But the target segment — medium/long-tail K-beauty **startups** —
often has almost nothing to collect: no polished brand page, no lab report, no
Reddit footprint, no KOL relationships. For them, an intake form alone is a blank
page. Pivota's job extends to **helping them create and distribute the presence**,
not just file it. This is the long-tail thesis (ADR-001) pushed one layer out:
the merchant lacks not only the *data* but the *demand and proof* — Pivota supplies
both, neutrally and authentically.

**Two modes, one engine:**

| Mode | The merchant has… | Pivota… | Status |
|---|---|---|---|
| **Collect** (this spec, v1) | existing sources (page, lab, reviews) | verifies, grades, serves | building now |
| **Create & distribute** (roadmap) | a product and little else | helps generate the missing presence | **mostly already exists in the audit action ladder** |

**The "create" actions — and what already exists to reuse:**
- **Draft the content** they don't have → `content_brief` generator (EXISTS; produces sourced briefs).
- **Answer where buyers ask** → facilitate *genuine, disclosed* participation in the
  Reddit/community threads the audit already surfaces (`authority_map`, source-route playbooks).
- **KOL / creator distribution** (Instagram / TikTok) → creator matching (`MatchedCreatorCard`,
  matched-creator NBA EXISTS; gated on a creator API — the known KOL `no_data` gap).
- **Authentic UGC / reviews** → seed **real** reviews via real customers/creators (then digest
  them neutrally per Section 3b).
- **Outreach / editorial pitches** → `DraftPitchButton` mailto drafts (EXISTS).

So the audit already *diagnoses* the demand and gaps and *drafts* the outreach,
creator, and content moves. The roadmap is to turn those from "here's a draft" into
**executed, tracked distribution** — and to feed the resulting real presence back
into the evidence locker (Section 3b) as graded community signal. The loop:

```
audit finds the gap → Pivota creates the presence (content / community / KOL) →
real third-party signal appears → evidence locker digests it (neutral, cited) →
canonical record reaches decision-grade → agent recommends → outcomes accrue
```

**Non-negotiable guardrail — authentic only.** Everything created must be **real,
disclosed, and FTC/ToS-compliant**: genuine founder answers (not astroturfing),
*disclosed* creator partnerships, *real* customer UGC. Pivota **never manufactures
fake reviews or undisclosed promotion** — that is illegal (FTC), violates platform
ToS (Reddit), and would destroy the exact trust moat the canonical record exists to
build. Pivota digests neutrally regardless (cons surfaced), so fabricated praise
gains nothing.

**Sequencing.** Ship **collect (v1)** first — a record must be decision-grade
*before* it's worth distributing, and the evidence locker is the substrate the
create/distribute actions write back into. The create/distribute layer then
graduates the audit's existing drafts (content/creator/outreach) into executed,
measured growth. Specced separately when v1 proves the loop.

---

[Agent-Decision-Grade Data Contract]: /tmp/pivota-kbeauty-data-contract.md
