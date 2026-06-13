# The Pivota Agent Product Record

> What an AI shopping agent (ChatGPT, Gemini, …) reads from Pivota to confidently
> recommend — and sell — a product. Worked example:
> [`examples/agent_product_record.aruen_tofu_collagen.json`](examples/agent_product_record.aruen_tofu_collagen.json)
> (Aruen Tofu Collagen Dual-Firming Jelly Cream).

## Why this exists

A frontier model recommending a long-tail K-beauty product on its own has almost
nothing to go on: a title, maybe a marketplace link. It can't see the
ingredients, can't justify a benefit, can't tell a real US offer from a reseller,
and will happily hallucinate attributes. **Pivota's job is to hand the agent a
structured, verified, claim-safe record so it doesn't have to guess** — and to
own that record so it's identical across every seller.

This is the company thesis made concrete (ADR-001): **Pivota owns the canonical
record; the merchant is a supplier of inventory.**

## The structure — five things an agent must do, plus provenance

| Block | The agent question it answers | What's in it |
|-------|-------------------------------|--------------|
| `identity` | *What is this?* | canonical title, brand, origin, category, format |
| `find` | *Does it fit my shopper?* | skin concerns, skin types, routine step, texture, vegan/cruelty-free |
| `ingredients` | *What's actually in it?* | **verified full INCI** + key actives with their role |
| `justify` | *Why should I recommend it?* | benefit claims, each **cited to a verified ingredient**, claim-safe |
| `trust` | *Is it authentic & safe to surface?* | brand-official provenance, counterfeit risk, certifications |
| `buy` | *Can my US shopper buy it?* | **brand-direct** US offer, price, availability |
| `compare` | *What are the alternatives?* | cross-brand alternatives (supply-gated) |
| `outcomes` | *Did people keep it?* | repurchase / return / satisfaction (the compounding moat) |
| `provenance` | *Why trust each field?* | every signal traceable to its source + precedence rule |

## Two rules that make it trustworthy (and uncopyable)

1. **Verified, not present.** Anything that drives a fit-match or a claim is
   tied to a verified source. Key actives come from the **INCI list** (not
   marketing copy); each benefit claim **names the ingredient** that justifies it
   ("helps firm ← soy isoflavones + collagen + adenosine"). No bare assertions,
   no drug/medical claims — cosmetic-vs-drug screened.
2. **Canonical-first, by precedence.** The record is sourced **brand-official >
   supplier-provided > reseller listing**, so a thin reseller sync can never
   degrade it. One verified record, shared across every offer.

## What this is worth to the merchant (the pitch)

- **You don't author agent-grade data.** Sync your catalog + price/stock. Pivota
  sources the rich record (we pulled this product's full INCI straight from your
  own site) and makes it agent-decision-grade.
- **Net-new US AI-channel share, brand-direct.** The `buy` block prioritizes your
  **brand-direct** offer — we help the *brand* get surfaced inside US shoppers'
  AI agents, not just route to a marketplace.
- **Claim-safe by construction.** Every benefit is cited to an ingredient; no
  liability-inviting efficacy claims. Pivota owns that discipline.
- **A compounding moat you can't get elsewhere.** As Pivota transacts, per-SKU
  outcomes (repurchase/return) accrue to the record — data no marketplace returns
  to you.

> You supply inventory. Pivota supplies the rich data **and** the distribution.
