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
