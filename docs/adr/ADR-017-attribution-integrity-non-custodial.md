# ADR-017: Attribution Integrity for a Non-Custodial Pivota

**Status:** Proposed
**Date:** 2026-07-18
**Deciders:** Founder (peng) — attribution/monetization; Commerce / Trust owners
**Builds on:** **ADR-016** (Pivota is non-custodial — attribution + trust brokering is *how* Pivota captures value, since it never touches money). Companion: the attribution-loop audit folded into ADR-016 ("Attribution — current state", 2026-07-18).
**Part of:** the agent-protocol interoperability model — **ADR-014**. Attribution is the **fifth concern** the non-custodial positioning makes load-bearing, alongside discovery / identity / authorization / settlement.
**Scope:** how Pivota reliably proves "our index/decision drove this merchant sale" while sitting **outside the fund flow** (and, increasingly, outside the checkout). Does not change the money boundary (ADR-016) or identity/consent (ADR-012).

---

## Context

ADR-016 decided Pivota is non-custodial and captures value from **attribution** (merchant-side: proof Pivota drove the sale) and **trust brokering** (agent/protocol-side: identity + authorization + signed receipt). That makes attribution the **revenue mechanism**, not telemetry.

The attribution-loop audit found it **closes only for the Pivota-orchestrated order path**, and breaks where the non-custodial positioning points:
- attribution is anchored on a Pivota-minted `pvt_click_id` that must round-trip into the order; if it doesn't, `upsert_order_attribution_edge` self-gates and writes nothing (only a silent-reject metric);
- the **AP2/x402 rail carries no attribution linkage at all** (and `initiate` doesn't even set `order_id`);
- the one genuinely non-custodial closure (merchant settles on their own store) runs only for connected Shopify; the WooCommerce/read-orders **pollers are scheduled nowhere**; `referral_only` has no closure; there is **no merchant conversion-report / receipt-ingest API**;
- coverage silently degrades to well-behaved callers (no fallback join).

**The core problem:** Pivota must attribute orders **it did not create and whose money it never sees.** Today's attribution assumes Pivota orchestrated the checkout (so it controls the order metadata). The non-custodial, pure-info-broker positioning removes that assumption.

### Forces
- **Pivota sees the order, not the money.** The audit's decisive reframe: closure needs a view of the *order/settlement*, not the funds — and that view can come from a webhook, a poller, or a **report**. None require custody.
- **The signed receipt is a trust anchor Pivota already produces** — non-repudiable proof of who authorized what; it can be the join key + integrity check for a reported conversion.
- **Coverage must be measurable to be sellable.** "We drove X% of your agent GMV" is credible only if the silent-reject/drop rate is a known number.
- **Incentives cut both ways.** A merchant paying on attribution has an incentive to *under*-report; the design must verify and reconcile reports, not trust them blindly.

## Decision

**Bind a merchant sale to a Pivota read/decision via a *reported or observed* conversion keyed on a durable Pivota join id (`pvt_click_id`), with the signed receipt as the trust anchor — closing the loop without Pivota ever seeing funds. Make the non-custodial closure channels actually run, give the AP2 rail attribution parity, and make coverage measurable via a fallback join + silent-reject observability.**

Four workstreams:

1. **Conversion-report / receipt-ingest API — the primary non-custodial closure.** A verified endpoint where a merchant, platform, or settlement rail reports a settled order referencing the Pivota `pvt_click_id` and/or the AP2 **signed receipt**. Pivota verifies the report (per-merchant HMAC / signature over the receipt), binds it to the existing edge (or creates one), and stamps attributed GMV — all outside the fund flow. This is the closure for merchants Pivota does **not** orchestrate. Reconcile reported vs. expected to bound under-reporting.
2. **Run + broaden the existing closure.** Schedule the WooCommerce/read-orders pollers (they exist but run nowhere); add a `referral_only` join key that survives to the merchant's order; keep the Shopify `orders/paid` webhook. The automatic channels for connected platforms.
3. **AP2/x402 rail attribution parity.** Carry `pvt_*` on the x402 transaction, set `order_id`, and write the attribution edge on `confirm` (dovetails with ADR-016's re-scope: confirm records + attests + routes). The future rail must be attributable from day one — and the AP2 receipt is the report artifact for workstream 1.
4. **Fallback join + observability.** When `pvt_click_id` is absent, a secondary join (authenticated `agent_id` + `canonical_product_id` + bounded window) recovers coverage; instrument the silent-reject/drop rate so attribution coverage is a *known* number before it is billed. Emit a lightweight read/decision event from plain PDP reads + `agent_api` search so a read is attributable even without a later Pivota link.

## Options Considered

### Option A: Report/observe-based binding on a durable join key (this ADR)
Merchant/rail/webhook/poller reports the settled order; Pivota binds via `pvt_click_id` / signed receipt. Works at any integration depth, non-custodial.
**Pros:** closes the non-custodial paths; the receipt is the trust anchor; scales to arbitrary merchants via the report API; keeps Pivota out of funds.
**Cons:** integrity depends on reports (verification + reconciliation needed); coverage loss when neither token nor report arrives.

### Option B: Integration-only (webhooks + pollers per platform)
**Pros:** automatic, no merchant action.
**Cons:** N-platform toil; leaves `referral_only`/unintegrated merchants and arbitrary agents uncovered; doesn't scale. Necessary but **insufficient** alone → folded in as workstream 2.

### Option C: Require Pivota-orchestrated checkout for attribution (status quo)
**Pros:** Pivota controls the metadata; tightest edge.
**Cons:** contradicts the non-custodial/pure-broker positioning — it only attributes what Pivota routed, which is exactly the shrinking slice. **Rejected.**

### Option D: Infer conversion from the money flow
**Pros:** most complete.
**Cons:** requires being in settlement = custody = the thing ADR-016 rejected. **Rejected.**

## Trade-off Analysis

The real trade is **control of the record vs. custody.** Options C/D give Pivota a tight, self-controlled attribution record — but only by orchestrating the checkout or holding the money, i.e. by *not* being the neutral non-custodial layer ADR-016 chose. Option A accepts that Pivota **does not control the order** and instead makes attribution an **information-integrity** problem: a durable join key, signed reports, a fallback, and reconciliation. That is squarely Pivota's competence (trust + records), needs no custody, and is the only model that attributes sales across merchants Pivota neither orchestrated nor settled. Its cost — coverage depends on a token or a report arriving — is real but **measurable** (workstream 4), so it can be managed and priced rather than hidden.

Decisive factor: **for a non-custodial Pivota, attribution is an information-integrity problem, not a payments problem — solve it with join keys, signed reports, and observability, not by re-entering the order or the money.**

## Consequences

**Becomes easier**
- Attribution closes for merchants Pivota doesn't orchestrate — the whole point of the non-custodial model.
- The AP2 rail becomes attributable (today it's a 100% blind spot).
- "We drove X% of your agent GMV" becomes a *measured*, defensible, billable claim.
- The signed receipt (ADR-016's agent/protocol product) and the attribution record converge on **one join key**.

**Becomes harder / must be owned**
- **Report integrity + reconciliation** — verify signed reports, reconcile reported vs. expected, bound under-reporting; the new trust surface.
- **Coverage transparency** — the silent-reject/fallback metrics must be owned and surfaced (internally and to merchants) so attribution isn't over-claimed.
- **Join-key propagation discipline** — `pvt_click_id` must survive across redirect/cart-permalink/AP2 paths; a fourth surface (plain read) must start emitting it.

**Must revisit if**
- Reports prove systematically gamed/under-reported even with verification → consider deeper platform integrations or a settlement-adjacent (still non-custodial) confirmation, before ever reconsidering custody.

## Action Items
*(tracked as issues; the two that gate any real non-custodial attribution are under the Pilot milestone)*
1. [ ] **AP2/x402 attribution parity** — carry `pvt_*`, set `order_id`, write the edge on `confirm`. *(Pilot; dovetails ADR-016's AP2 re-scope)*
2. [ ] **Run + broaden the existing closure** — schedule the Woo/read-orders pollers; add a `referral_only` join key. *(Pilot)*
3. [ ] **Fallback join + observability** — `agent_id`+product+window fallback; silent-reject/drop-rate metrics; read/decision event on plain PDP + search.
4. [ ] **Conversion-report / receipt-ingest API** — verified endpoint, `pvt_click_id`/signed-receipt join, reconciliation. *(the primary non-custodial closure — highest leverage; scope for the pilot cohort next)*
5. [ ] **Founder sign-off**; move to Accepted; define attribution/referral pricing on the resulting (measured) coverage.

## Rollback
Design-level; each workstream is additive and independently shippable. Reversing = staying with orchestration-only attribution (Option C), which under-attributes exactly the non-custodial traffic ADR-016 commits to. This ADR adds no runtime surface itself.
