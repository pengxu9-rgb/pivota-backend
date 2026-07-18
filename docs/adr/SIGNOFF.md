# Founder Sign-off — Agentic-Commerce ADR Set

**Status:** **DRAFT for founder (peng) review.** Nothing below is Accepted until you sign each line. This is a decision aid, not a decision.
**Date:** 2026-07-18
**Prepared by:** eng (with Claude), from the ADRs in `docs/adr/`.
**Read this first:** the set has converged on a coherent position, and **one decision (ADR-016) reframes the rest** — accepting or rejecting it is the point of this review. Two facts up front:
- **Nothing is live.** The AP2 rail is fully built but **flag-gated off** (`ENABLE_AP2_ROUTES=false`). Accepting these ADRs changes *plans*, not production. Turning AP2 on is a separate gated checklist (`AP2_ENABLEMENT.md`).
- **The bet is attribution, not payments.** ADR-016 *removes* the transaction-custody / money-transmitter track (Pivota's existing channel-partner rev-share **payout** rail is separate and unaffected); in exchange, Pivota's revenue depends on proving it drove a sale from *outside* the fund flow (ADR-017). That is the real thing you're signing up for.

---

## The load-bearing decision — ADR-016: Pivota is non-custodial

- **Decision:** Pivota is never in the **buyer→merchant transaction fund flow** (never holds / routes / cuts those funds). It brokers trust (identity / consent / mandate + a **signed receipt**), provides settlement **routing**, and records transactions for **attribution**. Money settles agent↔merchant on their own rails. Value = attribution/referral (merchant-side) + identity/authorization/receipt brokering (agent/protocol-side) — **never payment fees**.
- **What "yes" buys:** deletes the money-transmitter / custody / reserves track **on transaction funds** *and* the ledger/custodian build; keeps the moat on information + trust; one money boundary across the ACP and AP2 rails.
- **What "yes" bets:** that Pivota can *prove it drove a sale* while outside the fund flow — i.e. that attribution (ADR-017) works. The audit shows attribution is weakest exactly there today; **this is the real risk of the position.**
- **What "no" means:** Pivota becomes (some form of) a payments company — custodian or PSP-facilitator — inheriting money-transmission regulation, reserves, and a compliance org. A different, heavier, lower-margin business.
- **Reversibility:** high — "yes" only *removes* planned build; a later "no" is a deliberate re-entry into custody.
- **This is squarely your call.**

## The set, in dependency order

| ADR | Decision (one line) | Whose call | Status |
|---|---|---|---|
| **016** Non-custodial | Never in the buyer→merchant fund flow; brokers trust + attribution | **Founder** | Proposed |
| **007** Citable index | Open index for citation; curated gate for first-party transact | — | **Accepted** (2026-06-24) |
| **012** Rail boundary + identity | AP2 = signed/external rail; API-key ACP = partner/PSP; identity rooted in **DID/VC, never a platform key** | **Founder** + Commerce/Trust | Proposed |
| **014** Interop model | One canonical spine; each protocol a thin edge adapter; never fork the core | Architecture (founder-aware) | Proposed |
| **015** Authorized action | One deny-by-default authorization gate (action + limit + presence) across mandate / consent / token | Security (founder-aware) | Proposed |
| **017** Attribution integrity | Prove Pivota drove the sale via a reported/observed conversion keyed on `click_id` + signed receipt — non-custodial | **Founder** + Commerce | Proposed |
| **013** AP2 settlement | *(a Pivota-held ledger — money-touching)* | — | **Superseded by 016** |

## What accepting each commits you to

- **ADR-012 (rail boundary + identity).** Two payment rails selected by *caller-class × settlement* (not interchangeable), and agent identity rooted in **self-sovereign DID/VC, never a platform-issued API key**. Already built (did:key / did:web, mandates). **Your question:** comfortable that partner agents keep the lighter bearer-key rail while *external* agents must present a DID? (That asymmetry is the crux.)
- **ADR-014 (interop model).** Absorb new agent protocols (Visa TAP, Mastercard Agent Pay, …) as edge adapters over one canonical core, *never forking the core*. Low-risk engineering discipline; your awareness matters because it's the "how we stay neutral across protocols" bet.
- **ADR-015 (canonical authorized action).** One deny-by-default authorization gate across all rails; **the consent slice is built + merged** (#1475). Security-owned; your awareness: this is what makes "the user authorized this" enforceable and auditable.
- **ADR-016 (non-custodial).** The load-bearing call — see above.
- **ADR-017 (attribution integrity).** Make attribution an *information* problem (reported conversions, signed-receipt join key, fallback + observability) rather than by re-entering the order/money. This is **how ADR-016 makes money**, so accepting 016 implies committing to 017's direction. **Your question:** OK that attribution coverage is a *measured* number (not 100%) we price against — vs. demanding total coverage, which would require orchestrating checkout, contradicting the positioning?

## Risks to own before signing
1. **The bet is attribution, not payments.** ADR-016 removes custody risk but makes revenue depend on non-custodial attribution — the weakest link today (audit → #1479–1482). Signing 016 is signing up to build 017.
2. **Nothing is live** — the flag flip is a separate gated step; accepting these ADRs is not a go-live.
3. **CI is dark** (GitHub Actions billing) — the merge gate is currently off; worth restoring before more builds land.
4. **Regulatory (residual):** even non-custodial, *if* you ever add a "thin settlement confirmation" (ADR-017's "must revisit"), get counsel first.

## Sign-off
- [ ] **ADR-016** — Pivota is non-custodial — *Accepted*
- [ ] **ADR-012** — rail boundary + DID/VC identity — *Accepted*
- [ ] **ADR-014** — interoperability model — *Accepted*
- [ ] **ADR-015** — canonical authorized action — *Accepted*
- [ ] **ADR-017** — attribution integrity (direction) — *Accepted*
- [ ] **ADR-013** — acknowledged **Superseded by ADR-016**

Founder: ______________________   Date: ____________

*Record the sign-off here (or in the merge/deploy trail) so the decisions are auditable — per ADR-012's governance note.*
