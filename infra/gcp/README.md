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

### Why the Dockerfile is not at the repo root

Railway auto-detects a root `Dockerfile` and switches the builder from Railpack to it for **every
service that deploys from this repo** (`web`, `web-staging`, `pivota-acp`, `ucp-*`,
`reviews-proof-issuer`, `invitation worker`, `relgraph-sync-routine`). On 2026-08-19 that failed the
prod `web` healthcheck. Keep it at `infra/gcp/Dockerfile` and build with `-f`; `.dockerignore` stays
at the repo root because that is the build context root.

## Known gaps before the Sep 8-12 prod cutover

Tracked from the review of this PR; none is covered by these scripts yet.

1. **Second database `pci_kb`** — `bootstrap_env.sh` creates only `pivota`. `PCI_KB_DATABASE_URL` /
   `INGREDIENT_REFERENCE_DATABASE_URL` still point at Railway (`switchback.proxy.rlwy.net`, PG16,
   24 MB, 23 tables) and are read by BOTH the backend and the gateway.
2. **Cloud Scheduler** — nothing replaces the Railway crons (`relgraph-sync-routine`, the audit /
   executor / invitation drainers). `deploy_backend.sh` sets `AUDIT_WORKER_ENABLED=false` on
   staging, so today NOTHING drains those queues on GCP.
3. **Gateway / proof-issuer / acp deploys** — `sa-gateway` and `sa-worker` exist but have no deploy
   script. Staging services must be given the STAGING internal secrets, never Railway prod values.
4. **LB / DNS / TLS** — no external load balancer, no `gateway.pivota.cc` (must exist before
   partners copy URLs), no custom-domain mapping, no Cloud Armor. DNS is at Alibaba/HiChina and the
   apex is a plain A record (no ALIAS), so the apex flips to the LB anycast IP.
5. **Backup / restore drill** — PITR and 14 retained backups are configured but never exercised.
6. **Rollback** — deploys now go out `--no-traffic` behind a candidate tag and only take traffic
   after a health check; rollback is `gcloud run services update-traffic --to-revisions=<prev>=100`.
   Document it in the cutover runbook.
7. **Cloud SQL sizing** — prod is created with 20 GB SSD; IOPS scale with capacity and
   `--storage-auto-increase` never raises the ceiling proactively. Use >= 100 GB for prod.
8. **`--deny-maintenance-period`** is not set around Sep 8-12 or the late-Sep launch window.
   ENTERPRISE (not ENTERPRISE_PLUS) means maintenance is a real restart.
9. **Egress IP** — `--vpc-egress private-ranges-only` means no stable outbound IP. Resolve before
   any Antom/Adyen IP-allowlisting conversation (needs a NAT + `all-traffic`).
10. **Dependency pinning** — `requirements.txt` pins only a few packages, so rebuilding the same git
    SHA during cutover week can produce a different image. Pin or add a lockfile.
11. **Dump bucket** — prod data lands in `gs://pivota-staging-migration` (a staging-project bucket)
    with no lifecycle rule, retention policy, or CMEK.
12. **Cloud Build trigger** — images are built by hand with `COMMIT_SHA` passed in; provenance
    during cutover is manual.

## Gateway (PIVOTA-Agent) on Cloud Run

```bash
# from a PIVOTA-Agent checkout
gcloud builds submit --config ../pivota-backend-gcp/infra/gcp/cloudbuild.gateway.yaml \
  --project pivota-shared --substitutions=COMMIT_SHA=$(git rev-parse HEAD) .
python3 ../pivota-backend-gcp/infra/gcp/port_railway_env.py \
  --railway-service PIVOTA-Agent --railway-env production --env staging --prefix gateway --apply
../pivota-backend-gcp/infra/gcp/deploy_gateway.sh staging <sha>
```

The gateway reuses **its own repo's root Dockerfile**. That is safe there because PIVOTA-Agent's
Railway services pin `builder=RAILPACK` explicitly, so the Dockerfile is inert on Railway - unlike
pivota-backend, where adding a root Dockerfile hijacked the builder for 8 services.

**`/healthz` is NOT reachable on Cloud Run.** Google's frontend intercepts it and returns its own
404 before the request reaches the container; `/health` (the same handler) returns 200. Railway's
configured healthcheckPath is `/healthz`, so any Cloud Run healthcheck, uptime check, or LB backend
must use `/health`.

**`--prefix` is mandatory when two services share a project.** It names the outputs
`env.<env>.<prefix>.yaml` / `secrets.<env>.<prefix>.list`, prefixes Secret Manager entries
`<prefix>-env-<NAME>`, **and** selects `env.<env>.<prefix>.overrides.yaml`. Without the prefix on the
overrides file a port silently picks up another service's overrides and drops its own.

**Staging cross-service URLs must be overridden.** The gateway's Railway env points every backend
URL at production (`PIVOTA_API_BASE`, `PIVOTA_BACKEND_BASE_URL`, `PROMOTIONS_BACKEND_BASE_URL`,
`DISCOVERY_PRODUCTS_SEARCH_BASE_URL`, `AURORA_BFF_RECO_CATALOG_SEARCH_BASE_URLS`,
`NEXT_PUBLIC_API_URL`, `AGENT_AUTH_INTROSPECT_URL`, `PIVOTA_GATEWAY_URL`). A staging gateway left
pointing at them would read production data and mint production-scoped tokens.

### Open: staging service-to-service networking
Both staging services are IAM-gated (no `allUsers`). Cloud Run→Cloud Run over `*.run.app` takes the
public path, so with `--vpc-egress private-ranges-only` the gateway's calls to the backend will get
401 until one of these is chosen:
- **IAM + identity tokens** (correct, needs the gateway to mint an ID token per outbound call), or
- **`--ingress internal` + `--vpc-egress all-traffic` + Cloud NAT** (no code change; NAT also gives
  the stable egress IP that Antom/Adyen allowlisting will need).

## Service-to-service networking (decided 2026-08-19)

`infra/gcp/setup_egress_nat.sh <env>` creates a Cloud Router + Cloud NAT with a **reserved** static
egress IP, then services are switched to:

| service | ingress | egress |
|---|---|---|
| `web` (backend) | `internal` | `all-traffic` |
| `gateway` | `all` (IAM-gated) | `all-traffic` |

Cloud Run → Cloud Run over `*.run.app` takes the **public** path, so with `private-ranges-only`
egress the caller arrives anonymous and an IAM-gated callee answers 401. Routing all egress through
the VPC makes those calls arrive as internal, so `--ingress internal` becomes the perimeter and no
identity-token code is needed. The backend is now unreachable from the internet (404 at the ingress,
authenticated or not) while the gateway reaches it normally.

**Staging egress IP: `136.66.216.216`** — reserved, stable. This is the address Antom/Adyen should
IP-allowlist. Run the script for `prod` to reserve the production one before partner onboarding.

### `sslmode=require` does NOT mean the same thing in Python and Node

Same DSN, different behaviour, and it only shows up against Cloud SQL:

- **asyncpg (backend)** — `sslmode=require` encrypts but does **not** verify the server CA. Works
  against Cloud SQL's per-instance CA out of the box.
- **node-pg (gateway)** — maps `sslmode=require` to `rejectUnauthorized: true`, so it tries to verify
  Cloud SQL's per-instance certificate and fails with `unable to verify the first certificate`. Every
  DB-backed discovery provider reported `query_error` while the connection itself looked configured.

Fix: the gateway gets `DATABASE_URL_NOVERIFY` (`?sslmode=no-verify`), which node-pg maps to
`rejectUnauthorized: false` — encrypted, unverified, i.e. *exactly* what asyncpg was already doing,
on a private VPC address. This was never visible on Railway, where the DB was reached through a
public proxy hostname with a publicly-trusted certificate.

Verified end to end after the fix: `discovery_ready: true`, and
`GET /agent/v1/products/search?q=vitamin+c+serum` returns 200 with real catalog rows —
gateway → backend → Cloud SQL, entirely over the VPC.
