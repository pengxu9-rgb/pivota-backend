# Cutover runbook — Railway → GCP

The GCP production stack is built and validated BEFORE this runbook runs. Everything below is the
flip itself. Target window: Sep 8-12, comfortably before the late-September first real
Minds/Antom transaction.

## The pre-cutover posture, and why it exists

Between build-out and the flip, GCP prod runs against a **copy** of production data holding the
**real** production third-party credentials, while Railway prod is still serving. So the GCP stack is
deliberately inert:

| | pre-cutover | at cutover |
|---|---|---|
| `AUDIT_WORKER_ENABLED` / `REVIEWS_INVITATION_WORKER_ENABLED` | `false` (via `env.prod.overrides.yaml`) | `true` |
| `deploy_backend.sh prod` | `WORKERS` defaults to `false` | `WORKERS=true` explicitly |
| Cloud Scheduler triggers | created PAUSED (`PAUSED` defaults to 1) | `PAUSED=0` |
| gateway → backend URLs | the GCP prod `run.app` URLs | `api.pivota.cc` (which by then IS GCP) |
| DNS | unchanged, pointing at Railway | flipped |

Two live drainers over two copies of one queue means duplicate emails, duplicate settlement
attempts, duplicate captures. That is the single worst thing that can go wrong here, and it is why
workers are opt-in rather than opt-out.

**This is not hypothetical.** On 2026-08-20 `setup_scheduler.sh prod` armed the prod worker and both
Cloud Scheduler triggers — including `reviews-invitation-send` on `* * * * *` — because
`deploy_backend.sh` had been converted to opt-in and this script had not. Caught in under a minute;
no job execution fired and no send/charge occurred. The lesson is in the shape, not the outcome: any
script that derives "should this act on the world" from `$ENV` will eventually be run against prod
before prod is meant to act. Both scripts now default to inert and require an explicit
`WORKERS=true PAUSED=0`.

## T-48h
1. Drop DNS TTLs to 60s at HiChina for: `api`, `mcp`, `commerce.mcp`, `ucp`, `acp`, `gateway`,
   (the apex and `www` are NOT moving - see the apex section). HiChina serves the OLD ttl until it expires — a 60s TTL
   set on the day does nothing.
2. Re-run the acceptance corpus against GCP prod.
3. Confirm the Google console has BOTH redirect URIs registered (see
   `GOOGLE_OAUTH_REDIRECT_URI_registration.md`).
4. Announce the window to Minds and Antom.

## T-0, in order
1. **Stop Railway's workers first.** `relgraph-sync-routine`, `invitation worker`, and
   `AUDIT_WORKER_ENABLED` on `web`. Nothing may drain the queue while the final dump is taken.
2. **Freeze writes** on Railway (maintenance flag) and note the time.
3. **Final dump + import.** Same two commands used to build the stack, with a fresh stamp. The
   dump job self-verifies (completion trailer + table count) and the restore reconciles against the
   `.tables` manifest.
4. **Bring the GCP stack live:**
   ```bash
   WORKERS=true infra/gcp/deploy_backend.sh prod <sha>
   WORKERS=true PAUSED=0 infra/gcp/setup_scheduler.sh prod <backend-sha> <gateway-sha>
   # PAUSED=0 now genuinely RESUMES the Scheduler jobs (it previously only ever paused, so this
   # step silently left both triggers paused). Confirm afterwards:
   #   gcloud scheduler jobs list --location us-west1 --project pivota-prod   # both ENABLED
   infra/gcp/deploy_gateway.sh prod <gateway-sha>
   ```
5. **Point the gateway at the public names**: edit `env.prod.gateway.overrides.yaml` to
   `api.pivota.cc`, re-port, redeploy.
6. **Flip DNS** — all six Railway-backed CNAMEs together. They cross-reference each other
   (`commerce.mcp` is named inside documents served from `mcp`, `ucp` and `acp`), so a partial flip
   strands clients mid-discovery. The apex is a plain A record; HiChina has no ALIAS, so it points
   at the LB anycast IP directly.
7. **Move `GOOGLE_OAUTH_REDIRECT_URI`** to `https://api.pivota.cc/...` (console entry must already
   exist).
8. **Verify**: `/health` on every host, one real checkout end to end, one merchant Search Console
   connect, catalog image URLs resolving, `__catalog_health` counts matching the pre-cutover
   snapshot.

## Rollback

**Re-pausing the Scheduler triggers is part of rollback and was previously missing.** Rolling back
DNS while the GCP triggers keep firing means both stacks write to their own databases and the
divergence grows silently. The full stop is:

```bash
PAUSED=1 infra/gcp/setup_scheduler.sh prod <backend-sha> <gateway-sha>   # re-pause both triggers
# and set AUDIT_WORKER_ENABLED=false / REVIEWS_INVITATION_WORKER_ENABLED=false in
# infra/gcp/env.prod.overrides.yaml, then redeploy - the deploy scripts strip and re-set these keys,
# so the overrides file is the source of truth for them.
```

Railway stays warm for one week. Rollback is: flip DNS back, set `AUDIT_WORKER_ENABLED=true` on
Railway `web`, restart the Railway workers, and set GCP `WORKERS=false`.

**The asymmetry that matters:** DNS rollback is fast, but any write that landed in Cloud SQL after
the flip does NOT exist in the Railway database. Past roughly the first few minutes, rolling back
means losing those writes or hand-reconciling them. Decide to roll back early or not at all.

## Partner-facing constants (do not change at cutover)
- `api.pivota.cc`, `gateway.pivota.cc`, `mcp.pivota.cc`, `commerce.mcp.pivota.cc` — the names
  partners hold. The whole point of the R1-R8 work was that these do not move.
- **Prod egress IP `8.231.167.230`** — reserved, given to Antom/Adyen for allowlisting. Staging's
  `136.66.216.216` is a different address and must never be given to a partner.

## The apex is OUT OF SCOPE — do not flip it

`pivota.cc` and `www.pivota.cc` are served by **Vercel** (`server: Vercel`, A `216.198.79.1`), the
same as `agent.pivota.cc`. They are the marketing/UI site, not the backend or the gateway, and they
are deliberately **absent from the load balancer's host list** — so there is no certificate for them
in the cert map.

Pointing the apex at the LB would therefore not 404; it would **fail TLS**, because SNI would match
no cert-map entry. That is a hard outage on the most visible hostname you own.

Only these six move, and they move together:
`api` · `gateway` · `mcp` · `commerce.mcp` · `ucp` · `acp` — all `.pivota.cc`.

Also not moving: `agent.pivota.cc` (Vercel), `bulk-email-tool.pivota.cc` (a separate Railway project
that appears in no repo — decide keep/kill on its own).


## Rehearsal results — staging, 2026-08-20

The whole sequence was executed against staging. Five things were wrong; all are fixed, and the
timings below are real, not estimates.

| step | measured |
|---|---|
| dump Railway prod → GCS | **7m28s** (3.9 GB → 418 MiB gz) |
| import into a fresh Cloud SQL database | **~9m30s** |
| arm workers + resume both triggers | **~1m** |
| **total data path** | **~17 minutes** |

### What the rehearsal found

1. **`--wipe` is the wrong shape for a cutover, and was replaced by `--new-db`.** `DROP DATABASE`
   fails while any session holds the database — and scaling Cloud Run to zero does **not** kill
   running instances promptly. Three attempts failed with *"database is being accessed by other
   users"*, even after draining services and terminating backends. Discovering that at 2am, with the
   old data already dropped, is the worst possible position.
   `--new-db` imports into `pivota_<stamp>` while the services keep serving the current database.
   Nothing is dropped, nothing must be drained, and the switch is a `DATABASE_URL` secret update plus
   a redeploy — **which is also the rollback, in reverse.**
2. **Importing over a populated database fails** on the first `CREATE SCHEMA` (`schema
   "agent_center" already exists`). At cutover the target already holds the earlier import, so a
   plain re-import would have failed. `--new-db` sidesteps this entirely.
3. **The import verification never ran.** It used `--command psql`, but this image installs `libpq5`
   and no `postgresql-client`, so the job never started and the count came back empty. The script
   *did* fail closed with an actionable message — the behaviour was right, the check was absent. Now
   uses python+asyncpg and targets the database actually imported into. Verified: **365 tables,
   14,124 catalog_products.**
4. **`--new-db` named the database from the dump URI alone**, so re-running against the same dump
   collided with the previous run's database. Now includes a timestamp.
5. **`REGION` was undefined** in `restore_to_cloudsql.sh` — the drain step died on `unbound variable`
   under `set -u`.

### What the rehearsal confirmed works

- **`PAUSED=0` genuinely resumes both triggers.** This was previously a silent no-op, and it is the
  step the cutover depends on. Verified: `Job has been resumed`, both `ENABLED`.
- **The invitation job now runs and EXITS.** Two executions under a live `* * * * *` trigger,
  ~60s apart, each `succeeded=1` with a completion time. Before the fix each would have run to the
  3600s timeout while another started every minute.
- **Connections stay far inside budget with workers armed.** Measured on the live database, not
  derived: **20 of 200** in use (10 idle app + 2 cloudsqlagent + 1 active).
- **Zero send/charge log lines** for the whole armed window.

### Sequence changes this produces

- The final data sync uses **`--new-db`**, and the switch is a secret update plus a redeploy.
- **Deploy services one at a time**, not concurrently: a rolling deploy transiently doubles a
  service's connections.
- Staging's email credentials are deliberately invalid sentinels (it holds a restored **production**
  snapshot with real buyer addresses; a live SendGrid key plus one enabled worker sends real mail).
  Stripe in staging is a genuine `sk_test_` key taken from Railway's own `web-staging` service.
