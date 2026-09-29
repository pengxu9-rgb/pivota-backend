# SCHEDULER_JOB_ALLOWLIST — run only named jobs on a worker

`AUDIT_WORKER_ENABLED=true` starts **every** job in `services/audit_scheduler.py` (46 today), and
several of them act on the outside world: merchant crawls and audits, store disconnects, catalog
import and sync drains, merchant order-sync writes, PSP auto-finalize, Stripe Connect transfers,
alerts. `SCHEDULER_JOB_ALLOWLIST` narrows a worker to the jobs it names. Code:
`services/scheduler_job_allowlist.py`.

## Semantics

| Value | Meaning |
|---|---|
| unset, empty, or whitespace only | **no filter** — exactly the behaviour without this variable |
| `a,b` | only `a` and `b` start; everything else is skipped and reported |
| set but no ids (`,` or ` , `) | a filter that allows **nothing** (fail closed, not "no filter") |

- Comma-separated. Each entry is trimmed; empty entries are dropped.
- Matching is **exact and case-sensitive**. `REAP_AGENTIC_PURCHASE_POLL` does not match, and
  neither does a prefix such as `reap_agentic`.
- An id that matches nothing is logged once per scheduler boot at WARNING through the `pivota`
  logger (it reaches Cloud Logging) and listed on `/__scheduler_health` as
  `job_allowlist_unknown_ids`. The worker still boots; the typo simply starts nothing.
- It does **not** turn anything on. A process with the worker gate off (`AUDIT_WORKER_ENABLED`
  false, or a staging/preview service name) still registers no scheduler jobs. Jobs with their own
  dial (`REAP_AGENTIC_ENABLED`, `PAYMENT_RECONCILE_SWEEP_ENABLED`, …) still need it.
- Set it on a dedicated worker only. It also gates the boot-time process loops below, which run
  on **every** process including `web`; on `web` it would stop that process's webhook retries.

## Ids

The ids are the ones `_add_job` registers under — the `id` field in `/__scheduler_health`'s
`jobs` list on an unfiltered worker — plus three process-level loops started at boot outside
APScheduler (`main.startup_event`):

| Process loop id | What it does |
|---|---|
| `agent_webhook_retry_worker` | re-delivers failed outbound agent webhooks |
| `merchant_webhook_retry_worker` | re-delivers failed outbound merchant webhooks |
| `photo_cleanup_loop` | deletes expired photo uploads (also needs `PHOTO_CLEANUP_LOOP_ENABLED`) |

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

`GET /__scheduler_health` gains three fields **only when the variable is set**:

```json
"job_allowlist": ["reap_agentic_purchase_poll"],
"skipped_by_allowlist": ["agent_card_revocation_sweep", "agent_webhook_retry_worker", "..."],
"job_allowlist_unknown_ids": []
```

`jobs` / `job_count` then list only what registered. The boot also writes one WARNING line:
`audit_scheduler: SCHEDULER_JOB_ALLOWLIST ACTIVE allowlist=[...] worker_enabled=True registered=[...] skipped_by_allowlist=N`.

## Recipe: staging worker for the Reap partner demo

The Reap purchase poller is self-contained: its run is three ledger sweeps plus one `advance`
step per claimed row (`jobs/reap_agentic_purchase_poll.py`); it consumes no queue that another
job fills, so it needs no other job.

```
AUDIT_WORKER_ENABLED=true
SCHEDULER_JOB_ALLOWLIST=reap_agentic_purchase_poll
```

plus the rail's own settings from `docs/runbooks/reap_agentic_purchase.md` ("Arming it"):
`REAP_API_BASE_URL`, `REAP_API_KEY`, then `REAP_AGENTIC_ENABLED=1`. Pre-flight as that runbook
says: confirm the worker's `DATABASE_URL` host is staging's own instance before arming.

Verify after the deploy: `/__scheduler_health` shows `job_count: 1`, `jobs[0].id ==
"reap_agentic_purchase_poll"`, `job_allowlist_unknown_ids: []`, and `skipped_by_allowlist`
holding the other 45 scheduler jobs plus the two webhook retry workers (47 ids; 48 if
`PHOTO_CLEANUP_LOOP_ENABLED` is on — a loop whose own flag is off never gets as far as the
allowlist, so it is not listed).

To undo, remove the variable (or set it empty) and redeploy; there is no runtime toggle.
