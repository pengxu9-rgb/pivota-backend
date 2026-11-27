# Sandbox catalog copy (design step 6)

Code: `jobs/sandbox_catalog_copy.py`. Tests: `tests/test_sandbox_catalog_copy.py`,
`tests/test_sandbox_catalog_copy_streaming.py`, `tests/test_sandbox_catalog_copy_postgres.py`.

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
  resumable uploads created with `ifGenerationMatch=0` and never reads back. The import's needs
  `objectViewer` (it reads each object's generation, then its bytes).
* The manifest is written last. The import takes the newest stamp that has one.
* Prices keep their values. The gateway marks sandbox answers `sandbox: true` and treats copied
  prices as fresh (decision 4).

## Tables copied (50)

Whole rows, minus the dropped columns. The export reads prod's own column list (and types) at run
time and **refuses**, before anything is uploaded:

* any column not classified in `jobs/sandbox_catalog_columns.py` (S3: fail closed). Every copied
  column is listed there with a class (key, text, url, num, flag, time, json, replaced); 790 columns
  over 50 tables, taken from both repos' DDL parsed with PostgreSQL's own parser (pivota-backend main
  and the gateway's main e62b57681, which creates 18 of the tables). A column prod has and neither
  repo declares stops the copy, naming the table and column;
* any column whose name looks like personal data or a secret (email(s), phone(s), token, secret,
  api/access/secret/private/signing key, contact, address, customer, buyer, employee, notes, comment,
  `*_by*`, user_id/user_name/user_agent, session/session_id, cookie, authorization, bearer, jwt,
  username, reviewer, actor, passport, card_number, cvv/cvc, pan, tax_id, webhook_url, raw_payload,
  headers, ...) unless the spec drops, replaces or allows it;
* a JSON-typed column that is not classified JSON, or the other way round.

**JSON columns** (113) each carry the reason they are catalog data. The export reads each table
with `COPY ... (FORMAT csv, NULL <a random token>)` and scrubs it in Python before anything is
uploaded (`scrub_csv`):

* at any depth, a key that names a person, a secret, a request or free text (`*by` / `*_by_*` such as
  reviewed_by, createdBy, signed_by_name; `*mail*`, `*address*`, `*phone*`, mobile*; token, secret,
  api_key, session/request/trace id; user, customer_*, employee_*, reviewer*, contact*, name, handle;
  notes, comments, reason, rationale; card fields, raw_payload, headers, webhook_url, ...; and any
  key longer than 256 characters, which no catalog attribute is) has a
  string value replaced by `"redacted"` (a string: the gateway serves a `product_evidence` claim
  only when `reviewed_by` is a non-empty string) and ANY other value, objects and arrays included,
  by null. A JSON column of product data keeps `name` and `handle` at the product level, in the
  items of a top-level array, and under option paths (options, variants, values, breadcrumb, ...);
  elsewhere (reviews[].name, seller.handle) they are redacted;
* email- and phone-shaped parts of every other JSON string, every JSON key and every text column
  become `[redacted]` (a brand's support address in a description, a contact under a catalog-named
  key); a JSON object with a repeated key is always rewritten from its parsed value;
* the export then checks its own output and refuses on anything left, and on an email-shaped value
  in any other column (a key or URL); the manifest's `email_like_values` is 0 by construction and
  the import refuses one that is not;
* a rewritten JSON cell keeps its numbers' own text (2.50 stays 2.50); an untouched cell keeps its
  bytes. NULL and the empty string stay distinct;
* an array of JSON or a domain over JSON is refused (review it first).

The import checks every file again before touching the database. The manifest (format 2) lists
each table's `json_columns`; the import refuses format 1 (an export from before the scrub).
`aurora_ingredient_research_kb` copies only `status = 'ready'` rows with a `query_norm` of at most 64
characters (it is built from a user's ingredient query).

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

### Read directly by the gateway (22, from the sandbox gateway session's list, 25 Nov)

| table | why | dropped / classified |
|---|---|---|
| content_canonical_election | canonical listing per content key | |
| catalog_source_quarantine | quarantined sources stay hidden | drops `created_by`, `revoked_by`; `reason` (ops free text) = a placeholder |
| merchant_product_overlay | approved PDP content overlays | drops `approved_by` |
| product_evidence | claims and required disclaimers | |
| pdp_identity_listing (optional) | the PDP identity graph | `matched_by_rule` classified (a rule name) |
| pdp_identity_override (optional) | identity overrides | drops `created_by` |
| product_beauty_attributes (optional) | beauty attributes | drops `audit_notes` |
| relationship_candidate_labels, relgraph_reviewed_ingredient_evidence, relgraph_reviewed_seller_evidence (optional) | the relationship graph | `human_review` and the other JSON scrubbed |
| aurora_product_intel_kb, aurora_ingredient_research_kb, aurora_travel_context_kb (optional) | product KBs | |
| aurora_dupe_kb (optional) | dupe KB | drops `verified_by` |
| taxonomy_view, taxonomy_view_category, canonical_category, category_localization (optional) | category taxonomy | `replaced_by_id` classified |
| merchant_category_mapping, ops_category_override (optional) | category mapping and ops pins | drops `updated_by`; `ops_category_override.reason` = a placeholder |

Two names on the gateway's list are not copied: `product_relationship_edges` is a VIEW over
`relationship_candidate_labels` (gateway migrations 046/051; it arrives with them, and an import
cannot write a view), and `brands` is a CTE name inside one gateway query, not a table.

`agent_pdp_view.refreshed_by_proposal_id` is classified (a proposal id); a DDL-wide check found it.

"Optional" = created at runtime (a backend schema guard or the gateway's own migrations); absent in
prod is recorded in the manifest instead of failing. **Present in the export but absent in the
sandbox DB fails the import**, so the gateway's migrations (`node src/db/cli.js migrate`) must run
on the sandbox DB before the first import.

## Not copied (read on the serving path, excluded on purpose)

orders and order_events (buyers; search personalisation falls back to empty), agents and
agent_identity_issuers (step 7 syncs test identities), buyer_identity_links, shop_users,
reap_agentic_buyer_refs (buyers), reap_agentic_purchases, agent_purchase_ledger, a2a_rail_ledger
(ledgers), agent_purchase_consents (consents), commerce_attribution_edges and conversion_click_claims
(buyer-linked), merchant_psps (PSP secret keys), merchant_tasks (internal ops), creator_picks
(creators are real people), product_reviews, media_assets, commissions.

Read by the gateway but not copied: pdp_identity_review_queue (free-text reviewer notes),
reco_label_suggestions (session ids), creator_category_override (keyed by creator), external_seed_corrections
(ops ledger), external_offers_cache (a cache of live store reads), external_offer_snapshots (ops
inspector only), products_cache_embeddings and its fallback (pgvector, size; vector lane is a
follow-up), every aurora_* consumer table and the services/bookings tables (people). The gateway's
runtime tables (commerce_kv, gateway_caller_limit_*, external_seed_image_cache_jobs,
relationship_graph_anchor_attempts) start empty and are never copied; they must exist.

## Import semantics

The sandbox's catalog tables are a **mirror**: rows prod no longer has are deleted (children
first), the rest upserted (parents first), serial/identity sequences bumped past the copied ids,
and each table's row count must equal the manifest's or the whole import rolls back. A sandbox row
in a table that references a copied one (for example a test order on a merchant) survives while
prod keeps that merchant; if prod removes the merchant, the FK's own `ON DELETE` rule applies.

The importer does not trust the export: it re-checks the manifest against the same column rules,
verifies sha256, gzip framing, row count and the scrub per file in one streamed read before touching
the database, and re-applies the placeholder and `realtime_enabled=false` itself. The load then
streams the same object generation into COPY and checks its sha256 again at the end; a mismatch
raises inside the COPY and rolls the import back.

## Prod load and memory (S5)

Nothing holds a table in memory, on either side. COPY's output is cut into whole CSV records
(`RecordSplitter`), scrubbed and checked, gzipped and sent as a GCS resumable upload in 8 MiB
pieces. The import reads 1 MiB at a time and inflates at most 1 MiB per step, so a small file that
inflates hugely is still processed in bounded pieces. One record is the largest unit either side
holds; a record over 256 MiB (`MAX_RECORD_BYTES`) is refused.

On prod the export runs in one `REPEATABLE READ, READ ONLY` transaction with these `SET LOCAL`
limits, which end with it:

| Setting | Default | Why |
|---|---|---|
| `statement_timeout` | 20min | bounds each table's COPY (and its count) |
| `lock_timeout` | 5s | never queue behind a migration's `ACCESS EXCLUSIVE` request, which would stall every query queued behind it |
| `idle_in_transaction_session_timeout` | 2min | a stalled job gives its snapshot back |
| `client_connection_check_interval` (PG 14+) | 5s | a terminated session stops even inside a scan that sends nothing |
| `application_name` | `sandbox_catalog_export` | visible in `pg_stat_activity` |

A client-side deadline (45 min, at most 2 h) **terminates** the connection, which ends the session,
its transaction and its snapshot on the server at once. The deadline bounds the source, not the
job: a block already being scrubbed in its worker thread runs to its end (one COPY chunk's records,
at most one 256 MiB record), is dropped unwritten, and then the job refuses. A limit reached on the server is a refusal
naming it. Either way nothing is written: every file is an unfinished upload until the last table
has passed, and only then are the uploads committed and the manifest written. A failure during
that commit (an upload refused at its final piece, say) can leave some of the stamp's files in
place without a manifest. The import only reads a stamp that has a manifest, so it never imports
them; delete them by hand.

What the run line should still weigh:

* The snapshot holds `ACCESS SHARE` on every table it has read until it commits. A migration that
  needs `ACCESS EXCLUSIVE` on one of them waits until the export ends (up to the deadline), and
  queries queued behind that migration wait too. Run the export outside a deploy window.
* A long snapshot holds back vacuum's horizon for its duration.
* `DATABASE_URL` can name a read replica instead of the primary; nothing in the code changes. On a
  hot standby a long query can be cancelled by a recovery conflict (`max_standby_streaming_delay`),
  which shows up as a refusal or an error and writes nothing; rerun it.
* A refusal inside a table (a scrub finding) lets that table's COPY run to its end with the rest of
  its output dropped unread. asyncpg 0.31 stalls a connection whose COPY sink raises mid-stream, so
  the sink keeps the refusal and the export raises it after the COPY. `statement_timeout` and the
  deadline bound that COPY.

## Known limits

* The scrub knows people by key names and by email/phone shapes. A person's name under a
  catalog-named key (free text that is neither) is not caught; the key list is broad for that reason.
* A first prod export may refuse on a column the classification does not list (prod drift), on a
  risky name, or on a person key holding an object; that is the rule working. Classify, drop or
  scrub it in its own reviewed change.
* Phones are recognised only with context: an international number written with "+", or a number
  right after a phone word (tel, phone, call, mobile, fax, whatsapp, contact, and the Korean, Japanese
  and Chinese words). A bare digit group (a SKU, GTIN, year, size or price) is never redacted, so a
  phone written that way in free text is not caught. Emails exclude image names such as `hero@2x.png`.
* No markets filter yet (decision 5 is open): the copy carries every market.
