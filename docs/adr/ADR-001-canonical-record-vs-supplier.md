# ADR-001: Canonical product record (Pivota-owned) + merchant-as-supplier

**Status:** Accepted
**Date:** 2026-06-13
**Deciders:** Founder (peng), Claude
**Supersedes assumption:** merchant-sync-first enrichment (the implicit model behind the #841/#842/#847 enrichment pipeline)

## Context

The agent-decision-grade goal is: from a product's record alone an agent can FIND
it by fit, JUSTIFY it with cited evidence, TRUST it, and let a US user BUY it —
claim-safely. We had been filling that record by **enriching whatever the
merchant synced** (derive concerns/actives/claims from the merchant's listing).

A pre-flight on the test merchant (`merch_efbc46b4619cfbdf` — Winona / IPSA /
The Ordinary, real products) showed this model fails at the source: real
long-tail merchants sync **thin data** — no INCI, no structured concerns, no
substantiated claims. The deterministic enrichment then has nothing to work
from, so `find` is hit-or-miss (caught Niacinamide from The Ordinary's title,
missed actives on Winona/IPSA) and `justify` fails on all (no provenance-backed
claims). **0/3 reached decision-grade.** The data the agent needs is *canonical*
to the product (The Ordinary's full INCI and the niacinamide→brightening
mechanism are identical across every seller), yet we were re-deriving it, badly,
per reseller.

This contradicts the company thesis ("Pivota's commerce index layer for agentic
commerce" / "Stripe for long-tail agentic commerce"): the moat is **controlling
the rich canonical record + neutral distribution to agents**, not enriching
merchant scraps.

## Decision

**The commerce index owns the canonical product record; merchants are suppliers.**

- **Canonical product record** (keyed by `content_key`): the rich,
  agent-decision-grade, claim-safe data — identity, INCI, concerns, key actives,
  ingredient-substantiated claims, evidence, format, certs, usage. Sourced
  **canonical-first** (brand-official source + ingredient/INCI authority),
  curated and owned by Pivota, and **shared across all merchant offers** for that
  product. One record per product, regardless of how many merchants sell it.
- **Merchant = supplier.** A merchant resolves their SKU to the canonical product
  (entity resolution) and attaches an **offer** (price, stock, US-buyable,
  fulfillment). They do **not** own the data or the agent-facing presentation and
  **need not author agent-grade data**.
- **New long-tail SKUs with no crawlable source:** the supplier may provide **raw
  inputs** (INCI string, label photo) — as *input to* Pivota's canonical record,
  never as the agent-facing presentation. Pivota structures, screens,
  substantiates, and distributes it.

This is **index-first**, not merchant-sync-first.

## Options Considered

### Option A: Canonical-record-first, merchant-as-supplier *(chosen)*
| Dimension | Assessment |
|-----------|------------|
| Complexity | Med — needs a canonical-sourcing engine (brand-official + INCI authority) |
| Data quality | High — one verified record per product, not N thin re-derivations |
| Defensibility | High — Pivota controls the record + distribution (the moat) |
| Merchant friction | Low — "supply inventory, we supply the data" |

**Pros:** verified data once per product; claim-safety owned centrally; neutral
distribution control; strong supplier pitch; matches the existing spine.
**Cons:** Pivota must build/operate canonical sourcing; Pivota owns the
claim-safety liability (see Consequences — this is a feature).

### Option B: Merchant-sync-first enrichment *(status quo — rejected)*
| Dimension | Assessment |
|-----------|------------|
| Complexity | Low |
| Data quality | **Low — capped by the thinnest merchant's listing** |
| Defensibility | Low — we'd own nothing the merchant didn't already have |

**Pros:** simplest; no sourcing engine.
**Cons:** the pre-flight proves it can't reach decision-grade for the long tail;
re-derives the same canonical facts per reseller; no moat.

### Option C: Require merchants to author agent-grade data
**Rejected:** long-tail SMBs won't (and often can't) author INCI + substantiated
claims; this is exactly the work they come to Pivota to avoid.

## Trade-off Analysis

The core trade-off is **who is responsible for the rich record**. Option B makes
it the merchant (fails — they don't have it). Option C makes it the merchant by
fiat (fails — they won't do it). Option A makes it **Pivota**, which (a) is the
only place the data can come from reliably for the long tail, (b) lets the data
be *verified once and reused*, and (c) is precisely the asset the business is
supposed to own. The cost — building canonical sourcing and owning claim-safety —
is the moat, not a tax.

## Consequences

**Easier**
- Decision-grade is reachable: a verified canonical record (INCI → actives,
  brand claims → ingredient-substantiated) makes `find` + `justify` pass without
  depending on merchant data quality.
- Multi-merchant is natural: many offers, one record; neutral offer ranking.
- Merchant onboarding is light (identity + offer + optional raw inputs).

**Harder / new work**
- Pivota must build a **canonical-sourcing engine** (brand-official crawl + INCI/
  ingredient authority + supplier raw-input intake) feeding `content_key`.
- Pivota **owns claim-safety + liability** for the data it publishes — handled by
  the existing `claim_screening` + `required_disclaimers` framework (a feature:
  neutral, cited, claim-safe is the value prop).

**To revisit**
- Provenance/precedence rules when sources disagree (brand-official vs supplier
  raw input vs reseller listing).
- Freshness/ownership of the canonical record across merchant churn.

## Maps to the existing spine

- `content_key` / `pivota_signature_id` — canonical product identity (already
  shared across merchant rows).
- `catalog_offers` — the merchant/supplier offers.
- `external_product_seeds` + Aurora KB — already the "Pivota-sourced canonical
  content" pattern (see the Aurora-vs-merchant distinction); ADR generalizes it
  to all products.

The shape exists; this ADR makes the **sourcing discipline** explicit:
canonical-first, not merchant-first.

## Action Items

1. [ ] Reframe the in-progress evidence/claims layer to operate **canonical-first
   at `content_key` level** — source claims from the brand-official record and
   ingredient-mechanism-substantiate them, not scrape reseller marketing copy.
2. [ ] Spec the **canonical-sourcing engine**: brand-official source resolver +
   INCI/ingredient authority + supplier raw-input intake → `content_key` record.
3. [ ] Define **source precedence** (brand-official > supplier raw input >
   reseller listing) and provenance tagging on each canonical field.
4. [ ] Frame merchant onboarding as **supplier onboarding** (SKU→canonical
   resolution + offer + optional raw inputs).
