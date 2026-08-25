# Operating on production — the GCP equivalents of every `railway` command

**Production is Cloud Run in `pivota-prod`, region `us-west1`, since the 2026-08-22 cutover.
Railway is the ROLLBACK.** A `railway variables --set` today changes the platform nobody is served
from: the incident continues, the dial you turned reads as turned, and the evidence says you fixed
it.

That is not hypothetical. The rate-limit kill switch, the partner-settlement dry run and the Stripe
webhook-secret check were all still written against Railway after the cutover, and the settlement
runbook's own invariant warns that a missed flag risks **double-paying**.

Constants used throughout: `--project pivota-prod --region us-west1`. The backend service is `web`;
the gateway is `gateway`; the queue worker is `worker`.

---

## Read logs

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="web"' \
  --project pivota-prod --limit 2000 --format='value(textPayload)' --freshness=1h
```

`--freshness` is required in practice: without it the read starts from the beginning of retention
and the command appears to hang. `--limit` is a hard cap, not a page size.

Request logs are structured rather than text, so `httpRequest` is a separate field:

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="web" AND httpRequest.requestUrl!=""' \
  --project pivota-prod --limit 500 --format='value(httpRequest.requestUrl)' --freshness=1h \
  | sed -E 's|https?://[^/]+||; s|\?.*||' | sort | uniq -c | sort -rn
```

Scope to one revision when you are comparing before/after a deploy — otherwise a rollout mixes two
builds into one count:

```
AND resource.labels.revision_name="web-00030-m9h"
```

**Cloud Logging ingestion lag is unbounded.** Reading immediately after an event returns nothing,
which looks exactly like "the event never happened" — that stranded a healthy revision at 0% traffic
on 2026-08-25. Any fixed window is a guess and every guess eventually loses, so never take a VERDICT
from a log scrape: use the exit code, and read logs only for detail, behind a retry. `probe_health()`
in `infra/gcp/deploy_backend.sh` is the worked example of both halves.

## Read one env var

```bash
gcloud run services describe web --project pivota-prod --region us-west1 --format=json \
  | python3 -c 'import json,sys; e=json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0].get("env",[]); print([x for x in e if x["name"]=="PARTNER_REV_SHARE_USE_V2"] or "ABSENT")'
```

A secret-backed variable shows a `valueFrom.secretKeyRef`, not a value — that tells you the name
and version it resolves, which is usually what you actually want to check. **Do not print secret
values into a terminal or a ticket.**

## Set an env var (the kill-switch shape)

```bash
gcloud run services update web --project pivota-prod --region us-west1 \
  --update-env-vars ANON_RATE_LIMIT_ENABLED=false
```

`--update-env-vars` **merges** the named keys. `--set-env-vars` and `--env-vars-file` **remove every
existing plain env var first** — on `web` that is 192 of its 248 entries. Secret-backed variables
live in a separate flag group and survive, which makes the damage worse rather than better: the
service still boots, still reaches its database, and fails only in whatever depended on the 192.
Reach for `--update-env-vars` unless you have specifically decided otherwise.

This creates a new revision and starts new processes, which is what makes a value read at app
construction actually take effect. It also shifts traffic once the revision is healthy, so it is a
deploy: expect the usual cold-start cost on the first requests.

**Verify with the REVISION NAME, never with `/version`.** The image is unchanged by an env-only
update, so the build SHA is identical before and after whether or not the change applied:

```bash
gcloud run services describe web --project pivota-prod --region us-west1 \
  --format='value(status.latestReadyRevisionName)'
```

Traffic follows the new revision only while the service is on `latestRevision: true` — which all
three services are today. A service someone has pinned to a specific revision will accept the
update and keep serving the old one.

To remove one: `--remove-env-vars NAME`.

## Run a one-off script in the production environment

There is **no** equivalent of `railway run` or `railway ssh`. Cloud Run gives you no way to attach
to a running instance — the image does ship `sh`, and `--command sh` works in a job, but there is no
live process to enter. The pattern is a throwaway Cloud Run **job** on the same image, which
`infra/gcp/deploy_backend.sh` already uses for its in-VPC health probe:

```bash
JOB="oneoff-$$-$RANDOM"
gcloud run jobs create "$JOB" --project pivota-prod --region us-west1 \
  --image us-west1-docker.pkg.dev/pivota-shared/pivota/backend:latest \
  --service-account sa-worker@pivota-prod.iam.gserviceaccount.com \
  --network default --subnet default --vpc-egress all-traffic \
  --set-secrets DATABASE_URL=DATABASE_URL:latest \
  --max-retries 0 --task-timeout 600s \
  --command python --args=scripts/partner_settlement_dry_run.py
gcloud run jobs execute "$JOB" --project pivota-prod --region us-west1 --wait || {
  echo "job FAILED - do not read the log for a verdict, the exit code already gave you one" >&2; }
for i in 1 2 3 4 5 6; do
  OUT=$(gcloud logging read "resource.labels.job_name=\"$JOB\"" --project pivota-prod \
    --limit 200 --format='value(textPayload)' --freshness=10m)
  [ -n "$OUT" ] && break
  sleep 5
done
printf '%s\n' "$OUT"
gcloud run jobs delete "$JOB" --project pivota-prod --region us-west1 --quiet
```

`scripts/ops/run_oneoff_job.sh` wraps exactly this, with the three footguns below
already handled, and is what the operator scripts' own `--help` now points at:

```bash
scripts/ops/run_oneoff_job.sh scripts/partner_settlement_dry_run.py --json
```

It exits with the job's own exit status, deletes the job on every path including
Ctrl-C, and picks an `--args` delimiter that does not occur in the payload.
Reach for the raw form above when you need to change something it does not expose
(`SECRETS`, `IMAGE`, `TASK_TIMEOUT`, `SERVICE_ACCOUNT` and `JOB_PREFIX` are
environment overrides).

Three things that are easy to get wrong:

- **Secrets are not inherited.** A job mounts only what you pass. A script that reads
  `DATABASE_URL` gets nothing unless you `--set-secrets` it, and will usually fail in a way that
  looks like a database outage rather than a missing mount. Most secret names carry an `env-`
  prefix; the bare env-var secrets in `pivota-prod` are `DATABASE_URL`, `DATABASE_URL_NOVERIFY`, `REDIS_URL`,
  `PCI_KB_DATABASE_URL`, `PCI_KB_DATABASE_URL_NOVERIFY`, `STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY`,
  `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET` — eight, not the three named in #1866,
  and `GOOGLE_OAUTH_CLIENT_SECRET` exists under BOTH spellings while `web` mounts the `env-` one.
  Read the `secretKeyRef` off the running service rather than trusting any list, this one included.
- **`--args` splits on commas.** A Python one-liner containing a comma is shredded into separate
  argv entries. Use the alternate delimiter form: `--args="^|^-c|import x,y"`. The delimiter you
  pick **must not appear anywhere in the values** — `|` is a poor choice for Python that uses dict
  merge, regex alternation or bitwise or, and it fails by silently splitting rather than erroring.
  Pick a character the payload cannot contain.
- **Delete the job when you are done.** A left-behind job is a standing execution surface with a
  service account attached.

## Deploy

Do not deploy by hand. `infra/gcp/deploy_backend.sh` and `deploy_gateway.sh` carry the
candidate/`--no-traffic`/health-gate/promote flow and `sweep_stale_tags()` — a tagged 0%-traffic
revision at `minScale >= 1` stays alive forever on its boot-time secret versions, and hand deploys
are how three of those ended up running old code against live secrets on 2026-08-25.

```bash
CONFIG=preserve infra/gcp/deploy_backend.sh prod <sha>
```

## Still on Railway, deliberately

Railway remains the rollback until it is decommissioned, so `railway` commands are still the right
thing when you are **operating the rollback on purpose** — verifying it can still serve, or
comparing its state against production. They are the wrong thing when you mean "production".
