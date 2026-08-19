# Pivota on Google Cloud — infra bootstrap

Decided 2026-08-19: migrate Railway → Google Cloud Run before the late-September
first real Minds/Antom transaction. Cutover window Sep 8–12. See
`~/dev/GCP_MIGRATION_URL_AUDIT_2026-08-19.md` for the hostname/DNS audit.

| thing | value |
|---|---|
| Google account | `peng@woopay.tech` (credit lives here) |
| billing account | `01C8A5-869AD4-2872C8` ($100k GFS Scale Y1 + $12k support, to 2027-08-19) |
| org | `peng-org` (867785023602) |
| projects | `pivota-prod`, `pivota-staging`, `pivota-shared` (images/Cloud Build) |
| region | `us-west1` |

## Bootstrap an environment

```bash
gcloud auth login --update-adc            # once, as peng@woopay.tech
infra/gcp/bootstrap_env.sh staging        # ~15 min (Cloud SQL + Memorystore)
infra/gcp/bootstrap_env.sh prod
```

Idempotent; re-run freely. Creates: private-services-access range + peering on the
default VPC, Cloud SQL PG17 (private IP), Memorystore Redis 7.2 (AUTH on),
Secret Manager `pivota-db-password` / `DATABASE_URL` / `REDIS_URL`, service accounts
`sa-backend` / `sa-gateway` / `sa-worker` with least-privilege roles, and read access
to the shared Artifact Registry `us-west1-docker.pkg.dev/pivota-shared/pivota`.

Sizing: staging `db-custom-1-3840` zonal + 1 GB basic Redis; prod `db-custom-2-7680`
REGIONAL (HA) + 2 GB standard_ha Redis, deletion protection on.

## Runtime contract for Cloud Run services

- Direct VPC egress on `default` (Cloud SQL and Redis are private-IP only).
- `PIVOTA_ENV=production|staging` **must** be set — the platform shim
  (`config/platform.py`, PR #1771) fails closed and `require_platform_env()` kills boot without it.
- `DATABASE_URL` / `REDIS_URL` mounted from Secret Manager.
- min-instances ≥ 1 for anything holding a DB pool (cold start + pool repair = the wedge).

## Runbook: backend `web` to Cloud Run (what actually ran on 2026-08-19)

```bash
export GCLOUD=~/google-cloud-sdk/bin/gcloud PATH=~/google-cloud-sdk/bin:$PATH

# 1. build + push image (Cloud Build in pivota-shared; ~2 min)
gcloud builds submit --config infra/gcp/cloudbuild.backend.yaml --project pivota-shared \
  --substitutions=COMMIT_SHA=$(git rev-parse HEAD) .

# 2. port Railway env -> env.<env>.yaml + Secret Manager env-* (run from a dir linked to the Railway project)
#    write infra/gcp/env.<env>.overrides.yaml first (see env.overrides.example.yaml)
python3 infra/gcp/port_railway_env.py --railway-service web --railway-env production --env staging --apply

# 3. seed the database from prod (empty DB boot is broken: concurrent CREATE TABLE IF NOT EXISTS race)
gcloud builds submit --no-source --config infra/gcp/cloudbuild.dump-railway.yaml --project pivota-staging \
  --substitutions=_BUCKET=pivota-staging-migration,_STAMP=$(date -u +%Y%m%dT%H%MZ)
infra/gcp/restore_to_cloudsql.sh staging gs://pivota-staging-migration/prod-<stamp>.sql.gz --wipe

# 4. deploy
infra/gcp/deploy_backend.sh staging $(git rev-parse HEAD)
```

### Staging secret policy
`env.staging.overrides.yaml` (git-ignored, chmod 600) rotates every **internal** shared secret to a
fresh random value (`JWT_SECRET_KEY`, `SHOP_GATEWAY_AGENT_API_KEY`, `*_INTERNAL_KEY`, `CHECKOUT_*`,
`ADMIN_API_KEY`, `METRICS_BEARER_TOKEN`, …) so a token minted on the public staging URL is never
valid in prod. **The staging gateway / proof-issuer / acp must be given the same staging values**
(read them from Secret Manager `env-<NAME>` in pivota-staging — never copy from Railway prod).
Data-bound secrets (`CONNECTOR_CREDENTIALS_KEY`, `REVIEWS_*_SIGNING_SECRET`) are kept so a restored
prod dump stays readable. Third-party LIVE credentials (Stripe, SendGrid, Shopify, Adyen, AWS, …) are
still prod values in staging until test-mode keys are put in the overrides file — side-effect flags
(`REVIEWS_INVITATION_WORKER_ENABLED`, `AGENT_ACP_ALLOW_LIVE_CAPTURE`, `ALLOW_SHAKEOUT_ON_PROD`) are off.

### One-time project plumbing that bootstrap_env.sh does NOT do (done by hand 2026-08-19)
- `pivota-shared`: compute default SA → `roles/cloudbuild.builds.builder` + AR writer (Cloud Build runs as it)
- `pivota-staging`: compute default SA → builder + `secretmanager.secretAccessor` + `storage.objectAdmin`
  (for the dump job); bucket `gs://pivota-staging-migration`; secret `railway-prod-db-url`
