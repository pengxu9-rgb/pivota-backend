# Sandbox env profile: what each service runs with, and every staging setting it drops

Source: the staging Cloud Run services `web`, `worker`, `gateway` and `agenpas` in
`pivota-staging/us-west1`, read on 2026-11-25 with `gcloud run services describe`. Only env
**names**, secret **references** and plain flag values that are not secret-shaped were read. No
secret value was read, and plain values whose names look secret-shaped (`*AUTH*`, `*KEY*`, ...)
were withheld by the reading filter too (shown below as "value not read").

The profiles themselves are [`env/web.yaml`](env/web.yaml), [`env/worker.yaml`](env/worker.yaml),
[`env/gateway.yaml`](env/gateway.yaml) and [`env/agenpaz.yaml`](env/agenpaz.yaml). Secret mounts are
in [`secrets/`](secrets/README.md). [`check_profile.py`](check_profile.py) enforces the rules below
on every deploy and in `tests/test_sandbox_infra.py`.

## The contract (enforced by check_profile.py)

| rule | what it guarantees |
|---|---|
| `PIVOTA_ENV=sandbox` on all four | both boot checks key off it: the backend guard (sandbox-backend) and the gateway's (sandbox-gateway) refuse to serve on a prod host, credential or live flag, naming the setting only |
| no `A2A_*` setting at all | the real-money A2A rail is not configured, not merely switched off |
| `REAP_API_BASE_URL=https://sandbox.api.reap.global` | the Reap rail can only reach Reap's sandbox |
| `REAP_AGENTIC_SIMULATE_CHECKOUT=COMPLETED` | every Reap checkout is simulated and ends `completed` |
| `AGENPAS_RAILS=reap` and `AGENPAS_CONSENT_ENABLED=true` (gateway, agenpaz) | AgenPaz links are minted for the Reap rail only; without consent the gateway drops reap and mints a2a links |
| no `NODE_ENV`/`APP_ENV` of production or test (gateway, agenpaz) | the gateway boot check refuses both |
| every live-read, store-egress, scheduler, sweep, email and other-payment switch present **and off** | several of these default ON in code, so "absent" is not "off" |
| every URL/host on an allowlist | prod hosts (`api/gateway/mcp/...pivota.cc`, `pay.agenpaz.com`), staging, Railway, the A2A provider's hosts and any other project's `run.app` are refused; on web/worker a `pivota.cc`/`agenpaz.com`/`agenpas.com`/`reap.global` host also needs a `sandbox` label, as the backend guard requires |
| no secret-shaped plain env var | credentials only come from the sandbox's own Secret Manager |
| worker = web except 4 named keys | the two backend services cannot drift apart |
| hosts are found in EVERY value (URLs, bare hosts, `//host`, emails), and a pivota.cc / agenpaz.com / agenpas.com / reap.global host needs a `sandbox` label on all four services | mirrors both boot checks; the only exceptions are the two exact consent-link pairs and `AGENPAS_LEGAL_HOSTS` = pivota.cc / merchant.pivota.cc |
| an empty value is not "off" | several switches default ON in code when empty |
| no IPv4 literal at all, in any value | the DB and Redis arrive only through mounted secrets, so an address in a profile is a mistake; and the public repo carries no private IP |
| no credential-shaped value under any name (live Stripe/agent keys, `ak_test_` keys, private keys, cloud tokens, a DSN with a password) | a secret pasted into a "flag" is caught at review |
| secret-mount names are secret-shaped and never shadow or set a pinned key (`PIVOTA_ENV`, `REAP_API_BASE_URL`, `A2A_*`, `NODE_ENV`, ...) | a secret cannot override what the profile pins |
| `PRICE_CACHE_FIRESTORE_PROJECT` / `PIVOTA_AGENT_CENTER_GEMINI_VERTEX_PROJECT` equal `GOOGLE_CLOUD_PROJECT` when set; gateway/agenpaz `AURORA_*_FAIL_CLOSED` unset or off | the gateway's allow-list rules |

## The two boot checks this profile is written against

Both sibling sessions have local commits (not yet pushed) whose checks this profile satisfies:
- **backend** (sandbox-backend): always on under `PIVOTA_ENV=sandbox`; no switch. Requires the Reap
  sandbox URL + simulate, `ORDER_CREATE_LIVE_QUOTE_REVALIDATION_ENABLED=0`, no valued `A2A_RAIL_*`,
  live/crawl/probe flags off, a worker allowlist within `reap_agentic_purchase_poll,agent_purchase_events`,
  and no value naming a prod/staging project, prod/staging private IP, Railway host, or a brand host
  without a `sandbox` label. `config/platform.py` on main does not know `sandbox` yet, so web and
  worker cannot boot until that change ships in a release.
- **gateway** (sandbox-gateway): `ak_test_<64 hex>` keys only, `sandbox: true` on every MCP answer,
  copied prices not relayed as stale; refuses a live-format service key, `NODE_ENV=production|test`,
  any `A2A_RAIL_*` key, consent off with reap, and the live-read / image-cache switches (the image
  cache job runner must be off explicitly). The gateway's own service key `PIVOTA_API_KEY` must be
  an `ak_test_` key: the MCP door's loopback presents it, and with it unset the gateway is not in
  REAL mode. It is minted in the sandbox (secrets/README.md) and its hash registered as the
  gateway's service agent in the sandbox DB (RUNBOOK.md step 14).

**Ruled by sandbox-backend (2026-11-25):** `AGENPAS_TERMS_URL` (`https://pivota.cc/terms`) and
`AGENPAS_PRIVACY_URL` (`https://merchant.pivota.cc/privacy`) are the links a buyer accepts on the
AgenPaz page. Its guard allows exactly these two (setting, value) pairs, because the backend never
fetches them. `check_profile.py` names them on every run until that commit is in the release.

## Kept with a different value than staging, and why

| service | setting | staging → sandbox | why |
|---|---|---|---|
| all | `PIVOTA_ENV` | staging → sandbox | the environment's name |
| web, worker | `DB_POOL_MIN_SIZE` / `DB_POOL_MAX_SIZE` | 2/8 (worker 2/10) → 1/4 | db-g1-small allows 50 connections: web 2×4 + worker 4 + gateway 2×3 + import ≈ 22 |
| web, worker | `REAP_AGENTIC_PREFLIGHT_MODE` | shadow → off | extra Reap quote per purchase; not needed for a simulated flow |
| web, worker | `REAP_AGENTIC_PRICE_WRITEBACK` | shadow (worker: on) → off | writes catalog prices from store reads; the sandbox does no store reads and never edits the copied catalog |
| web, gateway | `REAP_AGENTIC_CART_LINK_ENRICHMENT_ENABLED` | 1 → 0 | enrichment reads the merchant's store |
| web, worker | `FF_ENABLE_REFUND_AUTO_RETRY` | true → false | retry loop toward a PSP; no PSP credential here |
| web, worker | `AGENPAS_TERMS_VERSION` / `AGENPAS_PRIVACY_VERSION` | staging-draft-2026-11 → sandbox-2026-11 | consent rows recorded in the sandbox say so |
| web, worker | `EXTERNAL_CONVERSION_POLLER_ENABLED`, `PAYMENT_RECONCILE_SWEEP_ENABLED`, `ENABLE_IDENTITY_RECONCILE_SWEEP` | on → off | background loops; the only background work is the Reap purchase poll |
| web | `STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED` | true → false | store probes fetch stores |
| web, worker | `OUTBOUND_WARM_HANDOFF_ENABLED`, `AGENT_CHECKOUT_UCP_DISCOVERY_ENABLED`, `SERVE_PDP_ENRICHMENT_ON_WRITE`, `CATALOG_ONBOARD_ENABLED`, `INDEXNOW_ENABLED`, `GSC_INTEGRATION_ENABLED`, `PDP_SCOPE_BACKFILL_ENABLED` | on → off | each fetches or notifies an outside host, or rewrites catalog rows |
| web, worker | `ORDER_CONFIRMATION_EMAIL_ENABLED` | true → false | no email provider; a partner's test buyer must not get mail |
| web, worker, gateway | `VERTEX_AI_ENABLED` | true → false | no LLM calls (the Vertex project is not the sandbox's) |
| web, worker | `AGENT_SHOP_PIVOT_MULTI_SHADOW_ENABLED`, `AGENT_SHOP_UPSTREAM_CLIENT_WARMUP_ENABLED` | true → false | shadow compare and a warm-up toward the dropped upstream URL |
| worker | `SCHEDULER_JOB_ALLOWLIST` | `reap_agentic_purchase_poll,a2a_rail_purchase_poll` → `reap_agentic_purchase_poll,agent_purchase_events` | no A2A rail; the second id is the other job the backend guard allows |
| worker | `PAYMENT_ORCHESTRATOR_ENABLED`, `REAP_EXTERNAL_AUTH_ENABLED=0`, `AGENT_CARD_ISSUANCE_ENABLED=0`, `PIVOTA_PLATFORM` | unset → as web | worker = web (rule R9) |
| gateway, agenpaz | `AGENPAS_RAILS` | a2a,reap (agenpaz: unset) → reap | Reap only |
| gateway, agenpaz | `AGENPAS_CONSENT_ENABLED` | 1 → true | same meaning; the spelling the gateway contract documents |
| agenpaz | `NODE_ENV` | production → unset | the gateway boot check refuses `NODE_ENV=production` in the sandbox |
| gateway | `PIVOTA_API_KEY` *(secret)* | staging's key → a minted `ak_test_` key | the gateway's service key must be in the sandbox format |
| gateway | `AGENT_CHECKOUT_UCP_*` (discovery, tool door, escalation, merchant pricing, seller discount codes), `AGENT_CHECKOUT_HOSTED_LINK_ENABLED`, `AGENT_CHECKOUT_ACP_FEED_ENABLED`, `ACP_PUBLIC_FEED`, `UCP_WARM_HANDOFF_*`, `UCP_BUYER_AGENT_PROFILE_ENABLED` | 1 → 0 | each reaches a merchant's own endpoint or needs a signing key; the sandbox's only purchase path is start_purchase → Reap |
| gateway | `PUBLIC_READ_MCP_ENABLED` | 1 → 0 | keyless reads off: every sandbox call carries an `ak_test_` key |
| gateway | `LLM_KILL_SWITCH` | false → true | no LLM calls |
| gateway | `PDP_GROUNDED_PRODUCT_INTEL_ENABLED`, `AURORA_TRAVEL_WEATHER_LIVE_ENABLED` | true → false | LLM / live third-party calls |
| gateway | `FIND_PRODUCTS_MULTI_VECTOR_ENABLED` | true → false | query embeddings need Gemini/Vertex. **Sandbox search is lexical only** (see RUNBOOK.md Q7) |
| agenpaz | `AGENPAS_HOSTED_HOSTS` | Reap sandbox page + A2A provider dev host → Reap sandbox page only | no A2A rail |
| agenpaz | `AGENPAS_COLLECT_SHIPPING` | 1 → 0 | the page's address form posts to a backend write gated by `A2A_SHIPPING_CAPTURE_ENABLED`; partners send the address in start_purchase |

URL values are rendered at deploy time: every staging `web-<staging number>.us-west1.run.app` becomes
the sandbox's own `@@WEB_URL@@`, and `AGENPAS_PUBLIC_BASE_URL` becomes `https://@@AGENPAZ_HOST@@`.
`AGENT_AUTH_INTROSPECT_URL` (value not read on staging) is rendered as
`@@WEB_URL@@/agent/internal/auth/introspect`, the route deploy_backend.sh documents.

## Added (not set on staging), and why

Explicit off-switches whose code default is ON or unknown, so the profile says what the sandbox
does instead of relying on a default: `ORDER_CREATE_LIVE_QUOTE_REVALIDATION_ENABLED=0` (default 1,
routes/order_routes.py; merchant re-quote), `LIVE_OFFER_VERIFICATION_ENABLED=0`,
`LIVE_OFFER_VERIFICATION_ALLOW_REQUEST_PATH_EGRESS=0`, `CHECKOUT_PREFLIGHT_MODE=off`,
`CHECKOUT_PREFLIGHT_ALLOW_EGRESS=0`, `OFFICIAL_DOMAIN_LIVENESS_ENABLED=0`, `WEBHOOK_DELIVERY_ENABLED=false`,
`QUALITY_BACKFILL_WORKER_ENABLED`, `CATALOG_INVARIANT_SWEEP_ENABLED`, `COMMERCE_INDEX_INSIGHT_REFRESH_ENABLED`,
`AGENT_CARD_REVOCATION_SWEEP_ENABLED`, `STORE_AUDIT_*_SCHEDULER_ENABLED`, `STORE_AUDIT_COMMERCE_PROBE_RECEIPT_ENABLED`
(all false); gateway `SERVE_LIVE_MERCHANT_PRICE=0`, `PDP_IDENTITY_GRAPH_AUTO_ENABLE_LIVE=0`,
`AURORA_BFF_PDP_HOTSET_PREWARM_ENABLED=false`, `DB_POOL_MAX=3`, and the gateway contract's live-read list set
to 0 on gateway and agenpaz (`UCP_LIVE_OFFER_READ`, `UCP_LIVE_SEARCH_READ`, `UCP_LIVE_PRODUCT_READ`,
`UCP_LIVE_PDP_READ`, `GET_OFFERS_LIVE_CART`, `CAFE24_CATALOG_SEARCH`, `MERCHANT_VARIANT_SOURCING_ENABLED`;
agenpaz also the image-cache and live-price switches); web/worker `LEGACY_SETTLEMENT_LIVE`,
`SETTLEMENT_TRANSFER_ALLOWED_ON_STAGING`, `ENABLE_INTERNAL_DEMO_FIXTURES` (all false).

web/worker also carry `ISSUING_AGENT_EXCLUDED_AGENT_IDS` and `ISSUING_ASSERTION_VOUCHER_AGENT_IDS`,
both `agent_388a63cd97b4fb54`: the sandbox gateway's own service agent (RUNBOOK.md step 14). The code
hard-codes prod's gateway agent for both roles; these settings only ADD ids, so the sandbox's must be
listed or MCP links are credited to the gateway and partners are never named as the issuing agent.
The naming itself needs `ISSUING_AGENT_ASSERTION_SECRET` on both ends (review S9): the gateway signs
the partner it verified into `X-Pivota-Issuing-Agent` and web verifies it. It is minted in the
sandbox and mounted by gateway and web only (`secrets/*.list`, `check_profile.py` R8); without it
every link and order made over MCP is credited to no partner, silently. Both must hold exactly
`ISSUING_AGENT_ASSERTION_SECRET=ISSUING_AGENT_ASSERTION_SECRET`: the key under another env name, or
that env name fed by another secret, breaks credit the same silent way (R8).
agenpaz runs the same image but never signs: with `AGENPAS_ENABLED=1` and `AGENPAS_PUBLIC_BASE_URL`
set (both pinned, R4/R5), the AgenPaz host guard (PIVOTA-Agent `src/agenpas/router.js`, mounted
first) serves only the buyer pages and the consent/address posts, the load balancer sends only that
host to agenpaz, and none of those paths calls the kernel's signing upstream. So it holds no key.

gateway and agenpaz also pin `SKIP_DB_MIGRATIONS=true` and `DB_AUTO_MIGRATE=false` (agenpaz newly).
Four public request paths in the gateway image run its whole migration set unless
`SKIP_DB_MIGRATIONS=true`, so on a public gateway any caller could start one. The gateway schema is
applied once, deliberately, by RUNBOOK step 10b.

All four also set `GOOGLE_CLOUD_PROJECT` to the sandbox's own project, rendered from `config.sh`
(token `@@SANDBOX_PROJECT@@`). Staging's value named another project (it is in the dropped table).
Here it is a second identity signal next to `PIVOTA_ENV`: the gateway boot check requires it, the
backend guard accepts only the sandbox project, and the deploy reads both back off every new revision.

Set by `deploy_sandbox.sh` on every revision, not in the profiles: `PIVOTA_SERVICE_NAME`,
`PIVOTA_COMMIT_SHA` (the release's commit), `SANDBOX_RELEASE` (the tag).

## Dropped: every staging setting not carried over, grouped by reason

Counts are per service. A name in this table is neither set nor mounted in the sandbox; the code
default applies, and for most of them the feature it configures is off or absent.

591 (service, setting) pairs dropped in all.

### a2a (web 6, worker 12)

The A2A rail moves real money; the sandbox configures no `A2A_*` setting at all (check_profile R2).

- **web:** `A2A_RAIL_CREATE_ENABLED`, `A2A_RAIL_ENABLED`, `A2A_RAIL_MAX_TOTAL_MINOR`, `A2A_RAIL_MERCHANTS`, `A2A_RAIL_PILOT_AGENT_IDS`, `A2A_SHIPPING_CAPTURE_ENABLED`
- **worker:** `A2A_RAIL_API_KEY` *(secret)*, `A2A_RAIL_BASE_URL`, `A2A_RAIL_CHECKOUT_CEILING_MAX_MINOR`, `A2A_RAIL_CHECKOUT_SHIPPING_ALLOWANCE_MINOR`, `A2A_RAIL_CHECKOUT_STEP_ENABLED`, `A2A_RAIL_CHECKOUT_TAX_ALLOWANCE_BPS`, `A2A_RAIL_CONTEXT_SECRET` *(secret)*, `A2A_RAIL_ENABLED`, `A2A_RAIL_MAX_TOTAL_MINOR`, `A2A_RAIL_TOKEN_STEP_ENABLED`, `A2A_RAIL_WORKER_ENABLED`, `A2A_SHIPPING_CAPTURE_ENABLED`

### thirdparty (web 44, worker 44, gateway 8)

Third-party account or credential (on staging most are the PROD values, infra/gcp/README.md). The sandbox holds no prod/staging credential, and none of these is on the partner purchase path.

- **web, worker:** `ADYEN_API_KEY` *(secret)*, `ADYEN_MERCHANT_ACCOUNT`, `ADYEN_WEBHOOK_PASSWORD` *(secret)*, `ADYEN_WEBHOOK_SECRET` *(secret)*, `ADYEN_WEBHOOK_USERNAME` *(secret)*, `AMAZON_SP_API_CLIENT_ID`, `AMAZON_SP_API_CLIENT_SECRET` *(secret)*, `AMAZON_SP_API_REGION`, `AWS_ACCESS_KEY_ID` *(secret)*, `AWS_ENDPOINT_URL`, `AWS_REGION`, `AWS_SECRET_ACCESS_KEY` *(secret)*, `DEEPSEEK_API_KEY` *(secret)*, `FEATURE_AMAZON_SP_API`, `FEISHU_WEBHOOK_URL` *(secret)*, `FROM_EMAIL`, `GOOGLE_CLOUD_LOCATION`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_REDIRECT_URI`, `OPENAI_API_KEY` *(secret)*, `SENDGRID_API_KEY` *(secret)*, `SERPAPI_API_KEY` *(secret)*, `SHOPIFY_APPSTORE_SCOPES`, `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET` *(secret)*, `SHOPIFY_DISCOUNT_RECONCILIATION_MODE`, `SHOPIFY_HEADLESS_CLIENT_ID`, `SHOPIFY_HEADLESS_CLIENT_SECRET` *(secret)*, `SHOPIFY_HEADLESS_REDIRECT_URI`, `SHOPIFY_MAX_PAGES_PER_RUN`, `SHOPIFY_MAX_PRODUCTS_PER_RUN`, `SHOPIFY_REDIRECT_URI`, `SHOPIFY_SCOPES`, `SHOPIFY_WEBHOOK_BASE_URL`, `SMTP2GO_API_KEY` *(secret)*, `STRIPE_BILLING_WEBHOOK_SECRET` *(secret)*, `STRIPE_PRICE_ID_GROWTH`, `STRIPE_PRICE_ID_SCALE`, `STRIPE_PRICE_ID_STARTER`, `STRIPE_SECRET_KEY` *(secret)*, `STRIPE_WEBHOOK_SECRET` *(secret)*, `WIX_API_KEY` *(secret)*, `WIX_STORE_URL`
- **gateway:** `AWS_ACCESS_KEY_ID` *(secret)*, `AWS_REGION`, `AWS_SECRET_ACCESS_KEY` *(secret)*, `EMAIL_PROVIDER`, `GOOGLE_CLOUD_LOCATION`, `GOOGLE_CLOUD_PROJECT`, `OPENAI_API_KEY` *(secret)*, `SENDGRID_API_KEY` *(secret)*

### stagingurl (web 15, worker 12, gateway 10)

Points at a staging/prod/Railway host or a staging-only issuer. Sandbox equivalents are rendered from tokens (`@@WEB_URL@@`) or registered per partner by hand in v0 (buyer-token issuers go in the sandbox DB, not env).

- **web:** `AGENT_API_BASE`, `AGENT_SHOP_MULTI_UPSTREAM_FALLBACK_BASE_URL`, `AGENT_SHOP_MULTI_UPSTREAM_FALLBACK_TIMEOUT_SECONDS`, `AGENT_USER_JWKS_JSON`, `AGENT_USER_JWT_AUDIENCE`, `AGENT_USER_JWT_ISSUERS`, `CATALOG_INTELLIGENCE_BASE_URL`, `MCP_OAUTH_AS_ALLOWED_RESOURCES`, `MCP_OAUTH_AS_ENABLED`, `MCP_OAUTH_AS_ISSUER`, `MCP_OAUTH_AS_KEY_ID` *(secret)*, `MCP_OAUTH_AS_PRIVATE_KEY_PEM` *(secret)*, `MCP_OAUTH_AS_REQUEST_SECRET` *(secret)*, `RECOMMENDATIONS_SERVICE_BASE_URL`, `RECOMMENDATIONS_SERVICE_TIMEOUT_MS`
- **worker:** `AGENT_API_BASE`, `AGENT_SHOP_MULTI_UPSTREAM_FALLBACK_BASE_URL`, `AGENT_SHOP_MULTI_UPSTREAM_FALLBACK_TIMEOUT_SECONDS`, `CATALOG_INTELLIGENCE_BASE_URL`, `MCP_OAUTH_AS_ALLOWED_RESOURCES`, `MCP_OAUTH_AS_ENABLED`, `MCP_OAUTH_AS_ISSUER`, `MCP_OAUTH_AS_KEY_ID` *(secret)*, `MCP_OAUTH_AS_PRIVATE_KEY_PEM` *(secret)*, `MCP_OAUTH_AS_REQUEST_SECRET` *(secret)*, `RECOMMENDATIONS_SERVICE_BASE_URL`, `RECOMMENDATIONS_SERVICE_TIMEOUT_MS`
- **gateway:** `IDENTITY_ISSUERS_JSON`, `MCP_OAUTH_AUTHORIZATION_SERVERS`, `MCP_OAUTH_ISSUERS_JSON`, `MCP_OAUTH_RESOURCE`, `PAYMENT_ISSUERS_JSON`, `PIVOTA_AGENT_PUBLIC_BASE_URL`, `PIVOTA_GATEWAY_URL`, `PUBLIC_READ_MCP_HOSTS`, `UCP_AGENT_PROFILE_URL`, `UCP_BASE_URL`

### stagingids (web 3, worker 3, gateway 2)

Staging-specific merchant or brand ids. Meaningless in the sandbox DB, and they name real merchants.

- **web, worker:** `PIVOTA_TEST_MERCHANT_IDS`, `SUBMIT_PAYMENT_MERCHANTS`, `TEST_PSP_PROBE_MERCHANTS`
- **gateway:** `PDP_IDENTITY_GRAPH_BRAND_ALLOWLIST`, `PIVOTA_TEST_MERCHANT_IDS`

### internalkey (web 5, worker 4, gateway 5)

Internal key for a feature that is off in the sandbox. Not minted; nothing in the sandbox presents or checks it. (ADMIN_API_KEY is the exception where it IS kept: see the secret lists.)

- **web:** `CHECKOUT_WEBHOOK_SECRET` *(secret)*, `PROMOTIONS_ADMIN_KEY` *(secret)*, `RECOMMENDATIONS_INTERNAL_KEY` *(secret)*, `SHAKEOUT_DEBUG_TOKEN` *(secret)*, `STORE_AUDIT_UCP_PROBE_INTERNAL_KEY` *(secret)*
- **worker:** `CHECKOUT_WEBHOOK_SECRET` *(secret)*, `PROMOTIONS_ADMIN_KEY` *(secret)*, `RECOMMENDATIONS_INTERNAL_KEY` *(secret)*, `SHAKEOUT_DEBUG_TOKEN` *(secret)*
- **gateway:** `AGENT_API_KEY` *(secret)*, `CREATOR_INVOKE_API_KEY` *(secret)*, `DISCOVERY_PRODUCTS_SEARCH_API_KEY` *(secret)*, `PROMOTIONS_ADMIN_KEY` *(secret)*, `RECOMMENDATIONS_INTERNAL_KEY` *(secret)*

### secretflag (web 4, worker 4, gateway 3)

Staging holds this FLAG as a Secret Manager secret, so its value was not read (rule: never read secret values). Left to the code default. If the proof run needs it, set it as a plain env value after the owner checks it.

- **web, worker:** `ATTRIBUTE_EXTRACTOR_MAX_TOKENS` *(secret)*, `CITABLE_TOKEN_MATCH` *(secret)*, `CORS_ALLOW_CREDENTIALS` *(secret)*, `PIVOT_CANONICAL_TOKEN_MATCH` *(secret)*
- **gateway:** `CREATOR_INVOKE_REQUIRE_KEY` *(secret)*, `PIVOT_BEAUTY_MAINLINE_TOKEN_MATCH_ENABLED` *(secret)*, `PIVOT_BEAUTY_TOKEN_RELEVANCE_RANK_ENABLED` *(secret)*

### egress (web 3, worker 3, gateway 32)

Writes to R2/S3, fetches merchant/store hosts, or signs for a door the sandbox keeps closed (UCP/ACP).

- **web, worker:** `OUTBOUND_LINKS_INTERNAL_KEY` *(secret)*, `OUTBOUND_WARM_HANDOFF_BRANDS`, `OUTBOUND_WARM_HANDOFF_INTERNAL_KEY` *(secret)*
- **gateway:** `ACP_FEED_SOURCE`, `ACP_SIGNING_SECRET` *(secret)*, `CATALOG_IMAGE_CACHE_BOOTSTRAP_FORCE_CACHE`, `CATALOG_IMAGE_CACHE_BOOTSTRAP_LIMIT`, `CATALOG_IMAGE_CACHE_BOOTSTRAP_MARKET`, `CATALOG_IMAGE_CACHE_BOOTSTRAP_PRODUCT_IDS`, `CATALOG_IMAGE_CACHE_PUBLIC_BASE_URL`, `CATALOG_IMAGE_CACHE_S3_ACCESS_KEY_ID` *(secret)*, `CATALOG_IMAGE_CACHE_S3_BUCKET`, `CATALOG_IMAGE_CACHE_S3_ENDPOINT`, `CATALOG_IMAGE_CACHE_S3_REGION`, `CATALOG_IMAGE_CACHE_S3_SECRET_ACCESS_KEY` *(secret)*, `OUTBOUND_LINKS_INTERNAL_KEY` *(secret)*, `OUTBOUND_WARM_HANDOFF_INTERNAL_KEY` *(secret)*, `PROXY_SEARCH_AURORA_ALLOW_EXTERNAL_SEED`, `PROXY_SEARCH_AURORA_DISABLE_SKIP_AFTER_RESOLVER_MISS`, `PROXY_SEARCH_AURORA_EXTERNAL_SEED_STRATEGY`, `PROXY_SEARCH_AURORA_FALLBACK_TIMEOUT_MS`, `PROXY_SEARCH_AURORA_FORCE_FAST_MODE`, `PROXY_SEARCH_AURORA_FORCE_INVOKE_FALLBACK`, `PROXY_SEARCH_AURORA_FORCE_SECONDARY_FALLBACK`, `PROXY_SEARCH_AURORA_FORCE_TWO_PASS`, `PROXY_SEARCH_AURORA_PASS1_TIMEOUT_MS`, `PROXY_SEARCH_AURORA_PASS2_TIMEOUT_MS`, `PROXY_SEARCH_AURORA_PRESERVE_SOURCE_ON_INVOKE`, `PROXY_SEARCH_AURORA_PRIMARY_TIMEOUT_MS`, `PROXY_SEARCH_AURORA_UPSTREAM_SOURCE`, `UCP_AGENT_SIGNING_KEY_ID` *(secret)*, `UCP_AGENT_SIGNING_PUBLIC_JWK` *(secret)*, `UCP_BUSINESS_SIGNING_PRIVATE_JWK` *(secret)*, `UCP_BUSINESS_SIGNING_PUBLIC_JWK` *(secret)*, `UCP_WARM_HANDOFF_INTERNAL_KEY` *(secret)*

### llm (web 18, worker 18, gateway 184)

LLM / Aurora consumer-beauty feature. The sandbox has no LLM credential and makes no LLM call (`LLM_KILL_SWITCH=true`); not on the partner path.

- **web, worker:** `AURORA_PRODUCT_GROUNDING_STABLE_ALIAS_PATH`, `CITATION_READ_TELEMETRY`, `DIAG_SKINMASK_BBOX_FALLBACK_ENABLED`, `DIAG_SKINMASK_CLASS_ID`, `DIAG_SKINMASK_ENABLED`, `DIAG_SKINMASK_MODEL_CACHE_PATH`, `DIAG_SKINMASK_MODEL_DOWNLOAD_TIMEOUT_MS`, `DIAG_SKINMASK_MODEL_MAX_BYTES`, `DIAG_SKINMASK_MODEL_PATH`, `DIAG_SKINMASK_MODEL_URL`, `ENABLE_SUBSTANTIATION_GRADING`, `ENABLE_TIER3_JUDGE`, `PHOTO_UPLOAD_ACCESS_KEY_ID` *(secret)*, `PHOTO_UPLOAD_BUCKET`, `PHOTO_UPLOAD_SECRET_ACCESS_KEY` *(secret)*, `PHOTO_UPLOAD_SESSION_TOKEN` *(secret)*, `PIVOTA_AGENT_CENTER_LLM_GATE_QUEUE_TIMEOUT_MS`, `PIVOTA_AGENT_CENTER_LLM_TENANT_CONCURRENCY`
- **gateway:** `AGENT_INTEL_PUBLIC_CLAIMS_ENABLED`, `ALLOW_GUARD_TEST`, `AURORA_ANALYSIS_STORY_LLM_TIMEOUT_MS`, `AURORA_ANALYSIS_STORY_MODEL_GEMINI`, `AURORA_BFF_ANALYSIS_BUDGET_MS`, `AURORA_BFF_AUTH_DEBUG_RETURN_CODE`, `AURORA_BFF_AUTH_EMAIL_FROM`, `AURORA_BFF_AUTH_ENABLED`, `AURORA_BFF_AUTH_PEPPER`, `AURORA_BFF_AUTH_SESSION_TTL_MS`, `AURORA_BFF_CHAT_RECO_BUDGET_MS`, `AURORA_BFF_CONFLICT_HEATMAP_V1_ENABLED`, `AURORA_BFF_PDP_IDENTITY_RECONCILE_ENABLED`, `AURORA_BFF_PRODUCT_INTEL_AGENT_ENABLED`, `AURORA_BFF_PRODUCT_INTEL_CATALOG_FALLBACK`, `AURORA_BFF_PRODUCT_INTEL_CATALOG_FALLBACK_MAX_QUERIES`, `AURORA_BFF_PRODUCT_URL_COMPETITOR_SYNC_TIMEOUT_MS`, `AURORA_BFF_PRODUCT_URL_COMPETITOR_TIMEOUT_MS`, `AURORA_BFF_RECO_ALTERNATIVES_SYNC_TIMEOUT_MS`, `AURORA_BFF_RECO_ALTERNATIVES_TIMEOUT_MS`, `AURORA_BFF_RECO_BLOCKS_BUDGET_MS`, `AURORA_BFF_RECO_BLOCKS_TIMEOUT_CATALOG_ANN_MS`, `AURORA_BFF_RECO_CATALOG_BEAUTY_ROUTE_FIRST`, `AURORA_BFF_RECO_CATALOG_ENABLE_BEAUTY_PATH_FALLBACK`, `AURORA_BFF_RECO_CATALOG_FAIL_FAST_COOLDOWN_MS`, `AURORA_BFF_RECO_CATALOG_FAIL_FAST_PROBE_INTERVAL_MS`, `AURORA_BFF_RECO_CATALOG_FAIL_FAST_PROBE_SEARCH_TIMEOUT_MS`, `AURORA_BFF_RECO_CATALOG_FAIL_FAST_THRESHOLD`, `AURORA_BFF_RECO_CATALOG_GROUNDED`, `AURORA_BFF_RECO_CATALOG_SEARCH_BASE_URLS`, `AURORA_BFF_RECO_CATALOG_SEARCH_PATHS`, `AURORA_BFF_RECO_CATALOG_SEARCH_SOURCE`, `AURORA_BFF_RECO_CATALOG_SEARCH_TIMEOUT_MS`, `AURORA_BFF_RECO_CATALOG_TRANSIENT_FALLBACK`, `AURORA_BFF_RECO_CENTRALIZED_FAILURE_MAPPING_ENABLED`, `AURORA_BFF_RECO_DOGFOOD_MODE`, `AURORA_BFF_RECO_PDP_CHAT_DISABLE_LOCAL_DOUBLE_HOP`, `AURORA_BFF_RECO_PDP_ENRICH_MAX_NETWORK_ITEMS`, `AURORA_BFF_RECO_PDP_FAST_EXTERNAL_FALLBACK`, `AURORA_BFF_RECO_PDP_LOCAL_SEARCH_FALLBACK_ON_TRANSIENT`, `AURORA_BFF_RECO_PDP_RESOLVE_ENABLED`, `AURORA_BFF_RECO_PDP_STRICT_INTERNAL_FIRST`, `AURORA_BFF_RECO_PRELABEL_ADMIN_KEY` *(secret)*, `AURORA_BFF_RECO_PRELABEL_ENABLED`, `AURORA_BFF_RECO_PRELABEL_MODEL`, `AURORA_BFF_RECO_STEP_AWARE_CATALOG_FIRST_ENABLED`, `AURORA_BFF_RECO_STEP_AWARE_SHADOW_COMPARE_ENABLED`, `AURORA_BFF_RECO_UPSTREAM_TIMEOUT_HARD_CAP_MS`, `AURORA_BFF_RECO_UPSTREAM_TIMEOUT_MS`, `AURORA_BFF_RELATIONSHIP_GRAPH_AGENT_ENABLED`, `AURORA_BFF_RELATIONSHIP_GRAPH_ALL_FEEDS_ENABLED`, `AURORA_BFF_RELATIONSHIP_GRAPH_ANCHOR_IDENTITY_HYDRATION_ENABLED`, `AURORA_BFF_RELATIONSHIP_GRAPH_DISCOVERY_ENABLED`, `AURORA_BFF_RELATIONSHIP_GRAPH_ENABLED`, `AURORA_BFF_RELATIONSHIP_GRAPH_FAMILY_COLLAPSE_ENABLED`, `AURORA_BFF_RELATIONSHIP_GRAPH_PDP_ENABLED`, `AURORA_BFF_USE_MOCK`, `AURORA_CHATCARDS_RESPONSE_CONTRACT`, `AURORA_CHAT_CATALOG_AVAIL_FAST_PATH`, `AURORA_CHAT_CLARIFICATION_FILTER_KNOWN`, `AURORA_CHAT_CLARIFICATION_FLOW_V2`, `AURORA_CHAT_CLARIFICATION_HISTORY_CONTEXT`, `AURORA_CHAT_RESPONSE_META_ENABLED`, `AURORA_CHAT_RESUME_PREFIX_V2`, `AURORA_CHAT_RESUME_PROBE_METRICS`, `AURORA_DECISION_BASE_URL`, `AURORA_DIAG_FORCE_GEMINI_MODEL`, `AURORA_EXPERIMENTS_JSON`, `AURORA_FORCE_VARIANT_DEBUG_KEY` *(secret)*, `AURORA_FORCE_VARIANT_ENABLED`, `AURORA_INGREDIENT_GOAL_ENRICH_TIMEOUT_MS`, `AURORA_INGREDIENT_RATE_LIMIT_COOLDOWN_MS`, `AURORA_INGREDIENT_RESEARCH_ASYNC_ENABLED`, `AURORA_INGREDIENT_RESEARCH_MODEL_GEMINI`, `AURORA_INGREDIENT_SINGLE_CALL_MODE`, `AURORA_INGREDIENT_SYNC_MODEL_GEMINI`, `AURORA_INGREDIENT_SYNC_REPORT_TIMEOUT_MS`, `AURORA_INGREDIENT_SYNC_RETRIES`, `AURORA_INGREDIENT_SYNC_TOTAL_BUDGET_MS`, `AURORA_KB_FAIL_MODE`, `AURORA_KB_V0_DISABLE`, `AURORA_LLM_OPENAI_FALLBACK_ENABLED`, `AURORA_LLM_QA_MODE`, `AURORA_LLM_SINGLE_PROVIDER`, `AURORA_LOOP_BREAKER_V2_ENABLED`, `AURORA_PHOTO_AUTO_ANALYZE_AFTER_CONFIRM`, `AURORA_PRODUCT_INTEL_LLM_MODEL`, `AURORA_PRODUCT_INTEL_LLM_PROVIDER`, `AURORA_PRODUCT_INTEL_PROMPT_VERSION`, `AURORA_PRODUCT_LOOKUP_LLM_FALLBACK_ENABLED`, `AURORA_PRODUCT_RELEVANCE_DUAL_LLM_QA`, `AURORA_PRODUCT_RELEVANCE_DUAL_LLM_TIMEOUT_MS`, `AURORA_PROFILE_V2_ENABLED`, `AURORA_PSEUDO_LABEL_DIR`, `AURORA_PSEUDO_LABEL_ENABLED`, `AURORA_QA_MODEL_GEMINI`, `AURORA_QA_PLANNER_V1_ENABLED`, `AURORA_RECO_FALLBACK_MODEL_GEMINI`, `AURORA_ROLLOUT_ENABLED`, `AURORA_ROLLOUT_V2_CORE_PCT`, `AURORA_ROLLOUT_V2_SAFETY_PCT`, `AURORA_ROLLOUT_V2_WEATHER_PCT`, `AURORA_ROUTES_FAIL_CLOSED`, `AURORA_ROUTINE_ANALYSIS_V2_ENABLED`, `AURORA_ROUTINE_AUDIT_V1_ENABLED`, `AURORA_ROUTINE_PRODUCT_AUTOSCAN_ENABLED`, `AURORA_ROUTINE_SUMMARY_FIRST_ENABLED`, `AURORA_SAFETY_ENGINE_V1_ENABLED`, `AURORA_SKIN_FORCE_VISION_CALL`, `AURORA_SKIN_VISION_ENABLED`, `AURORA_SKIN_VISION_MODEL_GEMINI`, `AURORA_SKIN_VISION_PROVIDER`, `AURORA_SURFACE_DENIED_HOSTS`, `AURORA_TRAVEL_LLM_CALIBRATION_V1`, `AURORA_TRAVEL_LLM_CALIBRATION_V1_ENABLED`, `DIAGNOSIS_V2_ENABLED`, `DIAG_CANARY_PERCENT`, `DIAG_ENSEMBLE_GEMINI_MODEL`, `DIAG_GEMINI_VERIFY`, `DIAG_GEMINI_VERIFY_HARD_CASE_PATH`, `DIAG_GEMINI_VERIFY_MODEL`, `DIAG_INGREDIENT_REC`, `DIAG_OVERLAY_MODE`, `DIAG_PHOTO_MODULES_CARD`, `DIAG_PIPELINE_VERSION`, `DIAG_PRODUCT_REC`, `DIAG_PRODUCT_REC_MIN_CITATIONS`, `DIAG_PRODUCT_REC_MIN_EVIDENCE_GRADE`, `DIAG_PRODUCT_REC_REPAIR_ONLY_WHEN_DEGRADED`, `DIAG_SHADOW_MODE`, `DIAG_SKINMASK_BBOX_FALLBACK_ENABLED`, `DIAG_SKINMASK_CLASS_ID`, `DIAG_SKINMASK_ENABLED`, `DIAG_SKINMASK_MODEL_CACHE_PATH`, `DIAG_SKINMASK_MODEL_DOWNLOAD_TIMEOUT_MS`, `DIAG_SKINMASK_MODEL_MAX_BYTES`, `DIAG_SKINMASK_MODEL_PATH`, `DIAG_SKINMASK_MODEL_URL`, `DIAG_VERIFY_ENABLE_VOTE`, `DIAG_VERIFY_MAX_CALLS_PER_DAY`, `DIAG_VERIFY_MAX_CALLS_PER_MIN`, `DIAG_VERIFY_SHADOW_ENABLED`, `DIAG_VERIFY_TIMEOUT_MS`, `GEMINI_GLOBAL_CIRCUIT_COOLDOWN_MS`, `GEMINI_GLOBAL_CIRCUIT_FAIL_THRESHOLD`, `GEMINI_GLOBAL_CONCURRENCY_MAX`, `GEMINI_GLOBAL_RATE_PER_MIN`, `GEMINI_IMAGE_JPEG_QUALITY`, `GEMINI_IMAGE_MAX_EDGE`, `GEMINI_TIMEOUT_MS`, `GEMINI_TRYON_IMAGE_MODEL`, `LAYER1_ENABLE_GEMINI_REFERENCE_LOOKSPEC`, `LAYER1_ENABLE_GEMINI_SELFIE_LOOKSPEC`, `LAYER2_ENABLE_EXTENDED_AREAS`, `LAYER2_ENABLE_EYE_ACTIVITY_SLOT`, `LAYER2_ENABLE_SELFIE_LOOKSPEC`, `LAYER2_ENABLE_TRIGGER_MATCHING`, `LLM_MAX_ATTEMPTS`, `LLM_TIMEOUT_MS`, `LOOK_REPLICATOR_API_KEY` *(secret)*, `LOOK_REPLICATOR_CHECKOUT_PROVIDER`, `LOOK_REPLICATOR_DISABLE_ONE_CLICK`, `LOOK_REPLICATOR_DISABLE_TRYON_IMAGE`, `LOOK_REPLICATOR_MAX_UPLOAD_BYTES`, `LOOK_REPLICATOR_ONE_CLICK_MODEL_OPENAI`, `LOOK_REPLICATOR_ONE_CLICK_PROVIDER`, `LOOK_REPLICATOR_PUBLIC_ASSET_BASE_URL`, `LOOK_REPLICATOR_S3_ACCESS_KEY_ID` *(secret)*, `LOOK_REPLICATOR_S3_BUCKET`, `LOOK_REPLICATOR_S3_ENDPOINT`, `LOOK_REPLICATOR_S3_REGION`, `LOOK_REPLICATOR_S3_SECRET_ACCESS_KEY` *(secret)*, `LOOK_REPLICATOR_SIGNED_URL_TTL_SECONDS`, `LOOK_REPLICATOR_TRYON_MODEL_OPENAI`, `LOOK_REPLICATOR_TRYON_PROVIDER`, `PIVOTA_AGENT_CENTER_OPENAI_MODEL`, `PIVOTA_EMBEDDINGS_MODEL_GEMINI`, `PIVOTA_EMBEDDINGS_PROVIDER`, `PIVOTA_INTENT_LLM_FALLBACK_PROVIDER`, `PIVOTA_INTENT_LLM_PROVIDER`, `PIVOTA_LAYER2_LLM_FALLBACK_PROVIDER`, `PIVOTA_LAYER2_LLM_PROVIDER`, `PIVOTA_UI_CHAT_LLM_MODEL_GEMINI`, `PIVOTA_UI_CHAT_LLM_PROVIDER`

### merchant (web 40, worker 40, gateway 5)

Merchant-portal, audit, billing or onboarding feature. Partners in the sandbox are agents, not merchants.

- **web, worker:** `ALLOWED_ORIGINS`, `AUDIT_ACTIONS_PAYWALL_ENABLED`, `AUDIT_ATTRIBUTE_EXTRACTOR_ENABLED`, `AUDIT_ATTRIBUTE_EXTRACTOR_MERCHANTS`, `AUDIT_DEEP_TIER_ENABLED`, `AUDIT_SHARE_LINKS_ENABLED`, `BILLING_EXEMPT_MERCHANT_IDS`, `CHECKOUT_CANCEL_URL`, `CHECKOUT_SUCCESS_URL`, `ENABLE_AP2_ROUTES`, `ENABLE_AUDIT_BRAND_FRAGMENTATION_GUARD`, `ENABLE_AUDIT_ER_GATE`, `ENABLE_BRAND_CLAIM_EMAIL_VERIFY`, `ENABLE_INTAKE_IDENTITY_AUDIT`, `ENABLE_INTAKE_IDENTITY_BRAND_AUTHORED`, `ENABLE_INTAKE_IDENTITY_ENRICHMENT`, `ENABLE_INTAKE_IDENTITY_MIRROR`, `ENABLE_INTAKE_IDENTITY_SYNC`, `FEATURE_PLATFORM_ONBOARDING_V2`, `FEATURE_PLATFORM_ORDERS_ACP`, `FEATURE_READINESS_AUDIT`, `FEATURE_READINESS_CANONICAL_CHECKOUT_ALPHA`, `FEATURE_READINESS_PAYMENT_BRIDGE_ALPHA`, `FEATURE_READINESS_PAYMENT_INTENT_ALPHA`, `FEATURE_READINESS_PAYMENT_STATUS_SYNC_ALPHA`, `FEATURE_READINESS_REAL_MERCHANT_ALPHA`, `FEATURE_READINESS_REFUND_ALPHA`, `FEATURE_READINESS_RETURN_SYNC_ALPHA`, `FEATURE_READINESS_SOURCE_OF_TRUTH_V1`, `FEATURE_READINESS_UCP_THIN_SLICE`, `FF_ENABLE_INTERNAL_REFUND`, `FREE_AUDIT_COUNT_SINCE`, `FREE_URL_AUDITS_PER_MERCHANT`, `INDEX_ELIGIBLE_SITEMAP`, `MERCHANT_AUDIT_RATE_MAX`, `METRICS_QUERY_VERSION`, `PLATFORM_ORDERS_ACP_TOKEN` *(secret)*, `READINESS_ALPHA_MERCHANT_ID`, `READINESS_INTERNAL_API_KEY` *(secret)*, `STRATEGIC_BRIEF_ENABLED`
- **gateway:** `CREATOR_CATALOG_AUTO_SYNC_LIMIT`, `NEXT_PUBLIC_AGENT_API_KEY` *(secret)*, `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_MERCHANT_ID`, `READINESS_INTERNAL_API_KEY` *(secret)*

### reviews (web 15, worker 15)

Reviews / invitations / review media. Not on the partner path; needs S3 credentials and sends email.

- **web, worker:** `REVIEWS_BUYER_INVITATION_LINK_BASE_URL`, `REVIEWS_BUYER_INVITATION_SHORTLINK_BASE_URL`, `REVIEWS_BUYER_PROOF_ISSUER_INTERNAL_KEY` *(secret)*, `REVIEWS_BUYER_PROOF_SIGNING_SECRET` *(secret)*, `REVIEWS_BUYER_SUBMIT_ENABLED`, `REVIEWS_BUYER_SUBMIT_INTERNAL_ISSUER_ENABLED`, `REVIEWS_IMPORT_DELETE_ON_COMMIT`, `REVIEWS_INVITATION_ISSUER_INTERNAL_KEY` *(secret)*, `REVIEWS_INVITATION_SEND_DELAY_SECONDS`, `REVIEWS_MEDIA_RPM`, `REVIEWS_MEDIA_S3_BUCKET`, `REVIEWS_MEDIA_S3_PREFIX`, `REVIEWS_MEDIA_SIGNING_SECRET` *(secret)*, `REVIEWS_PROOF_ISSUER_BASE_URL`, `REVIEW_MEDIA_PUBLIC_BASE_URL`

### pcikb (web 2, worker 2, gateway 3)

Second database (ingredient/PCI reference) and its credential. Staging credential; only beauty-ingredient features read it.

- **web, worker:** `PCI_KB_DATABASE_URL` *(secret)*, `POSTGRES_DB`
- **gateway:** `INGREDIENT_REFERENCE_DATABASE_URL` *(secret)*, `INGREDIENT_REFERENCE_DB_SSL`, `PCI_KB_DATABASE_URL` *(secret)*

### redis (web 1, worker 1, gateway 1)

No Memorystore in v0 (saves ~$36/mo). The one shared use on the partner path, the assertion replay guard, falls back to an in-process map (services/assertion_replay_guard.py). Add `WITH_REDIS` later if web runs >1 instance under load.

- **web, worker:** `REDIS_URL` *(secret)*
- **gateway:** `REDIS_URL` *(secret)*

### authcache (gateway 4)

Staging value not read (name is auth-shaped and the filter withholds such values). Code defaults apply.

- **gateway:** `AGENT_AUTH_CACHE_MAX_ENTRIES`, `AGENT_AUTH_CACHE_NEGATIVE_TTL_MS`, `AGENT_AUTH_CACHE_POSITIVE_TTL_MS`, `AGENT_AUTH_INTROSPECT_TIMEOUT_MS`

### offfeature (web 3, worker 3)

Tuning value for a feature the sandbox profile turns off (ACP capture caps; upstream client warm-up).

- **web, worker:** `AGENT_ACP_LIVE_MAX_CENTS`, `AGENT_ACP_TEST_MAX_CENTS`, `AGENT_SHOP_UPSTREAM_CLIENT_WARMUP_TIMEOUT_SECONDS`

### tuning (web 1, worker 1, gateway 9)

Debug/eval-only knob, or a tuning value whose feature is off here.

- **web, worker:** `ECALL_RELEVANCE_V2`
- **gateway:** `FIND_PRODUCTS_MULTI_DEBUG_STATS`, `SEARCH_DEBUG_BUNDLE_ALLOWLIST`, `SEARCH_DOMAIN_CONDENSER_ENTROPY_TH`, `SEARCH_DOMAIN_CONDENSER_MIN_CANDS_AFTER`, `SEARCH_DOMAIN_CONDENSER_MIN_CANDS_BEFORE`, `SEARCH_EVAL_INTERNAL_ONLY_ENABLED`, `SEARCH_EVAL_INTERNAL_ONLY_FORCE_NO_EARLY_DECISION`, `SEARCH_EVAL_INTERNAL_ONLY_HEADER`, `SEARCH_EVAL_INTERNAL_ONLY_UPSTREAM_DISABLED`

### unread (web 1, worker 1)

Read by no code in pivota-backend (checked 2026-11-25); kept off the sandbox profile so it cannot be mistaken for a live switch.

- **web, worker:** `CHECKOUT_MODE`

### node_env (agenpaz 1)

The gateway image's sandbox boot check refuses `NODE_ENV=production` (it would relabel the service as production) and `NODE_ENV=test` (it disables auth).

- **agenpaz:** `NODE_ENV`
