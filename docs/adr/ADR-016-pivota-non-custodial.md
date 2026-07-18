# ADR-016: Pivota is Non-Custodial — settlement is delegated; Pivota brokers trust + attribution

**Status:** Proposed
**Date:** 2026-07-18
**Deciders:** Founder (peng) — positioning; Commerce / Trust owners
**Supersedes:** **ADR-013** (AP2 Settlement Model) — whose adopted Option A ("an internal, Pivota-held agent ledger that `confirm` debits") makes Pivota custody agent funds, contradicting the positioning decided here.
**Part of:** the agent-protocol interoperability model — **ADR-014** (meta). This ADR fixes the **settlement layer's** money boundary (Pivota routes, never executes) and names a concern the positioning makes load-bearing: **attribution**.
**Relationship:** aligns with **ADR-007** (Pivota is the citable index + decision layer) and **ADR-012** (the API-key ACP rail already settles via the *merchant's* PSP — Pivota orchestrates, never holds). Companion: `docs/AP2_ENABLEMENT.md`.
**Scope:** the platform-wide **money boundary** and how it re-scopes the AP2/x402 rail + attribution. Does not change identity/consent/mandate (ADR-012) or the read surface (ADR-007).

---

## Context

Pivota is the **neutral commerce index + decision layer** that connects agents and merchants — an **information flow** between two parties. **By design Pivota does not touch money.** The API-key ACP rail already embodies this: the *merchant's* PSP settles; Pivota orchestrates the checkout and never holds funds.

ADR-013 (Proposed, same session) drifted from that. Framed as "how an AP2 transaction moves value," it adopted **Option A: an internal Pivota-held agent ledger that `confirm` debits, backed later by a custodian** — making Pivota a **custodian of agent funds** (money transmission, stored value, the full regulatory surface), the exact thing the positioning rejects. The drift was invisible because the question was "how does *Pivota* settle," when the correct question is "**does Pivota settle at all?**" — and the answer is **no**.

This ADR sets the money boundary platform-wide, supersedes ADR-013, re-scopes the AP2 rail, and names the concern the positioning makes revenue-critical: **attribution** — if Pivota captures value from *proving it drove the sale* rather than from the payment, attribution is a product, not plumbing.

### Forces at play
- **Custody is a different company.** Holding agent/merchant funds triggers money-transmitter / stored-value regulation (no de-minimis in several US states), reserve/segregation duties, and a compliance org — a categorically heavier, lower-margin business than an info/trust layer.
- **The moat is information + trust, not payments.** Payments are commoditizing across protocols (x402, ACP, PSPs). Answer-completeness, substantiated product truth, and non-repudiable authorization are defensible and compounding (ADR-007).
- **The parties already have rails.** Merchants have PSPs; agents have wallets (x402) or delegated tokens (ACP). Pivota inserting itself into the fund flow adds risk and cost and removes none.
- **Pivota's leverage is the artifacts, not the funds.** The **signed receipt** (who authorized what) and the **attribution record** (that Pivota's index drove the sale) are pure information — and are exactly the two things Pivota monetizes.

## Decision

**Pivota is non-custodial. It never holds, routes-through, or takes a cut of the funds. Money settles directly between agent and merchant on their own rails — the merchant's PSP for the ACP rail; agent-wallet↔merchant-wallet via x402 for the AP2 rail. Pivota's role at the transaction is to broker trust (identity + authorization + a signed receipt), provide settlement *routing* (where/how to pay), and record the transaction for *attribution*. Value is captured from attribution/referral (merchant-side) and identity + authorization + receipt brokering (agent/protocol-side) — never from the payment.**

Concretely:

1. **Supersede ADR-013.** There is no Pivota agent ledger, no custodian, no Pivota-held float. `confirm` does **not** move value.
2. **AP2 settlement = direct, non-custodial.** On the x402 rail, funds move **agent wallet → merchant wallet** on the parties' rail. Pivota supplies the merchant's **settlement routing** (network + address / PSP endpoint), verifies/records the outcome, and is **never in the transfer path**. `x402_transactions` is an **attribution + audit record**, not a balance store.
3. **`wallet/balance` is out of scope for Pivota — permanently.** Pivota does not hold the balance, so it does not report one. Keep the `501` (or, if ever needed, a thin pass-through read of the agent's own custodian). This stops being a "gap awaiting a ledger" and becomes the correct answer.
4. **The two monetized artifacts are first-class products:**
   - **Signed receipt** (identity + authorization proof) → the **agent/protocol** product.
   - **Attribution record** (Pivota-index-drove-this-sale proof) → the **merchant** product. Attribution gets its own audit + ADR — it is revenue, not telemetry.
5. **ADR-014's settlement port routes, never executes.** `select_settlement` returns *where/how the parties settle*; the core records the outcome — no Pivota-side `debit`/`transfer`.
6. **The consent spending-limit gate (#1475) stays, re-framed.** `debit_within_limit` no longer debits a Pivota-held balance — it enforces an **authorization ceiling**: the cumulative amount Pivota will *authorize/attest* against a consent must stay within the user-granted limit. The code (atomic conditional UPDATE) is unchanged and correct; its *meaning* is "authorized-to-date ≤ user's cap," an attestation control, not custody. (`spent_amount` reads as "authorized to date.")

## Options Considered

### Option A: Non-custodial broker — Pivota routes + records, the parties settle (this ADR)
**Pros:** no money-transmitter/custody regulation; keeps the moat on info + trust; the parties' rails already exist; the monetized artifacts (receipt, attribution) are pure information; one money boundary across ACP + AP2.
**Cons:** Pivota cannot *guarantee* settlement (it doesn't control the funds) — it attests authorization and records outcome, so parties could transact off-Pivota and evade attribution (the **attribution-integrity** problem — see action items); reconciliation depends on the parties/rails reporting back.

### Option B: Custodial settlement — Pivota holds a ledger/float (ADR-013 Option A — superseded)
**Pros:** Pivota can guarantee settlement; tightest attribution (it sees every cent).
**Cons:** **money transmission + custody regulation**, reserves, a compliance org; a categorically heavier, lower-margin business; contradicts the positioning; concentrates the exact risk the neutral-layer strategy avoids. **Rejected.**

### Option C: PSP-facilitator — Pivota is merchant-of-record / routes funds through its own PSP
**Pros:** real settlement on familiar rails.
**Cons:** still in the fund flow (merchant-of-record liability, chargebacks, KYC); still a payments company — a softer custody, same category error. **Rejected.**

## Trade-off Analysis

The decision is **guaranteed settlement + total attribution (B/C) vs. zero custody + a defensible info/trust moat (A)** — indexed to *what business Pivota is*. B/C buy settlement certainty by **becoming a payments company**, inheriting its regulation, margins, and risk, and eroding the neutrality that makes the index trustworthy. A keeps Pivota an information layer: it cannot force settlement, but it doesn't need to — it sells **proof** (authorization + attribution), and the money is the parties' concern. A's one real cost — **attribution integrity** (knowing/proving the sale happened while outside the fund flow) — is an *information* problem Pivota is uniquely suited to solve (signed receipts, merchant/rail report-back, read↔order linkage), not a reason to take custody.

Decisive factor: **the moat and the margins are in information and trust; custody adds regulation and risk without strengthening either. Pivota sells proof, not payment.**

## Consequences

**Becomes easier / de-risked**
- The entire money-transmitter / custody / reserves regulatory track **disappears**.
- ADR-013's ledger / custodian / FX-execution build is **dropped** — roadmap reclaimed.
- `wallet/balance` `501` and "no balance source" stop being gaps — they are the correct answer.
- One coherent money boundary across ACP (merchant PSP) and AP2 (x402 direct).

**Becomes harder / must be owned**
- **Attribution integrity** is now the crown-jewel problem: outside the fund flow, Pivota must *reliably prove* it drove a sale (signed receipts + read↔order linkage + merchant/rail report-back). This is the revenue mechanism — it gets its own audit (in progress) and likely its own ADR.
- **Settlement routing + verification** must be added to the transaction record (the merchant's pay endpoint; a non-custodial way to confirm the parties settled) without touching funds.
- **Reconciliation** becomes "did the parties settle what Pivota authorized/attributed" — a report-back problem, not a Pivota-ledger balance.

**Must revisit if**
- A pilot shows attribution is impossible outside the fund flow (parties systematically evade the record) → reconsider a *thin, non-custodial* settlement-orchestration role — but only after attribution has genuinely failed as an information problem, and with counsel.

## Action Items
1. [ ] **Founder sign-off** on the non-custodial money boundary. Move to Accepted; mark ADR-013 **Superseded by ADR-016**.
2. [ ] **Stop ledger/custodian work** and update ADR-013's status line to Superseded.
3. [ ] **Re-scope AP2:** `confirm` records + attests + routes (no value movement); reframe `debit_within_limit` as an authorization ceiling (attestation, not a fund debit); add **settlement routing** (merchant network + address / PSP endpoint) to the transaction record.
4. [ ] **Correct `AP2_ENABLEMENT.md`:** the auth rail is complete *and* settlement was never Pivota's deliverable — the "moves money / Bar 2" scope is out by design.
5. [ ] **Attribution audit → ADR:** determine whether the read → decision → transaction → proof loop closes today (in progress), then own attribution integrity as a product in its own ADR.
6. [ ] **ADR-014 settlement port:** document that it *routes*, never *executes*.
7. [ ] **Monetization surfaces:** define attribution/referral (merchant) + identity/authorization/receipt (agent/protocol) pricing on top of the two artifacts.

## Rollback

Positioning-level; adds no runtime behavior on its own. Reversing means re-opening custody (ADR-013 / Option B / C) — a deliberate decision to become a payments company, with the regulatory program that implies. The non-custodial default is additive and safe: it *removes* planned build (ledger / custodian) rather than adding runtime surface.
