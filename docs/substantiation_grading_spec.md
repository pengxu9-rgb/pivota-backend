# Substantiation Grading — Spec

Status: DRAFT for founder review (2026-06-23). Author: build agent.
Scope: the one substantive remaining P1 piece — turning brand-submitted evidence
into a *substantiated*, served claim.

---

## 1. Why this exists

The brand lifecycle is:

```
unclaimed → claimed → attested → substantiated
```

Today a brand can do everything up to and including **attested**: it claims
(DNS / email / support), attests its own content (title, description, bullets,
usage — all reach agents), and **submits lab evidence that is durably stored**
(`brand_attestation_evidence`, `grading_status='submitted'`). But **nothing
advances `attested → substantiated`** — the lifecycle's trust apex is unreachable.

`substantiated` is the level an agent can cite a brand's claim *with evidence
backing*. The grading step is the deliberate review that turns submitted
evidence into a substantiated, served claim.

This was built as a **separate, explicit step on purpose**: submitting evidence
is NOT the same as verifying it (the hard lesson from the verify-was-off
incident). Grading must never be implicit.

---

## 2. What already exists (do NOT rebuild)

| Piece | Where | Notes |
|---|---|---|
| `advance_product_to_substantiated(merchant_id, product_key)` | `services/claim_state.py` (#1002) | The primitive. Guarded `attested → substantiated` only. **Zero callers today** — it waits for this step. |
| `brand_attestation_evidence` table | mig 163, `db/brand_attestation_evidence.py` (#1002) | Columns: `evidence_ref`, `evidence_kind`, **`grading_status`** (`submitted` \| `substantiated` \| `flagged` \| `rejected`), `graded_at`, `graded_by`, `notes`. |
| `substantiated_claims()` serve gate | `services/claim_safety.py`, used by `routes/agent_pdp_v1.py` | agent_pdp_v1 emits only substantiated claims as `evidence_claims`. **So "substantiated" already controls what agents see** — grading is what populates it for brand-attested claims. |
| Audit substantiation signals | `_has_substantiation` / "Substantiated claims" in the audit | Existing per-audit substantiation context the grader can lean on. |
| Employee-gated endpoint pattern | `get_current_employee`, e.g. `POST /api/brands/claim/{id}/approve` (#1007) | Mirror this exactly for the grading endpoints. |

The data model and the serve gate are **already in place**. The missing piece is
the **grading action** that writes `grading_status` and conditionally calls the
advance primitive.

---

## 3. The grading model — the three decisions

### (A) Who grades?

- **MVP — employee (manual).** Mirror #1007's support-assisted claim approval: a
  Pivota employee reviews the submitted evidence + the product's attested claims,
  grades each evidence row, and on `substantiated` advances the SKU. Lowest risk,
  accountable, no new ML.
- **Phase 2 — automated assist.** An automated pre-screen (does `evidence_ref`
  resolve to a real document? does an LLM-judge say the evidence supports the
  *specific* attested claim?) that **suggests** a grade; an employee confirms — or
  auto-advance only above a high-confidence threshold on a narrow claim type.
  **Never auto-rubber-stamp** (verify-off lesson).

> Recommendation: **MVP manual now; automated assist as a clearly-separate later phase.**

### (B) By what rule?

- **Granularity.** Grade per **evidence row** (the `grading_status` column is
  per-row). Derive the product outcome from the rows.
  - Decision: does ONE substantiated row substantiate the product, or must EACH
    attested claim have its own backing evidence?
  - Recommendation: **product advances to `substantiated` when it has ≥1 evidence
    row graded `substantiated`** that backs a material claim. Per-claim mapping is
    a later refinement (`evidence_kind` + `notes` capture which claim it backs).
- **Criteria the grader applies** (substantiated / flagged / rejected, with a note):
  1. authentic + resolves (the ref opens a real document);
  2. credible source (accredited lab, registry, official certificate);
  3. actually supports the *specific* attested claim ("77% heartleaf" → an
     ingredient assay; "dermatologist-tested" → a test report);
  4. current (not expired).

### (C) What does `substantiated` unlock? (make grading worth it)

- **Serve.** Substantiated claims are emitted by `substantiated_claims()` → agents
  cite them *with* backing. Already wired; grading populates it for brand-attested
  claims.
  - Decision: surface a **"substantiated / evidence on file"** block on the served
    `agent_pdp_view` (evidence_kind + graded date — **never the raw document**) so
    agents/users see the backing? Recommend **yes** (Phase 1.5; same pattern as the
    bullets/usage serve work).
- **Rank.** The plan's merit-ranking signal (`merchant_reputation` / substantiation,
  currently `w_business=0`) should be fed by substantiation — **never take-rate**.
  Future; grading produces the signal.
- **Syndicate.** A higher-trust syndication gate (`substantiated > attested`).

---

## 4. Data model — what (if anything) to add

- Evidence rows + `grading_status` + `graded_by` / `graded_at` / `notes`: **exist** (mig 163).
- `claim_state='substantiated'`: **exists**.
- **Optional (per-claim backing):** today evidence is product-level (keyed by
  `product_key`). Linking an evidence row to the *specific* claim it backs would
  need a `claim_ref` column or a claims table. **MVP keeps product-level**; per-claim
  is a refinement.
- **Optional (serve the badge):** to surface substantiation on the served PDP, add
  a `substantiation` summary to `agent_pdp_view` (evidence_kind + graded_at, no raw
  doc) — a small migration mirroring the bullets/usage work. Phase 1.5.

---

## 5. API surface (MVP — employee grading)

Mirror #1007 (employee-auth, flag-gated, best-effort):

- `GET /api/admin/brands/evidence?status=submitted` — the **grading queue**:
  pending evidence rows joined to the product's attested claims + the audit's
  existing substantiation signals, for review.
- `POST /api/admin/brands/evidence/{evidence_id}/grade`
  - body: `{ grade: "substantiated" | "flagged" | "rejected", notes?: string }`
  - updates the evidence row (`grading_status`, `graded_by` from the employee
    token, `graded_at`, `notes`);
  - when `grade == "substantiated"` → `advance_product_to_substantiated(merchant_id,
    product_key)` for the evidence's product (best-effort) + refresh the served PDP
    so the substantiated claim/badge appears;
  - records the grader (accountability), exactly like manual claim approval.
- Service: `grade_evidence(evidence_id, *, graded_by, grade, notes)` — the write +
  the conditional advance.

---

## 6. Build phases

| Phase | Scope | Size |
|---|---|---|
| **1 (MVP)** | grading queue (GET) + grade endpoint (POST) + `grade_evidence` service + advance-on-substantiated. Employee-gated, flag-gated, best-effort. Reuses #1002/#1007. | ~1 PR |
| **1.5** | substantiation badge/block on `agent_pdp_view` (the backing visible to agents/users). | ~1 PR + small migration |
| **2** | automated pre-screen (resolve check + LLM-judge "does this evidence support this claim") → suggests a grade; employee confirms. Optional high-confidence auto-advance for narrow claim types. | larger |
| **3** | feed substantiation into merit ranking (when that signal is built). | depends on ranking work |

---

## 7. Honesty guardrails (load-bearing)

- Submitting evidence NEVER auto-substantiates (already true). Grading is explicit.
- Don't auto-advance on a weak automated check (verify-off lesson). Automated =
  suggestion + employee confirm, or a very narrow high-confidence auto-path.
- A served "substantiated" claim MUST be backed by a graded evidence row. Never
  label unsubstantiated claims as substantiated.
- `rejected` / `flagged` evidence does NOT advance. A previously-substantiated claim
  whose evidence is later rejected should be revisitable — a **downgrade path**
  (`substantiated → attested`) — log the need now, build later.

---

## 8. Founder decisions needed before building

1. **Grading actor for MVP** — employee-only (recommended), confirm? (vs. wait for automated.)
2. **Granularity** — product-level (≥1 substantiated row → product substantiated) for MVP (recommended), or require per-claim backing now?
3. **Serve the badge** — add a "substantiated / evidence on file" block agents can cite (recommended, Phase 1.5), or keep substantiation gate-only/internal for now?
4. **Downgrade path** — handle "evidence later rejected → downgrade" now, or log + defer (recommended)?
5. **Grader authority** — any employee, or a specific permission (`require_employee_permissions(['substantiation_grade'])`)?
