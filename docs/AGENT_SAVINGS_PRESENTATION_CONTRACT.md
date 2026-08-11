# Agent Savings Presentation Contract

Status: implemented behind additive response fields

Contract version: `savings.v1`

## Purpose

This contract gives Pivota Agent, external agents, and LLM clients one safe, store-platform-neutral way to explain savings across product cards, PDPs, offer sheets, quotes, and checkout summaries.

The contract is intentionally presentation-first. It tells a caller what can be displayed, what is verified, what is only unlockable, and what must not affect the charged amount.

## Source Of Truth Rules

- `pricing.total` is the amount used for the pay button and PSP charge.
- Store-platform quote evidence is the authority for applied store discounts. For Shopify, that evidence is Storefront quote allocations and selected delivery evidence.
- `store_discount_evidence` was RETIRED with the promotions lane (ADR-022): no producer populates it any more, and the key is absent from quote and card payloads. Consumers must not require it. `confidence.store_discount_metadata` is consequently always `"not_applicable"`.
- Payment card, wallet, issuer, PSP, and BNPL benefits are display-only in v1.
- Payment benefits must not reduce `pricing.total`, PSP amount, Shopify order total, Shopify discount codes, or Shopify discount allocations in v1.
- If a payment benefit and a store-native discount both exist, display them as separate sections.

## Response Fields

Surfaces may expose these additive fields:

| Field | Meaning | Can reduce charged amount |
| --- | --- | --- |
| `promotion_lines` | Applied store discount lines backed by quote evidence | Yes |
| `discount_evidence` | Store-platform code applicability, allocations, pricing confidence, shipping evidence | Yes, only when allocations or selected delivery evidence support it |
| `store_discount_evidence` | RETIRED (ADR-022) — never emitted; listed only so integrators know the key is permanently absent. | No |
| `payment_offer_evidence` | Card/wallet/issuer/PSP/BNPL display offers and eligibility evidence | No in v1 |
| `payment_pricing` | Display-only estimated payment benefit totals | No in v1 |
| `savings_presentation` | Normalized grouped presentation contract for agents and UI | Depends on row policy |

## Savings Presentation Shape

```json
{
  "contract_version": "savings.v1",
  "pricing_confidence": {
    "store_discounts": "authoritative|partial|unverified|not_applicable",
    "store_discount_metadata": "metadata_available|metadata_unlockable|unverified|not_applicable",
    "payment_benefits": "display_estimate|context_matched|psp_verified|unverified|not_applicable"
  },
  "appliedStoreDiscounts": [],
  "availableStoreOffers": [],
  "cartUnlocks": [],
  "paymentBenefits": [],
  "summaryBadges": [],
  "checkoutRows": [],
  "agentFacing": {
    "externalAgentsCanRender": true,
    "priceAuthority": "pivota_quote_psp_charge",
    "payButtonUses": "pricing.total",
    "paymentBenefitsMutateCharge": false,
    "unverifiedStoreOffersMutatePrice": false
  },
  "applicationPolicy": {
    "storePlatformQuoteIsStoreDiscountAuthority": true,
    "appliedStoreDiscountSourceOfTruth": "store_platform_quote|shopify_storefront_quote|...",
    "paymentOffersAreDisplayOnlyV1": true,
    "doNotEncodePaymentOffersAsStoreDiscounts": true,
    "doNotEncodePaymentOffersAsShopifyDiscounts": true
  }
}
```

## Groups

### `appliedStoreDiscounts`

Use this group for verified store discounts that have already reduced the quote total.

Requirements:

- Must come from `promotion_lines` or store-platform `discount_evidence.applications`.
- Must have positive savings evidence.
- May be shown in checkout as `Store discounts`.
- May affect seller/cart value only when the quote total already includes it.

### `availableStoreOffers`

Use this group for store-native discounts that metadata says may apply but the current quote has not proven.

Examples:

- Product/order amount off code metadata.
- Automatic discount metadata.
- Customer context or segment metadata that is synced but not locally verified.

Rules:

- Do not say `applied`.
- Do not reduce displayed product price.
- Prompt the user to quote/apply code when needed.

### `cartUnlocks`

Use this group for offers that depend on cart shape or address.

Examples:

- Buy X Get Y.
- Minimum quantity.
- Minimum subtotal.
- Free shipping threshold or address-dependent free shipping.

Rules:

- Show progress when available, for example `Add 2 more to unlock`.
- Do not reduce checkout total until quote evidence exists.
- Free shipping amount is only final after selected delivery evidence.

### `paymentBenefits`

Use this group for card, wallet, issuer, PSP, statement credit, points, installment, and BNPL messaging.

Rules:

- Always display as `Estimated payment benefit` or `Pay with X`.
- Never display as `Discount applied` in v1.
- Never change `pricing.total`.
- Never write into Shopify discount fields.
- Upgrade only to future executable mode after PSP-side evidence exists.

## Checkout Rows

Safe checkout summary order:

1. `Subtotal`
2. `Store discounts`, only from applied Shopify/store evidence
3. `Shipping`
4. `Tax`
5. `Total charged now`
6. `Estimated payment benefit`, visually separate and `affects_total=false`

The pay button must use `pricing.total`.

## Product Card Rules

- Show at most two badges.
- Badge priority:
  1. Applied store discount
  2. Cart unlock
  3. Available store offer
  4. Payment benefit
- Do not change product price based on display-only payment benefits.
- Do not rank sellers by unverified payment benefits.

## PDP Rules

Render savings as `Ways to save` with these groups:

- Store offers
- Cart offers
- Shipping offers
- Pay with

Use `applied` only when quote evidence exists. Use `eligible`, `available`, `unlockable`, or `estimated` otherwise.

## Offer Sheet Rules

- Sort by real checkout estimate, not display-only payment benefit.
- Store/cart discounts may affect `best cart value` only when already quote-backed or clearly unlockable with selected quantity.
- Payment benefit chips must be separate, for example `Pay with Mastercard`.

## External Agent Guidance

External agents should use this decision order:

1. Read `savings_presentation` if present.
2. Render `summaryBadges` for compact cards.
3. Render grouped arrays for detailed surfaces.
4. Use `checkoutRows` for quote/checkout summaries.
5. Use `pricing.total` for any pay button or payment instruction.
6. Treat `paymentBenefits` as explanatory copy only in v1.

Agents must not calculate their own final price by subtracting `paymentBenefits`.

## Evidence Status Language

| Status | Allowed copy |
| --- | --- |
| `applied` | `Applied at checkout` |
| `available` | `Available at checkout` |
| `unlockable` | `Add more to unlock` |
| `potential` | `Available when paying with X` |
| `context_matched` | `Matches selected payment context` |
| `psp_verified` | Reserved for future executable mode |
| `unverified` | `Eligibility not verified yet` |
| `rejected` | `Not eligible` |
| `expired` | Do not show as active |

## Main Surfaces

- Product cards: `/agent/v1/products/merchants/{merchant_id}`, `/agent/v1/products/search`, `/agent/v2/products/search`
- PDP/product detail: agent product detail routes and gateway product results
- Offer resolution: `/agent/shop/v1/invoke` with `offers.resolve`
- Quotes: `/agent/v1/quotes/preview`, `/agent/v2/quotes/preview`, `/v1/pivot/quote`
- Orders: pricing quote metadata carries evidence and hashes for reconciliation

## Validation Checklist

- Applied store-platform discount appears under `appliedStoreDiscounts`.
- BXGY or threshold metadata appears under `cartUnlocks` until quote proves application.
- Payment benefit appears under `paymentBenefits`, with `affects_psp_amount_v1=false`.
- `checkoutRows.total_charged_now.amount` equals `pricing.total`.
- `checkoutRows.estimated_payment_benefit.affects_total=false`.
- `agentFacing.priceAuthority` is `pivota_quote_psp_charge`.
- No payment benefit is copied into Shopify discount allocations or discount codes.
