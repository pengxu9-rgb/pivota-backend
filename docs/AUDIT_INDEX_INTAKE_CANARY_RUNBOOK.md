# Canary runbook — URL-audit index seeding (`ENABLE_AUDIT_INDEX_INTAKE`)

Turns on the "audit IS the index-build motion": each URL-audited product is
upserted into `catalog_products` as an OBSERVED, unclaimed `url_audit` seed, and
the per-SKU report emits an evidence-attachable pipe `product_key`
(`merchant|url_audit|source`) so the merchant portal's "Supply proof / upload
docs" action lights up on URL audits.

Default OFF. Ships dark. This runbook enables it for **one canary merchant**,
verifies end-to-end, and lists rollback. Prereq: PR #1105 (seed + guardrail) and
the per-merchant gate are on `main` and deployed.

## 0. Pick the canary

Use a **test/owned merchant** (not a real customer). You need its `merchant_id`
and a login/token for the portal or API. Prefer a merchant that has run URL
audits before so the flow is familiar.

## 1. Enable the flag for ONLY that merchant

The flag is per-merchant via a CSV allowlist (NOT a global boolean — a global
flip would seed every merchant's URL audits at once). On Railway, on the
**backend service** (the one running the audit worker), set:

```
ENABLE_AUDIT_INDEX_INTAKE_MERCHANT_IDS=<canary_merchant_id>
```

Leave `ENABLE_AUDIT_INDEX_INTAKE` unset/false (that's the later graduation switch
for all merchants). Redeploy/restart so the worker + API pick up the env.

> The seed write and the report's pipe key both read this with the same
> `merchant_id`, so they agree — a non-allowlisted merchant sees no change.

The **ADR-008 brand-fragmentation guard follows intake**: it is automatically
active for any merchant intake is enabled for (no separate flag) — a same-brand +
host canonical under another merchant routes the seed to identity review instead
of minting a fragmenting orphan (fail-open: guard errors never block seeding).
`DISABLE_AUDIT_BRAND_FRAGMENTATION_GUARD=true` is an explicit opt-out escape hatch
only, if the guard ever needs to be forced off for a canary; leave it unset in
normal operation.

## 2. Run a URL audit as the canary

Via the portal (paste a product URL and run), or the API:

```
POST /api/merchant-center/audit/url-readiness      # body: the curated URL(s)
GET  /api/merchant-center/audit/url-readiness/{run_id}   # poll until done
```

Use the brand's OWN product URL for the clean path (seeds key on
`canonical_url`; a retailer paste de-conflates to the brand site).

## 3. Verify — the four checks

Prod DB access (read-only is enough): `DATABASE_PUBLIC_URL` + `?sslmode=require`
+ `DB_POOL_MIN_SIZE=1` (see MEMORY / crawl-ingest runbook for the exact psql
incantation). Substitute `:m` = canary merchant_id.

**(a) The seed minted, and is un-served.**
```sql
SELECT product_key, platform, content_key, pdp_lifecycle_stage
FROM catalog_products
WHERE merchant_id = :m AND platform = 'url_audit'
ORDER BY updated_at DESC LIMIT 20;
```
Expect ≥1 row per audited URL; `pdp_lifecycle_stage` **NULL** (un-served). Then
confirm NO serving row exists for those content_keys (unless collided — see d):
```sql
SELECT ips.content_key, ips.serving_eligible
FROM index_pipeline_state ips
WHERE ips.content_key IN (
  SELECT content_key FROM catalog_products WHERE merchant_id = :m AND platform = 'url_audit'
);
```
Expect **0 rows** (a fresh seed has no index_pipeline_state row → not recalled,
searched, or served). If a row appears, it belongs to a COLLIDING real product —
go to (d).

**(b) The report carries the evidence-attachable pipe key.**
In the `GET /url-readiness/{run_id}` response, each `per_sku_reports[].product_key`
should be `"<merchant_id>|url_audit|<source>"` (pipe form) — NOT `urlwedge:...`.
That pipe form is what the portal's `parseProductKey` needs to show the button.

**(c) The evidence endpoint resolves (no 404).**
As the canary merchant, the portal button should now appear on the audit result.
Or hit the endpoint directly (auth as the merchant):
```
GET /merchant/products/url_audit/<source>/evidence        # expect 200, {"claims":[],...}
POST /merchant/products/url_audit/<source>/evidence/lab-report   # upload a small PDF → candidate claims
```
`<source>` = the third segment of the report's pipe product_key. A 404 "sync
your catalog first" means the seed didn't mint (check worker logs for
`url-audit index seed failed`) or the URL differed between seed and report.

**(d) Collision safety spot-check.**
Find any seed content_key shared with a non-audit row, and confirm the real row
still wins canonical (so no claimed PDP was overwritten):
```sql
SELECT content_key, count(*) AS n, array_agg(platform) AS platforms
FROM catalog_products
WHERE content_key IN (
  SELECT content_key FROM catalog_products WHERE merchant_id = :m AND platform = 'url_audit'
)
GROUP BY content_key HAVING count(*) > 1;
```
For any collision, open that product's agent PDP (by content_key) and confirm the
served title/description/image are the REAL product's, not the audit seed's —
this is what the `pick_canonical` tier-0 guardrail protects. (Seeds sort last, so
the real row wins; verified by unit tests, this confirms it in prod data.)

## 4. Graduate or roll back

- **Graduate to all merchants:** set `ENABLE_AUDIT_INDEX_INTAKE=true` (the global
  switch); the allowlist becomes redundant.
- **Roll back:** unset `ENABLE_AUDIT_INDEX_INTAKE_MERCHANT_IDS` and redeploy. New
  audits stop seeding immediately. Existing seeds are harmless (un-served,
  guardrailed) but can be removed if desired:
  ```sql
  DELETE FROM catalog_products WHERE merchant_id = :m AND platform = 'url_audit';
  ```
  (Also clears their agent_pdp_view display-cache rows on the next assembly.)

## What "success" looks like

Seed rows exist (pdp_lifecycle_stage NULL, no index_pipeline_state row) · report
product_key is pipe form · the portal button appears and a lab-report upload
returns candidate claims · any content_key collision still serves the real
product's PDP.
