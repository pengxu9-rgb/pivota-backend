# Commerce Index v2: multi-source facts and incremental publication

## Serving model

`catalog_products`, `catalog_skus`, and `catalog_offers` remain the serving
projection.  `catalog_field_facts` remains the field-evidence store: every fact
already carries `source_system`, `source_ref`, `observed_at`, `fresh_until`,
`confidence`, and review state.

Migration `194_commerce_index_v2.sql` adds the missing operational layers:

1. `commerce_index_sources`: merchant consent, source type, capabilities and
   field-refresh policy.  Catalogue and payment integrations are separate rows.
2. `commerce_index_field_changes`: immutable old/new fingerprints for changed
   facts, with source evidence and the reason a change was accepted or held.
3. `commerce_index_publication_jobs`: per-projection work items, so the system
   recomputes only affected search, graph, checkout-validation, or insight data.

## Field authority

| Source kind | Authority | Default role |
| --- | ---: | --- |
| Merchant API / PIM / ERP / POS | 100 | Product, SKU, price, stock and availability truth |
| Contracted feed | 95 | Authorized catalogue or partner offer feed |
| Review provider | 90 | Review content and aggregates |
| Official PDP | 75 | Enrichment and independent verification |
| Retailer listing | 60 | Comparison evidence |
| Public crawl | 45 | Discovery/evidence only; never auto-publishes checkout-sensitive facts |

Price, inventory and availability from a source below official-PDP authority are
recorded but held for review.  Checkout always performs a live validation even
after a source fact has been published.

## Incremental flow

```text
authorized source / crawl
  -> field fact (source, confidence, observed_at, fresh_until)
  -> value fingerprint comparison
  -> commerce_index_field_changes
  -> targeted publication jobs
     -> search index | relation graph | product insights | checkout validation
```

The delta planner is `services/commerce_index_v2.py`; persistence is
`services/commerce_index_delta_service.py`.  A GCP worker should claim pending
jobs by target through `db/commerce_index_publication_jobs.py`, which uses a
lease and `FOR UPDATE SKIP LOCKED`, then set the result atomically.  The planner deliberately
does not send price or stock changes to the relation graph; taxonomy/content
changes do, while price and stock trigger checkout validation instead.

Existing Shopify/Wix/Commerce adapters now pass through this lane from
`services/catalog_sync_service._upsert_field_fact`.  It is controlled by
two gates: `COMMERCE_INDEX_V2_ENABLED=true` and an explicit
`COMMERCE_INDEX_V2_MERCHANT_ALLOWLIST=merchant_a,merchant_b`. This defaults to
off even when the global flag is set, making a canary genuinely merchant-scoped.
Apply migration 194 first, register an active consented catalog source, deploy
the code, and then allowlist only the staged merchant. An allowlisted merchant
without an active source contract is withheld before canonical catalog writes;
the legacy, non-allowlisted path is unchanged.

## Source registration

`POST /merchant/integrations/commerce-index/sources` registers the authority
contract for the authenticated merchant.  It accepts `provider`, `status`,
`consent_ref`, and non-secret `source_metadata`; it never accepts connector
credentials.  Reconnecting the same merchant/provider/layer updates the same
deterministic source record rather than creating competing truth sources.

An `active` source requires a merchant `consent_ref`.  `antom_catalog` may be
registered as `pending` but cannot become active until its contracted catalogue
feed adapter exists.  `antom` normalizes to `antom_ucp`, so payment onboarding
cannot accidentally activate a product-feed authority.

All v2 changes require a non-null `source_id` that references an active source
contract. Authority is derived from that contract and the connector platform,
not from a free-form writer label such as `universal_product_sync`.

## GCP release gates

Run publication work as Cloud Run Jobs triggered by Cloud Scheduler, following
the existing `infra/gcp/setup_scheduler.sh` pattern. Do not add another
in-process scheduler job. Keep the publication job paused until all four target
handlers are real and idempotent:

1. `search_index`: update the affected document only.
2. `relation_graph`: rebuild edges for the affected product scope only.
3. `product_insights`: regenerate insights with the new fact evidence.
4. `checkout_validation`: fetch a live merchant quote before a price/stock
   change is allowed to affect checkout.

Do not run crawler traffic through the payment worker's shared egress/IP
allowlist.  `antom_ucp` payment traffic needs an isolated, stable payment egress
path; public crawling must use a separately rate-limited worker and egress.

The first real target bridge is Gateway's
`scripts/drain-commerce-index-relgraph.js`: it resolves changed canonical
`product_key` values into the graph routine's affected-product manifest and only
acknowledges queue rows after the guarded graph routine succeeds. GCP creates it
inert through `RELGRAPH_PUBLICATION_WORKER=false`; enable it only after migration
194, a staging canary, and `COMMERCE_INDEX_V2_ENABLED=true` on the catalog writer.

The search bridge is Gateway's `scripts/drain-commerce-index-search-index.js`.
It expands a changed product, SKU, or offer to its canonical product and then
to every member of its sellable-item group before using the existing Catalog
Serving document builder and OpenSearch bulk publisher. It is paused by default
through `SEARCH_INDEX_PUBLICATION_WORKER=false`; enable only after
`CATALOG_SERVING_INDEX_BASE_URL` and the managed
`CATALOG_SERVING_INDEX_API_KEY` secret are configured on the Cloud Run Job.
Migration 195 adds a source-ref-to-document membership pointer used to repair
the old group after identity resolution moves a product. The worker writes the
new documents first, deletes only obsolete old document IDs, then replaces the
membership pointers; a deletion or mapping failure leaves the publication job
pending for retry.

`product_insights` is intentionally a review-request lane: its worker writes
`commerce_index_insight_refresh_requests(status=pending_review)` and never
writes `aurora_product_intel_kb` directly. The existing Insights workflow must
first form a seller-grounded baseline and manually pass/rewrite any external
highlight before publish preparation.

Both the Insights and checkout-validation jobs are created paused through
`INSIGHT_REFRESH_WORKER=false` and `CHECKOUT_VALIDATION_WORKER=false`. Enabling
either only drains its request queue; it does not authorize automatic external
evidence publication or any payment action.

`checkout_validation` is an execution-safety lane, not a synthetic quote
generator. Its worker writes `commerce_index_checkout_validation_requests` with
`requires_live_quote`; the existing order path already re-prices through
`QuoteService.validate_quote_snapshot_live` immediately before order creation.
No background worker can create a cart, quote, payment intent, or charge.

## Antom boundaries

- `antom_catalog`: independent authorized catalogue-feed source. It may create
  product/offer field facts and delta jobs only after the merchant feed contract
  is configured in `commerce_index_sources`.
- `antom_ucp`: Antom payment/UCP source. It owns payment session and notification
  events, not catalogue facts. Credentials, webhooks and payment data remain out
  of the Commerce Index evidence store.

Before enabling UCP execution, require sandbox verification, signature checking,
idempotent notification handling, live pilot tests, and a live price/stock quote
from the merchant's commerce source.
