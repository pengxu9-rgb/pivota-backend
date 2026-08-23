# Commerce Index connector rollout

## Contract boundary

Commerce Index accepts product facts only from a merchant-authorized catalogue
source: a storefront API, PIM/ERP/POS, or a contracted data feed.  Payment
providers are stored separately as payment-orchestration sources.  They never
authorize product crawling merely because a merchant connected a PSP account.

`services/commerce_source_registry.py` is the runtime policy boundary.  The
universal product-sync endpoint returns `status=unsupported` for payment-only
providers instead of returning an empty successful product sync.  Antom has two
separate source identities: `antom_catalog` and `antom_ucp` (with legacy
`antom` resolving to `antom_ucp`).

Register the merchant's non-secret authority contract first with
`POST /merchant/integrations/commerce-index/sources`.  An active registration
requires a `consent_ref`; connector API keys and private keys continue through
the existing secret/onboarding paths and are rejected by this endpoint.

## Initial providers

| Provider | Current role | Commerce Index catalogue sync |
| --- | --- | --- |
| Shopify, Wix, WooCommerce, BigCommerce | Merchant-authorized storefront | Enabled through the existing product adapters |
| Stripe | Payment orchestration | Disabled by default; add only a separately-authorized catalogue mapping/feed |
| Adyen | Payment orchestration | Disabled by default; enable only after the merchant's contracted data-feed/API is provisioned |
| Antom Catalog (`antom_catalog`) | Merchant-authorized catalogue/offer feed | Separate connector; disabled until the contracted feed schema and credentials are provisioned |
| Antom UCP (`antom_ucp`) | Payment orchestration | Antom merchant ID/client ID configuration can be stored, but signed execution is gated |

## Antom two-layer onboarding path

### 1. Antom Catalog

This connector owns product facts only: product/variant identity, offers and
prices, inventory, images, public review references, and source update events.
It does not receive UCP payment credentials and does not execute checkout.

1. Obtain the merchant-authorized Antom catalogue feed/API contract, including
   schema, merchant scope, update/event semantics, market/currency context, and
   permitted review/image usage.
2. Store the catalogue credential independently from `merchant_psps`; create a
   source schedule that writes observations and field-level change events.
3. Enable graph/insight recomputation from the changed product set only after
   feed validation, source attribution, and freshness checks pass.

### 2. Antom UCP payments

1. Use `POST /merchant/integrations/psp/connect` with `provider=antom`, an
   Antom `merchant_id`, `client_id`, API credential, and explicit `sandbox` or
   `live` environment.  This stores an **inactive** canonical PSP configuration
   only, so it cannot enter generic payment routing.
2. Put signing key material in the managed secret path; do not place a private
   key in `provider_config`, product cache, relation graph, or analytics logs.
3. Provision the merchant-specific Antom payment API and notification contract,
   then add an RSA-signature verifier and idempotent webhook consumer before
   enabling live execution.
4. If the merchant receives an Antom catalogue/offer feed, register that feed as
   an independent `catalogue` source with its schema, consent record, refresh
   SLA, and field authority.  It must not share the PSP credential contract.

## GCP production next steps

1. Move the remaining Catalog Intelligence base URL from Railway to a GCP
   service, then run scheduled delta extraction through Cloud Run Jobs.
2. Emit field-level change events to Pub/Sub and dispatch source-specific work
   with Cloud Tasks; price and stock require a live validation before checkout.
3. Run graph and insight recomputation only for affected products; retain source
   evidence, timestamps, and confidence for every edge and insight.
4. Keep release gates: sandbox connection test, signed webhook verification,
   replay/idempotency test, and a staged merchant rollout before any live charge.
