# Stripe PSP Runtime Repair Runbook

## Goal
- Repair Stripe `merchant_psps` rows after test/live drift, stale webhook secrets, or duplicate active rows.
- Keep runtime truth in `merchant_psps` only.
- Re-run the canonical `/merchant/psp/{psp_id}/test` validation path so live webhook endpoint metadata is reprovisioned by the application, not by ad hoc DB edits.

## Preconditions
- Backend convergence branch is merged and deployed before running this procedure.
- Use `git push` driven deployment only.
- Run all commands from the backend repo root.
- Required environment variables:
  - `DATABASE_URL`
  - `PIVOTA_API_BASE_URL=https://api.pivota.cc`
  - `PIVOTA_ADMIN_EMAIL`
  - `PIVOTA_ADMIN_PASSWORD`

## Scripts
- Audit / normalize drifted rows:
  - `scripts/backfill_canonical_merchant_psps.py`
- Re-run deployed Stripe validation / webhook provision:
  - `scripts/ops_revalidate_stripe_psps.py`

## Step 1: Read-only drift audit
- Generate a Stripe-only audit report for active rows:

```bash
python3 scripts/backfill_canonical_merchant_psps.py \
  --provider stripe \
  --only-drifted \
  --output /tmp/stripe_psp_drift_report.json
```

- Review the summary at the end of stdout.
- Review `/tmp/stripe_psp_drift_report.json` for row-level evidence.

Expected drift buckets:
- `environment_mismatch`
- `stripe_live_missing_webhook`
- `stripe_missing_public_key`
- `duplicate_active_provider`
- `valid_but_not_live_ready`

## Step 2: Canary merchant review
- Start with the known canary PSP:
  - `psp_stripe_40z8k0n42xi8`
- Inspect only that row:

```bash
python3 scripts/backfill_canonical_merchant_psps.py \
  --provider stripe \
  --psp-id psp_stripe_40z8k0n42xi8 \
  --output /tmp/stripe_psp_canary_audit.json
```

- Confirm before apply:
  - API key prefix matches intended environment.
  - `environment` is normalized correctly.
  - `provider_config.webhook_endpoint_secret` will be cleared if identity drift is detected.
  - `validation_status` is reset to `unknown` for drifted rows.

## Step 3: Apply DB normalization
- Apply the canonical normalization to drifted Stripe rows:

```bash
python3 scripts/backfill_canonical_merchant_psps.py \
  --provider stripe \
  --only-drifted \
  --apply \
  --output /tmp/stripe_psp_drift_apply.json
```

- This step only normalizes DB truth and clears stale validation/webhook state when required.
- This step does not provision new webhooks by itself.

## Step 4: Dry-run validation targets
- Preview which rows the deployed validation runner will hit:

```bash
python3 scripts/ops_revalidate_stripe_psps.py \
  --input /tmp/stripe_psp_drift_apply.json \
  --output /tmp/stripe_psp_revalidate_plan.json
```

- Default targeting rules:
  - live rows only
  - rows that are not `valid`, or
  - live rows missing webhook readiness, or
  - live rows not charge-ready

## Step 5: Canary revalidation
- Re-run only the canary PSP through the deployed validation endpoint:

```bash
python3 scripts/ops_revalidate_stripe_psps.py \
  --psp-id psp_stripe_40z8k0n42xi8 \
  --apply \
  --output /tmp/stripe_psp_canary_revalidate.json
```

- Success criteria:
  - response is HTTP 200
  - row returns to `validation_status=valid`
  - `webhook_ready=true` for live Stripe
  - checkout creates live-ready payment state without falling back to test credentials

## Step 6: Batch revalidation
- After the canary looks correct, apply to the remaining targeted Stripe rows:

```bash
python3 scripts/ops_revalidate_stripe_psps.py \
  --input /tmp/stripe_psp_drift_apply.json \
  --apply \
  --delay-ms 500 \
  --output /tmp/stripe_psp_revalidate_apply.json
```

- If you need to constrain the batch further:
  - add repeated `--merchant-id ...`
  - add repeated `--psp-id ...`

## Step 7: Post-apply verification
- Re-audit drift:

```bash
python3 scripts/backfill_canonical_merchant_psps.py \
  --provider stripe \
  --only-drifted \
  --output /tmp/stripe_psp_postcheck.json
```

- Verify the canary merchant checkout:
  - creator entry reaches hosted checkout, not local creator checkout
  - Stripe Elements loads for the live merchant
  - completed payment produces webhook `200`
  - webhook no longer fails with `Invalid signature`

## Monitoring
- Watch `/webhooks/stripe/*` for `400 Invalid signature`.
- Watch Stripe validation failures returned by `/merchant/psp/{psp_id}/test`.
- Watch creator checkout traffic for any remaining local `/checkout` hits.

## Rollback
- There is no recommended rollback to stale webhook secrets or stale environment flags.
- If the apply step exposes a bad merchant credential, fix the merchant's actual Stripe credential set in the portal/admin flow and re-run the same canonical validation path.
- If a batch apply must be halted, stop after the current canary/batch segment and continue with `--merchant-id` or `--psp-id` scoped reruns.
