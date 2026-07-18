# ADR-013: AP2 Settlement Model — how an AP2 transaction actually moves value

**Status:** Proposed
**Date:** 2026-07-18
**Deciders:** Founder (peng) — settlement strategy; Commerce / Trust / Finance owners
**Part of:** the agent-protocol interoperability model — **ADR-014** (meta). ADR-013 is its **settlement-layer** instance: the rails an adapter's `select_settlement` chooses (ledger / PSP / on-chain), x402 being one such rail.
**Builds on:** ADR-012 (agent transaction rail boundary) — which decided *that* AP2 is the signed rail for external + stablecoin-settled transactions and how it roots identity, but **explicitly deferred how settlement executes** (ADR-012 Action Item #8 "reconciliation owner … before AP2 carries real money"; and "Must revisit if x402/wallet settlement proves unviable"). Companion: `docs/AP2_ENABLEMENT.md` (§6 known limitations); [PR #1471](https://github.com/pengxu9-rgb/pivota-backend/pull/1471) (wallet/balance → `501`).
**Scope:** the **settlement leg** of the AP2 rail only — what `/ap2/transaction/confirm` does to move value, and where `/ap2/wallet/balance` reads a balance from. Out of scope: identity/mandates (ADR-012), discovery (ADR-007), and the API-key ACP rail's existing PSP settlement.

---

## Context

ADR-012 established AP2 as the **signed rail for external + stablecoin-settled** agent transactions and built its hardest part — DID/VC identity, scoped consent, nonce-replay protection, signed Intent→Cart→Payment mandates, platform-signed receipts. That is the **authorization** layer: proving an agent is *entitled* to pay. ADR-012 deliberately left one question open (its Action Item #8): **how the money actually moves.**

Today it does not move at all. Verified 2026-07-18:

- **`/ap2/transaction/initiate`** = `INSERT` a `pending` row into `x402_transactions` + sign a receipt.
- **`/ap2/transaction/confirm`** = verify the caller's wallet is registered, then `UPDATE x402_transactions SET status='completed'`. **No debit, no transfer, no external call.**
- **`/ap2/wallet/balance`** → **errors** on phantom columns today (`SELECT balance, currency, last_updated …` against a table that has none); becomes an honest `501` once [PR #1471] lands — the 501 baseline the rest of this ADR assumes.
- **`/ap2/x402/exchange`** → `501`; **`/ap2/x402/quote`** reads a manually-fed rate table.
- **`agent_wallets`** is an address registry with **no balance store**; there is **no on-chain RPC, no custodian client, and no settlement service** anywhere in the codebase. `adapters/ap2_payment_adapter.py` exists but is wired to nothing (referenced by no route).

So a "completed" AP2 transaction is a **signed bookkeeping record that moves zero value.** The rail can authorize a payment it cannot execute. ADR-012's strategic label for the rail — "stablecoin/x402 settled" — describes an *intent*, not a built mechanism.

This ADR decides the settlement mechanism, distinguishing the **pilot** (a small, known cohort, near-term) from the **north-star** (ADR-012's stablecoin vision).

### Forces at play
- **The auth rail is real; the value rail is empty.** The expensive, novel work (cryptographic agent identity) is done. Settlement is "plumbing" — but it is the plumbing that makes AP2 a payment system rather than a signature demo.
- **No crypto infrastructure exists.** On-chain/custodian stablecoin — the ADR-012 north star — is a large greenfield build (chain RPC or a custodian like Circle/Fireblocks; key management; deposit/withdraw; finality/reorg handling). Nothing is started.
- **A funding on-ramp is unavoidable.** Whatever settles, an agent's spendable value has to *come from somewhere*. That on-ramp (card top-up, crypto deposit, or manual credit) is itself a build; the settlement model determines how heavy it is.
- **Correctness debt in `confirm`.** The current `UPDATE` matches on `transaction_id` only — it does **not** check the transaction **exists**, **belongs to the caller**, its **current status**, or its **amount**. Any settlement mechanism must make `confirm` **atomic** (value move + status flip in one DB transaction), **idempotent** (a replayed confirm settles once), and **authorized** (bound to the agent + the mandate-authorized amount).
- **Custody = regulatory surface.** Any model where Pivota holds agent funds (prepaid float, or custodian sub-accounts) touches money-transmission / stored-value regulation. A real cost that scales with real external funds — though manageable at a Pivota-funded pilot scale.
- **Don't re-litigate ADR-012's boundary.** PSP-settled transactions were routed to the **API-key ACP rail** (`/orders/*`). A settlement model that puts card/PSP settlement *under AP2* re-opens the boundary ADR-012 drew.

## Decision

**Adopt Option A: settle AP2 transactions against an internal, Pivota-held agent ledger — a real balance that `confirm` debits atomically and `wallet/balance` reads. Treat this as the off-chain accounting layer that the ADR-012 stablecoin north-star (Option C — custodian/on-chain) later *backs*, not a throwaway. Do not settle AP2 via PSP/card (Option B) — that is the API-key ACP rail's job per ADR-012.**

Phased, so the pilot is not blocked on building the ledger or custody:

| Phase | `confirm` settles by… | `wallet/balance` returns… | Funding on-ramp | Unblocks |
|---|---|---|---|---|
| **0 — auth-only pilot** | recording the signed transaction; **value settled out-of-band** (invoice / manual) | still `501` (documented) | none (out-of-band) | pilot the *authorization* rail now; prove demand |
| **1 — ledger settlement** *(this decision's core)* | **atomic debit** of the agent ledger + **credit** to a merchant-payable balance | the agent's ledger balance | manual/admin credit, or a Stripe **top-up** (card→credits) | AP2 actually moves value; `wallet/balance` gets a real source |
| **2 — custodian-backed** *(north star)* | the same ledger debit, now **backed by real USDC** held at a custodian; deposits/withdrawals settle on-chain | ledger balance, reconciled to custodian | crypto deposit via custodian | ADR-012's stablecoin rail, for real |

The ledger is the **single source of truth** at every phase; phase 2 changes where the *float* lives (Pivota bank ↔ custodian/on-chain), not the accounting seam. That makes **A→C an evolution, not a rewrite.**

## Options Considered

### Option A: Internal agent ledger (custodial, off-chain) — this ADR
A new `agent_ledger` (balance credit/debit with entries keyed to `x402_transactions.transaction_id`: agent-debit, merchant-payable-credit). `confirm` runs the debit + status flip in one transaction; `wallet/balance` returns the agent's balance.

| Dimension | Assessment |
|---|---|
| Complexity | **Low–Med** — a ledger table + service; reuses the balance-mutation/ledger pattern already in `credit_ledger` (migration 106), `partner_settlement_service`, `merchant_credit_balance_service` |
| External dependency | **None** — no chain, no custodian, no PSP at settle time |
| Time-to-pilot | **Fast** — phase 1 is a bounded backend change |
| Regulatory | **Medium** — Pivota holds agent float (money-transmission surface); bounded at pilot scale |
| Fit with ADR-012 | **High** — preserves the wallet/balance model; clean precursor to the stablecoin north-star |

**Pros:** fastest **real** settle-and-balance loop (not bookkeeping); fully controlled and testable; gives `wallet/balance` a genuine source; reuses existing ledger machinery; A→C is incremental (the ledger stays; only custody moves under it); does not blur ADR-012's PSP boundary.
**Cons:** Pivota holds customer funds → money-transmission/stored-value exposure that must be owned before real external money; a funding on-ramp still has to be built (mitigated: manual/Stripe top-up for pilot); it is *not* stablecoin settlement — it is the off-chain layer, so ADR-012's "stablecoin rail" claim is only fully honored at phase 2.

### Option B: Delegate settlement to a PSP (Stripe)
`confirm` creates + captures a Stripe PaymentIntent (the shape `adapters/ap2_payment_adapter.py` already models). "Balance" becomes meaningless.

| Dimension | Assessment |
|---|---|
| Complexity | **Med** — wire the adapter to the live Stripe integration + idempotency/refund mapping |
| External dependency | Stripe at settle time |
| Regulatory | **Low** — Stripe is the money transmitter |
| Fit with ADR-012 | **Low** — PSP settlement is explicitly the **API-key ACP rail's** job |

**Pros:** real external money using an integration that already exists; Stripe carries custody/compliance; no float on Pivota.
**Cons:** **duplicates Rail 1.** ADR-012 routes PSP-settled transactions to `/orders/*`; putting card settlement under AP2 rebuilds the order path behind a signature and collapses the two rails ADR-012 separated. It also **voids the wallet/balance model** — AP2 becomes "a signed card charge," and `wallet/balance` has no meaning. If a caller wants PSP settlement, ADR-012's answer is *use the API-key ACP rail*. **Rejected for AP2** (not a rejection of PSP settlement generally — it lives on Rail 1).

### Option C: On-chain / custodian stablecoin — the ADR-012 north star
`confirm` moves real USDC — via a custodian (Circle/Fireblocks/Coinbase) or directly on-chain — between agent and merchant wallets. `wallet/balance` reads chain/custodian.

| Dimension | Assessment |
|---|---|
| Complexity | **High** — custodian/chain integration, key management, deposit/withdraw, finality/reorg, gas/fees |
| External dependency | Custodian and/or chain node |
| Time-to-pilot | **Slow** — large greenfield; nothing exists |
| Regulatory | **High** — custody, VASP/MTL considerations |
| Fit with ADR-012 | **Highest** — this *is* the rail ADR-012 described |

**Pros:** delivers the actual stablecoin/x402 position; agent-sovereign funds; matches the `agent_wallets` (network/address/custodian) schema as designed.
**Cons:** the heaviest lift by far with **zero** existing infrastructure; too slow and too risky to be the *pilot* mechanism; most of its value (do external agents want a signed, wallet-settled rail?) can be validated behind Option A's ledger first. **Deferred, not rejected** — it is the **phase-2 backing** for Option A's ledger.

### Option D: Do nothing — keep `confirm` as a bookkeeping record
Ship AP2 with settlement permanently out-of-band (Phase 0 made permanent).

**Pros:** zero settlement build.
**Cons:** AP2 is not a payment system — it authorizes payments it never executes, and `wallet/balance` stays `501` forever. Acceptable only as the *first* pilot step (Phase 0), **not an end state.** Rejected as a destination.

## Trade-off Analysis

The real axis is **time-to-working-pilot and control vs. regulatory/custody burden and strategic purity.**

- **C** is the strategically pure endpoint (it is literally what ADR-012 named), but it front-loads the largest, riskiest build *before any pilot signal* — the classic "build the hardest thing first" trap. Almost everything a pilot needs to learn can be learned with an off-chain ledger standing in for the chain.
- **B** is the cheapest path to *real money*, but it buys that by demolishing ADR-012's rail boundary and the wallet model — it turns AP2 into a redundant, signed re-skin of `/orders/*`. The moment settlement is PSP, ADR-012 says *use the other rail*. So B is not an AP2 settlement model; it is an argument against AP2.
- **A** threads it: a **real** value movement (not bookkeeping) with **no external dependency**, preserving the wallet/balance semantics AP2 is built around, and standing as the **off-chain ledger that C later backs.** Its one serious cost — Pivota holding float — is bounded at a Pivota-funded pilot cohort and is the same custody question C answers more heavily.

Decisive factor: **the pilot needs a real, controllable value loop today, and a credible path to on-chain stablecoin tomorrow, without rebuilding in between.** A is the only option that gives both. C is A's phase 2; B is a different rail.

## Consequences

**Becomes easier**
- AP2 becomes a real payment system for a pilot with **no chain/custodian/PSP dependency at settle time.**
- `wallet/balance` retires its `501` with a genuine source (the ledger) — closes the [PR #1471] follow-up.
- A→C is incremental: the ledger and its accounting seam survive; phase 2 only relocates the float under it.
- Reconciliation (ADR-012 #8) gets a home: the agent ledger is the AP2 side of the combined economic ledger.

**Becomes harder / must be owned**
- **Custody/regulatory:** holding agent float is a money-transmission/stored-value surface. Several US state money-transmitter regimes have **no de-minimis exemption** — holding *any* customer float can trigger licensing regardless of pilot size — so this must be validated with **counsel before phase 1**, not in parallel with it. Needs a compliance owner; prefer a bounded, Pivota-funded test-credit pilot (no external customer funds) until cleared.
- **`confirm` correctness is now load-bearing:** it must be **atomic** (debit+status in one DB txn), **idempotent** (a replayed confirm settles once), and **authorized** (transaction belongs to the consenting agent, is `pending`, amount matches the mandate/authorized amount). The current agent-unscoped, guard-less `UPDATE` must be replaced.
- **A funding on-ramp** must exist even for pilot (manual/admin credit or Stripe top-up) — small, but real.
- **Merchant payout:** the credited merchant-payable balance needs an eventual payout path (out-of-band for pilot; reconciled with the `/orders/*` ledger long-term). This is where custody risk actually **concentrates** — merchant-payable float accumulates with every settled transaction — so the payout path needs an owner *before* real external funds move, not after.
- **Currency semantics:** the pilot ledger is **single-currency (USD)** — reject non-USD at `initiate` rather than settling through the manually-fed `x402_exchange_rates` table. A multi-currency ledger (per-currency balances, FX at settle time) is a classic correctness trap and stays out of scope until a real FX source exists.
- **Refund/reversal semantics** on the ledger (compensating entries) must be defined before real money.

**Must revisit if**
- The pilot proves external agents specifically need **on-chain/agent-sovereign** funds (not a Pivota-held balance) → accelerate phase 2 (Option C).
- Regulatory review finds pilot-scale float unacceptable → fall back to Phase 0 (auth-only, out-of-band) until custody is solved, or bring a custodian forward.

## Action Items

1. [ ] **Founder sign-off** on Option A + the phased path (0→1→2). Move to Accepted on sign-off.
2. [ ] **Compliance owner + counsel review** of custody/float **before phase 1** (money-transmission licensing has no de-minimis in several states, so "bounded at pilot scale" is not self-evidently safe); define the pilot funding source (recommend Pivota-funded test credits — no external customer funds until cleared).
3. [ ] **Phase 0 (now):** scope an **auth-only pilot** — grant→initiate→confirm as a signed record, settlement out-of-band; keep `wallet/balance` `501` and documented. Validates the ADR-012 auth rail without settlement.
4. [ ] **Phase 1 — ledger schema:** `agent_ledger` (balance + entries keyed to `transaction_id`; agent-debit + merchant-payable-credit), modeled on `credit_ledger` / `partner_settlement_service`. **Single-currency (USD) for the pilot** — non-USD rejected at `initiate`, not converted. New migration.
5. [ ] **Phase 1 — settle in `confirm`:** atomic debit + status flip in one DB transaction; **idempotent** on `transaction_id`; **authorized** (transaction belongs to the consenting agent, is `pending`, amount matches the mandate/authorized amount). Fixes the current unscoped/guard-less `UPDATE`.
6. [ ] **Phase 1 — `wallet/balance`:** implement against the ledger (retire the `501`), restoring the consent-only gate (prior handler is in git history / [PR #1471]).
7. [ ] **Phase 1 — funding on-ramp:** admin/manual credit for the pilot, optionally a Stripe **top-up** (card→ledger credit). Keep the top-up PSP *out* of the settle path — it funds the ledger; it does not settle the transaction — preserving the ADR-012 boundary.
8. [ ] **Refund/reversal** on the ledger (compensating entries) + AP2 receipt semantics for reversals.
9. [ ] **Reconciliation** (ADR-012 #8): define how `agent_ledger` + `x402_transactions` reconcile with the `/orders/*` PSP ledger into one economic view.
10. [ ] **Phase 2 (deferred, north star):** evaluate a custodian (Circle/Fireblocks) to back the ledger with real USDC; on-chain deposits/withdrawals; reconcile custodian balance ↔ ledger. Separate ADR when scoped.

## Rollback

Adds no runtime behavior on its own; AP2 stays gated by `ENABLE_AP2_ROUTES` (default false). Reversing means not building phase 1 (leave settlement out-of-band, Phase 0) or, if the boundary view changes, routing settlement to the API-key ACP rail (Option B / Rail 1) — a separate decision. **No phase is a one-way door:** the ledger (phase 1) is additive, and phase 2 backs it without replacing it.
