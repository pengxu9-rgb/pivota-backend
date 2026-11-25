# Sandbox catalog copy (design step 6)

Code: `jobs/sandbox_catalog_copy.py`. Tests: `tests/test_sandbox_catalog_copy.py`,
`tests/test_sandbox_catalog_copy_postgres.py`.

```
prod (pivota-prod)                       GCS                                sandbox (pivota-sandbox)
python -m jobs.sandbox_catalog_copy      gs://pivota-sandbox-catalog-drop/  python -m jobs.sandbox_catalog_copy
  export                          ---->    catalog/<YYYYmmddTHHMMZ>/  ---->   import
  READ ONLY snapshot, create-only          <table>.csv.gz ... _MANIFEST.json  one transaction, behind the guard
```

* Env on both jobs: `CATALOG_COPY_GCS_URI` (e.g. `gs://pivota-sandbox-catalog-drop/catalog`) and
  `DATABASE_URL` (prod's on the export, the sandbox's on the import). Nothing else.
* Export refuses unless `PIVOTA_ENV` resolves to production; import refuses unless it is `sandbox`
  and `config.sandbox_guard` passes. Exit code 2 on a refusal, 0 on success, one `DONE {...}` line.
* The export's service account needs `roles/storage.objectCreator` on the bucket only; it writes
  with `ifGenerationMatch=0` and never reads back. The import's needs `objectViewer`.
* The manifest is written last. The import takes the newest stamp that has one.
* Prices keep their values. The gateway marks sandbox answers `sandbox: true` and treats copied
  prices as fresh (decision 4).

## Tables copied (30)

Whole rows, minus the dropped columns. The export reads prod's own column list at run time and
**refuses** any column whose name looks like personal data or a secret (email, phone, token, secret,
api_key, contact, address, customer, buyer, employee, notes, `*_by*`, user_id, ...) unless the spec
drops, replaces or classifies it. A new risky column in prod therefore stops the copy instead of
leaking.

| table | why | dropped / restricted |
|---|---|---|
| catalog_merchants | sellers; every search lane joins it | |
| catalog_products | products | |
| catalog_skus | variants; get_product and the Reap quote | |
| catalog_offers | price, currency, stock; get_offers and the Reap quote | |
| catalog_payment_incentives | incentives on offers | `card_network` classified (a scheme) |
| catalog_offer_incentive_links | incentive ↔ offer | |
| catalog_incentive_rules | incentive rules | |
| beauty_product_profiles, beauty_sku_ingredients, beauty_usage_guides, beauty_shades, beauty_content_assets, beauty_compatibility_rules | beauty attributes on search and PDP | `concentration_notes_json` classified |
| external_product_seeds | reviewed external listings: search, offers, Reap quote | drops `notes`, `created_by_employee_id` |
| products_cache | search and get_product | |
| product_group_members | grouping get_offers expands | |
| identity_resolution_proposals (optional) | applied identity merges | drops `decided_by` |
| catalog_row_trust | the serving verdict the gateway's catalog, seed-fastpath and canonical-PDP lanes INNER JOIN on `serving_decision='public'` (empty = the sandbox serves nothing); keys, states, timestamps only | |
| agent_pdp_view | the view search serves | |
| index_pipeline_state | index readiness | |
| x402_exchange_rates (optional) | FX reference rates | |
| product_quality_snapshot | ranking | |
| product_enrichment (optional) | ranking | drops `updated_by_employee_id`, `updated_by_email` |
| brand_claims (optional) | verified-brand status | drops `challenge_token`, `proof_ref`, `created_by_user_id` |
| merchant_purchasability (optional) | can a merchant take an agent purchase | `card_available` classified |
| tierb_cart_link_eligibility (optional) | Reap cart-link eligibility | |
| reap_agentic_eligibility (optional) | reviewed Reap merchant allowlist | |
| enrichment_cart_variant_proofs (optional) | variant price witnesses | |
| merchant_onboarding | ids, names, domains, status | **keep-list only**: `id, merchant_id, business_name, store_url, operating_mode, website, region, status, psp_connected, psp_type, mcp_connected, mcp_platform, mcp_shop_domain, created_at, updated_at, verified_at, is_test`; `contact_email` (NOT NULL) = `sandbox-merchant@example.invalid`. Never `mcp_access_token, api_key, api_key_hash, psp_sandbox_key, contact_phone, kyc_documents, rejection_reason, signup_source` |
| merchant_stores | ids, platforms, domains | **keep-list only**: `store_id, merchant_id, platform, name, domain, status, connected_at, last_sync, product_count, is_primary, created_at, query_ttl_seconds`; `realtime_enabled` forced false. Never `api_key` (the store credential), `api_endpoint`, `support_email`, writeback order ids |

"Optional" = created at runtime by a schema guard in prod; absent is recorded in the manifest
instead of failing.

## Not copied (read on the serving path, excluded on purpose)

orders and order_events (buyers; search personalisation falls back to empty), agents and
agent_identity_issuers (step 7 syncs test identities), buyer_identity_links, shop_users,
reap_agentic_buyer_refs (buyers), reap_agentic_purchases, agent_purchase_ledger, a2a_rail_ledger
(ledgers), agent_purchase_consents (consents), commerce_attribution_edges and conversion_click_claims
(buyer-linked), merchant_psps (PSP secret keys), merchant_tasks (internal ops), creator_picks
(creators are real people), product_reviews, media_assets, commissions.

## Import semantics

The sandbox's catalog tables are a **mirror**: rows prod no longer has are deleted (children
first), the rest upserted (parents first), serial/identity sequences bumped past the copied ids,
and each table's row count must equal the manifest's or the whole import rolls back. A sandbox row
in a table that references a copied one (for example a test order on a merchant) survives while
prod keeps that merchant; if prod removes the merchant, the FK's own `ON DELETE` rule applies.

The importer does not trust the export: it re-checks the manifest against the same column rules,
verifies sha256 and row count per file before touching the database, and re-applies the
placeholder and `realtime_enabled=false` itself.

## Known limits

* JSON columns (for example `catalog_merchants.metadata_json`) cannot be checked by column name.
  The manifest reports `email_like_values` per table as a review signal; read it after the first
  prod export.
* A first prod export may refuse on a column name this list has not classified; that is the rule
  working. Classify it (drop or `allow_risky` with a reason) in its own reviewed change.
* No markets filter yet (decision 5 is open): the copy carries every market.
