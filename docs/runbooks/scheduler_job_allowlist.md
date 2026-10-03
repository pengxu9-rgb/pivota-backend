# SCHEDULER_JOB_ALLOWLIST — run only named jobs on a worker

`AUDIT_WORKER_ENABLED=true` starts **every** job in `services/audit_scheduler.py` (46 today), and
several of them act on the outside world: merchant crawls and audits, store disconnects, catalog
import and sync drains, merchant order-sync writes, PSP auto-finalize, Stripe Connect transfers,
alerts. `SCHEDULER_JOB_ALLOWLIST` narrows a worker to the jobs it names. Code:
`services/scheduler_job_allowlist.py`.

> **Staging is a restored copy of production**, and some money-path dials are armed there
> (`payment_reconcile_tick` with `PAYMENT_RECONCILE_SWEEP_ENABLED=1`, `CHECKOUT_MODE=real`).
> A staging worker that runs "everything" is a staging worker that auto-finalizes payments
> against whatever credentials that copy carries. Every step below is written so that no
> intermediate state runs everything.

## Semantics

| Value | Production | Outside production (`platform_env()` ≠ production) |
|---|---|---|
| unset, empty, or whitespace only | **no filter** (today's behaviour) | no filter **unless** `AUDIT_WORKER_ENABLED` is explicitly true — then **allow nothing**, plus one ERROR |
| `a,b` | only `a` and `b` start | same |
| set but no ids (`,` or ` , `) | allows **nothing** | same |
| `*` (anywhere in the list) | every job | every job — the **only** way to run everything on a non-production worker |

- Comma-separated. Each entry is trimmed; empty entries are dropped.
- Matching is **exact and case-sensitive**. `REAP_AGENTIC_PURCHASE_POLL` does not match, and
  neither does a prefix such as `reap_agentic`.
- An id that matches nothing is logged once per scheduler boot at WARNING through the `pivota`
  logger (it reaches Cloud Logging) and listed on `/__scheduler_health` as
  `job_allowlist_unknown_ids`. The worker still boots; the typo simply starts nothing.
- It does **not** turn anything on. A process with the worker gate off (`AUDIT_WORKER_ENABLED`
  false, or a staging/preview service) still registers no scheduler jobs. Jobs with their own
  dial (`REAP_AGENTIC_ENABLED`, `PAYMENT_RECONCILE_SWEEP_ENABLED`, …) still need it.
- Set it on a dedicated worker only. It also gates the boot-time process loops below, which run
  on **every** process including `web`; on `web` it would stop that process's webhook retries.

### Why unset fails closed outside production

The variable disappears silently in more than one way, and on staging the fail-open result is
every job at once:

- `infra/gcp/deploy_worker.sh` with `CONFIG=apply WORKERS=true` replaces the **whole** env via
  `--env-vars-file` (only keys in the ported `env.<env>.yaml` survive) and sets
  `AUDIT_WORKER_ENABLED=true`;
- gcloud can turn `SCHEDULER_JOB_ALLOWLIST=,` into an empty value;
- the Cloud Run console can clear the field.

So on a non-production service with `AUDIT_WORKER_ENABLED` **explicitly** true (`1/true/yes/on`),
an unset or blank allowlist starts **no** scheduler job and **no** boot loop, and logs once:
`scheduler_job_allowlist: AUDIT_WORKER_ENABLED is explicitly true on a NON-PRODUCTION service …`.
`/__scheduler_health` then shows `"job_allowlist": []` and
`"job_allowlist_source": "fail_closed_non_production"`. Local development counts as
non-production: a developer who sets `AUDIT_WORKER_ENABLED=true` locally sets
`SCHEDULER_JOB_ALLOWLIST=*` too (leaving the flag unset keeps today's local default, all jobs).

Unchanged by this rule: production (any flag value), and any service whose flag is unset or false
— staging `web` and today's staging `worker` (`AUDIT_WORKER_ENABLED=false`) behave exactly as
before, webhook loops included.

## Ids

The ids are the ones `_add_job` registers under — the `id` field in `/__scheduler_health`'s
`jobs` list on an unfiltered worker — plus three process-level loops started at boot outside
APScheduler (`main.startup_event`):

| Process loop id | What it does |
|---|---|
| `agent_webhook_retry_worker` | re-delivers failed outbound agent webhooks |
| `merchant_webhook_retry_worker` | re-delivers failed outbound merchant webhooks |
| `photo_cleanup_loop` | deletes expired photo uploads (also needs `PHOTO_CLEANUP_LOOP_ENABLED`) |

The two webhook retry loops have a SECOND, independent gate: `WEBHOOK_RETRY_DELIVERY_ENABLED`
(`services/webhook_retry_delivery_gate.py`, #2445), which decides whether a started loop delivers
anything (by default only in production). The allowlist decides whether the loop STARTS; the
delivery gate decides whether it DELIVERS. A retry goes out only when both allow it, and neither
overrides the other: listing a loop does not opt it into delivery on staging, and
`WEBHOOK_RETRY_DELIVERY_ENABLED=true` does not start a loop the allowlist excludes
(`tests/test_webhook_retry_loop_allowlist_composition.py`). The inline `process_due_retries` call
in `list_deliveries` is request-triggered, so only the delivery gate applies to it.

## What it covers

Everything a worker process starts on its own:

- every `_add_job` registration in `start_scheduler`, including the ones scheduled to fire shortly
  after boot (`nightly_index_health_catch_up`, first tick ~90s after start) and the ones registered
  paused (`invoice_generation_monthly`, `partner_settlement_monthly`);
- `POST /admin/scheduler/restart` (re-runs `start_scheduler`, so the same filter applies);
- `POST /admin/scheduler/jobs/{id}/run-now` — a skipped job is never wrapped, so it answers
  `404 job_not_registered` rather than running;
- `POST /admin/scheduler/jobs/{id}/resume|pause` — acts only on registered jobs (`404 job_not_found`);
- the per-run deadline watchdog and zombie-connection terminate in
  `services/scheduler_job_runner.py`, and job-internal advisory locks — these exist only for runs of
  registered jobs;
- the three process loops above.

Not covered, deliberately: the database reconnect supervisor (`main.app_lifespan`; DB-only
repair of this process's own pool) and work started by an incoming request (e.g. the agent
decision-event flush), which a worker receives no traffic for.

## Checking it

`GET /__scheduler_health` gains these fields **only when a filter is in force** (the variable is
set, or the non-production fail-closed rule applies):

```json
"job_allowlist": ["reap_agentic_purchase_poll"],
"job_allowlist_source": "env",
"skipped_by_allowlist": ["agent_card_revocation_sweep", "agent_webhook_retry_worker", "..."],
"job_allowlist_unknown_ids": []
```

`job_allowlist_source` is `env`, `all` (the list contains `*`; `job_allowlist` is `["*"]`) or
`fail_closed_non_production`. If reading the allowlist itself fails, the page shows
`"job_allowlist_error": "<ExceptionType>"` rather than nothing — an absent key means unset.

`jobs` / `job_count` then list only what registered. The boot also writes one WARNING line (for `*` too, as `allowlist=['*']`, and for the fail-closed case as `allowlist=[]`):
`audit_scheduler: SCHEDULER_JOB_ALLOWLIST ACTIVE allowlist=[...] worker_enabled=True registered=[...] skipped_by_allowlist=N`.

## Setting it with gcloud: commas

`gcloud run services update --update-env-vars` splits its argument **on commas** into
`KEY=VALUE` pairs, so `--update-env-vars SCHEDULER_JOB_ALLOWLIST=a,b` leaves `b` with no `=`.
gcloud then **rejects the whole flag** (`Bad syntax for dict arg: [b]`) and **applies nothing** —
no new revision, no variable changed, including any other pair in the same flag. That fails safe,
but it also means the command you thought armed (or disarmed) the worker did not. For any value
with a comma — and whenever you set more than one variable, which is always the case here — use
gcloud's custom-delimiter form: the argument starts with `^<delim>^` and then uses `<delim>`
between pairs:

```
--update-env-vars='^|^SCHEDULER_JOB_ALLOWLIST=a,b|AUDIT_WORKER_ENABLED=true'
```

Pick a delimiter that appears nowhere in the values (`|` is safe for job ids). Read the result back
from the new revision (`gcloud run services describe … --format=yaml(spec.template.spec.containers[0].env)`)
or from `/__scheduler_health` rather than trusting the command line.

## Recipe: staging worker for the Reap partner demo

The Reap purchase poller is self-contained: its run is three ledger sweeps plus one `advance`
step per claimed row (`jobs/reap_agentic_purchase_poll.py`); it consumes no queue that another
job fills, so it needs no other job. The allowlist is `reap_agentic_purchase_poll`.

**Before anything here**, do the mandatory Reap pre-flight in
`docs/runbooks/reap_agentic_purchase.md` ("Staging pre-flight"): the staging database is a
restored copy of production, so non-terminal production purchases and enrollments in it must be
counted and scrubbed (or you STOP) before the rail is armed — **and again after every staging
restore**, which brings the live production rows back — and `REAP_API_BASE_URL` must be
exactly a sandbox host — `sandbox.api.reap.global`, `sg.sandbox.api.reap.global` or
`mx.sandbox.api.reap.global` (outside production the poller refuses any other host anyway).

### Arm the worker: ONE command, both variables together

Two separate `gcloud run services update` calls create an in-between revision. If the flag lands
first, that revision is `AUDIT_WORKER_ENABLED=true` with no allowlist — which on staging now
starts nothing (fail closed), but on any code predating this change starts **all 46 jobs**. Set
both in one revision:

```
gcloud run services update worker --project pivota-staging --region us-west1 \
  --update-env-vars='^|^SCHEDULER_JOB_ALLOWLIST=reap_agentic_purchase_poll|AUDIT_WORKER_ENABLED=true'
```

Only after `/__scheduler_health` on the new revision shows the expected state (below) do you set
the Reap rail's own variables (`REAP_API_BASE_URL`, `REAP_API_KEY`, then `REAP_AGENTIC_ENABLED=1`)
as the Reap runbook describes — also with `--update-env-vars`, which merges and keeps the two
variables above.

**Never deploy this worker with `infra/gcp/deploy_worker.sh CONFIG=apply` while it is armed**:
that rewrites the whole env from the ported file and drops `SCHEDULER_JOB_ALLOWLIST` (the worker
then fails closed and runs nothing, which is safe but not the demo). `CONFIG=preserve` — the
default, and what CI uses — merges and keeps it.

### Verify

`/__scheduler_health` on the new revision: `worker_enabled: true`, `job_count: 1`,
`jobs[0].id == "reap_agentic_purchase_poll"`, `job_allowlist: ["reap_agentic_purchase_poll"]`,
`job_allowlist_source: "env"`, `job_allowlist_unknown_ids: []`, and `skipped_by_allowlist`
holding the other 45 scheduler jobs plus the two webhook retry workers (47 ids; 48 if
`PHOTO_CLEANUP_LOOP_ENABLED` is on — a loop whose own flag is off never reaches the allowlist).
Anything else — in particular `job_count` above 1 — means disarm now (below).

### Undo / disarm

**Turn the worker flag off. Never remove the allowlist while the flag is true.** Removing or
blanking `SCHEDULER_JOB_ALLOWLIST` on a worker whose `AUDIT_WORKER_ENABLED=true` is the "run
everything" configuration: on this code it fails closed only because of the non-production rule,
and on an older image it starts all 46 jobs plus both webhook retry loops.

```
# stop every scheduled job on the staging worker (the demo included)
gcloud run services update worker --project pivota-staging --region us-west1 \
  --update-env-vars AUDIT_WORKER_ENABLED=false
```

To stop all new partner calls and keep the PII sweeps running, set `REAP_AGENTIC_RECONCILE_ENABLED=0`
instead (see the Reap runbook's "Stopping it"). Once `/__scheduler_health` shows
`worker_enabled: false` and `job_count: 0`, the allowlist may be left in place or removed — it is
inert with the flag off.
