# Isolated staging preparation entrypoint

This opt-in package is based on backend `ffae02260a8520eaa7e9cbf044a90b62d376d6ab`. Normal Docker startup is unchanged. Never point the normal `main:app` entrypoint at a rehearsal database: even `SKIP_HEAVY_STARTUP_INIT=true` leaves light startup DDL enabled.

## Deployment gate

**Do not deploy this current target.** The pinned existing Cloud SQL instance is `pivota-staging:us-west1:pivota-pg`, private host `10.122.0.3`. Read-only topology review found other databases grant PUBLIC CONNECT/TEMP. A new role cannot revoke inherited PUBLIC privileges without changing existing ACLs. This package rejects effective CONNECT to every other connectable database, including inherited PUBLIC grants. Existing shared topology therefore fails admission; it does not revoke any existing ACL. Root must approve a dedicated instance, independently verify its metadata/ACLs, then review a source change pinning that new instance identity/private address. The CLI exposes no environment/argument host or database override. Cloud instance labels are configuration checks, not cryptographic attestation; actual server IP/database/session actor/owner are checked over SQL.

No agent is authorized to provision, deploy, change shared ACLs or seed provider state. PostgreSQL17 remote acceptance is required; local role/schema tests use PostgreSQL15. ACL-name expansion rejects unknown privileges including17's MAINTAIN without invoking unsupported15 privilege strings.

## Exact commands and flags

Use `python -m reap_rehearsal migrate` once with the migrator DSN, then `python -m reap_rehearsal probe`, `python -m reap_rehearsal web` or `python -m reap_rehearsal tick` with the runtime DSN. Cloud Run command is `python`, args are `-m,reap_rehearsal,web`. Web starts one process, no production startup/lifespan or scheduler. `tick` invokes the normal worker once only on the empty prepared cohort; owned-fixture tick is refused pending a later phase review. Do not schedule this command.

Required literal values:

```
PIVOTA_ENV=staging
REAP_REHEARSAL_PREPARATION_ONLY=1
REAP_AGENTIC_ENABLED=0
REAP_AGENTIC_CREATE_ENABLED=0
REAP_AGENTIC_CART_LINK_ENABLED=0
AUDIT_SCHEDULER_ENABLED=false
REVIEWS_SCHEDULER_ENABLED=false
SCHEDULER_ALLOWLIST=,
SKIP_HEAVY_STARTUP_INIT=true
AGENT_AUTH_KEY_TABLE=api_keys
AGENT_AUTH_ENABLE_LEGACY_API_KEY_FALLBACK=false
GOOGLE_CLOUD_PROJECT=pivota-staging
REAP_REHEARSAL_SQL_INSTANCE=pivota-staging:us-west1:pivota-pg
```

`DATABASE_URL` must explicitly encode the fixed actor, host, port and database; query/options/fragments and ambient libpq redirects are rejected. Proposed new database name `reap_rehearsal_20261002_01a0f`; roles `reap_rehearsal_01a0f_migrator` and `reap_rehearsal_01a0f_runtime`. Bind the matching DSN secret to a numeric Secret Manager version. Guard expects `REAP_REHEARSAL_DATABASE_SECRET_REFERENCE=projects/pivota-staging/secrets/reap-rehearsal-20261002-01a0f-{migrator|runtime}-dsn/versions/<positive integer>`. This reference label does not fetch the secret: root must verify the actual deployment uses the same numeric binding, never `latest`.

Provider credentials/base/simulation and remote buyer JWKS are refused. A process socket/DNS guard only permits the fixed database endpoint before route imports; no provider, metadata service, merchant or remote JWKS transport. This is defense in depth, not a VPC firewall or credential-isolation substitute. The receiving service still requires Cloud Run IAM configured by root. Actual source `/agent/internal/auth/introspect` additionally checks `X-Internal-Key` using `AGENT_AUTH_INTROSPECT_INTERNAL_KEY`; application API-key/buyer headers remain source-authenticated on Reap routes. Never expose health/auth service publicly for convenience.

## Migration and state admission

Root provisions the genuinely fresh database with migrator ownership and public-schema ownership, no PUBLIC database/schema access. On the separately approved fresh dedicated instance only, root must also remove PUBLIC CONNECT to other databases (including postgres/template1) and verify neither scoped actor has effective CONNECT elsewhere. Do not change any existing shared instance ACLs. Migrator LOGIN has no superuser, CREATEDB, CREATEROLE, replication, BYPASSRLS or role memberships. Runtime CONNECT/schema USAGE only; no database CREATE/TEMP/schema CREATE. Runtime cannot own tables, run administrative table privileges, grant privileges onward (including per-column grant options) or write the manifest, agents, API keys, catalog/proof/eligibility tables. Cloud SQL's default built-in superuser membership fails this guard.

Migration checks identity and emptiness before importing application models, then repeats those checks under an advisory transaction lock before DDL. It creates known ledger/auth/profile tables and the ten-table278-column public catalog read contract. Catalog SQL is original column/type/nullability metadata; four core catalog PKs follow `db/catalog.py`, canonical-election PK follows migration181. It does not restore original triggers/indexes/FKs/defaults or rows; source migrations add known Reap/seed constraints/indexes. Catalog is runtime read-only. Preserve original source row/proof/eligibility timestamps in a separately reviewed loader; no copied buyer/agent/enrollment state or generated proof freshness.

`prepared` marker is a zero-state baseline: no agents/keys/purchases/enrollments/buyer links/attribution rows. Repeated web starts validate this census. A future root-audited, migrator-owned `owned` marker can bind one new synthetic agent, owner hash, buyer ID and provider buyer reference. It permits only that cohort and at most one row of each payment/identity/attribution type, including nonterminal or terminal purchase restarts. Current source permits owner-bound **read/auth preparation**, with all create/provider gates still off; it does not seed/promote the marker or enable any real checkout. Runtime cannot promote its own scope. Later activation, actual ingress JWT trust, fixture loader, poller supervision and real sandbox-provider egress require a separately reviewed phase. No ACTIVE enrollment is seeded.

The stock `api_keys` schema is copied exactly from source (hash/status-based, without expiry columns). Introspection accepts active status; revoked-status test clears the existing process cache explicitly. Existing source positive-cache TTL60s remains, so this is not a cross-process instant-revocation guarantee. Do not claim timed-key expiry is enforced by that schema.

## Local tests

```
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q tests/test_reap_rehearsal_guard.py tests/test_reap_rehearsal_guard_postgres.py
```

Set `REAP_GUARD_TEST_MANIFEST` only to the mode0600 disposable localhost55435 manifest prepared by root. Without it, database tests skip. Tests never load ambient/cloud DSNs. Production target constants are replaced only by explicit in-process test bindings, not an operator override. Negative cases revert local privilege/schema/state mutations. Source auth probe is a subprocess using real ASGI routes and restricted-role SQL; it never connects to a provider. Real Reap routes transitively import pure schema helper definitions, but startup initializer calls are trapped and absent.

Rollback before activation: remove the dedicated candidate service/job; do not fall back to `main:app`. Preserve private manifest and scoped database for investigation; deletion of new cloud resources is root-controlled. Current preparation contains no provider state to reconcile. Later owned/real-provider rollback must keep read/recovery/reconciliation available and needs its own reviewed procedure.
