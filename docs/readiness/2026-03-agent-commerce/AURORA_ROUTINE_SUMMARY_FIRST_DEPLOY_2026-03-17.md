# Aurora Routine Summary-First Deploy

Date: March 17, 2026

## Scope

Production rollout for the Aurora routine AM/PM stability fix:

- decouple `/v1/analysis/skin` from blocking routine product deep-scan
- return routine summary first
- render routine product preview as a separate on-demand layer
- trigger product deep-scan only after explicit user click

## Deployed Versions

Backend:

- repo: `pengxu9-rgb/PIVOTA-Agent`
- production commit: `aae996ed638a41f192ad95846b482ff24fbb5997`
- short commit/header: `aae996ed638a`

Frontend:

- repo: `pengxu9-rgb/pivota-aurora-chatbox`
- production commit: `200022d`
- Vercel deployment: `dpl_BS1GTYzmY2xkFSbNyHrC4RjNSCf6`
- production alias: `https://aurora.pivota.cc`

## Production Config Snapshot

Confirmed during rollout:

- `AURORA_BFF_ANALYSIS_BUDGET_MS=30000`
- `AURORA_ROUTINE_PRODUCT_AUTOSCAN_ENABLED=false`
- `DIAG_PIPELINE_VERSION=legacy`
- `DIAG_SHADOW_MODE=false`
- `DIAG_CANARY_PERCENT=0`

Operational interpretation:

- backend summary-first path is live
- routine product autoscan remains disabled as a safety guard
- routine product analysis now relies on explicit product actions from the UI

## Validation Completed

### Backend Deploy Verification

Validated:

- Railway deployment list shows the new backend commit as successful
- production response headers on Railway expose `x-service-commit: aae996ed638a`
- production runtime logs include the new routine intake snapshot fields and `routine_summary_first_enabled: true`

### Frontend Deploy Verification

Validated:

- Vercel production deployment is `Ready`
- production alias `https://aurora.pivota.cc` points to the new deployment

### Production Live Smoke

Guest live smoke was completed against `https://aurora.pivota.cc/routine`.

Observed behavior:

- `/routine` shell opens successfully
- routine builder modal opens successfully
- AM/PM form works
- `Same as AM` works
- `Save & analyze` no longer stalls indefinitely
- summary returns first
- routine preview card renders:
  - `Products found in your current routine`
- clicking `Analyze this product` triggers on-demand parse/analyze
- resulting thread shows:
  - `Product parse`
  - `Product deep scan`

Observed network outcome:

- `POST /v1/analysis/skin` => `200`
- `POST /v1/product/parse` => `200`
- `POST /v1/product/analyze` => `200`

Observed browser outcome:

- no console errors
- no console warnings

### Production Auth Smoke

Auth live smoke was also completed against production with a newly created disposable test account.

Validated flow:

- create a fresh mailbox
- request Aurora email code
- receive OTP email
- verify OTP successfully
- set password successfully
- perform fresh password login successfully
- load Profile page in a browser and confirm signed-in account state
- run logged-in `/routine` flow end to end

Observed logged-in browser outcome:

- Profile page shows the signed-in account email
- `Sign out` is visible
- logged-in `/routine` opens successfully
- logged-in `Save & analyze` returns summary first
- logged-in routine preview card renders
- logged-in explicit product analyze returns parse + deep scan cards

Observed logged-in network outcome:

- `GET /v1/session/bootstrap` => `200`
- `POST /v1/auth/password/login` => `200`
- `POST /v1/analysis/skin` => `200`
- `POST /v1/product/parse` => `200`
- `POST /v1/product/analyze` => `200`

## Evidence Artifacts

Production smoke artifacts were saved under:

- `output/playwright/aurora-production-live-test-2026-03-17-routine-summary-first/`

Key files:

- `routine_summary_with_preview.yml`
- `routine_product_deep_scan.yml`
- `routine_product_deep_scan.png`
- `routine_product_deep_scan.network.log`
- `routine_product_deep_scan.console.log`
- `routine_trace.trace`

Logged-in auth smoke artifacts were saved under:

- `output/playwright/aurora-production-live-test-2026-03-17-routine-summary-first-auth/`

Key files:

- `profile_login_entry.yml`
- `profile_login_success.yml`
- `auth_routine_summary_with_preview.yml`
- `auth_routine_product_deep_scan.yml`
- `auth_routine_product_deep_scan.png`
- `auth_routine_product_deep_scan.network.log`
- `auth_routine_product_deep_scan.console.log`
- `auth_routine_trace.trace`

## Remaining Gap

No critical validation gap remains for the routine summary-first rollout.

Minor note:

- the disposable auth test account was created only for rollout verification and does not include a completed quick profile
- some resulting cards still show `Current profile: pending`, which is expected for a brand-new account and not a regression in the routine flow

## Rollback Plan

If the routine path regresses again, apply rollback in this order:

1. Keep `AURORA_ROUTINE_PRODUCT_AUTOSCAN_ENABLED=false`
2. Roll back frontend on Vercel to the previous production deployment
3. Roll back backend on Railway to the last known-good deployment before `aae996ed`
4. Re-run guest routine smoke before reopening traffic confidence

## Current Verdict

The routine AM/PM production path is materially healthier after this rollout.

Specifically:

- the previous blocking `Save & analyze` behavior is resolved on the guest path
- summary-first rendering is live
- routine product analysis is now deferred and explicit
- production evidence supports shipping this as the new baseline behavior
