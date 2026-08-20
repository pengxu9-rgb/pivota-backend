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
| Cloud Scheduler triggers | not created | created enabled |
| gateway → backend URLs | the GCP prod `run.app` URLs | `api.pivota.cc` (which by then IS GCP) |
| DNS | unchanged, pointing at Railway | flipped |

Two live drainers over two copies of one queue means duplicate emails, duplicate settlement
attempts, duplicate captures. That is the single worst thing that can go wrong here, and it is why
workers are opt-in rather than opt-out.

## T-48h
1. Drop DNS TTLs to 60s at HiChina for: `api`, `mcp`, `commerce.mcp`, `ucp`, `acp`, `gateway`,
   and the apex + `www` if they are moving. HiChina serves the OLD ttl until it expires — a 60s TTL
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
   WORKERS=true infra/gcp/setup_scheduler.sh prod <backend-sha> <gateway-sha>
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
