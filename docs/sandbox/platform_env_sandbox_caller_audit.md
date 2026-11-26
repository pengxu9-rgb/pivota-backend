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
| `main.py` `docs_viewer_allowed` / `require_docs_ui_enabled` | full internal OpenAPI and `/docs` served anonymously | **open, as on staging** — see follow-ups |
| `main.py` robots.txt | disallow all | disallow all |
| `utils/service_token.py` | weak-token length check prod-only | exempt (sandbox keys are freshly minted per the infra note) |
| `utils/runtime_safety.refuse_in_production` (agent card issuance, Reap card-issuer webhooks) | open | open; Reap is the sandbox host (guard) |
| `db/buyer_vault.py` | ERROR vs WARNING on a missing hash key | WARNING |
| `jobs/reap_operator.py`, `jobs/reap_agentic_purchase_poll.py` | must use a Reap sandbox host | agrees with the guard |
| `routes/shakeout_debug.py` | endpoints mounted; need `SHAKEOUT_DEBUG_TOKEN` | **guard refuses a set token**, so they answer 403 |
| `routes/auth.py`, `routes/auth_routes.py` | demo logins if `ENABLE_INTERNAL_DEMO_FIXTURES` | **guard refuses the flag on** |
| `routes/webhook_routes.py` Stripe livemode gate | test-mode events accepted | accepted (no live Stripe key allowed by the guard) |
| `routes/billing_routes.py` | test-mode accepted | same |
| `routes/payment_routes.py` | unsigned checkout webhook logged, not fatal, when no secret | **same as staging** — see follow-ups |
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

## Follow-ups (not in this change)

1. The full internal OpenAPI is anonymously readable outside production. On a partner-reachable
   sandbox web service that is a route map. Either keep the sandbox web service private (only the
   gateway public), or close `docs_viewer_allowed` for the sandbox in its own PR.
2. `routes/payment_routes.py` accepts an unsigned checkout webhook outside production. In the
   sandbox that can only fake a simulated order in the sandbox's own database; set
   `CHECKOUT_WEBHOOK_SECRET` there or close it for the sandbox.
3. `proof_issuer_main.py` does not run the sandbox guard; it is not part of sandbox v0.
