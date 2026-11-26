# `PIVOTA_ENV=sandbox`: what every environment check does with it

Partner sandbox v0, design step 4 (claude.ai artifact N72GzumjqaW8h7SCE96jt7). Written 2026-11-25
from a grep of the callers (not just the names) on `main` at 207e9c451.

## The choice

`sandbox` is its own label in `config/platform.py`:

| predicate | sandbox answers | why |
|---|---|---|
| `platform_env()` | `"sandbox"` | its own label; accepted only from `PIVOTA_ENV`, exactly `sandbox` (any case, trimmed). `RAILWAY_ENVIRONMENT=sandbox` and near-misses (`sandbx`) still fail closed to production |
| `is_production()` | False | it must never take a prod-only money path (settlement transfers, prod Reap) |
| `is_staging()` | False | sandbox is not staging; no non-test code calls `is_staging()` today |
| `is_sandbox()` | True | new |
| `is_development()` | False | |
| `is_deployed()` | **True, even with no platform marker** | every `is_deployed()` gate is a strict gate; a laptop that sets `PIVOTA_ENV=sandbox` gets the strict answers too |
| `require_platform_env()` | returns `"sandbox"` | then `main.app_lifespan` runs `config.sandbox_guard` |

Net: for every `is_production()` and `is_deployed()` call site the sandbox gives **the same answer
as a staging Cloud Run revision**. It differs from staging only where code compares
`platform_env()` with a string, listed below.

Prod and staging are unchanged. The new `config/platform.py` was compared with main's on 8,160
answers (17 `PIVOTA_ENV` values × 5 `RAILWAY_ENVIRONMENT` values × 6 platform-marker sets × with and
without `PYTEST_CURRENT_TEST`; every public predicate, `platform_metadata()`,
`require_platform_env()` raise/return and `pytest_bypass_allowed()`): 0 differences.
`tests/test_sandbox_guard.py::test_every_existing_label_answers_as_before` pins the truth table. The
one changed byte for non-sandbox hosts is the text of `require_platform_env()`'s error, which now
lists `sandbox` among the accepted values.

## `platform_env()` compared with a string (where sandbox ≠ staging)

| call site | does | sandbox |
|---|---|---|
| `services/audit_scheduler.py` `_queue_worker_enabled` | shared-queue worker off on staging/dev/test by default | **changed: `sandbox` added**, so off by default like staging; `AUDIT_WORKER_ENABLED=true` opts in, and the guard then requires a `SCHEDULER_JOB_ALLOWLIST` of purchase jobs only |
| `services/audit_scheduler.py` boot log | prints the label | prints `sandbox` |
| `services/reap_agentic_purchase.py` `_pilot_scope` | staging may use the base-only pilot scope | sandbox may not: it needs the complete scope, `unrestricted`, or unset (unset outside prod = unrestricted) |
| `services/scheduler_job_allowlist.py` `_fail_closed_applies` | outside production, worker on + allowlist unset = allow nothing | applies (sandbox is not production) |
| `services/webhook_delivery_gate.py` | outbound webhooks only in production unless `WEBHOOK_DELIVERY_ENABLED` | off by default; set `WEBHOOK_DELIVERY_ENABLED=true` if partners should get purchase webhooks |
| `config/sentry_config.py` | Sentry environment tag | `sandbox` (unless `ENVIRONMENT` is set) |

`raw_environment_label()` (health payloads in `main.py` and `proof_issuer_main.py`, settlement
metadata, `scripts/inventory_legacy_test_merchants.py`) publishes `sandbox`. Nothing compares it.

## `is_production()` call sites (sandbox: False, as staging)

| call site | outside production | in the sandbox |
|---|---|---|
| `main.py` `_should_skip_heavy_startup_init` | heavy init runs unless `SKIP_HEAVY_STARTUP_INIT` | same as staging |
| `main.py` `docs_viewer_allowed` / `require_docs_ui_enabled` | full internal OpenAPI and `/docs` served anonymously | **closed, as in production (S6)**: the full spec needs the admin key (an anonymous read gets the same 307 to the public partner spec), and `/docs` / `/redoc` answer 404. Partners reach the sandbox |
| `main.py` robots.txt | disallow all | disallow all |
| `utils/service_token.py` | weak-token length check prod-only | exempt (sandbox keys are freshly minted per the infra note) |
| `utils/runtime_safety.refuse_in_production` (agent card issuance, Reap card-issuer webhooks) | open | open; Reap is the sandbox host (guard) |
| `db/buyer_vault.py` | ERROR vs WARNING on a missing hash key | WARNING |
| `jobs/reap_operator.py`, `jobs/reap_agentic_purchase_poll.py` | must use a Reap sandbox host | agrees with the guard |
| `routes/shakeout_debug.py` | endpoints mounted; need `SHAKEOUT_DEBUG_TOKEN` | **guard refuses a set token**, so they answer 403 |
| `routes/auth.py`, `routes/auth_routes.py` | demo logins if `ENABLE_INTERNAL_DEMO_FIXTURES` | **guard refuses the flag on** |
| `routes/webhook_routes.py` Stripe livemode gate | test-mode events accepted | accepted (no live Stripe key allowed by the guard) |
| `routes/billing_routes.py` | test-mode accepted | same |
| `routes/payment_routes.py` | unsigned checkout webhook logged, not fatal, when no secret | **fatal, as in production (S6)**: `_unsigned_webhook_is_fatal` adds `is_sandbox()`. The sandbox has no Checkout.com account, so with no secret every event is a 503; with one, only a signed event passes |
| `routes/photos.py` | schema ensure on request | same as staging |
| `services/reap_agentic_purchase.py` pilot scope unset | allowed outside prod | allowed |
| `services/settlement_file_service.py` | no transfer unless `SETTLEMENT_TRANSFER_ALLOWED_ON_STAGING=true` | **guard refuses that flag** |
| `config/settings.py` JWT secret length | enforced on deployed or prod | enforced (deployed) |

## `is_deployed()` call sites (sandbox: True)

All strict, all as on staging: `utils/runtime_safety.is_production_runtime` (closes the opt-in
admin/cleanup routes), `utils/startup_mode` (skips heavy DDL at boot), `db/database.py` (refuses
`DB_SSL_NO_VERIFY`), `db/buyer_vault.py` (reports a missing hash key), `routes/webhook_routes.py`
`_shopify_prod_runtime` and `routes/shopify_setup.py` (HMAC enforced), `services/buyer_access_token.py`
(shared PEM required), `config/settings.py` (JWT), `services/audit_scheduler.py`,
`config.platform.pytest_bypass_allowed` (test bypasses off).

## `ENVIRONMENT` / `APP_ENV` read directly (bypassing `config/platform.py`)

`utils/runtime_safety.py`, `utils/service_token.py`, `routes/webhook_routes.py` (Stripe livemode,
Shopify strictness), `routes/payment_routes.py`, `routes/billing_routes.py`, `routes/shopify_setup.py`,
`routes/photos.py`, `routes/refund_webhook_routes.py`, `routes/agent_shop_gateway.py` (`DEV_MODE`)
and `config/sentry_config.py` (the environment tag) read these directly. The guard pins both to
unset or `sandbox` (S1), so none of them sees `production` in the sandbox, and Sentry tags it
`sandbox`.

## Settings whose code default is a prod host (`config/settings.py`)

Unset in the sandbox, these would call or link to prod. The two the server CALLS must be set
(guard, S1): `PIVOTA_AGENT_INTERNAL_URL` (default `https://gateway.pivota.cc`: the beauty-search and
observed-seller detail proxies, both flag-off by default, and the LLM probe) and
`OUTBOUND_WARM_HANDOFF_RESOLVE_URL`. Set each to empty or an allowed sandbox URL. The rest are links
shown to a person, never fetched by the server (portal base URLs, `CHECKOUT_SUCCESS_URL`,
`CHECKOUT_CANCEL_URL`, `AGENT_ACP_CHECKOUT_URL_BASE`, `GOOGLE_OAUTH_REDIRECT_URI`): see follow-ups.

### Fallbacks an env value cannot switch off (SF1)

`routes/accounts_orders_api.py` (`AURORA_BFF_BASE ... or DEFAULT_AURORA_BFF_BASE`) and
`routes/employee_kb_monitoring.py` (`AURORA_BFF_BASE_URL or DEFAULT_BASE_URL`) fall back to prod's
gateway even when the setting is empty. Unrefused, the anonymous `POST /accounts/auth/aurora/exchange`
would send the caller's bearer token to prod's `/v1/auth/me` and create a shop_user with the real email
prod returns. In the sandbox the exchange answers 404 before any call and the monitor reports itself
disabled. `tests/test_sandbox_prod_default_hosts.py` lists every prod host named in any string in the
code (URLs, bare host constants composed later, concatenation and f-string pieces, emails, User-Agent
texts; pivota.cc/.ai, agenpaz/agenpas.com, reap.global, run.app and Railway hosts) with what keeps it
from prod in the sandbox, so a new fallback of this shape fails a test. It found one more thing outside
the sandbox's scope: `run_refund_migration.py` (a root one-off script, not imported by the app)
hard-codes a legacy Railway DSN with its credential; see follow-ups.

`config.settings.resolve_public_api_base_url` falls back to `https://api.pivota.cc` (managed-inbox
receiver URLs, the MCP OAuth login redirect, OpenAPI servers). The guard requires its RESOLVED value
to be `https://sandbox.mcp.pivota.cc` (SF2), so `PUBLIC_API_BASE_URL` must be set.

## Follow-ups (not in this change)

1. `proof_issuer_main.py` does not run the sandbox guard; it is not part of sandbox v0.
2. Display-only links that default to prod hosts (portal base URLs, checkout success/cancel,
   `AGENT_ACP_CHECKOUT_URL_BASE`) would hand a partner a prod URL. No money and no prod write, but a
   wrong link. Set them for the sandbox when the sandbox has those pages.
3. `routes/agent_shop_gateway.py` mounts `/agent/shop/v1/dev/*` whenever `APP_ENV` is not
   `production` (whether prod sets `APP_ENV` was not checked here). Read-only catalog debugging, not
   sandbox-specific.
4. `run_refund_migration.py` carries a hard-coded legacy Railway database URL including its password,
   in a public repository: confirm that credential is revoked (Railway is no longer prod) and delete
   the script. Not changed here (outside the sandbox).
