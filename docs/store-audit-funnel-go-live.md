# Store Audit funnel — go-live runbook (2026-08-25)

The marketing funnel (pivota.cc/ai-readiness → anonymous UCP teaser →
registration) is code-complete across three repos: backend #1875, gateway
PIVOTA-Agent#2106, marketing pivota-marketing#11 (Vercel auto-deploys).
Everything below is ops, in order. Each step is idempotent.

Already done (2026-08-25):
- Secret `STORE_AUDIT_UCP_PROBE_INTERNAL_KEY` exists in pivota-prod with an
  enabled version 1 (64-char random base64; never displayed anywhere).
- Gateway image build for `a2d08eba494a65b6d4ed623f5a1eb1ff990e9d32`
  (post-#2106 main) submitted to Cloud Build in pivota-shared.
- `web` serving `1667064e` (idle-claim 204 fix) — the commerce crons are
  safe to unpause from this commit onward.

## 1. Deploy `web` at current main (public intake endpoints)

```bash
gh workflow run deploy-prod.yml -f sha=74544dd36d45907ff5a7b0b76824d755d2f8e0c2 -f promote=true
```

(Main tip as of writing; any main commit containing `3f272205` / #1875 works.)
Wait for the workflow to finish and confirm `/health` reports the sha.

## 2. UCP lane identities (three least-privilege service accounts)

```bash
bash infra/gcp/setup_store_audit_ucp_identity.sh prod
```

(It refuses to run unless the dedicated secret already has a non-empty
version — it does.)

## 3. Receipt flag + secret mount + public intake flag on `web`

`deploy-prod.yml` uses CONFIG=preserve, which deliberately cannot add env or
secrets, so this is a one-time explicit change. Traffic on `web` follows
latest, so this takes effect immediately:

```bash
gcloud run services update web --project pivota-prod --region us-west1 \
  --update-env-vars STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED=true,STORE_AUDIT_PUBLIC_INTAKE_ENABLED=true \
  --update-secrets STORE_AUDIT_UCP_PROBE_INTERNAL_KEY=STORE_AUDIT_UCP_PROBE_INTERNAL_KEY:latest
```

Verify: `POST https://api.pivota.cc/public/store-audit/intake` with
`{"store_url":"<any real domain>"}` now answers 202 `{"state":"pending"}`
(it answered 404 while the flag was off).

## 4. UCP jobs + paused schedulers (isolated — touches nothing else)

```bash
bash infra/gcp/setup_store_audit_ucp_jobs.sh prod <backend-sha-from-step-1> a2d08eba494a65b6d4ed623f5a1eb1ff990e9d32
```

The script gates on step 3 (active `web` revision must carry the flag and the
mounted secret) and on both images existing. It creates
`store-audit-ucp-reprobe-enqueue` + `store-audit-ucp-probe` and their two
Scheduler triggers, always paused.

## 5. Arm the UCP lane

```bash
gcloud scheduler jobs resume store-audit-ucp-probe-cron --location us-west1 --project pivota-prod
gcloud scheduler jobs resume store-audit-ucp-reprobe-enqueue-cron --location us-west1 --project pivota-prod
```

The probe cron (every 5 minutes) is what drains funnel intakes; the daily
03:30 UTC reprobe cron keeps existing routes fresh.

## 6. Arm the commerce lane (independent of the funnel)

```bash
gcloud run jobs update store-audit-commerce-probe --project pivota-prod --region us-west1 --update-env-vars STORE_AUDIT_COMMERCE_REPROBE_ARMED=true
gcloud run jobs update store-audit-commerce-reprobe-enqueue --project pivota-prod --region us-west1 --update-env-vars STORE_AUDIT_COMMERCE_REPROBE_ARMED=true
gcloud scheduler jobs resume store-audit-commerce-probe-cron --location us-west1 --project pivota-prod
gcloud scheduler jobs resume store-audit-commerce-reprobe-enqueue-cron --location us-west1 --project pivota-prod
```

Safe from `1667064e` onward (idle claims answer 204, not 500).

## 7. End-to-end verification

1. Submit a real store URL on https://pivota.cc/ai-readiness — expect the
   "Live check queued" card (GA event `audit_teaser_shown`,
   `teaser_state=queued`).
2. Within ~5 minutes the probe job executes; re-submit or poll
   `GET /public/store-audit/teaser?store_url=…` — expect
   `ready`/`agent_ready` or `inconclusive` (WAF).
3. Check `execution_routes` gained a domain row and, on a positive,
   `evidence_items` has one `acceptance_signal`.

## Rollback levers

- Funnel dark: unset `STORE_AUDIT_PUBLIC_INTAKE_ENABLED` (the marketing form
  falls back to the plain signup redirect on the resulting 404s).
- Probes stopped: pause the two `store-audit-ucp-*-cron` triggers.
- Receipt door shut: unset `STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED` (workers
  get 404s; claims simply expire).
