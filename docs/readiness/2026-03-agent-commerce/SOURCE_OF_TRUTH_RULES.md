# Source Of Truth Rules

## 1. Catalog / Title / Description / Media

- canonical source: normalized Shopify cache payload
- fallback source: Shopify Admin product fetch
- freshness: 24h for title/description; media warning at 72h
- degradation: stale catalog can still appear in report/export with warnings
- blockers: missing title, missing primary image, missing canonical product identity

## 2. Price / Currency

- canonical source: normalized Shopify variant offer payload
- fallback source: Shopify Admin product fetch
- freshness: 1h
- degradation: stale price remains reportable/exportable with warnings
- blockers: missing price, zero/invalid price, missing currency

## 3. Inventory / Availability

- canonical source: live Shopify inventory intent
- fallback source: cached Shopify inventory snapshot
- freshness: 15m
- degradation: stale inventory stays visible for diagnostics
- blockers: stale inventory on checkout path, missing inventory state, out-of-stock variant

## 4. Fulfillment / Returns Policy

- canonical source: `readiness.alpha_policy_config.v1`
- fallback source: none
- freshness: 30d review window
- degradation: report may still render but channel-ready checkout/export should not claim readiness if policy is missing
- blockers: missing shipping profile, missing shipping support, missing returns support

## 5. Checkout Capability

- canonical source: readiness capability resolver from feature flags + Shopify connectivity + active PSP config
- fallback source: none
- freshness: realtime evaluation at report/checkout generation time
- degradation: report/export still available with blockers
- blockers: missing Shopify configuration, missing active PSP, disabled readiness alpha flags

## 6. Order Status

- canonical source: readiness order-sync journal/session state
- fallback source: local `orders` row
- freshness: realtime locally, async against merchant write-back side effects
- degradation: session remains replayable and explicit about blocked/failed states
- blockers: local order creation failure, merchant write-back failure, missing buyer context, missing merchant configuration
