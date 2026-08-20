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

## Production stack (created 2026-08-20)

| resource | value |
|---|---|
| Cloud SQL | `pivota-prod:us-west1:pivota-pg` — POSTGRES_17, `db-custom-2-7680`, **REGIONAL** (HA), PITR on, deletion protection on, private IP `10.25.0.2` |
| Memorystore | `pivota-redis` — `STANDARD_HA`, 2 GB, 1 replica, AUTH on, `10.25.7.196:6379` |
| **Egress IP** | **`8.231.167.230`** — RESERVED (not ephemeral), via Cloud NAT `pivota-nat` on router `pivota-router` |
| service accounts | `sa-backend`, `sa-gateway`, `sa-worker` @ `pivota-prod.iam.gserviceaccount.com` |
| secrets | `pivota-db-password`, `DATABASE_URL`, `REDIS_URL` |

**`8.231.167.230` is the address to give Antom and Adyen for IP allowlisting.** It is a reserved
static address, so it survives NAT/router/instance changes. Staging's equivalent is
`136.66.216.216` — do not hand that one to a partner.

Note the prod path exercised a line staging never did: Memorystore's HA tier is spelled `standard`
(the tier WITH a replica, reported back as `STANDARD_HA`). `standard_ha` is the API enum, and gcloud
normalizes it to `standard-ha`, which is not a valid `--tier` choice — so the first prod run failed
there while every staging run had passed.

## Known gaps before the Sep 8-12 prod cutover

Tracked from the review of this PR; none is covered by these scripts yet.

1. **Second database `pci_kb`** — `bootstrap_env.sh` creates only `pivota`. `PCI_KB_DATABASE_URL` /
   `INGREDIENT_REFERENCE_DATABASE_URL` still point at Railway (`switchback.proxy.rlwy.net`, PG16,
   24 MB, 23 tables) and are read by BOTH the backend and the gateway.
2. **Scheduled work is deployed but inert** — `setup_scheduler.sh` creates the single-instance
   `worker` plus the `relgraph-sync` and `reviews-invitation-send` Cloud Run Jobs and their two
   Cloud Scheduler triggers. Everything is created PAUSED / workers-off; arming is the explicit
   cutover step `WORKERS=true PAUSED=0 ...`, run only after Railway's workers stop.
3. **Remaining un-migrated services.** The gateway and proof-issuer are deployed (see the sections
   above). See also **`docs/adr/ADR-021`**, which already decided the disposition of four of these.

   | service | Railway project | source repo (branch) | prod state | disposition |
   |---|---|---|---|---|
   | `ucp-worker` | Pivota Infra | `pivota-acp` (`main`) | **running** on a Jul-10 image; Aug-20 redeploy failed | **retire — ADR-021** |
   | `ucp-web-production` | Pivota Infra | `pivota-acp` (`main`) | **never once booted** (99 deploys, 0 SUCCESS) | **retire — ADR-021** |
   | `ucp-platform-receiver` | Pivota Infra | `pivota-acp` (`main`) | SUCCESS; 0 real requests | **retire — ADR-021** |
   | `pivota-acp` | Pivota Infra | `pivota-acp` (`main`) | SUCCESS | **retire — ADR-021** (see the warning below) |
   | `catalog-intelligence` | catalog-intelligence | own repo | service SUCCESS; 2 of its 3 sub-services FAILED | migrate — own Postgres + Redis |
   | `bulk-email-tool` | bulk-email-tool | `bulk-email-tool` | SUCCESS | **KEEP** (2026-08-20) — see below |

   **ADR-021 (Accepted, 2026-08-01) already retired the four Infra services** and called for rotating
   the Stripe/Adyen keys they held. This is a closed decision, not an open question. Supporting
   evidence gathered 2026-08-20: the UCP/ACP/MCP doors are custom domains on **PIVOTA-Agent** (none
   of the four has a custom domain); the only writer to `ucp_order_webhook_deliveries` lives in
   `ucp-web`, which has never booted; `ucp-worker` polls successfully every 30s and logs
   `drained=0 total=0` because nothing enqueues — **not** because it lacks a database. ADR-021 is
   explicit that outbound UCP webhook delivery deliberately has **no home** after retirement.

   **Ordering:** delete `ucp-web` with or before `ucp-worker` — `ucp-web` is the only thing that can
   write the queue, so removing the worker first would leave a writer with no drainer.

   **`pivota-acp` was briefly deployed to Cloud Run on 2026-08-20 and has been REMOVED** (same day,
   following ADR-021). Deleted: the Cloud Run `acp` service and its four `acp-env-*` secrets
   (`STRIPE_SECRET_KEY`, `ADYEN_API_KEY`, `ACP_SERVICE_TOKEN`, `PIVOTA_AGENT_API_KEY`). Nothing on
   GCP referenced it. `acp.pivota.cc` is unaffected — it is a custom domain on **PIVOTA-Agent** and
   the load balancer routes it to `pivota-bes-gateway`, never to that service.

   Do not redeploy it. ADR-021 §1 moved ACP checkout in-process into pivota-backend
   (`acp_checkout_session_service`, migration 191) and §4 states `PLATFORM_ORDERS_ACP_URL` "stays
   unset forever". A future migration script that enumerates Railway services will try to bring it
   across again — the ADR is the reason not to.

   **Still outstanding from ADR-021: key rotation.** The Stripe key on the Railway `pivota-acp`
   service is `sk_test_`, so that one is not a live-money credential. The Adyen key is a real API
   key whose environment cannot be told from its prefix. ADR-021 asked for the keys these retired
   services held to be rotated; deleting the GCP copies is not rotation, and the Railway originals
   are still in place until the services are deleted there.

### `bulk-email-tool` — KEEP, and easy to lose

It lives in its own Railway project (`bulk-email-tool`, repo `pengxu9-rgb/bulk-email-tool`) and
serves a live custom domain `bulk-email-tool.pivota.cc` that is deliberately **not** in the load
balancer's six-host list and has no cert-map entry. Nothing in the cutover touches it, and nothing
in the cutover would notice if it disappeared.

- It is **not** part of the Sep 8-12 DNS flip. Its CNAME stays pointed at Railway, which is correct.
- **Do not decommission the Railway account** on the assumption that everything moved. This service,
  plus `catalog-intelligence`, still run there.
- Migrating it later means its own image, its own Cloud Run service, and a **seventh** certificate
  plus host rule on the LB. Keeping it off the cutover critical path is deliberate.

## Which services a repo redeploys — check this before merging

Railway auto-deploys **every service whose source is a repo you push to**, not just the one you were
thinking about. Merging a change to `pivota-acp` on 2026-08-20 redeployed four services and left two
of them failed.

| repo | services it redeploys |
|---|---|
| repo (branch) | project | services it redeploys |
|---|---|---|
| `pivota-acp` (`main`) | Pivota Infra | `pivota-acp`, `ucp-platform-receiver`, `ucp-web-production`, `ucp-worker` |
| `pivota-backend` (`main`) | Pivota Infra | `web`, `reviews-proof-issuer`, `invitation worker` |
| `pivota-backend` (`staging/reviews-center`) | staging/reviews | `pivota-backend`, `Cron/Job service`, `proof issuer` |
| `pivota-backend` (`feature/ap2-safe`) | pivota-ap2-staging | `web` |
| `PIVOTA-Agent` (`main`) | Pivota Agent | `PIVOTA-Agent` |

**The branch matters.** `pivota-backend` has seven repo-connected service instances across four
projects, not three — a push to `main` redeploys three of them, but the table above is the full
picture. `web-staging` and `relgraph-sync-routine` have NO repo source: they are deployed by CLI
`railway up`, so a push does not touch them.

Regenerate this table rather than trusting it — services get added:

```bash
# deploymentTriggers is authoritative - serviceInstances.source.repo is NOT (a service can carry a
# repo source with no trigger, and a push then does nothing). Run this per project, for all of them.
railway api 'query { project(id: "<id>") { name
  services { edges { node { name deploymentTriggers { edges { node { repository branch } } } } } } } }'
```

Two incidents came from not doing this:

- A root `Dockerfile` added to `pivota-backend` switched the builder from Railpack to Docker for
  **every service that builds from that repo**; prod `web` built, then failed its health check.
- A merge to `pivota-acp` redeployed `ucp-worker`, which had been running a July image and could not
  start on current code. That service turned out to be vestigial, so the blast radius was luck
  rather than diligence.

**Before merging into any repo that Railway builds from, list its services and know what a redeploy
will do to each.**
