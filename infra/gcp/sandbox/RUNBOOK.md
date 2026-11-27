# Partner sandbox v0: apply runbook (design build steps 3 and 8)

Design: the "Partner sandbox for new agents" design doc (decided 2026-11-25: own GCP project; v0 =
build steps 3, 4, 5, 6, 8, 10 with partners set up by hand). This runbook builds the
**infrastructure** (step 3) and the **AgenPaz sandbox host** (step 8), and says where the other
steps plug in. Nothing in this directory has been applied. Every script is a dry run unless
`APPLY=1`.

**Every step below is "controller + owner go".** One step per go, in order, through the PR
controller session; the owner's go is given in the controller session, and the step's check must
pass before the next step is asked for. A step that fails stops the runbook: use that step's
rollback, then report. Steps marked **PROD** change something in `pivota-prod`.

## What gets built

```
                  partner agent (ak_test_ key)                     buyer's browser
                           │                                              │
             https://sandbox.mcp.pivota.cc                   https://sandbox.agenpaz.com
                           └──────────────┐            ┌──────────────────┘
                                 sandbox LB (own IP, own cert map, default = redirect)
                                          │            │
   pivota-sandbox / us-west1      Cloud Run gateway   Cloud Run agenpaz      (PIVOTA-Agent image)
   VPC sandbox-vpc                        │  ingress internal-and-cloud-load-balancing
   (direct VPC egress)                    ▼
                                  Cloud Run web  ◄── worker (min=max=1, only reap_agentic_purchase_poll)
                                  ingress internal │      (pivota-backend image)
                                          ▼        ▼
                                  Cloud SQL sandbox-pg (PG17, private IP only)
                                          ▲
                     catalog-import job (sa-catalog-import) reads gs://pivota-sandbox-catalog-drop
                                          ▲  write-only (objectCreator)
   pivota-prod                    export job (sa-sandbox-export@pivota-prod, sandbox-backend's design)

   egress: everything leaves through Cloud NAT on the sandbox's own reserved IP -> Reap SANDBOX API
```

| resource | name | notes |
|---|---|---|
| project | `pivota-sandbox` (`SANDBOX_PROJECT` overrides) | in the same org and billing account as prod/staging |
| VPC / subnet | `sandbox-vpc` / `sandbox-run` (private ranges in the untracked `config.local.sh`) | custom mode, Private Google Access; the auto `default` VPC is deleted |
| egress | `sandbox-router` + `sandbox-nat` on `sandbox-egress-ip` | the sandbox's own static IP |
| Cloud SQL | `sandbox-pg`, POSTGRES_17, `db-g1-small`, zonal, 20 GB SSD | private IP only, 7 backups, no PITR, deletion protection |
| Redis | none in v0 | the replay guard falls back in-process (ENV_PROFILE.md) |
| Cloud Run | `web`, `worker`, `gateway`, `agenpaz` | images pinned by digest from `pivota-shared` |
| service accounts | `sa-backend`, `sa-worker`, `sa-gateway`, `sa-agenpaz`, `sa-catalog-import`, `sa-sandbox-ops` (one-off jobs only) | logging/metrics only at project level; secrets granted one by one |
| secrets | see `secrets/README.md` | DB (3), Reap sandbox key (1), minted internal keys (18, one of them the gateway's `ak_test_` service key, one the assertion key of S9) |
| LB | `sandbox-lb-ip`, `sandbox-urlmap`, `sandbox-certmap` | two hosts; unknown host → 301 to the gateway host |
| bucket | `gs://pivota-sandbox-catalog-drop` | prod writes (create only), sandbox reads; 7-day lifecycle |
| prod SA | `sa-sandbox-export@pivota-prod` | **PROD**; gets nothing in prod from this runbook |

The only grants that cross a project boundary: sandbox SAs and the sandbox's Cloud Run agent
READ the shared image registry (`pivota-shared`), and the prod export SA CREATES objects in the
sandbox bucket. `verify_sandbox.sh iam` and `bucket` check there is nothing else, in either
direction.

## Before step 1

- **The sandbox's two private ranges are NOT in the repo** (it is public; the owner's rule is no
  private IPs in committed files). Copy `infra/gcp/sandbox/config.local.example.sh` to
  `infra/gcp/sandbox/config.local.sh` (gitignored) and fill in `SUBNET_RANGE` (an RFC 1918 /24-/28) and
  `PSA_RANGE_START` (the start of the private /20 for Cloud SQL, which must be the range
  sandbox-backend's `config/sandbox_identity.py` holds as a hash, and apart from prod's and
  staging's). Every script refuses to run without it, or with a non-RFC-1918, too-wide or
  overlapping range. The file is PARSED, never run: only comment lines, blank lines and those two
  `KEY=value` lines are accepted; anything else refuses every script. Keep the file outside any commit; the controller and the owner hold the values.
- **The project id has one source: `SANDBOX_PROJECT` in `config.sh`** (default `pivota-sandbox`,
  Q13). Every script reads it, and the bucket name derives from it. The commands in this runbook
  use the same two variables, so set them once per shell from that file:
  ```bash
  eval "$(bash -c '. infra/gcp/sandbox/config.sh >/dev/null && printf "SANDBOX_PROJECT=%q DROP_BUCKET=%q\n" "$SANDBOX_PROJECT" "$DROP_BUCKET"')"
  ```

- This PR merged (scripts and docs only; merging changes nothing in any cloud).
- The operator account has, at the org: `resourcemanager.projectCreator`; on the billing account:
  `billing.user`; on `pivota-shared`: `artifactregistry.admin` (to grant registry read). Before
  step 7 it also needs `roles/storage.admin` on the sandbox project itself (step 7 says why and how).
- `BILLING_ACCOUNT` = the account listed in `infra/gcp/README.md`.
- `export GCLOUD=gcloud` (or a pinned SDK path). Every command names `--project` itself; the
  operator's `gcloud config` project is never used.
- Dry-run every step first (same command without `APPLY=1`) and paste its output into the
  controller message with the go request. The dry run is the review of the exact commands.

---

## Step 1. Project, billing, APIs

```bash
BILLING_ACCOUNT=<id> infra/gcp/sandbox/bootstrap_sandbox.sh project            # dry run
BILLING_ACCOUNT=<id> APPLY=1 infra/gcp/sandbox/bootstrap_sandbox.sh project    # go
```
Whether the id is free cannot be known before this command (Q13). If `projects create` answers
that the id is already in use, nothing was created: stop, report to the controller, and the owner
picks another id (Q13). Change it in `config.sh` only, re-run `check_profile.py` and the tests,
then repeat this step.

**Check:** `infra/gcp/sandbox/verify_sandbox.sh project` (in the org, label `env=sandbox`), and
`gcloud services list --enabled --project "$SANDBOX_PROJECT"` shows the 12 APIs.
**Rollback:** `gcloud projects delete "$SANDBOX_PROJECT"` (soft delete, restorable for 30 days).
The id is NOT freed by a delete: it cannot be used for a new project while the deletion is
pending, and Google does not hand a deleted project's id out again. A rollback of step 1 means a
new id (Q13) for any second attempt. This one command also undoes every later step except the
registry read grants in `pivota-shared` (step 5 rollback), the prod SA (step 8 rollback), the
release tags (step 9) and the DNS records (steps 6 and 13).

## Step 2. Network: VPC, subnet, private services access, Cloud NAT on a reserved IP

```bash
APPLY=1 infra/gcp/sandbox/bootstrap_sandbox.sh network
```
**Check:** `verify_sandbox.sh network`: peers only with servicenetworking, no `default` VPC, egress
IP reserved. Record the egress IP. Nothing is sent to Reap about it up front (Q1): step 12b's
Reap call from this IP is the check.
**Rollback:** in this order:
```bash
gcloud --project "$SANDBOX_PROJECT" compute routers nats delete sandbox-nat --router sandbox-router --region us-west1 --quiet
gcloud --project "$SANDBOX_PROJECT" compute routers delete sandbox-router --region us-west1 --quiet
gcloud --project "$SANDBOX_PROJECT" compute addresses delete sandbox-egress-ip --region us-west1 --quiet
gcloud --project "$SANDBOX_PROJECT" services vpc-peerings delete --service=servicenetworking.googleapis.com --network=sandbox-vpc --quiet
gcloud --project "$SANDBOX_PROJECT" compute addresses delete sandbox-psa --global --quiet
gcloud --project "$SANDBOX_PROJECT" compute networks subnets delete sandbox-run --region us-west1 --quiet
gcloud --project "$SANDBOX_PROJECT" compute networks delete sandbox-vpc --quiet
```

## Step 3. Cloud SQL (about 10-15 minutes)

```bash
APPLY=1 infra/gcp/sandbox/bootstrap_sandbox.sh sql
```
**Check:** `verify_sandbox.sh sql` (private IP only) and
`gcloud --project "$SANDBOX_PROJECT" sql instances describe sandbox-pg --format='value(state,settings.tier)'`
→ `RUNNABLE db-g1-small`.
**Rollback:**
```bash
gcloud --project "$SANDBOX_PROJECT" sql instances patch sandbox-pg --no-deletion-protection
gcloud --project "$SANDBOX_PROJECT" sql instances delete sandbox-pg --quiet
```

## Step 4. Secrets, then the owner adds the Reap sandbox key

```bash
APPLY=1 infra/gcp/sandbox/bootstrap_sandbox.sh secrets
```
Creates the DB user and `pivota-db-password`, `DATABASE_URL`, `DATABASE_URL_NOVERIFY` for
`sandbox-pg`; mints the 18 internal keys inside the project (`PIVOTA_API_KEY` as `ak_test_<64 hex>`,
the format the gateway's boot check and its MCP loopback require); creates `REAP_API_KEY` **empty**.

The key is the Reap **sandbox** key that production frees when it moves to the real production
key (Q1, decided). No second key is requested from Reap. Order:
1. Production is confirmed working on the live key first. Until then, stop here: step 4's check
   and every step from 11 on need the key, so they wait.
2. Then **the owner only** types the key into a version, from the Reap sandbox dashboard, without
   it touching a file, a chat or a log:
```bash
gcloud --project "$SANDBOX_PROJECT" secrets versions add REAP_API_KEY --data-file=-
# paste the key, press Enter, then Ctrl-D
```
3. Then, as its own owner + controller go, the key's old version in `pivota-prod`'s `REAP_API_KEY`
   is disabled, so production holds no usable copy. Disable it by its version number
   (`gcloud --project pivota-prod secrets versions list REAP_API_KEY` shows numbers and states;
   metadata only), never `latest`.

Nobody reads the value or copies it out of prod's or staging's Secret Manager, not even
secret-to-secret.

**Check:** `verify_sandbox.sh secrets`: exactly the inventory, every secret labelled `env=sandbox`
with its origin, each with an enabled version. (Reads metadata only.)
**Rollback:** `for s in $(gcloud --project "$SANDBOX_PROJECT" secrets list --format='value(name.basename())'); do gcloud --project "$SANDBOX_PROJECT" secrets delete "$s" --quiet; done`
and `gcloud --project "$SANDBOX_PROJECT" sql users delete pivota --instance sandbox-pg --quiet`.

## Step 5. Service accounts and IAM (writes READ grants in pivota-shared)

```bash
APPLY=1 infra/gcp/sandbox/bootstrap_sandbox.sh iam
```
**Check (heads-up to the controller first: it reads pivota-prod, -staging and -shared policies):**
`verify_sandbox.sh iam`. In prod, staging and shared it reads the project policy AND, through
Cloud Asset search, every resource-level policy (buckets, secrets, repos, ...). None may name a
sandbox principal: its own service accounts, its service agents (`service-<N>@...`), its default
Compute SA or its Cloud Build SA. The one allowance is `artifactregistry.reader` on the two image
repos in pivota-shared. In the sandbox's own policy, nothing may be public, there is no
project-wide secret access, no service account holds editor/owner (this phase removes the default
Compute SA's automatic editor; Google's APIs agent `<N>@cloudservices` keeps its own by design),
and no other project's service account appears. A failed or disabled Asset search FAILS the
check rather than passing it. Prerequisite: the Cloud Asset API must be usable for those scopes;
enabling it anywhere in prod is its own controller step. Then `verify_sandbox.sh secrets`: each
secret's policy is exactly secretAccessor for the service accounts its lists name
(`REAP_API_KEY`: `sa-backend` and `sa-worker` only).
**Rollback:** delete the 6 SAs in the sandbox project (`gcloud --project "$SANDBOX_PROJECT" iam service-accounts delete <email> --quiet`),
then remove their registry grants in pivota-shared:
```bash
for repo in pivota pivota-gateway; do for m in $(gcloud artifacts repositories get-iam-policy $repo --location us-west1 --project pivota-shared --format=json | P="$SANDBOX_PROJECT" python3 -c 'import json,os,sys; p=os.environ["P"]; [print(m) for b in json.load(sys.stdin).get("bindings",[]) for m in b["members"] if m.endswith("@"+p+".iam.gserviceaccount.com") or "service-<SANDBOX_PROJECT_NUMBER>@serverless-robot-prod" in m]'); do
  gcloud artifacts repositories remove-iam-policy-binding $repo --location us-west1 --project pivota-shared --member="$m" --role=roles/artifactregistry.reader --quiet; done; done
```

## Step 6. Load balancer, certificates, DNS round 1 (validation records only)

Early on purpose: certificates validate by DNS and take a while; the hosts carry no traffic until
step 12.
```bash
APPLY=1 infra/gcp/sandbox/setup_sandbox_lb.sh
infra/gcp/sandbox/setup_sandbox_lb.sh dns       # prints the records
```
**DNS round 1 (owner, at the DNS hosts for pivota.cc and agenpaz.com):** add ONLY the two
`_acme-challenge.<host>` CNAMEs the script prints. **No A or AAAA record for either host yet.**
**Check:** after the owner reports the round done, on the authoritative servers and 1.1.1.1:
```bash
for pair in sandbox.mcp.pivota.cc:pivota.cc sandbox.agenpaz.com:agenpaz.com; do
  h=${pair%%:*}; zone=${pair##*:}
  for ns in $(dig +short NS "$zone" | head -1) 1.1.1.1; do echo "$h @$ns A: $(dig +short A "$h" @"$ns") AAAA: $(dig +short AAAA "$h" @"$ns")"; done
done   # A and AAAA must be EMPTY for both hosts
```
Then wait for `verify_sandbox.sh edge` (both certs ACTIVE) before step 12.
**Rollback:** remove the two CNAMEs; delete forwarding rules, proxies, url maps, cert map entries,
certs, DNS authorizations, backend services, NEGs and `sandbox-lb-ip` (names in the script).

## Step 7. Catalog drop bucket, sandbox side

**Prerequisite (own go):** the operator account holds `roles/storage.admin` on the sandbox
project, granted to the account itself (not a group) and without a condition. Project Owner is
not enough: an Owner reaches a bucket's IAM policy only through the bucket's legacy convenience
binding (`projectOwner` → `legacyBucketOwner`), and this step removes exactly that binding. On the
first apply (2026-11-27) the removal took away the operator's `getIamPolicy`/`setIamPolicy`
mid-run, and the reader grant failed. `setup_catalog_drop.sh` (both sides) now refuses before
any change unless the grant is there.
```bash
gcloud projects add-iam-policy-binding "$SANDBOX_PROJECT" --member="user:<operator account>" --role=roles/storage.admin --condition=None
```

```bash
APPLY=1 infra/gcp/sandbox/setup_catalog_drop.sh sandbox
```
It adds the `sa-catalog-import` reader first, then removes every legacy convenience binding, then
reads the policy back and fails if a legacy binding remains or the reader is missing. A re-run on a
half-done bucket finishes it.
**Check:** `verify_sandbox.sh bucket` will fail on the missing prod writer until step 8; check now:
`gcloud storage buckets describe gs://$DROP_BUCKET --project "$SANDBOX_PROJECT" --format='value(public_access_prevention,uniform_bucket_level_access)'`
→ `enforced True`, and its IAM policy holds only `sa-catalog-import` as objectViewer.
**Rollback:** `gcloud storage rm -r gs://$DROP_BUCKET --project "$SANDBOX_PROJECT"`.

## Step 8. **PROD**: the export service account and its write-only grant

```bash
APPLY=1 infra/gcp/sandbox/setup_catalog_drop.sh prod-writer
```
Needs step 7's `roles/storage.admin` prerequisite too (the grant is on the sandbox bucket), and
checks it, the bucket and its project number before creating anything. Creates
`sa-sandbox-export@pivota-prod` and grants it `objectCreator` on the sandbox bucket. It
gets **no** role in pivota-prod here; the export job's own prod-side grants (a catalog-only DB
read, Q3) come with sandbox-backend's export PR, as their own controller step.
**Check:** `verify_sandbox.sh bucket` → exactly two bindings.
**Rollback:** remove the bucket binding FIRST, then the SA. Deleting the SA alone leaves a
`deleted:serviceAccount:...` binding on the bucket, and an SA re-created with the same name does
not inherit it back.
```bash
gcloud storage buckets remove-iam-policy-binding gs://$DROP_BUCKET --project "$SANDBOX_PROJECT" \
  --member=serviceAccount:sa-sandbox-export@pivota-prod.iam.gserviceaccount.com --role=roles/storage.objectCreator
gcloud iam service-accounts delete sa-sandbox-export@pivota-prod.iam.gserviceaccount.com --project pivota-prod --quiet
```

## Step 9. Cut release `sandbox-r1` (tags in both repositories)

Set the release name once for steps 9-12: `REL=sandbox-r1` (the next release is `sandbox-r2`, and
so on; never reuse a name).

Prerequisites: sandbox-backend's PR (PIVOTA_ENV=sandbox accepted, boot guard, catalog copy job)
and sandbox-gateway's PR (ak_test_ only, boot check, `sandbox: true` marker) are merged,
`check_profile.py` passes with no pending note left (the legal-URL ruling, Q12), and both commits
have images.
```bash
# pivota-backend, at the chosen main commit
git tag -a sandbox-r1 <backend-sha> -m "sandbox r1: <one line>" && git push origin refs/tags/sandbox-r1
# PIVOTA-Agent, at the chosen main commit
git -C <PIVOTA-Agent> tag -a sandbox-r1 <gateway-sha> -m "sandbox r1: <one line>" && git -C <PIVOTA-Agent> push origin refs/tags/sandbox-r1
```
The BACKEND tag also fixes the deploy config of every service (step 11 runs the tag's own
`infra/gcp/sandbox/`), so the backend commit must hold the profile changes this release needs.

**Check:** `git ls-remote origin 'refs/tags/sandbox-r1*'` in each repo shows the tag object and the
peeled `^{}` commit; the deploy dry run (step 11, without APPLY) resolves both images and prints
`deploy config: infra/gcp/sandbox as committed at backend <sha> (sandbox-r1), verified file by file`.
**Rollback:** `git push origin :refs/tags/sandbox-r1` (and in PIVOTA-Agent). Never move a pushed
release tag; cut `sandbox-r2` instead.

## Step 10. Schema, then the first catalog copy (design step 6)

Three sub-steps, in this order, each its own go. The database is empty after step 3, and the
importer refuses unless every copied column already exists, so both schemas come first. All three
are one-off Cloud Run jobs in the sandbox, on the release's images (step 9), on the sandbox VPC.
Set the two images once:
```bash
BE_IMG=us-west1-docker.pkg.dev/pivota-shared/pivota/backend@<digest of the sandbox-r1 backend commit>
GW_IMG=<gateway image of the sandbox-r1 PIVOTA-Agent commit, by digest>   # deploy_sandbox.sh's dry run prints both
VPC_FLAGS="--region us-west1 --network sandbox-vpc --subnet sandbox-run --vpc-egress all-traffic --max-retries 0"
```

**10a. Schema from prod's, by a schema-only dump (decided 2026-11-25, Q15: option A).** The
sandbox gets prod's exact table shape: the repo migrations plus the boot-time heals plus the
hand-applied files, which only prod has together, and which is the only shape the purchase path
has run on. Tables that are never copied (orders, Reap ledgers, consents, agents, issuers) arrive
as definitions only. `web` cannot create the schema itself (empty-DB boot race), and no prod
credential enters the sandbox. The code and its own doc are sandbox-backend's
(`scripts/sandbox/dump_prod_schema.sh`, `jobs/sandbox_schema_apply.py`,
`docs/sandbox/schema_bootstrap.md`). What leaves prod: DDL with no owners, grants, comments,
publications, subscriptions or tablespaces, plus the ROWS of the two migration ledgers
(`startup_sql_migrations`, `schema_migrations`: file ids and timestamps), so 10b applies only what
prod lacks. The dump refuses (exit 3, nothing uploaded) anything that can hold a credential: user
mappings, servers, roles, grants, or row data in the schema file.

*10a-image (pivota-shared, own go).* The dump needs `pg_dump` 17 and `curl`, which the backend
image lacks. Build only from a release whose `scripts/sandbox/dump_prod_schema.sh` is
sandbox-backend's manifest-format-2 version (DSN off argv, lock wait, deadline, explicit-column
ledgers). **Do not build yet:** the controller did not approve 77a881c7f (its apply gate had
bypasses), so the format-2 script will come from a later, fixed sandbox-backend commit, and this
step waits for it: the sandbox apply accepts only format 2, and the Dockerfile refuses to
build from an older script. The script's only subcommand, `check <schema.sql> <ledgers.sql>`, runs
just its refusals and serves the human read below. From a pivota-backend checkout at
`$BACKEND_SHA` (the release commit):
```bash
gcloud builds submit --config infra/gcp/sandbox/cloudbuild.schema-dump.yaml --project pivota-shared \
  --substitutions=COMMIT_SHA=$BACKEND_SHA .
DUMP_IMG=us-west1-docker.pkg.dev/pivota-shared/pivota/sandbox-schema-dump@$(gcloud artifacts docker images describe \
  us-west1-docker.pkg.dev/pivota-shared/pivota/sandbox-schema-dump:$BACKEND_SHA --format='value(image_summary.digest)')
```

*10a-role (**PROD**, own go; Q3 decided 2026-11-25 by the owner: the dedicated hardened role).*
The dump needs a credential that can lock every table (`pg_dump` takes ACCESS SHARE on each,
which needs SELECT), so the catalog export's narrow role cannot do it, and prod's full
application `DATABASE_URL` is NOT used. The dump runs as a **dedicated schema-dump role** used only by this job (it reads no rows except the two migration
ledgers), its DSN in its own prod secret `SANDBOX_SCHEMA_DUMP_DATABASE_URL`, in the exact form the
script accepts: `postgresql://user:password@<host>[:port]/<db>[?sslmode=...]` (percent-encode `@`,
`/`, `?` in the password; the script splits the DSN into PG* variables and a 0600 passfile, so the
password never reaches an argv). It is NEVER reused for the daily catalog export (10c), which
gets its own catalog-only role. Creating the role and the secret is its own gated prod one-off
(program and sha to the controller after the T1-T6 fixes are in).

The role is created ONCE, as a gated prod DB one-off through the controller (program and sha to
the controller first, slot rules, armed only after its OK), run by an admin connection, with this
SQL. It is created in SQL, NOT with `gcloud sql users create`, which would make it a member of
`cloudsqlsuperuser`:
```sql
CREATE ROLE sandbox_schema_dump LOGIN NOINHERIT;          -- no password yet; LOGIN is turned off below
ALTER ROLE sandbox_schema_dump NOLOGIN;                    -- off between runs (see "per run")
ALTER ROLE sandbox_schema_dump SET default_transaction_read_only = on;
GRANT CONNECT ON DATABASE pivota TO sandbox_schema_dump;
-- explicit USAGE, one schema at a time, for every non-system schema that has tables:
GRANT USAGE ON SCHEMA public TO sandbox_schema_dump;
```
No `REVOKE TEMPORARY`: TEMP reaches every role through PUBLIC, so revoking it from this role alone
is a no-op; removing it from PUBLIC is an app-wide change, its own decision.

**PUBLIC's legacy CREATE on `public`.** A database restored from a pre-15 dump can still grant
`CREATE ON SCHEMA public TO PUBLIC`, which would let this role create a function there (the review
reproduced it). The one-off revokes it (`REVOKE CREATE ON SCHEMA public FROM PUBLIC;`) ONLY if the
owner approves that as part of the one-off; otherwise the checks below refuse the role while the
grant is present, and nothing runs.

Read-only checks, run by the one-off after the SQL above and again on EVERY run after the per-run
grant (step 1 below). Each must return 0 rows, or the role is not used:
```sql
-- 1. a member of NO role (not cloudsqlsuperuser, not the app role) and no dangerous attribute
SELECT 'member of ' || b.rolname FROM pg_auth_members m JOIN pg_roles b ON b.oid = m.roleid
 WHERE m.member = 'sandbox_schema_dump'::regrole
UNION ALL SELECT 'attribute' FROM pg_roles WHERE rolname = 'sandbox_schema_dump'
   AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls);
-- 2. owns nothing, and may write NOTHING: every relation kind that can be written (tables,
--    partitioned tables, views, materialized views, sequences, foreign tables), table-level AND
--    column-level, and the privileges that change data or schema indirectly
SELECT c.oid::regclass, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%'
   AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
   AND (c.relowner = 'sandbox_schema_dump'::regrole
        OR has_table_privilege('sandbox_schema_dump', c.oid, 'INSERT, UPDATE, DELETE, TRUNCATE, TRIGGER, REFERENCES, MAINTAIN')
        OR has_any_column_privilege('sandbox_schema_dump', c.oid, 'INSERT, UPDATE, REFERENCES')
        OR (c.relkind = 'S' AND has_sequence_privilege('sandbox_schema_dump', c.oid, 'USAGE, UPDATE')));
--    (USAGE on a sequence allows nextval(), a write: a PUBLIC grant of it was shown to let the role
--    advance a sequence once it turned default_transaction_read_only off)
-- 3. may create NOTHING: in no schema (PUBLIC's legacy CREATE on public included) and not in the
--    database
SELECT 'CREATE on schema ' || nspname FROM pg_namespace
 WHERE nspname NOT LIKE 'pg\_%' AND nspname <> 'information_schema'
   AND has_schema_privilege('sandbox_schema_dump', oid, 'CREATE')
UNION ALL SELECT 'CREATE on database' WHERE has_database_privilege('sandbox_schema_dump', current_database(), 'CREATE');
-- 4. SECURITY DEFINER functions it can execute (PUBLIC gets EXECUTE by default): each row is read
--    by a person; a definer function that writes or reads secrets is a finding before any run
SELECT p.oid::regprocedure, pg_get_userbyid(p.proowner) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE p.prosecdef AND n.nspname NOT IN ('pg_catalog', 'information_schema')
   AND has_function_privilege('sandbox_schema_dump', p.oid, 'EXECUTE');
-- 5. per-role settings: default_transaction_read_only=on is the ONLY one sandbox-backend's script
--    accepts (it also forces read-only sessions itself, PGOPTIONS, and refuses a session that is
--    not read only). Any row here is a refusal:
SELECT setdatabase, setconfig FROM pg_db_role_setting
 WHERE setrole = 'sandbox_schema_dump'::regrole AND setconfig <> ARRAY['default_transaction_read_only=on'];
-- 5b. database-wide settings (`ALTER DATABASE ... SET`, setrole = 0): a HUMAN READ, like check 4.
--     A role's own setting outranks a database-wide one, so this cannot undo the read-only default
--     above, and pg_dump pins its own search_path; any row is still read and explained before a run.
SELECT setconfig FROM pg_db_role_setting
 WHERE setrole = 0 AND setdatabase = (SELECT oid FROM pg_database WHERE datname = current_database());
-- 6. owns NOTHING at all: functions, types, schemas, large objects, anything with an owner record
SELECT classid::regclass, objid FROM pg_shdepend
 WHERE refobjid = 'sandbox_schema_dump'::regrole AND deptype = 'o'
UNION ALL SELECT 'pg_namespace'::regclass, oid FROM pg_namespace WHERE nspowner = 'sandbox_schema_dump'::regrole
UNION ALL SELECT 'pg_proc'::regclass, oid FROM pg_proc WHERE proowner = 'sandbox_schema_dump'::regrole
UNION ALL SELECT 'pg_type'::regclass, oid FROM pg_type WHERE typowner = 'sandbox_schema_dump'::regrole
UNION ALL SELECT 'pg_largeobject_metadata'::regclass, oid FROM pg_largeobject_metadata WHERE lomowner = 'sandbox_schema_dump'::regrole;
-- 7. may write no large object (Postgres 17: has_largeobject_privilege), and the compat switch that
--    would skip large-object ACLs is off
SELECT 'large object ' || oid FROM pg_largeobject_metadata
 WHERE has_largeobject_privilege('sandbox_schema_dump', oid, 'UPDATE')
UNION ALL SELECT 'lo_compat_privileges=on' WHERE current_setting('lo_compat_privileges')::boolean;
```
**Decided 2026-11-26 by the owner: accepted as documented (no prod change).** PUBLIC may EXECUTE
`lo_create` on prod, and a READ ONLY transaction does not stop it (measured on Postgres 15 by
sandbox-backend). The dump script never calls it, so the checks accept it. The prod-wide
`REVOKE EXECUTE ON FUNCTION lo_create(oid), lo_creat(integer), lo_from_bytea(oid, bytea) FROM PUBLIC`
is NOT run, by that decision; revisiting it would be its own gated prod change.

(Checks 4 and 5b are human reads, not "0 rows". `MAINTAIN` exists from Postgres 17, prod's version; on an
older server drop it from check 2.) Two limits, stated rather than hidden:
`default_transaction_read_only` is a default a session could change, so checks 2 and 3 (what the
role COULD do), not the default, are the gate; and the script's own refusal (sandbox-backend
b44db13e0: refuses a superuser, CREATEROLE/CREATEDB/REPLICATION/BYPASSRLS, any owned table or any
INSERT/UPDATE/DELETE/TRUNCATE) is the second lock.

**Per run** (the owner types every secret; no session reads or echoes one, and nothing goes in a
file, chat, log or terminal echo):
1. SELECT on everything that exists NOW (tables created since the last run have no grant), then
   checks 1-3 above again: `GRANT SELECT ON ALL TABLES IN SCHEMA public TO sandbox_schema_dump;`
   (per schema), `GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO sandbox_schema_dump;`
2. a NEW password: in an admin `psql` session, `\password sandbox_schema_dump` (prompted, sent as
   a client-side SCRAM verifier), then `ALTER ROLE sandbox_schema_dump LOGIN;`
3. the DSN into a new secret version, read without echo and without a trailing newline:
   ```bash
   read -rs DSN && printf %s "$DSN" | gcloud secrets versions add SANDBOX_SCHEMA_DUMP_DATABASE_URL --project pivota-prod --data-file=- ; unset DSN
   ```
4. 10a-prod below (the job), then the human read (first run) and the checks;
5. afterwards, always: `ALTER ROLE sandbox_schema_dump NOLOGIN PASSWORD NULL;`, disable that secret
   version by its NUMBER, never `latest` (the number `versions add` printed in 3, or
   `gcloud secrets versions list SANDBOX_SCHEMA_DUMP_DATABASE_URL --project pivota-prod --filter=state=ENABLED --format='value(name.basename())'`):
   `gcloud secrets versions disable <n> --secret SANDBOX_SCHEMA_DUMP_DATABASE_URL --project pivota-prod`,
   and 10a-prod's script removes the job's grant on exit.

*10a-prod (**PROD**, own go: program and sha reviewed by the controller, outside blackout windows).*
The export SA may read only that secret, and only while the step runs: `schema_dump_prod.sh` adds
the grant, deploys and executes the job, and REMOVES the grant from an EXIT trap on every path
(success, a failed step under `set -e`, ^C), returning the run's own exit status (review T3):
```bash
APPLY=1 infra/gcp/sandbox/schema_dump_prod.sh "$DUMP_IMG"
# what it runs, in order (dry run without APPLY prints exactly these):
#   gcloud secrets add-iam-policy-binding SANDBOX_SCHEMA_DUMP_DATABASE_URL --project pivota-prod --member=serviceAccount:sa-sandbox-export@... --role=roles/secretmanager.secretAccessor
#   gcloud run jobs deploy sandbox-schema-dump --project pivota-prod ... --set-secrets DATABASE_URL=SANDBOX_SCHEMA_DUMP_DATABASE_URL:latest
#   gcloud run jobs execute sandbox-schema-dump --project pivota-prod --region us-west1 --wait
#   (EXIT trap) gcloud secrets remove-iam-policy-binding SANDBOX_SCHEMA_DUMP_DATABASE_URL --project pivota-prod ...
# optional: SCHEMA_DUMP_LOCK_WAIT / SCHEMA_DUMP_DEADLINE_SECONDS keep the script's defaults (10s / 600)
```
**Check, first run (the upload is approved only after a human read):** the first dump is read by a
person BEFORE its upload is approved: a literal inside DDL (a DEFAULT, a CHECK, a function body)
is copied as written, and only a reader can tell whether one holds something that must not leave
prod. How the first dump is held for that read, and the dump's `--lock-wait-timeout` and
`PGOPTIONS` statement/lock timeouts, come from sandbox-backend's revised script (requested in the
controller's backend review); this step runs the job exactly as that revision documents, and is
not run before it lands. **Every run:** the execution succeeded, and
`gcloud storage ls gs://$DROP_BUCKET/schema/` (as the operator, in the sandbox project) shows one
new `<stamp>/` with `schema.sql.gz`, `ledgers.sql.gz` and `_MANIFEST.json`.
**Never copied, by construction (sandbox-backend a586c7719):** the export and the importer both
refuse identity, key, issuer, order and buyer tables by name (`agents`, `api_keys`,
`agent_identity_issuers`, `orders`, ... plus a name pattern), whatever a manifest says; the schema
dump carries their definitions only.
**Rollback:** remove the secret grant (the same command as the last one above:
`gcloud secrets remove-iam-policy-binding SANDBOX_SCHEMA_DUMP_DATABASE_URL --project pivota-prod
--member=serviceAccount:sa-sandbox-export@pivota-prod.iam.gserviceaccount.com --role=roles/secretmanager.secretAccessor`),
`ALTER ROLE sandbox_schema_dump NOLOGIN;`, disable the run's secret version, delete the job
(`gcloud run jobs delete sandbox-schema-dump --project pivota-prod --region us-west1`), and delete
the `schema/<stamp>/` objects from the drop.

*10a-sandbox, in three own-go parts.* The apply runs as a dedicated NON-superuser role,
`sandbox_schema_apply`: a statement allowlist cannot limit what executes at apply time (CTAS, a
materialized view WITH DATA or a trigger can run a function body), so the real gate is the role.
sandbox-backend's job REFUSES (exit 2) unless the role has no superuser/createrole/createdb/
replication/bypassrls, `current_user = session_user`, it is a member of NO role, it may not CREATE
in the database, it may not create TEMP objects (a temp relation is searched before `pg_catalog`),
it has NO per-role setting (never `ALTER ROLE sandbox_schema_apply SET ...`), and it owns nothing
yet (relations, schemas, functions, types, extensions, large objects). The role is created in SQL, NOT with
`gcloud sql users create` (which joins `cloudsqlsuperuser`, and the job refuses that). Step 4 mints
its DSN `SCHEMA_APPLY_DATABASE_URL` (own password, the sandbox's private IP) and, from the same
password, its SCRAM-SHA-256 verifier `SCHEMA_APPLY_ROLE_VERIFIER`, together or not at all. The role
is created with the verifier only: Postgres writes a failing statement to the server log, so the
plaintext password is never part of one. The apps' `DATABASE_URL` is never used for the apply.

Both role steps run `sql/schema_apply_role.sh` as a one-off job on the digest-pinned postgres:17
image (the Dockerfile.schema-dump base), in the sandbox VPC, as `sa-sandbox-ops` (secrets/ops.list).
The verifier reaches psql only through the environment (`\getenv`), never an argv.
The script is passed through gcloud's alternate argument delimiter `^@@^`, which it never contains:
```bash
PG_IMG=docker.io/library/postgres@sha256:1eff563f6c868b9b433566e9f3cf6e5c79fbd3b95bf70ec7277d376cdbfc50d7
role_job(){ # create|retire|drop
  gcloud --project "$SANDBOX_PROJECT" run jobs deploy sandbox-schema-apply-role --image "$PG_IMG" $VPC_FLAGS \
    --service-account "sa-sandbox-ops@$SANDBOX_PROJECT.iam.gserviceaccount.com" \
    --set-env-vars PIVOTA_ENV=sandbox \
    --set-secrets DATABASE_URL=DATABASE_URL:latest,SCHEMA_APPLY_DATABASE_URL=SCHEMA_APPLY_DATABASE_URL:latest,SCHEMA_APPLY_ROLE_VERIFIER=SCHEMA_APPLY_ROLE_VERIFIER:latest \
    --command bash --args="^@@^-c@@$(cat infra/gcp/sandbox/sql/schema_apply_role.sh)@@schema_apply_role@@$1" --task-timeout 300s
  gcloud --project "$SANDBOX_PROJECT" run jobs execute sandbox-schema-apply-role --region us-west1 --wait
}
```
1. **Role (own go), before the apply:** `role_job create`. In one transaction it creates the role
   (LOGIN NOINHERIT NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS, CONNECTION
   LIMIT 2), grants CONNECT on the database and USAGE, CREATE on schema `public` (NOT CREATE on
   the database), takes TEMP away from PUBLIC (`REVOKE TEMPORARY ON DATABASE <db> FROM PUBLIC`)
   and gives it back to the app owner role only (`GRANT TEMPORARY ... TO` the admin it runs as:
   the catalog copy, 10c, stages rows in TEMP tables), and pre-creates the fixed extension list: `pg_trgm`, `pgcrypto` (backend) and
   `vector` (gateway). The dump never creates an extension; every `CREATE EXTENSION` in it must
   already be installed, or the apply refuses. The transaction fails unless the role has no
   membership, no forbidden attribute, no database CREATE, no TEMP and no per-role setting, and
   the app owner role still has TEMP. It REFUSES, changing nothing, when
   the role already exists: roles are cluster-wide and survive a database re-create (rollback
   below). Before running it, read the first
   dump's `CREATE EXTENSION` lines (the human read in 10a-prod) and confirm the list. Also
   confirm the dump creates no schema besides `public` (none known today); any other schema is
   created by the admin first, with USAGE, CREATE granted on it, or the apply fails and rolls back.
   **Check:** `DONE create`.
2. **Apply (own go):** on `$BE_IMG` as `sa-catalog-import`, with the full web profile as env (the
   sandbox guard runs first) plus `SANDBOX_SCHEMA_GCS_URI`, and the APPLY role's DSN mounted as
   `DATABASE_URL` (`secrets/schema-apply.list`). It refuses unless the database is empty and the
   role check above passes. It then re-checks the manifest, the sha256 and the statement rules,
   applies schema then ledger rows in ONE transaction, and rolls everything back unless the table
   and ledger counts match the source's:
```bash
{ cat /tmp/sbx-env/web.yaml; printf 'SANDBOX_SCHEMA_GCS_URI: "gs://%s/schema"\n' "$DROP_BUCKET"; } > /tmp/sbx-env/schema-apply.yaml
gcloud --project "$SANDBOX_PROJECT" run jobs deploy sandbox-schema-apply --image "$BE_IMG" $VPC_FLAGS \
  --service-account "sa-catalog-import@$SANDBOX_PROJECT.iam.gserviceaccount.com" \
  --env-vars-file /tmp/sbx-env/schema-apply.yaml --set-secrets DATABASE_URL=SCHEMA_APPLY_DATABASE_URL:latest \
  --command python --args=-m,jobs.sandbox_schema_apply --task-timeout 1800s
gcloud --project "$SANDBOX_PROJECT" run jobs execute sandbox-schema-apply --region us-west1 --wait
```
   (`/tmp/sbx-env` is rendered as in step 14a, with `--sandbox-project "$SANDBOX_PROJECT"`.)
   **Check:** `DONE {...}` (exit 0); executing it again answers `REFUSED` (the database is no
   longer empty, and the role now owns objects). Every sandbox DB writer (this job, the import, the
   14a script) refuses a `DATABASE_URL` whose host is outside the sandbox's private Cloud SQL range
   (`config.local.sh`, and the hash sandbox-backend holds) or its Cloud SQL socket; step 4's URLs use the private IP, so they pass.
3. **Retire (own go), right after a DONE apply:** `role_job retire`. As the admin, in one
   transaction: borrow the role (`GRANT … TO` the admin, which REASSIGN needs), `REASSIGN OWNED BY
   sandbox_schema_apply` to the app owner role (the one the apps and 10b's migrate connect as),
   give the role back, and `ALTER ROLE … NOLOGIN PASSWORD NULL`. The apply role itself never gains
   a membership. Then disable every enabled version of both secrets (a later run mints new ones,
   step 4). `versions disable` takes a version NUMBER, never the `latest` alias, so list them:
   ```bash
   for s in SCHEMA_APPLY_DATABASE_URL SCHEMA_APPLY_ROLE_VERIFIER; do
     for v in $(gcloud --project "$SANDBOX_PROJECT" secrets versions list "$s" --filter='state=ENABLED' --format='value(name.basename())'); do
       gcloud --project "$SANDBOX_PROJECT" secrets versions disable "$v" --secret "$s" --quiet
     done
   done
   ```
   **Check:** `DONE retire`; the role owns no relation; both secrets have no enabled version.

**Rollback (all of 10a-sandbox):** the role does NOT go with the database: roles are
cluster-wide. Run `role_job drop` first (it reassigns and drops anything the role still owns, then
drops the role; a missing role is a no-op), then re-create the database (below), and start
10a-sandbox again from part 1 (with the same two secrets, or both re-minted together).

**10b. Gateway schema.** The gateway has its own migrations (PIVOTA-Agent `src/db/migrations`,
ledger `schema_migrations`, so no clash with the backend's). Prod and staging apply them by hand;
the sandbox services never do (`DB_AUTO_MIGRATE=false` and `SKIP_DB_MIGRATIONS=true`, enforced by
`check_profile.py`), so this is the one deliberate run, applying every file in order under the
CLI's advisory lock:
```bash
gcloud --project "$SANDBOX_PROJECT" run jobs deploy sandbox-gateway-migrate --image "$GW_IMG" $VPC_FLAGS \
  --service-account "sa-sandbox-ops@$SANDBOX_PROJECT.iam.gserviceaccount.com" \
  --set-secrets DATABASE_URL=DATABASE_URL_NOVERIFY:latest --set-env-vars "PIVOTA_ENV=sandbox,GOOGLE_CLOUD_PROJECT=$SANDBOX_PROJECT" \
  --command node --args src/db/cli.js,migrate --task-timeout 1800s
gcloud --project "$SANDBOX_PROJECT" run jobs execute sandbox-gateway-migrate --region us-west1 --wait
```
Prerequisites the run hits (sandbox-gateway): `CREATE EXTENSION vector` (002) and `pg_trgm`
(033 and later). The sandbox DB user is created through the Cloud SQL Admin API (step 4) and so
belongs to `cloudsqlsuperuser`, which may create both on Cloud SQL; after 10a they normally exist
already, because prod's schema has them. Migrations 049/057/058 alter backend tables, so 10b runs
after 10a and before 10c. Each file runs in its own transaction under the CLI's advisory lock: a
failure rolls back that file only, and a re-run continues from it.

**Check:** not "a second run is a no-op" (that also passes when nothing was pending for the wrong
reason). Run sandbox-gateway's read-only checker **v2**, `gw_migrations_read_v2.cjs` (the version the
controller approved and armed; held with the controller, sha256
`d58a1eb11db96388caffc66165d9349e23143e273cf89d789e7f9124686b4711`, passed to `node -e`; v2 adds
the 060 count, so v1 `a359233f…` is not used) on the SAME image, with the sandbox `DATABASE_URL` and `PIVOTA_ENV=sandbox`. It
compares `schema_migrations` with the image's own files in one READ ONLY transaction. Pass =
its `COUNTS` line with `files` equal to `rows`, `pending=0` and `applied_only=0`, then
`MIGRATIONS_READ_DONE`.
**Rollback:** the job is idempotent and forward-only. To undo, re-create the database (below).

**10c. First catalog copy** (sandbox-backend's `jobs.sandbox_catalog_copy`). What the infra
requires of it:
- the export runs **in pivota-prod** as `sa-sandbox-export`, with
  `CATALOG_COPY_GCS_URI=gs://$DROP_BUCKET/catalog`. It writes `catalog/<UTC stamp>/...` with
  `_MANIFEST.json` LAST and unique names per run (create-only grant). This is a **PROD** one-off,
  its own go; It reads prod as the catalog-only role `sandbox_catalog_export`, never
  the app's `DATABASE_URL`: per run R3 (`python -m jobs.sandbox_catalog_export_role grant`, as the tables'
  owner), R4 (the owner's password, LOGIN and a new secret version), R5
  (`infra/gcp/sandbox/catalog_export_prod.sh <backend image by digest> <secret version number>`) and
  R6, always (NOLOGIN, end its sessions, `... revoke`, disable the version). See
  docs/sandbox/catalog_copy.md "Prod run";
- the import runs **in pivota-sandbox** on `$BE_IMG` as `sa-catalog-import`, with the same
  `CATALOG_COPY_GCS_URI`, the web profile's env (the sandbox guard runs first) and secret
  `DATABASE_URL=DATABASE_URL:latest` (`secrets/catalog-import.list`). It reads only the newest
  drop that has a manifest.
- the copy must **never** include `agents`, `api_keys` or any other identity, key, issuer, order
  or buyer table. One reason beyond PII: the backend code trusts prod's gateway agent id as a
  voucher floor (routes/agent_auth.py), so a prod `agents`/`api_keys` row in the sandbox would turn
  a prod key into a trusted voucher here. Agents exist in the sandbox only through step 14.

**Check:** the import prints `DONE` (exit 0; `REFUSED` is exit 2) and its row counts match the
manifest. **Rollback for all of step 10:** re-create the database
(`gcloud --project "$SANDBOX_PROJECT" sql databases delete pivota --instance sandbox-pg`, then
`bootstrap_sandbox.sh sql`), and start again at 10a.
The daily schedule (Cloud Scheduler → import job) is sandbox-backend's step and its own go. It
must stay daily: Tier B cart-link verdicts expire after 48 h (db/tierb_cart_link_eligibility.py),
so a missed day can make copied offers unbuyable.

## Step 11. Deploy the release (web → worker → gateway → agenpaz)

```bash
GATEWAY_REPO_DIR=<PIVOTA-Agent checkout> infra/gcp/sandbox/deploy_sandbox.sh sandbox-r1            # dry run
GATEWAY_REPO_DIR=<PIVOTA-Agent checkout> APPLY=1 infra/gcp/sandbox/deploy_sandbox.sh sandbox-r1    # go
```
First deploy of each service takes traffic directly (nothing routes to it yet). Later releases go
out at 0% under tag `c-<sha12>`, pass an in-VPC `/health` probe, then take 100% by revision name.

**What runs is the tag's copy, not this checkout's (review S4).** Once `sandbox-r1` resolves, the
script writes the backend tag commit's `infra/gcp/sandbox/` and `infra/gcp/_gateway_image.sh`
from git objects into a temp dir and runs THAT `deploy_sandbox.sh`: the env profiles (all four
services, gateway and agenpaz included), the secret lists, `check_profile.py` and the service
shapes are all the release's. The release copy then checks, file by file, that it holds exactly
the commit's files (blob hash and exec bit; no extra file except the untracked `config.local.sh`
and bytecode caches), and refuses if the tag moved between the two resolutions. So a config
change ships only by merging it and cutting `sandbox-r(N+1)`; an edit in the operator's checkout
is never deployed. The output says `handing over to sandbox-r1's own deploy script` and
`deploy config: ... verified file by file`; a dry run with no tag says it used the WORKING TREE,
and an APPLY run with no tag stops.
Both repository directories must fetch (the URL `git remote get-url origin` gives) from
`pivota-backend` and `PIVOTA-Agent`, and two github.com origins must have the same owner. This is
protection against an ACCIDENTALLY wrong remote only (a checkout of another repository, or one
repo from a fork and one not): two non-GitHub remotes compare as equal (no owner), and a fork of
BOTH repositories under one owner passes. It is not an identity check.

**What S4 does NOT protect against:** a malicious checkout. The checkout's own `config.sh` and
the first half of `deploy_sandbox.sh` run BEFORE the hand-over and before any verification, so
an operator's checkout that was tampered with can do anything the operator's credentials allow.
S4 guards against DRIFT (an edited profile, a stray secret list, a branch that is not the tag),
not against code you do not trust: run the deploy from a checkout you would run any `gcloud`
command from. `config.local.sh` is parsed, never run, so it cannot change any of this.

**Check:**
- The deploy itself refuses to promote a revision that does not read back `PIVOTA_ENV=sandbox` and
  `GOOGLE_CLOUD_PROJECT=$SANDBOX_PROJECT` from its own spec.
- `verify_sandbox.sh run`: 4 services, digest-pinned images, `PIVOTA_ENV=sandbox`, no `A2A_*`,
  Reap sandbox URL + simulate, `AGENPAS_RAILS=reap`.
- `verify_sandbox.sh` (all groups) passes.
- Worker runs only the Reap poll:
  `gcloud --project "$SANDBOX_PROJECT" logging read 'resource.labels.service_name="worker" AND textPayload:"skipped_by_allowlist"' --limit 5`
  and no other job id logs a run.
- No revision logs a refusal: web/worker name the setting on a refused boot; the gateway and
  agenpaz log `SANDBOX_CONFIG_REFUSED: <SETTING(reason)>` (names only). Search for both.
**Rollback:** first deploy: `gcloud --project "$SANDBOX_PROJECT" run services delete <svc> --region us-west1 --quiet`.
Later releases: the script prints and writes (`$SANDBOX_ROLLBACK_DIR`, default
`~/.pivota-sandbox-deploys/$SANDBOX_PROJECT/*.rollback`) one
`update-traffic --to-revisions <previous>=100` line per service; or deploy the previous tag.

## Step 12. Check through the LB before any A record exists

```bash
IP=$(gcloud --project "$SANDBOX_PROJECT" compute addresses describe sandbox-lb-ip --global --format='value(address)')
curl -sS --resolve sandbox.mcp.pivota.cc:443:$IP https://sandbox.mcp.pivota.cc/health | head -c 400; echo
curl -sS -o /dev/null -w '%{http_code}\n' --resolve sandbox.agenpaz.com:443:$IP https://sandbox.agenpaz.com/health
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' -H 'Host: anything.example' http://$IP/   # 301, never a backend
```
Expect: gateway `/health` 200 with the sandbox marker and the release commit; agenpaz answers on
its host; an unknown host gets a redirect. A call with an `ak_live_` key is refused.

Then pin what is answering: the environment label, the release's gateway commit and the serving
revision. `/version` is keyless. `env` comes from the gateway's own platform label
(PIVOTA-Agent `src/config/platform.js`), so it reads `sandbox` only when the gateway really runs
under `PIVOTA_ENV=sandbox`; any other value, or a missing field, fails the step:
```bash
GW_SHA=$(git -C <PIVOTA-Agent checkout> ls-remote origin "refs/tags/$REL^{}" | cut -f1)
GW_REV=$(gcloud --project "$SANDBOX_PROJECT" run services describe gateway --region us-west1 \
  --format=json | python3 -c 'import json,sys; t=[x for x in json.load(sys.stdin)["status"]["traffic"] if x.get("percent")==100]; print(t[0]["revisionName"] if t else "")')
curl -sS --resolve sandbox.mcp.pivota.cc:443:$IP https://sandbox.mcp.pivota.cc/version \
  | GW_SHA="$GW_SHA" GW_REV="$GW_REV" python3 -c '
import json, os, sys
v = json.load(sys.stdin)
want = {"env": "sandbox", "full_sha": os.environ["GW_SHA"], "deployment_id": os.environ["GW_REV"]}
bad = {k: v.get(k) for k, w in want.items() if not w or v.get(k) != w}
print("version OK: env=sandbox, release commit and serving revision match" if not bad else f"version MISMATCH: {bad}")
sys.exit(1 if bad else 0)'
```
Expect `version OK`. A mismatch on `env` means the gateway is not running as the sandbox (stop: do
not add A records). A mismatch on `full_sha` or `deployment_id` means another revision is serving
than the one step 11 promoted. `env` needs a gateway release that includes the sandbox label
(sandbox-gateway's 31e551b86, and 2f64123a9 for `env` in every environment); an older gateway
leaves it out and fails here, by design.

**12b. One Reap sandbox call from the sandbox's egress IP (own go: it creates a job).** The checks
above never reach Reap. This one does, before any partner exists: one read-only product search,
run in the sandbox VPC, so it leaves through Cloud NAT on the step-2 IP, exactly as the worker's
calls will. It runs as `sa-worker` (it holds `REAP_API_KEY`), with the worker profile as env
(`/tmp/sbx-env` rendered as in step 14a), and prints one line: never the key, never the response
body.
```bash
REACH='import asyncio
from services import reap_agentic_client as rc
assert rc.is_sandbox_base_url(), "REAP_API_BASE_URL is not exactly a Reap sandbox host"
r = asyncio.run(rc.search_products(query="t-shirt", limit=1))
print("reap_reach", "ok" if r.ok else "refused", r.status, r.error or "")
raise SystemExit(0 if r.ok else 1)'
gcloud --project "$SANDBOX_PROJECT" run jobs deploy sandbox-reap-reach --image "$BE_IMG" $VPC_FLAGS \
  --service-account "sa-worker@$SANDBOX_PROJECT.iam.gserviceaccount.com" \
  --env-vars-file /tmp/sbx-env/worker.yaml --set-secrets REAP_API_KEY=REAP_API_KEY:latest \
  --command python --args="^@@^-c@@$REACH" --task-timeout 120s
gcloud --project "$SANDBOX_PROJECT" run jobs execute sandbox-reap-reach --region us-west1 --wait
```
Expect `reap_reach ok 200`, read from the execution's logs. `refused 401` or `403` with the key
in place is the source-IP signal (Q1): stop before step 13 and ask Reap to allow the egress IP.
Any other `refused` (`transport_error:...`, `reap_status_5xx`) means Reap is unreachable from the
sandbox, so check NAT first.
**Rollback:** the checks above are read-only. 12b leaves one job:
`gcloud --project "$SANDBOX_PROJECT" run jobs delete sandbox-reap-reach --region us-west1 --quiet`.

## Step 13. DNS round 2: the A records

Only after step 12 passes and both certs are ACTIVE. Owner adds:
```
sandbox.mcp.pivota.cc  A  <sandbox-lb-ip>
sandbox.agenpaz.com    A  <sandbox-lb-ip>
```
**Check:** `dig +short A <host>` on the authoritative servers and 1.1.1.1 shows the LB IP; repeat
step 12's curls without `--resolve`.
**Rollback:** delete the two A records (the hosts go dark; nothing else changes).

## Step 14. The gateway's service agent, then partners, by hand (v0)

v0 has no identity sync (design step 7 is v1). Every write here is to the **sandbox** DB only, as
a one-off job reading `DATABASE_URL` from the sandbox project, never from prod.

**14a. The gateway's own service agent** (sandbox-backend's
`scripts/sandbox/register_gateway_service_agent.py`). It registers the hash of the minted
`PIVOTA_API_KEY` under exactly `agent_388a63cd97b4fb54` (`config.sh` `SANDBOX_GATEWAY_AGENT_ID`),
the id web/worker already list in `ISSUING_AGENT_EXCLUDED_AGENT_IDS` (links never credited to the
gateway) and `ISSUING_ASSERTION_VOUCHER_AGENT_IDS` (its signed header may name the partner). The
script refuses any id not in both lists, so with this profile it accepts that id and nothing
else. It reads the key in-process from the sandbox's Secret Manager, only after its environment
and id checks pass, accepts only `ak_test_<64 hex>`, and prints one JSON line with no secret. It
needs the full web profile as env (the sandbox guard runs first):
```bash
python3 infra/gcp/sandbox/check_profile.py --render /tmp/sbx-env --web-url "https://web-<PN>.us-west1.run.app" \
  --gateway-host sandbox.mcp.pivota.cc --agenpaz-host sandbox.agenpaz.com --sandbox-project "$SANDBOX_PROJECT"
gcloud --project "$SANDBOX_PROJECT" run jobs deploy sandbox-register-gateway-agent --image "$BE_IMG" $VPC_FLAGS \
  --service-account "sa-sandbox-ops@$SANDBOX_PROJECT.iam.gserviceaccount.com" \
  --env-vars-file /tmp/sbx-env/web.yaml --set-secrets DATABASE_URL=DATABASE_URL:latest \
  --command python --args=-m,scripts.sandbox.register_gateway_service_agent,--agent-id,agent_388a63cd97b4fb54
gcloud --project "$SANDBOX_PROJECT" run jobs execute sandbox-register-gateway-agent --region us-west1 --wait   # dry run: DRY_RUN line
```
Then, on its own go, the same job with `--apply` appended to `--args`, executed again.
**Check:** the `--apply` run prints `DONE` (exit 0). A second `--apply` is a no-op. A call through
`https://sandbox.mcp.pivota.cc` that needs the gateway's own key now authenticates.
**Rollback:** deactivate the `api_keys` row for that agent in the sandbox DB (a one-off, own go).

**14b. Partners.** Per partner: create the agent and its `ak_test_` key (hash stored), and register
its buyer-token issuer for the sandbox. Our own partner-style driver is the first (design step
10). The sandbox backend accepts only `ak_test_` keys (sandbox-backend, 04145cd09).

## Step 15. Proof run (design step 10)

The frozen partner-style driver (search_catalog → get_product → get_offers → start_purchase →
AgenPaz page → get_order) against `https://sandbox.mcp.pivota.cc`, pinned to the sandbox's
`/version` commit and revision. Expect `completed` with no store order id, every answer marked
sandbox. Then the guides get the endpoint.

---

## Steady state: shipping a new sandbox release

A merge to main never touches the sandbox. To ship: pick a backend commit and a gateway commit on
main that have images, cut `sandbox-rN` in both repos (step 9), dry run, then deploy (step 11) on
a controller go. Rollback = the printed `update-traffic` lines, or redeploy `sandbox-r(N-1)`.

## Cost estimate (us-west1 list prices, per month)

| item | sizing | ≈ USD/month |
|---|---|---|
| Cloud Run `web` | 1 vCPU, 2 GiB, min 1, CPU always allocated | 58 |
| Cloud Run `worker` | 1 vCPU, 1 GiB, min=max 1, CPU always allocated | 53 |
| Cloud Run `gateway` | 1 vCPU, 2 GiB, min 1, CPU always allocated | 58 |
| Cloud Run `agenpaz` | 1 vCPU, 1 GiB, min 0, request-billed | 0-2 |
| Cloud SQL | db-g1-small + 20 GB SSD + 7 backups | 31 |
| Load balancer | 2 forwarding rules (one charge for the first 5) | 18 |
| Cloud NAT + reserved egress IP | low traffic | 5-7 |
| Secret Manager, GCS drop (7-day), Scheduler, import job, logging (within free 50 GiB) | | 2-4 |
| **total** | | **≈ 225-230** |

Cheaper: `web` and `gateway` at min 0 saves about 115 (≈ 115/month total) at the price of 10-20 s
cold starts on a partner's first call. Dearer: Memorystore basic 1 GB +36; `db-custom-1-3840`
(staging's tier) +25. The billing account carries the Google for Startups credit (infra/gcp/README.md).
The prod-side export job adds well under 1/month to pivota-prod.

## Open questions

**Q1. Reap key: decided (2026-11-26, the owner).** The sandbox reuses the Reap sandbox key that
production frees when it moves to the real production key. No second key is requested from Reap.
- **Order** (step 4): the key moves only after production is confirmed working on the live key.
  The owner pastes it into the sandbox's `REAP_API_KEY` by hand. Then, on its own owner +
  controller go, its version in `pivota-prod`'s Secret Manager is disabled by number, so the
  sandbox holds the only copy outside staging. Nobody reads or copies the value out of prod's
  secrets.
- **Shared with staging, if it is the same key.** Then staging and partner sandbox traffic share
  one key, its logs at Reap and its rate limits, and revoking it cuts off both. That is accepted
  at v0 volume. A second key becomes the option only if the two need separating later.
- **Source-IP allowlist:** nothing is asked of Reap up front. Step 12b makes one Reap sandbox call
  from the sandbox's own egress IP (step 2) before any partner exists; a refusal there
  (`reap_reach refused 401` or `403`) is the signal to ask Reap to allow that IP.
- **Open notes**, for the shared key:
  - (c) rate limits: per key or per account? Either way, staging rehearsals and partner
    sandbox runs draw on the same limit, so a heavy staging run can slow a partner's sandbox
    test. Watch for `429`s in both.
  - (d) quote coverage: do sandbox quotes cover the US stores in our catalog copy, or only
    stores Reap has enabled for this key? A partner's purchase at a store Reap cannot quote
    fails at the quote step. The proof run (step 15) shows it for the stores it uses.
  - (e) the hosted card page host: the key is the same, so the sandbox should get the card page
    host this key gets today (`sandbox.collect.prava.space` on staging). The proof run (step 15)
    confirms it.
Reap's sandbox completes checkouts by polling, so the sandbox needs no callback URL.

**Q2. Cost:** ≈ 225-230/month as sized above; ≈ 115 with cold starts. Owner to pick.

**Q3. Catalog copy path.** Defined here: bucket, layout, the two grants (steps 7-8,
`setup_catalog_drop.sh`). Recommendation for sandbox-backend's export: give the export job a
**read-only Postgres role** in prod (`SELECT` on the catalog tables only, its own secret in
pivota-prod), so even the exporter cannot read orders, buyers or keys. The sandbox never holds any
prod credential either way. Uploads must not rely on reading the destination (create-only grant):
use unique object names, and no `--no-clobber`-style existence check. The schema dump (step 10a) reads every
table's definition, and `pg_dump` locks each table it dumps, which a SELECT-on-catalog-only role
may not be allowed to do. So the read-only role either covers the dump too, or the dump runs under
its own dedicated role (decided: step 10a-role).

**Q3 decided (2026-11-25, the owner, in the controller's session): the dedicated hardened
schema-dump role** (step 10a-role), not prod's full `DATABASE_URL`; the catalog export keeps its
own catalog-only role.

**Q4. Internal keys.** The brief says the sandbox's Secret Manager holds only the Reap sandbox key
and its own DB credentials. The services also need 18 internal shared keys between
gateway, web and the AgenPaz page (introspect, link signing, consent, checkout tokens...).
Proposal: these are **minted inside the sandbox** at step 4 (labelled `origin=minted-in-sandbox`),
so no prod or staging secret is ever copied, which is the rule's purpose. Owner to confirm.

**Q5. Project id.** See Q13.

**Q6. AgenPaz sandbox host.** Proposed `sandbox.agenpaz.com` (obviously not a real-money page,
and apart from `pay.agenpaz.com`). The host is exclusive: it serves the AgenPaz page and nothing
else. Change `AGENPAZ_HOST` in `config.sh` before step 6 if another name is wanted.

**Q7. Search quality.** With no LLM credentials, query embeddings are off
(`FIND_PRODUCTS_MULTI_VECTOR_ENABLED=false`) and sandbox search is lexical only, so its ranking
can differ from prod's. Option: enable Vertex AI in pivota-sandbox and let the gateway use its own
service account (ADC, no secret); small cost; needs the gateway to point `GOOGLE_CLOUD_PROJECT` at
the sandbox. Not in v0 unless the owner wants it.

**Q8. Network-level "never fetch a store".** v0 enforces it with flags, the profile guard and
the startup check. A hard guarantee would be an egress firewall policy allowing only the Reap
sandbox API, its card page and Google APIs (Cloud NGFW Standard FQDN rules on `sandbox-vpc`).
Extra cost and one more moving part; recommended for v1.

**Q9. Boot checks and `PIVOTA_ENV=sandbox`.** web/worker cannot boot until sandbox-backend's
change ships in a release (config/platform.py on main knows no `sandbox`). Both boot checks key off
`PIVOTA_ENV` alone, so the profile carries no extra switch for them (ENV_PROFILE.md).

**Q10. Gateway's own agent keys.** `PIVOTA_API_KEY` is mounted (a minted `ak_test_` key whose hash is
registered at step 14): the gateway's REAL mode and its MCP loopback need it. `AGENT_API_KEY` and
`DISCOVERY_PRODUCTS_SEARCH_API_KEY` are not mounted. If the proof run finds a call that needs one,
it is minted in the sandbox in the same format, never copied.

**Q11. Optional edge protection.** No Cloud Armor policy in v0 (the gateway rate-limits per key).
Adding the prod-style policy to `sandbox-bes-gateway` is a small follow-up if abuse shows up.

**Q12. Legal links on the consent screen. Ruled by sandbox-backend (2026-11-25).** The buyer
accepts `https://pivota.cc/terms` and `https://merchant.pivota.cc/privacy` on the AgenPaz page
(web's `AGENPAS_TERMS_URL` / `AGENPAS_PRIVACY_URL`; the page's `AGENPAS_LEGAL_HOSTS`). The backend
guard will allow exactly those two (setting, value) pairs: the backend only stores and shows them,
never fetches them, and a sandbox copy could drift from the legal text. Any other path, query or
setting naming them is still refused. The exemption is sandbox-backend's bd2663741 (local, with the controller); before step 9,
confirm it is in the release.

**Q13. Is the project id `pivota-sandbox` free? Not knowable without creating; the owner picks.**
Checked read-only on 2026-11-25 from the org-admin account:
- `gcloud projects describe pivota-sandbox` → PERMISSION_DENIED ("or it may not exist"). A random
  id that cannot exist (`pivota-nx-<8 hex>-zz`) gets the identical answer, so the error carries no
  information either way: GCP gives the same answer for "absent" and "someone else's", by design.
- The id is **not in our org**. The account holds `resourcemanager.organizationAdmin` and sees all
  four org projects (pivota-prod, pivota-staging, pivota-shared and one auto-named project); no
  `pivota-sandbox*`, active or pending deletion. `gcloud alpha projects search` finds nothing.
- So it is either free, or held by a project outside our org. Only `projects create` can tell,
  and a refused create creates nothing.

Options for the owner:
1. **Keep `pivota-sandbox`** (recommended): step 1 is the test. If it is taken, step 1 fails
   cleanly, the owner picks a new id, and the change is one line in `config.sh`.
2. **Pick a more specific id now** (e.g. `pivota-partner-sandbox`, `pivota-sbx-us`), which is less
   likely to be taken but is still only confirmed by step 1.

Either way, a rename is `SANDBOX_PROJECT` in `config.sh` and nothing else: the scripts, the derived
bucket name, `verify_sandbox.sh` and this runbook's commands all read it, and
`tests/test_sandbox_infra.py` runs every dry run under a different id and fails if
`pivota-sandbox` appears in any planned command. Outside this directory, a rename also needs
sandbox-backend's `config/sandbox_identity.py` (the backend's single source for the sandbox
project and its private DB range; its guard refuses any other) and the drop URIs in its jobs
(`gs://<id>-catalog-drop/catalog`, `.../schema`), and sandbox-gateway's GOOGLE_CLOUD_PROJECT rule
if it ever names the id. Tell both sessions before the change.

**Q14. Partner webhooks.** `WEBHOOK_DELIVERY_ENABLED=false`: the sandbox sends no outbound webhook.
Partners read order state with `get_order`. Turning webhooks on means calling partner-registered
URLs from the sandbox; owner's call, not in v0 unless asked.

**Q15. Who creates the backend schema on the empty sandbox DB? Decided 2026-11-25 by the owner:
option A, a schema-only dump of prod** (step 10a). The rejected option B rebuilt it from the
repo's own DDL with no prod read, but drift in tables that are not copied (orders, ledgers) would
only show up when a purchase failed. A adds one prod read, inside the export's existing gated
one-off and the same go.

**Q16. Backend URLs on the gateway host: verify or route before turning a feature on.** The web
and worker profiles set `PUBLIC_API_BASE_URL` to the sandbox gateway host, which the backend's
sandbox guard requires: unset, it resolves to prod's API host. The sandbox backend has no public
host of its own (the LB sends that host only to Cloud Run `gateway`). So the managed-inbox
webhook receiver URLs and the MCP OAuth login redirect that the backend hands out name
`https://sandbox.mcp.pivota.cc`, and they work only if the gateway routes those paths on to the
sandbox `web`. Both features are off in v0 (`WEBHOOK_DELIVERY_ENABLED=false`, Q14), so this does
not block the first boot. Before either is turned on, verify those routes on the sandbox gateway,
or add them, as part of that change and its own go.
