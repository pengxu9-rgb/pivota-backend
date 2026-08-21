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
valid in prod. **The staging gateway / proof-issuer must be given the same staging values**
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
   | ~~`ucp-worker`~~ | Pivota Infra | `pivota-acp` (`main`) | **DELETED 2026-08-20** | retired — ADR-021 |
   | ~~`ucp-web-production`~~ | Pivota Infra | `pivota-acp` (`main`) | **DELETED 2026-08-20** | retired — ADR-021 |
   | ~~`ucp-platform-receiver`~~ | Pivota Infra | `pivota-acp` (`main`) | **DELETED 2026-08-20** | retired — ADR-021 |
   | `pivota-acp` | Pivota Infra | `pivota-acp` (`main`) | SUCCESS | **retire — ADR-021** (see below) |
   | `catalog-intelligence` (main) | catalog-intelligence | own repo | SUCCESS, `/health` 200 | **not a cutover blocker** — see below |
   | ~~`ingredient-harvester`~~ | catalog-intelligence | own repo | **DELETED 2026-08-20** | killed — see below |
   | ~~`Worker service`~~ | catalog-intelligence | own repo | **DELETED 2026-08-20** | killed — see below |
   | `bulk-email-tool` | bulk-email-tool | `bulk-email-tool` | SUCCESS | **KEEP** (2026-08-20) — see below |

   **ADR-021 (Accepted, 2026-08-01) already retired the four Infra services** and called for rotating
   the Stripe/Adyen keys they held. This is a closed decision, not an open question. Supporting
   evidence gathered 2026-08-20: the UCP/ACP/MCP doors are custom domains on **PIVOTA-Agent** (none
   of the four has a custom domain); `ucp-worker` polls every 30s and logs `drained=0 total=0`
   because nothing enqueues — **not** because it lacks a database. ADR-021 is explicit that outbound
   UCP webhook delivery deliberately has **no home** after retirement.

   🚨 **Two claims previously repeated here are FALSE — see the Correction appended to ADR-021.**
   Measured read-only against prod `Postgres-xMr6` on 2026-08-21: `ucp_checkout_sessions` holds
   **56** rows and `ucp_order_webhook_deliveries` holds **22**, not zero. The 22 deliveries went to
   `ucp-platform-receiver` (15), `ucp-web-production` (4) and three dev tunnels — so
   `ucp-platform-receiver` **did** serve real requests, and something answered at the
   `ucp-web-production` host in January 2026. Do not repeat "never booted" or "0 real requests".

   The retirement is still correct, on a narrower and checkable basis: **all 22 deliveries are
   `status='sent'`, `attempt_count=1`, `last_error` null — the queue was fully drained before the
   drainer was removed**, and no row is pending, failed or retryable.

   Everything else about deploy history — "99 deploys, 0 SUCCESS", "polls every 30s" — is now
   **permanently unfalsifiable**: Railway retains nothing for a deleted service. Given that the two
   claims above did not survive contact with the database, treat the rest as testimony rather than
   fact, and measure before relying on any of it.

   **Ordering (recorded for accuracy): the stated order was NOT followed.** The rule was to delete
   `ucp-web` with or before `ucp-worker`, because `ucp-web` is the only writer to the queue and
   removing the drainer first leaves a writer with no drainer. The actual deletion order on
   2026-08-20 was `ucp-worker`, `ucp-platform-receiver`, `ucp-web-production` (a first-hand operator
   observation — Railway retains no record of a deleted service, so this is not re-checkable).

   It stranded nothing — but **not** for either reason first recorded here. `ucp-worker` *was*
   running its 2026-07-10 image (a FAILED redeploy on Railway does not remove the deployment already
   serving), and `ucp-web` is not a service that "never booted". What actually makes the
   out-of-order deletion safe is measurable and was measured: **the queue was empty of work.** All
   22 delivery rows are `status='sent'` with `attempt_count=1`, the newest from 2026-01-16, and
   nothing has enqueued since. A drainer removed from a fully-drained queue strands nothing,
   whatever order it goes in.

   Keep the rule anyway. It is correct for any future pair where the writer is live and the queue is
   not provably empty — which is the normal case, and was assumed rather than checked here.

   ✅ **RESOLVED — the drainerless-queue hazard recorded here does not exist.** Settled by reading
   the `pivota-acp` repo (the previous note could not be closed from this repo alone):

   - `DISABLE_WEBHOOK_OUTBOX` is read in **two** places, and only one of them executes in
     production: `pivota_infra/src/main.py:105`, the live startup hook. (The other,
     `pivota_infra_main/src/acp/outbox_queue.py:15`, is in an unreferenced tree.) Both concern
     **`webhook_outbox`**; neither references `ucp_order_webhook_deliveries`. Flipping the flag
     cannot fill the UCP queue.
   - The UCP table's only *enqueuer* is `pivota_infra_main/routes/ucp_business_proxy_routes.py`,
     mounted **only** by `ucp_web.py` → `Dockerfile.ucp-web` → the deleted `ucp-web-production`. Its
     only drainer was `scripts/ucp_order_webhook_worker.py` → `Dockerfile.ucp-worker` → the deleted
     `ucp-worker`. **Enqueuer and drainer were deleted together**, so the queue is inert.
   - `pivota-acp` builds the root `Dockerfile` and runs `--app-dir ./pivota_infra src.main:app`. The
     `pivota_infra_main/` tree **is** in the image (`PYTHONPATH` includes it) but is never mounted,
     and it never references the UCP table.

   🚨 **The analogous hazard on `webhook_outbox` is real, and worse than a stalled queue.** Measured
   on prod 2026-08-21: **3 `order_create` rows `pending` since 2026-07-10**, `attempts = 0/3`, never
   attempted. `pivota-acp` runs `DISABLE_WEBHOOK_OUTBOX=true`, so its dispatcher never starts.

   ✅ **Both defects below are FIXED** in `pivota-acp` PR #34 (`e8f18a8`). Kept as the record of
   what was wrong, and because the operator precondition still stands: **set `OPENAI_WEBHOOK_URL`
   and `MERCHANT_WEBHOOK_SECRET` before flipping the flag**, or the dispatcher correctly refuses to
   run and the in-process queue fills toward its cap.

   The live dispatcher is `pivota_infra/src/acp/outbox_queue.py`. Two independent defects sat behind
   the flag:

   1. It called `r.get(...)` on a `databases` `Record`. Under the pinned `databases>=0.8.0,<0.9.0`
      a `Record` is a `Sequence`, not a `Mapping` — but **`.get` is not simply absent**, which is
      how this was first recorded here and it was wrong. `Record.__getattr__` returns
      `self._mapping.get(name)`, so on an *instance* `r.get` resolves to a column-lookup miss —
      `None` — and **calling** it raises `TypeError: 'NoneType' object is not callable`.
      (`hasattr(Record, "get")` is `False` on the **class** and `True` on an **instance**; checking
      the class and generalising is the mistake.) The first pending row raised; the `except` handler
      called `r.get` again and raised too; `dispatch_loop` was `try/finally` with no `except`, so
      **the asyncio task died permanently** until the next deploy.

      **Grep production logs for `TypeError`, not `AttributeError`.**
   2. Even repaired, `pivota_infra/src/acp/outbox.py:23` returns early when `OPENAI_WEBHOOK_URL` or
      `MERCHANT_WEBHOOK_SECRET` is unset — **both are unset on `pivota-acp`** — after which the
      dispatcher's success branch marks the row `sent`. Fixing only (1) converts a visible stall
      into **rows marked delivered that were never sent**.

   ⚠️ **Do not describe those 3 rows as test residue.** They have no `merchant_id` and no
   `webhook_url`, but the only enqueuer (`pivota_infra/src/acp/router.py:458`, the order-creation
   path) never writes those columns — so a real undelivered order event is indistinguishable from
   test traffic by that signature. Treat them as possibly real.

   The `INTERVAL ':backoff minutes'` defect is real but lives in the **unreferenced**
   `pivota_infra_main/` copy (fixed in `pivota-acp` PR #33). Its runtime error is **not** the
   message a naive test suggests: SQLAlchemy *does* bind the placeholder — into the literal — so
   asyncpg receives `INTERVAL '$2 minutes'` and Postgres fails at Parse with:

   ```
   ERROR:  could not determine data type of parameter $2
   ```

   Grep logs for that string, not for `invalid input syntax for type interval`, which only appears
   if the raw `:backoff` text reaches the server — which on this path it never does.

   **`pivota-acp` was briefly deployed to Cloud Run on 2026-08-20 and has been REMOVED** the same
   day, following ADR-021. Deleted: the Cloud Run `acp` service and its four `acp-env-*` secrets.
   Nothing on GCP referenced it, and `acp.pivota.cc` is unaffected — it is a custom domain on
   **PIVOTA-Agent**, which the load balancer routes to `pivota-bes-gateway`, never to that service.

   Do not redeploy it. A future migration pass that enumerates Railway services will try to bring it
   across again; ADR-021 §1 (ACP checkout runs in-process in pivota-backend —
   `acp_checkout_session_service`, migration 191) and §4 (`PLATFORM_ORDERS_ACP_URL` "stays unset
   forever") are the reasons not to.

   **Still outstanding from ADR-021: key rotation — scoped, on the evidence available, to
   `pivota-acp`.** The **production-environment** variables of the three `ucp-*` services were
   inventoried (names + masked prefixes) immediately before deletion, because **deleting a service
   destroys the record of what it held**. The dump lives outside the repo at
   `~/dev/.pivota-gcp-env/retired_service_credentials_<date>.txt`.

   ⚠️ **Two gaps in that evidence — do not read it as "settled".** `Pivota Infra` has a **staging**
   environment as well, deleting a service removes it from *both*, and per-environment variable sets
   differ materially here (`pivota-acp` carries 30 vars in production and 9 in staging; a real
   `STRIPE_SECRET_KEY` sits on `web-staging`/staging today). The staging instances of the three
   services were deleted **uninventoried**. The dump is also point-in-time — taken 2026-08-21, twenty
   days after ADR-021 called for the rotation — so a credential removed in between is invisible to
   it. Rotation is a question about what was *ever* exposed, which this dump cannot answer.

   - **No PSP credential appears on the production instances of the three.** The Stripe/Adyen
     concern in ADR-021 is about `pivota-acp`, which is still running. Rotate the Adyen key
     regardless: it is a real key on a live service, its environment cannot be told from its prefix,
     and the evidence above is not strong enough to close the item on its own.
   - `UCP_ORDER_WEBHOOK_SIGNING_PRIVATE_JWK` on `ucp-worker` was the literal placeholder
     `adsfas...#$%5`, confirming ADR-021's finding that outbound signatures could never have
     verified.
   - The two `UCP_OFFER_TOKEN_SECRET` values **disagreed** between `ucp-worker` and
     `ucp-web-production`, so that secret was split-brain as well as unused.

   On `pivota-acp` the Stripe key is `sk_test_`, so it is not a live-money credential. The Adyen key
   is a real API key whose environment cannot be told from its prefix. Deleting the GCP copies is not
   rotation — the Railway original remains until that service is deleted there.

   **Do a masked env dump before deleting any further service.** Names and value prefixes are enough
   to drive rotation; the values themselves must not be captured.

   **`SERPER_API_KEY` (exposed 2026-08-20) is now free to rotate.** It lived only on the two deleted
   harvesters — `Pivota-catalog-intelligence` does not carry it — so rotating it at serper.dev
   affects no running service.

   **Two loose ends from the harvester deletion, neither of them a break:**

   - `Postgres-4hoG` in the catalog-intelligence project is **orphaned but not empty** — ~8k
     harvested rows across six tables (counts in the catalog-intelligence assessment below).
     **Resolved 2026-08-21: archived, instance kept.** `pg_dump -Fc` of all six tables lives at
     `gs://pivota-prod-archives/postgres-4hog/postgres-4hog-archive-20260821.dump` (1.35 MiB), and
     the Railway instance stays up — coherent with `Pivota-catalog-intelligence` remaining on
     Railway. The archive is a SECOND copy, not the only one; a byte-level restore test has not been
     run. Use `pg_dump` from `libpq` (18.4), not `postgresql@15` — a v15 client refuses a v17 server.
   - `Pivota-catalog-intelligence` still carries `INGREDIENT_HARVESTER_BASE_URL`, which now points at
     nothing. Behaviour is unchanged — the target had been FAILED since 2026-05-29, so the URL was
     already dead — but it should be removed on the next touch of that service.

4. **DNS cutover mechanics** — the load balancer EXISTS with six ACTIVE certificates
   (`api`, `gateway`, `mcp`, `commerce.mcp`, `ucp`, `acp`). The **apex does NOT move**:
   `pivota.cc`/`www` are Vercel-served and have no cert-map entry, so pointing them at the LB fails
   TLS rather than 404ing. See CUTOVER.md. Still open: Cloud Armor, and TTLs dropped to 60s at
   T-48h.
5. **Backup / restore drill** — PITR and 14 retained backups are configured but never exercised.
6. **Rollback** — deploys now go out `--no-traffic` behind a candidate tag and only take traffic
   after a health check; rollback is `gcloud run services update-traffic --to-revisions=<prev>=100`.
   Document it in the cutover runbook.
7. **Cloud SQL sizing — DONE 2026-08-21.** Raised 20 GB → **100 GB** (online, non-disruptive;
   disk cannot be reduced afterwards). `storageAutoResize` remains on. IOPS scale with capacity,
   and `--storage-auto-increase` only reacts to pressure rather than provisioning for it.
8. **`--deny-maintenance-period` — DONE 2026-08-21.** Set **2026-08-22 → 2026-10-05**, covering the
   cutover and the late-Sep launch. ENTERPRISE (not ENTERPRISE_PLUS) means maintenance is a real
   restart, so this is not cosmetic.
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


## proof-issuer on Cloud Run

Both are deployed with the SAME script as the backend — `deploy_backend.sh` is now parameterised
rather than duplicated, so every safety fix (opt-in workers, candidate-verify before traffic, egress
mode, duplicate-key stripping) applies to all of them:

```bash
# proof-issuer: the SAME backend image, a different ASGI app. It ships from this repo, is stateless
# (no DB, no scheduler), so it needs no image and no gating of its own.
SERVICE=proof-issuer IMAGE_NAME=backend ENV_PREFIX=proofissuer \
  RUN_COMMAND=python RUN_ARGS="-m,uvicorn,proof_issuer_main:app,--host,0.0.0.0,--port,8080" \
  infra/gcp/deploy_backend.sh prod <backend-sha>

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

**`acp` is NOT deployed to GCP** — ADR-021 retires it; see the un-migrated services table above.


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


### `catalog-intelligence` — assessed 2026-08-20, not a cutover blocker

No ADR constrains this project. The assessment below is why it is safe to leave on Railway through
the Sep 8-12 cutover, and why two of its three services should be deleted rather than migrated.

**The main service is not on the buyer or payment path.** `CATALOG_INTELLIGENCE_BASE_URL` is consumed
only by `services/catalog_intelligence_client.py`, whose sole caller is `bd_cold_start_service` —
reached from `agent_center_bd_routes`, `merchant_audit_routes` and `agent_readiness_score`, i.e. the
business-development and merchant-audit surfaces. Nothing in the Minds/Antom lane touches it.

**And the client cannot break a request.** Its docstring is explicit: *"Never raises. All errors
logged at WARNING/ERROR."* It returns `None` when unconfigured, on any HTTP error, and on an empty
response; callers fall back. Leaving the URL pointed at Railway after cutover degrades BD/audit
enrichment gracefully — it does not fail anything.

**`ingredient-harvester` and `Worker service` are dead with no consumers.** The harvester is a
FastAPI + RQ service that batch-harvests `raw_ingredient_text` for beauty SKUs; the worker
(`python -m app.worker`) drains its Redis queue. Both have been FAILED since 2026-05-29 and Railway
no longer retains their logs. Two facts settle their disposition:

- **They write to their own database** (`postgres-4hog`), **not** to `pci_kb` (`switchback`) — the KB
  the gateway and backend actually read. The dead harvester is therefore NOT upstream of the
  ingredient data served today.
- **That database is NOT empty — the earlier "0 rows" claim in this file was wrong and is
  retracted.** Re-measured directly on 2026-08-20, all six tables: `candidate_rows` **4,616**,
  `task_rows` **3,135**, `candidate_row_audit_findings` **234**, `candidate_row_corrections` **179**,
  `harvest_tasks` **53**, `imports` **41**. Do not repeat the earlier mistake of asserting emptiness
  without counting every table — the original claim named only four of the six.

So the lane has **no consumer**, but it does have accumulated output. That still settles the
*migration* question the same way — delete the two failed services rather than move them — but it
does **not** settle the *database*. Deleting `Postgres-4hoG` destroys ~8k harvested rows.

⚠️ **Rotate `SERPER_API_KEY`.** It was set in plaintext on both failed services (now deleted) and was
exposed in a terminal session on 2026-08-20. Rotate at serper.dev — verified free to rotate across
all 40 Railway project x environment x service combinations, both GCP projects' Secret Manager, every
Cloud Run service, and the repo: nothing reads it.

🚨 **`SERPAPI_API_KEY` is a DIFFERENT vendor and must not be touched.** `env-SERPAPI_API_KEY` exists
in both GCP projects and is live on Cloud Run `web` and `worker`. SerpAPI is not Serper.dev; rotating
the wrong one breaks running services.

### `bulk-email-tool` — KEEP, and easy to lose

It lives in its own Railway project (`bulk-email-tool`, repo `pengxu9-rgb/bulk-email-tool`) and
serves a live custom domain `bulk-email-tool.pivota.cc` that is deliberately **not** in the load
balancer's six-host list and has no cert-map entry. Nothing in the cutover touches it, and nothing
in the cutover would notice if it disappeared.

- It is **not** part of the Sep 8-12 DNS flip. Its CNAME stays pointed at Railway, which is correct.
- **Do not decommission the Railway account** on the assumption that everything moved. After the
  2026-08-20 deletions, **two** services are deliberately staying on Railway *through and after the
  cutover*: this one and `Pivota-catalog-intelligence` (the `catalog-intelligence` row in the table
  above). `pivota-acp` also still runs there, but its disposition is **retire — ADR-021**, so it is
  unfinished business rather than a decision. Before the flip, everything else in Pivota Infra is of
  course still serving from Railway — this bullet is about what remains *afterwards*. GCP `web` and
  `worker` both still carry `CATALOG_INTELLIGENCE_BASE_URL` pointed at Railway — deliberately, since
  that client never raises and degrades to `None`.
- **Enumerate before assuming anything is disposable** — `railway list`, then `railway service list`
  per project and per environment. Pivota Infra alone still hosts `web`, `reviews-proof-issuer`,
  `invitation worker`, `relgraph-sync-routine`, `web-staging` and the Postgres/Redis instances, and
  `PIVOTA-Agent` lives in its own project. Counting from this file rather than from Railway is how
  services get deleted by surprise.
- Migrating it later means its own image, its own Cloud Run service, and a **seventh** certificate
  plus host rule on the LB. Keeping it off the cutover critical path is deliberate.

## Which services a repo redeploys — check this before merging

Railway auto-deploys **every service whose source is a repo you push to**, not just the one you were
thinking about. Merging a change to `pivota-acp` on 2026-08-20 redeployed four services and left two
of them failed.

| repo (branch) | project | services it redeploys |
|---|---|---|
| `pivota-acp` (`main`) | Pivota Infra | `pivota-acp` only (the three `ucp-*` services were deleted 2026-08-20) |
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
