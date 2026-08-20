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

   ⚠️ **`pivota-acp` is deployed to Cloud Run, which contradicts ADR-021.** That deployment was made
   on 2026-08-20 without reference to the ADR. ADR-021 §1 moved ACP checkout in-process into
   pivota-backend and §4 states `PLATFORM_ORDERS_ACP_URL` "stays unset forever". Resolve this before
   cutover: either delete the Cloud Run `acp` service and its ported secrets, or supersede ADR-021
   deliberately. Note the ported secrets include live `STRIPE_SECRET_KEY` and `ADYEN_API_KEY`, which
   ADR-021 said to **rotate** as part of retirement — copying them to a new home is not rotation.

4. **DNS cutover mechanics** — the load balancer EXISTS with six ACTIVE certificates
   (`api`, `gateway`, `mcp`, `commerce.mcp`, `ucp`, `acp`). The **apex does NOT move**:
   `pivota.cc`/`www` are Vercel-served and have no cert-map entry, so pointing them at the LB fails
   TLS rather than 404ing. See CUTOVER.md. Still open: Cloud Armor, and TTLs dropped to 60s at
   T-48h.
5. **Backup / restore drill** — PITR and 14 retained backups are configured but never exercised.
6. **Rollback** — deploys now go out `--no-traffic` behind a candidate tag and only take traffic
   after a health check; rollback is `gcloud run services update-traffic --to-revisions=<prev>=100`.
   Document it in the cutover runbook.
7. **Cloud SQL sizing** — prod is created with 20 GB SSD; IOPS scale with capacity and
   `--storage-auto-increase` never raises the ceiling proactively. Use >= 100 GB for prod.
8. **`--deny-maintenance-period`** is not set around Sep 8-12 or the late-Sep launch window.
   ENTERPRISE (not ENTERPRISE_PLUS) means maintenance is a real restart.
9. **Cloud SQL connection budget — RESOLVED, measured.** `max_connections` raised 200 → 300.
   Measured worst case 230/300 (headroom 70): web 20x6, gateway 20x5, worker 1x10, and
   proof-issuer + acp contribute **0** because they mount no `DATABASE_URL` at all. Re-derive this
   from the live services (not from comments) whenever a service or a pool default changes.
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

**Staging egress IP: `136.66.216.216`** — reserved and stable, but staging-only. **Never give this
to a partner.** The address for Antom/Adyen allowlisting is the PROD one, `8.231.167.230`.
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

## Second database: `pci_kb`

A second live Railway database (PG 16, ~24 MB, 23 tables — ingredient/PCI reference data) is read by
**both** the backend (`PCI_KB_DATABASE_URL`) and the gateway (`PCI_KB_DATABASE_URL` +
`INGREDIENT_REFERENCE_DATABASE_URL`). It now lives as a second database on the same Cloud SQL
instance:

```bash
gcloud builds submit --no-source --config infra/gcp/cloudbuild.dump-railway.yaml --project pivota-staging \
  --substitutions=_BUCKET=pivota-staging-migration,_STAMP=$(date -u +%Y%m%dT%H%MZ),\
_SECRET=railway-pcikb-db-url,_NAME=pcikb,_SSLMODE=prefer
DB=pci_kb infra/gcp/restore_to_cloudsql.sh staging gs://pivota-staging-migration/pcikb-<stamp>.sql.gz
```

Two things this surfaced:

- **`_SSLMODE=prefer` is required for this source.** The `pci_kb` Railway TCP proxy does not
  terminate TLS (`server does not support SSL, but SSL was required`), unlike the main DB's proxy.
  The hop is therefore unencrypted; acceptable only because the content is public reference data
  (ingredients, papers, KB snippets) with no credentials and no buyer PII. Do not reuse `prefer` for
  the main database.
- **The dump verifier was wrong, not the dump.** It counted `CREATE TABLE` across all schemas but
  compared against `table_schema='public'` only, so any database with a second schema false-alarmed
  (`dumped=23 live=19`). It now counts every non-system schema and fails only on a *shortfall* —
  a dump legitimately carries more CREATE TABLE statements than the query counts.

Secrets: `PCI_KB_DATABASE_URL` (asyncpg, `sslmode=require`) and `PCI_KB_DATABASE_URL_NOVERIFY`
(node-pg, `sslmode=no-verify`) — see the sslmode section above for why they differ.

## Scheduling: `infra/gcp/setup_scheduler.sh <env> <backend-tag> <gateway-tag>`

Railway ran the periodic work three different ways; Cloud Run needs three different answers:

| Railway | interval | Cloud Run |
|---|---|---|
| audit/executor drainer ticks, inside the `web` service | 5–10 s | **`worker` service**, min=max=1, ingress internal |
| 8 APScheduler jobs inside `web` (daily … 15/30/60 min) | mixed | same `worker` process, gated by `AUDIT_WORKER_ENABLED` |
| `relgraph-sync-routine` | cron `37 10 * * *` | Cloud Run **Job** + Cloud Scheduler |
| `invitation worker` (`while true; sleep 60`) | 60 s | Cloud Run **Job** + Scheduler `* * * * *` |

**The drainers cannot become Scheduler jobs** — Scheduler's minimum interval is one minute. And they
must not stay on `web`: `services/audit_scheduler.py` has no cross-process lock (APScheduler's
`max_instances=1` is per-process), so every autoscaled instance would drain the same queue. Pinning
them to a single-instance service reproduces Railway's semantics exactly, which is what a cutover
needs; splitting the 8 periodic jobs into individual Scheduler entries is a later refactor.

**Both environments are created inert on purpose**: both Scheduler triggers are PAUSED and the worker runs with
`AUDIT_WORKER_ENABLED=false`, because staging holds a restored production snapshot and still carries
production third-party credentials — draining that queue would execute production-derived rows
against live Stripe/SendGrid/Shopify. **Prod is inert too**: until the DNS flip, GCP prod runs
against a COPY of production data with the REAL production credentials while Railway still serves,
so a second set of drainers would double-send and double-charge. Arming is an explicit, deliberate
cutover step: `WORKERS=true PAUSED=0 infra/gcp/setup_scheduler.sh prod <a> <b>`, run only AFTER
Railway's workers are stopped.

Verified 2026-08-19: the `worker` revision logs
`shared-queue worker ticks DISABLED on this service (service_name='worker' platform_env='staging')`
— the platform shim resolving its own identity — and `gcloud run jobs execute relgraph-sync`
completed in 3m50s with `ok: true`.


## proof-issuer and acp on Cloud Run

Both are deployed with the SAME script as the backend — `deploy_backend.sh` is now parameterised
rather than duplicated, so every safety fix (opt-in workers, candidate-verify before traffic, egress
mode, duplicate-key stripping) applies to all of them:

```bash
# proof-issuer: the SAME backend image, a different ASGI app. It ships from this repo, is stateless
# (no DB, no scheduler), so it needs no image and no gating of its own.
SERVICE=proof-issuer IMAGE_NAME=backend ENV_PREFIX=proofissuer \
  RUN_COMMAND=python RUN_ARGS="-m,uvicorn,proof_issuer_main:app,--host,0.0.0.0,--port,8080" \
  infra/gcp/deploy_backend.sh prod <backend-sha>

# acp: its own repo (pengxu9-rgb/pivota-acp) and its own root Dockerfile.
gcloud builds submit --config <acp cloudbuild> --project pivota-shared --substitutions=COMMIT_SHA=$(git rev-parse HEAD) .
SERVICE=acp IMAGE_NAME=acp ENV_PREFIX=acp \
  RUN_COMMAND=uvicorn RUN_ARGS="--app-dir,./pivota_infra,src.main:app,--host,0.0.0.0,--port,8080" \
  infra/gcp/deploy_backend.sh prod <acp-sha>
```

`PUBLIC=1` is required, not optional: these services are reachable only from the VPC (ingress), and
`PUBLIC=0` would make every deploy REVOKE the `allUsers` invoker binding the model depends on, so the
backend's shared-secret calls would start 403ing. `--proxy-headers` and `--timeout-keep-alive 75`
mirror what the backend image's own CMD sets; without them uvicorn falls back to a 5s keep-alive,
the classic source of intermittent 502s behind Cloud Run's frontend.

Three things worth knowing:

- **The acp image's own CMD ends in `--reload`.** That is a uvicorn development flag: it spawns a
  file-watcher process and reloads on change. It is harmless on Railway but wasteful and fragile on
  Cloud Run, so the deploy overrides the command to drop it. `RUN_ARGS` must be passed as
  `--args=VALUE` (the script does this) because the value legitimately starts with a dash and
  argparse would otherwise read it as the next flag.
- **Both are IAM-open but network-closed** — `--ingress internal-and-cloud-load-balancing` plus
  `allUsers` invoker. That is deliberate: the backend calls them with shared-secret headers
  (`REVIEWS_BUYER_PROOF_ISSUER_INTERNAL_KEY`, `ACP_SERVICE_TOKEN`), not identity tokens, so an
  IAM-gated service would 403 every legitimate call. The VPC is the perimeter and the shared secret
  is the app-level auth, exactly as on Railway. Verified: 200 from a VPC-attached job, 404 from the
  public internet.

acp is deployed inert for the pre-cutover window — `ACP_ENABLE_REAL_CAPTURE=false` and
`DISABLE_WEBHOOK_OUTBOX=true`, and its backend/webhook URLs point at the GCP backend rather than the
live Railway one, so it cannot act on production orders while Railway is still serving.


## Connection budget — how it was DERIVED (and how to actually measure it)

Do not trust a budget written in a comment. This one was wrong twice:

1. **acp was given a `DATABASE_URL` it never had on Railway.** The deploy script mounted the datastore
   DSNs unconditionally, so the Cloud Run copy logged `ACP ready with database persistence` while
   Railway logs `DATABASE_URL not set, using in-memory storage`. That is not just connections - it is
   a silent persistence change in the payment path. acp's `Database(DATABASE_URL)` takes NO pool
   arguments, so it would have been asyncpg's default 10 per instance and it reads neither
   `DB_POOL_MAX_SIZE` nor `DATABASE_POOL_SIZE`. Mounting is now opt-in (`MOUNT_DB`), default on only
   for `web` and `worker`.
2. **The gateway's pool sizing was set with variable names nothing reads.** `PG_POOL_MAX` /
   `PGPOOL_MAX` / `DB_POOL_MAX_SIZE` are not consulted anywhere in PIVOTA-Agent. It opens **four**
   pools and reads `DB_POOL_MAX` (default 5), `PCI_KB_DB_POOL_MAX` (3),
   `INGREDIENT_REFERENCE_DB_POOL_MAX` (3) and `INGREDIENT_SIGNAL_DB_POOL_MAX` (3) — 14 per instance
   by default, not the 10 the old comment assumed.

The lesson both times: **grep the application for the variable name before believing a limit is
applied.** A pool setting the app does not read is indistinguishable from no setting at all, and it
looks correct in `gcloud run services describe`.

**This is a derivation from configuration, not a measurement.** It reads env vars, which tells you
what was *requested*, not what is *held*. The real measurement is against the running database:

```sql
select usename, application_name, count(*) from pg_stat_activity group by 1,2 order by 3 desc;
```

Run that under load before the cutover window. Two things the derivation cannot see:

- **Rolling deploys double a service transiently.** `--no-traffic` then `--to-latest` means old and
  new revisions coexist, and old instances linger for in-flight requests up to `--timeout 300`. At
  web's ceiling that is up to 240 for web alone for ~5 minutes - more than the 70 headroom. Deploy
  services one at a time during the cutover window, not concurrently.
- **The gateway's 100 assumes its three KB DSNs point at `pivota-pg`.** They currently point at
  Railway (see gap #1), so only `DB_POOL_MAX` (20x2=40) lands on Cloud SQL today. 100 is the
  post-`pci_kb`-migration figure - conservative now, correct later.
- The ingredient pools fall back to `DB_POOL_MAX` *before* their own default of 3, so raising
  `DB_POOL_MAX` alone raises three pools, not one.

Re-derive config with:
```bash
gcloud run services describe <svc> --project pivota-prod --region us-west1 --format=json \
  | python3 -c "import sys,json;d=json.load(sys.stdin);c=d['spec']['template']['spec']['containers'][0];print({e['name']:e.get('value') for e in c.get('env',[]) if 'POOL' in e['name'] or e['name']=='DATABASE_URL'})"
```


## Known schema drift in the cutover database

While `acp` briefly had a `DATABASE_URL` (revisions before `acp-00004`), its startup DDL ran against
the shared `pivota` database and added a `metadata` column to `checkout_sessions`, which the
backend's own `db/migrations/025_acp_sessions.sql` does not define.

Assessed and left in place: the column is nullable, **nothing reads it**
(`grep checkout_sessions.*metadata` across the backend returns only SQLAlchemy's unrelated
`metadata.create_all`), and the backend's schema won the race - the live table still has its 2 FKs
and 5 indexes. It also disappears on its own, because the prod database is re-imported from a fresh
Railway dump at cutover.

Verify after the cutover import rather than trusting this note:
```sql
select column_name from information_schema.columns where table_name='checkout_sessions' order by 1;
```


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
