# Post-cutover security & failure-mode audit — 2026-08-22

Three independent adversarial audits (infrastructure, security, record-accuracy) run against the
live stack hours after the Railway→GCP cutover. Every load-bearing claim below was re-verified by
hand before landing here.

**Read the framing first.** Most of what follows is **pre-existing application weakness that the
migration exposed, not migration regression**. The cutover did not add `Depends(lambda: None)`; it
put a real audit on a live surface for the first time. That distinction matters for triage, not for
whether these are real. They are.

Two items were causing active harm and were **fixed during the audit** — see "Already fixed".

---

## SEV-1 — before the Minds integration

### 1.1 `POST /merchant/onboarding/setup-psp` is unauthenticated and writes PSP credentials

`routes/employee_store_psp_fixes.py:174`

```python
current_user: Optional[dict] = Depends(lambda: None)  # No auth required for merchant onboarding
if current_user and current_user.get("role") not in ["employee","admin","merchant"]:
```

`Depends(lambda: None)` resolves to `None` on every request, so the role check can never fire. The
handler writes `merchant_psps` rows — `api_key`, `secret_key`, `account_id`, `provider` — for a
**caller-chosen `merchant_id`**. Mounted unconditionally (`main.py:1083`); not env-gated, so it
never closes.

Verified live: `GET` returns `405` (routed, method-mismatch) and a malformed `POST` reaches body
validation with no credential.

Same file family, same shape — `routes/quick_index_setup.py` (`main.py:1165`), both live:

- `POST /setup/create-all-indexes` — `current_user: dict = None` has **no `Depends`**, so FastAPI
  binds it as a request *body* field the caller supplies. `require_admin` is imported and never
  called. `SETUP_KEY` is unset on the live revision. Runs 12 `CREATE INDEX` statements.
- `POST /setup/create-usage-logs-table` — no parameters, no guard of any kind.

These are **hardcoded gaps, not fail-open config**, which is why no middleware-level review would
surface them: authentication here is entirely per-route `Depends(...)`.

**Fix:** real auth dependencies. Until then these are the first thing an attacker reaches.

### 1.2 The MCP OAuth resource indicator points at a Railway host

`docs/agent-checkout/MCP_OAUTH_AS_SELF_HOSTED.md:65` publishes
`MCP_OAUTH_RESOURCE=https://pivota-agent-production.up.railway.app/mcp`, contradicting the
byte-exact allowlist at line 49 (`commerce.mcp.pivota.cc/mcp`). An external MCP client echoes this
back per RFC 8707, so **every `/authorize` fails `invalid_target`**.

This is the document the Minds integration reads next week.

### 1.3 Gateway `/v1/*` and `/metrics` are anonymous on four public hosts

`AURORA_SURFACE_AUTH_MODE` and `AURORA_SURFACE_DENIED_HOSTS` are not mounted, so
`auroraSurfaceAuth.js` resolves to `observe` and returns `allow: true` unconditionally. The nominal
gate accepts any non-empty `X-Aurora-UID` — a device label, not a credential.

Anonymous on `gateway` / `commerce.mcp` / `ucp` / `acp` (`mcp` is the only host denied): 9 LLM
routes, object-storage writes (`/v1/photos/presign|upload`), **unthrottled credential endpoints**
(`/v1/auth/password/login`, `/v1/auth/start`, `/v1/auth/verify`), and `/metrics` (24 KB of
Prometheus; `METRICS_BEARER_TOKEN` has zero references in the gateway — the Python `/metrics` *is*
armed and returns 403).

⚠️ **Do not flip this unilaterally.** Arming it (`AURORA_SURFACE_AUTH_MODE=enforce` plus the
already-mounted `AURORA_SURFACE_INTERNAL_KEY`) will 401 any consumer that has not shipped the
header. Cross-repo, coordinated.

---

## SEV-2 — standing exposure, no attacker required

### 2.1 Zero monitoring

`alertPolicies: 0` · `notificationChannels: 0` · `uptimeCheckConfigs: 0` · `logging metrics: 0`.

Discovered **only by user report**: LB 5xx spikes; the `reviews-invitation-send` job failing (it
runs 1,440×/day); Cloud SQL saturation; Memorystore eviction; a revision failing to start.

The data exists — LB request logging is on at `sampleRate 1.0`, and there were 0 ERROR-severity
Cloud Run logs in the audit window. Nobody is alerting on it.

**Certificates are *not* the risk the runbook implies:** all six are Certificate-Manager *managed*,
`ACTIVE`, with every `_acme-challenge` CNAME resolving — they auto-renew. The real risk is silent:
drop one of those CNAMEs while editing the zone and renewal fails with **nothing alerting** until
TLS breaks.

Minimum viable: LB 5xx rate, Cloud Run job `failedCount > 0`, Cloud SQL connections > 80%, one
uptime check per public host — plus at least one notification channel, since there are zero.

### 2.2 No Cloud Armor; rate limiting covers 1 slice of the API

`gcloud compute security-policies list` → 0. Both backend services have an empty `SECURITY_POLICY`.

`middleware/rate_limiter.py:518` returns early unless the path starts with `/agent/`. The public
OpenAPI publishes **1,011 paths / 1,079 operations**, of which **257** match
`admin|internal|setup|debug`. Everything outside `/agent/*` — including §1.1 — has **no rate
limiting at any layer**.

One thing the migration *improved* and nobody has taken: that file's XFF reasoning was written for
Railway's edge. Google's external ALB appends the true client IP as the second-to-last element, so
a trustworthy per-IP bucket is newly available.

### 2.3 Railway is a second live copy of every production secret

Still serving on a valid certificate (`api.pivota.cc` via its direct IP → `200`, `environment:
production`), with background sweeps **on** (`EXTERNAL_CONVERSION_POLLER_ENABLED`,
`PAYMENT_RECONCILE_SWEEP_ENABLED`, `ENABLE_IDENTITY_RECONCILE_SWEEP`, `PDP_SCOPE_BACKFILL_ENABLED`).

Railway `web` holds **255** variables including every PSP, JWT, OAuth, cloud and mail credential —
plus `DATABASE_PUBLIC_URL`, an **internet-facing Postgres proxy onto full production data**, and
`PLATFORM_ORDERS_ACP_TOKEN`, which was deleted from GCP Secret Manager and is still live there.
`railway variables --kv` prints values in plaintext to any authenticated CLI user, with no read
audit.

**Correction to a premise we had been carrying:** Railway is not an older build. `141f975c` (GCP at
audit time) is an *ancestor* of `e59e0b74` (Railway) — the rollback target was one commit *ahead*
of production. Divergent, not known-good.

**On decommission, rotate everything:** Stripe secret + all three webhook secrets; Adyen API key,
webhook HMAC and basic-auth password; `JWT_SECRET_KEY` (invalidates sessions — sequence it);
`CHECKOUT_TOKEN_SECRET`; `ORDER_TRACK_TOKEN_SECRET`; `CONNECTOR_CREDENTIALS_KEY`;
`MCP_OAUTH_AS_PRIVATE_KEY_PEM` + `MCP_OAUTH_AS_REQUEST_SECRET`; AWS/S3 pairs; SendGrid/SMTP2GO;
Shopify client + headless secrets; `WIX_API_KEY`; `OPENAI_API_KEY`;
`GOOGLE_APPLICATION_CREDENTIALS_JSON`; `PLATFORM_ORDERS_ACP_TOKEN`; `ACP_SERVICE_TOKEN`; every
`*_INTERNAL_KEY` / `*_ADMIN_KEY`.

Recommended window: **at most one business cycle (~7 days) post-cutover.** It should not survive
the Minds integration.

### 2.4 Production data with buyer PII sits in a staging-project bucket

`gs://pivota-staging-migration/prod-20260822T0834Z.sql.gz` — 419 MB, taken inside the cutover
freeze — in the **staging** project, with
`roles/storage.legacyObjectReader → projectViewer:pivota-staging`.

**Any principal with Viewer on `pivota-staging` can read full production data.** Today that is one
human plus staging service accounts, so exposure is contained — but the grant is *structural* and
survives the next person added to staging. `public_access_prevention: inherited` (not enforced), no
lifecycle rule, dumps persist indefinitely.

Full inventory of prod-data copies: Cloud SQL live db · Cloud SQL retired `pivota` db · Railway
Postgres (+ its public proxy) · this bucket (5 dumps) · `gs://pivota-prod-migration` ·
`gs://pivota-prod-archives`.

**Fix:** move prod dumps to the prod project, `public_access_prevention: enforced`, a 7–14 day
lifecycle rule, consider CMEK.

### 2.5 Project-wide secret access, and secret reads are not audited — PARTLY FIXED 2026-08-22

**Fixed:** Data Access audit logs are now ON for Secret Manager (`DATA_READ` + `DATA_WRITE`), and
`sa-gateway` — the internet-facing service with the anonymous `/v1` surface in §1.3 — has been
narrowed from **project-wide to its 39 specific secrets**.

It could previously read all 105, including `env-ADYEN_API_KEY` and all three Adyen webhook
secrets, Stripe, `env-AWS_SECRET_ACCESS_KEY`, `env-JWT_SECRET_KEY`,
`env-MCP_OAUTH_AS_PRIVATE_KEY_PEM`, `env-SHOPIFY_CLIENT_SECRET`, `env-SENDGRID_API_KEY` and both
database DSNs — 29 credential-bearing secrets it has no use for. It now cannot.

Sequenced so a mistake could not break production: all 39 per-secret grants applied and verified
**39/39 present** *before* the project-level grant was removed, then a **fresh gateway revision was
forced** — secrets resolve at instance start, so a new revision is the only real test that the
grant set is complete. Result: `gateway-00017-zoh` Ready, all five gateway-served hosts 200, zero
errors, and `sa-gateway` confirmed to have no access to `env-ADYEN_API_KEY`.

The audit config was applied by editing the IAM policy JSON directly and asserting the 25 existing
bindings were byte-identical before writing — `set-iam-policy` replaces the whole document, so a
careless edit silently drops bindings. Verified working by performing a read and finding it in the
log within seconds.

**Still open, deliberately:** `sa-backend` and `sa-worker` keep project-level access. They each
need 57 of the 97 mounted secrets, so per-secret grants buy far less isolation there while adding a
real foot-gun — every newly added secret would need a matching grant or the next deploy fails at
instance start. Also open: the default compute SA still holds project-level `secretAccessor`, and
`roles/owner` is still a single human with no break-glass second principal.

*(One audit finding here was already stale: both scheduler jobs were reported as running under the
default compute SA. They run as `sa-worker` — verified before acting on it.)*

#### Original finding

`roles/secretmanager.secretAccessor` is granted at **project** level to `sa-backend`, `sa-gateway`,
`sa-worker` and the default compute SA. **No per-secret IAM on any of the 105 secrets.**
`auditConfigs` is absent → **Data Access audit logs are OFF**, so there is no record of which
principal read which secret, before or after an incident.

`sa-gateway` — the internet-facing service with the anonymous `/v1` surface in §1.3 — can read all
105, including Stripe, Adyen, and `railway-prod-db-url`. With `vpc-access-egress: all-traffic`, an
SSRF or RCE there also reaches Cloud SQL's private IP and Memorystore.

Both scheduler-driven Cloud Run jobs run as the **default compute SA**, which additionally holds
`storage.objectAdmin` and `cloudbuild.builds.builder`.

`roles/owner → user:peng@woopay.tech` is the **only** human principal across all three projects.
**No break-glass second owner.**

### 2.6 A client has a Railway hostname hardcoded, and its writes are being lost — DIAGNOSED AND FIXED 2026-08-25

**The "client" was us.** No third party was involved, and no customer data was lost. The investigation
found something more useful and slightly different from the original finding.

`PIVOTA-Agent` on Railway — the old gateway, still running as part of the rollback stack — carries
**7 variables still pointing at `web-production-fedb.up.railway.app`**
(`AGENT_AUTH_INTROSPECT_URL`, `PIVOTA_API_BASE`, `PIVOTA_BACKEND_BASE_URL`,
`DISCOVERY_PRODUCTS_SEARCH_BASE_URL`, `AURORA_BFF_RECO_CATALOG_SEARCH_BASE_URLS`,
`NEXT_PUBLIC_API_URL`, `PROMOTIONS_BACKEND_BASE_URL`). It was calling Railway `web`, which
authenticated the calls and served them — which is what the photo-upload and `/agent/shop/v1/invoke`
traffic in the original finding actually was.

**The real problem it exposed: the rollback stack was never inert.** The Railway gateway was running
`pdp_identity_auto_resolve` on a 30-minute in-process timer with `dry_run=false`, writing **200 rows
per tick** into the Railway database — roughly **25,000 rows since the cutover**, still going three
days later.

This is the *same defect class* as the GCP zombie revisions fixed on 2026-08-22, and it is worth
naming as a class: **an in-process timer keeps running wherever the container runs.** Retiring a
platform by moving DNS does not stop the code on it. Stopping a *scheduler* (Cloud Scheduler, a
Railway cron) is visible and obvious; stopping a `setInterval` inside a long-lived process is
neither, and nothing alerts on it.

**Fixed:** `PDP_IDENTITY_AUTO_RESOLVE_ENABLED=false` on the Railway gateway, then **restarted** —
a variable change alone does not affect a running container, which is the same trap that made the
worker flags look applied during the cutover. Verified by waiting past the next scheduled tick
(~00:54) and confirming **zero** ticks since the 00:24 restart, with the service still healthy and
all five of its custom-domain hosts still 200 on GCP. The GCP gateway continues to tick against
live data, as it should.

**Assessed, and less alarming than first stated:** the divergence is confined to derived identity
and review-queue rows, which both platforms recompute independently. Catalog integrity is still
byte-identical across the two databases (14,124 / 14,128, drift 0), so the rollback remains viable
for the data that matters.

**Still open:** the 7 Railway-pointing variables are left as they are *on purpose* — the Railway
gateway is the rollback, and a rollback stack should point at the rollback backend. They must be
revisited as part of §2.3 decommissioning, not before.

#### Original finding

Railway edge logs show `POST /photos/presign`, `/photos/confirm` and `GET /photos/download-url`
arriving on the legacy `web-production-fedb` Railway host from an AWS us-west-1 address — **not**
through `api.pivota.cc`. DNS cannot fix a hardcoded hostname. Every such write lands in the Railway
database and is silently discarded.

> The full hostname is written in that split form on purpose.
> `tests/test_legacy_backend_url_guard.py` fails the build if the literal
> `web-production-fedb` + `.up.railway.app` appears anywhere outside its own
> allowlist — the guard exists so that URL can never quietly come back into
> config or code. Please do not "fix" this line by restoring the literal; it
> will turn main red. Reconstruct it from the guard's `LEGACY_BACKEND_URLS`
> tuple if you need it verbatim.

**Find the client and repoint it before Railway is decommissioned**, or the failure mode changes
from "silently lost" to "hard error".

---

## SEV-3 — correctness and hygiene

| # | Finding | Evidence |
|---|---|---|
| 3.1 | **Gateway's primary LLM provider is dead in production.** `/healthz/gemini` → `{"ok":false,"key_count":0,"reasons":["missing_keys"]}`. Neither `GEMINI_API_KEY` nor `GOOGLE_API_KEY` is mounted, yet the revision sets `AURORA_LLM_SINGLE_PROVIDER=gemini` and 25 Gemini tuning vars. The openai fallback covers intent and layer-2 only — **embeddings, skin vision and the Aurora single-provider lane have none.** A credential was dropped in the port. | measured |
| 3.2 | `PCI_KB_DATABASE_URL` missing on GCP `web` (present on `worker` and `gateway`). `services/pci_kb_scope_review.py:105` raises rather than degrading, so the employee scope-review path 500s. | name-level diff |
| 3.3 | Full API surface published anonymously — `/docs`, `/redoc`, `/openapi.json` → 200, handing an attacker the exact path list for §1.1. | measured |
| 3.4 | `requireInternalKey` is one empty secret from open: `NODE_ENV`/`APP_ENV` are both unset, so `isProd` is false and the `CONFIG_MISSING` refusal is **unreachable**. Armed today only because the key is mounted non-empty. Comparison is `===`, not constant-time. | code |
| 3.5 | `/api/links/resolve` enforcement off — `OUTBOUND_LINKS_RESOLVE_REQUIRE_KEY` unset, so unauthenticated callers mint signed redirect tokens with caller-chosen `ctx`. The file's own docstring names attribution stuffing and open-redirect laundering. | code + probe |
| 3.6 | No security response headers on either host — no HSTS, `X-Content-Type-Options`, `X-Frame-Options`, CSP or `Referrer-Policy`. Without HSTS the first request of every session is downgradeable. | measured |
| 3.7 | Default-VPC firewall allows `0.0.0.0/0` on tcp:22 and tcp:3389. Zero instances today, so latent — but Cloud Run egresses through this same network and the first VM anyone creates is SSH-open to the world. | measured |
| 3.8 | Old secret versions still enabled: `DATABASE_URL` (3), `DATABASE_URL_NOVERIFY` (2), `railway-pcikb-db-url` (2), `env-GOOGLE_OAUTH_CLIENT_SECRET` (2). Disable **only after** confirming no revision is pinned to them. | measured |
| 3.9 | 8 orphan secrets mounted by nothing, including `railway-prod-db-url` and `railway-pcikb-db-url` — Railway DSNs stored in GCP prod. | measured |
| 3.10 | ~44 operational runbooks still instruct operators to act on **Railway** as production — including the partner-settlement runbook whose own invariant warns that a missed flag risks **double-paying**, the Stripe webhook incident runbook, the rate-limit kill switch, and three capture canaries that arm real money. An operator following any of them today acts on the rollback. | grep + spot-check |

---

## Already fixed (2026-08-22, during the audit)

- **Zombie revisions writing to the retired database.** Cloud Run resolves `secret:latest` at
  instance start and `min-instances >= 1` keeps stale tagged revisions alive forever, pinned to
  their boot-time secret version. `gateway-00010-mar` wrote ~4,800 rows into the old `pivota`
  database — 400 of them *after* the secret was repointed. Stale tags removed; verified no
  `db=pivota` activity afterward. The rows were derived identity-resolve state, recomputed by the
  live revision against the snapshot — not lost data. Permanent fix in #1814.
- **Merges not reaching production.** #1812 shipped to the rollback and left prod on the previous
  commit. Prod brought to `main`; drift alarm in #1814.
- **Railway `relgraph-sync` cron still armed**, diverging the rollback database — disarmed.

## Verified good — checked, and they hold

Platform shims fail **closed** to production (`PIVOTA_ENV` set on all four services; the gateway
has zero live `RAILWAY_*` reads and dies rather than guessing) — **no prod-detection guard
regressed at cutover**. Raw `*.run.app` URLs return Google-Frontend `404`; the LB is the only
external door. Cloud SQL is regional-HA, PITR on, **no public IPv4**, deletion protection on.
`/ui/chat`, ACP checkout signing, and the Python `/metrics` are all **armed** (verified at the
deciding line, not by config). Catalog integrity byte-identical across both platforms — 14,124
rows, drift 0. No user-managed service-account keys. All 97 mounted secret references resolve.
`acp-env-*` and `PLATFORM_ORDERS_ACP_TOKEN` confirmed absent from GCP.

## Scope — what was not checked

`services/` (328 entries), `jobs/`, `adapters/`, `psp/`, `orchestrator/`, `proof_issuer_main.py`
(a second FastAPI app with its own mounting), and the gateway's `mcp-server/`, `connectors/` and
`safety-kernel/` trees. Secret **values** were never read, so "mounted" does not prove "non-empty"
— §3.4 turns on exactly that distinction. The `orders` row count was not compared across platforms
(Cloud SQL is private-IP-only and no read path was available); `/__trust_health` covers
`catalog_products` and `catalog_row_trust` only.
