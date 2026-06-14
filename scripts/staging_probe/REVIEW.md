# Staging Probe Review Notes

## Scope

Implemented artifacts under `scripts/staging_probe/` for a controlled staging-only TEST-mode Stripe probe.

No production commands were run. Do not use `api.pivota.cc`, `Pivota Infra / production`, `pivota-ap2-staging`, or `web-staging`.

## Investigation Findings

- Agent auth for `/agent/v1/...` is in `routes/agent_auth.py`.
- Auth accepts `X-API-Key` or `Authorization: Bearer`.
- Internal trusted env keys are `SHOP_GATEWAY_AGENT_API_KEY`, `PIVOTA_API_KEY`, `PIVOTA_BACKEND_AGENT_API_KEY`, `PIVOTA_AGENT_API_KEY`, and `AGENT_INTERNAL_TRUSTED_API_KEYS`.
- Non-internal keys must match `ak_<64hex>` or `ak_live_<64hex>`.
- DB auth hashes raw keys with SHA-256. `get_agent_by_key` prefers `api_keys`, then `agent_api_keys`, then legacy `agents.api_key` only when enabled or no key table exists.
- `routes/agent_shop_gateway.py` exposes `/agent/shop/v1/invoke`.
- Gateway `create_order` proxies to `/agent/v1/orders/create`.
- Gateway `submit_payment` proxies to `/agent/v1/payments`.
- Public agent order creation requires `quote_id`; the seeded quote fingerprint matches the runbook payload.
- Shopify policy requires a primary active `merchant_stores` row with `platform='shopify'`.
- PR #738 bypass is in `routes/order_routes.py`; it is honored only when order metadata requests it and env has `ALLOW_TEST_PSP_PROBE=1` plus the merchant in `TEST_PSP_PROBE_MERCHANTS`.
- `/agent/v1/payments` still creates new PSP surfaces with `enforce_live_readiness=True`; the controlled path must create the Stripe test PaymentIntent during `create_order`, then reuse it in `submit_payment`.
- Stripe minor units are produced in `adapters/psp_adapter.py` with `int(amount * 100)`. Seed amount `19.99 USD` should produce `1999`.
- Stripe webhook verification reads per-PSP `merchant_psps.provider_config.webhook_endpoint_secret` when posting to `/webhooks/stripe/{psp_id}`.

## Files

- `scripts/staging_probe/seed.py`: idempotent seed and read-only verifier. Requires real `sk_test_...`, `pk_test_...`, and `whsec_...`; refuses placeholders; prints only non-secret verification fields.
- `scripts/staging_probe/setup_staging.sh`: staging-only Railway setup and deploy script.
- `scripts/staging_probe/RUNBOOK.md`: end-to-end setup and operator probe call.
- `scripts/staging_probe/REVIEW.md`: this review log.

## Commands Run So Far

```bash
pwd
git status --short --branch
git rev-parse HEAD
git branch --show-current
railway status
railway --version
railway add --help
railway variables --help
railway domain --help
railway run --help
railway up --help
railway logs --help
railway status --json
railway deployment --help
railway whoami
rg/sed reads across routes, services, adapters, db, migrations, config
python3 -m py_compile scripts/staging_probe/seed.py
bash -n scripts/staging_probe/setup_staging.sh
rg -n "sk_test_|pk_test_|whsec_|DATABASE_URL=postgres|api\\.pivota\\.cc|pivota-ap2-staging|web-staging" scripts/staging_probe
chmod +x scripts/staging_probe/seed.py scripts/staging_probe/setup_staging.sh
rm scripts/staging_probe/__pycache__/seed.cpython-314.pyc
rmdir scripts/staging_probe/__pycache__
```

Results:

- Local repo is `/Users/pengchydan/dev/pivota-backend`.
- Branch is `main`.
- HEAD is `370a27561ab2eb969c9aa9ab238ea72b735db142`.
- Railway CLI is authenticated as `peng.xu9@gmail.com`.
- Current Railway link is `Pivota Infra / staging / web`.
- Staging web domain discovered from read-only Railway status JSON: `web-staging-3f9e.up.railway.app`.
- Local syntax checks passed for `seed.py` and `setup_staging.sh`.
- Secret scan found only documented placeholders/prefix checks and the known staging domain; no real secrets were written.

## Open Items For Execution

Execution was stopped before mutating Railway because the shell does not have the required Stripe TEST inputs:

- `STRIPE_TEST_SECRET_KEY`
- `STRIPE_TEST_PUBLISHABLE_KEY`
- `STRIPE_TEST_WEBHOOK_SECRET`

Remaining steps after the operator supplies those values:

1. Provision Postgres in `Pivota Infra / staging`.
2. Wire staging web `DATABASE_URL` to the new Postgres reference variable.
3. Set staging web probe env vars.
4. Deploy `main` to staging web.
5. Verify `/version` and `/health`.
6. Seed with real Stripe TEST secret, publishable key, and webhook signing secret.
7. Run read-only seed verification.

## Risks

1. The gateway `submit_payment` wrapper does not expose an idempotency key, so B3 may need a direct `/agent/v1/payments` call or a gateway patch if Claude requires gateway-level idempotency proof.
2. `/agent/v1/payments` cannot create a new TEST PSP surface because it enforces live readiness. The intended test path creates the PaymentIntent during `create_order` with the PR #738 bypass, then reuses that surface in `submit_payment`.
3. The seeded Shopify store has no live Shopify Admin token and order writeback is disabled. This isolates the money path but does not validate Shopify fulfillment/writeback.
4. `FRESH_QUOTE_VALIDATE_SKIP_SECONDS=86400` is used for staging so the seeded quote can be used later without a live Shopify quote refresh. This is a staging-only accommodation and should not be copied to production.
