# Crawl global pacing: the crawl IP's total rate, its breaker, and its address

Every crawl job leaves from one NAT address (`pivota-crawl-nat`). On 2026-10-16 Cloudflare
challenge-listed it (tm899: 391 `cf-mitigated: challenge` answers, 262 distinct hosts in 60 s) after
nights of 2.5-4.0 origin reads/s from the external-referral refresh. `services/crawl_global_pacer.py`
caps the IP's total rate for the jobs that install it; `services/crawl_ip_throttle.py`'s
`StagedIpThrottleBreaker` stops them when the address is being challenged.

## Which jobs are paced

| Job | Paced | Where |
|---|---|---|
| `external-referral-refresh` (05:15Z) | yes (be#2640) | `run_external_referral_refresh_batch` |
| `external-seed-destination-sweep` (03:15Z live) | yes (be#2641) | `run_destination_sweep`; knobs `EXTERNAL_SEED_DESTINATION_SWEEP_GLOBAL_RPS` / `_MAX_NEW_HOSTS_PER_MINUTE` / `_BUDGET_SECONDS`, same rules and kill switch |
| `jobs.catalog_destination_sweep` | **NO** (no prod job today) | reads the same storefronts per-host-paced only. Do not schedule it (`CATALOG_DESTINATION_SWEEP=true`) before it installs a pacer |
| `scripts/audit_external_seed_destination_liveness.py` | **NO** | an operator audit, per-host-paced only. Do not run it at scale from the crawl egress while the address is under watch |
| every other lane | no global pacer (per-host `crawl_politeness` pacing and, for Shopify hosts, the shared edge pacer still apply) | |

The destination sweep also requests each catalogue on the host spelling its seeds store (`www.` is not
folded away for the request, only for grouping), queues handle-less seeds after every verifiable one,
and raises its coverage alarm on the hosts it actually asked (`hosts_attempted`), not on every host in
the candidate list. Its liveness rules for a final URL:

* a redirect onto a DIFFERENT path matching `/challenge`, `/checkpoint`, `/captcha` or `/cdn-cgi/`,
  or onto a captcha host, is `unverifiable` (never dead). A product answering on its own URL is never a
  wall, whatever its handle (`/products/checkpoint-mesh-bag` stays readable);
* a landing on another host that left `/products/` is `unverifiable`, never dead;
* a landing on another host that keeps a product handle classifies as usual (same handle = a domain
  move, `live`; another handle = `redirected_to_product`), with `cross-host landing on <host>` in the
  observation's note and a count in the summary's `cross_host_landings`.

## Knobs (no knob has an "off" value)

All resolve through `crawl_global_pacer.resolve_setting`: the flag, then the env, then the default.
**0, a negative, NaN, inf or garbage is logged at ERROR and the default is used** (before 2026-10-17,
`GLOBAL_RPS=0` meant unpaced and `int()` truncated a cap of 0.9 to 0, i.e. no cap).

| Knob | Default | Rule |
|---|---|---|
| `--global-rps` / `EXTERNAL_REFERRAL_REFRESH_GLOBAL_RPS` | 0.5 | clamped to [0.05, 1.0] |
| `--max-new-hosts-per-minute` / `EXTERNAL_REFERRAL_REFRESH_MAX_NEW_HOSTS_PER_MINUTE` | 6 | rounded up, at least 1; counts real first-request starts (redirect hops onto a new host included) |
| `--budget-seconds` / `EXTERNAL_REFERRAL_REFRESH_BUDGET_SECONDS` | 3300 | > 0; no request starts at or after it |
| `EXTERNAL_REFERRAL_REFRESH_MIN_BUDGET_REACH` | 0.5 | in (0, 1]; the share of candidates a budget stop must reach to count as `success` |
| `CRAWL_IP_CHALLENGE_TRIP_HOSTS` | 8 | distinct challenged hosts in `CRAWL_IP_CHALLENGE_WINDOW_SECONDS` (120) that trip the cool-down. Healthy nights 10-08..10-15 peaked at 4/3/3/1/2/1; the 10-16 listing night at 360 |
| `CRAWL_IP_CHALLENGE_EXCLUDED_HOSTS` | empty | comma/space list, `www.` folded: hosts whose challenges never count toward a trip |

| `EXTERNAL_REFERRAL_REFRESH_HOST_CHALLENGE_TRIP` | 3 | rounded up, at least 1: a host answering this many Cloudflare challenges in a row (any status) is skipped for the rest of the run, its rows unstamped (`skipped_for_host_challenge`, `host_challenge_skips`). Excluded hosts are skipped too |

Every redirect hop is gated like a first request while a pacer is installed (an httpx request hook,
`redirect_client_kwargs`): it waits for its host's own slot (interval, backoff, Crawl-delay) and a
global slot, and a hop onto a DIFFERENT host must be allowed by that host's robots.txt or it is not
sent (`RobotsDisallowed`). A crawl fetch follows at most 5 hops (robots.txt: 3, global slot only).
With no pacer installed (the interactive route) a hop only gets the hop cap.

**Throughput is host-shaped.** 6 new hosts a minute is ~330 hosts in a 3,300 s run. A queue head of
one row per host therefore reaches ~331 of 2,000 rows (`budget_reach` 0.17, `degraded`, exit 1);
>= 4 rows per host reaches ~0.66. Measure the head before tuning the cap or the floor
(`refresh_head_hosts.py`, a read-only census that copies the job's candidate SQL).

## The kill switch

`CRAWL_GLOBAL_PACER_DISABLED=true`: the exact word `true` (any case). `1`, `yes`, `on`, `false` or
any other non-empty value is UNREADABLE: it is logged at ERROR and the run stays paced, because a
switch whose failure mode is "unpaced" must fail paced. There is no per-lane alias (an
`..._GLOBAL_PACER_ENABLED=false` spelling existed for one commit and is not read). It logs at ERROR on every run, and the run's
summary says `global_pacer_disabled: true`, `global_rps: null`. The pacer is still installed
**unpaced**: no rate and no new-host cap, but the breaker's cool-down and stop and the budget's
deadline still reach every request. Unset it when the incident is over.

## Exit codes, and the late first trip

* 0 `success`.
* 1 `degraded`: includes a **budget stop**. A breaker FIRST trip after ~2,400 s (a 900 s cool-down
  that would end past the 3,300 s budget) ends the run as a budget stop, exit 1, not 3:
  `breaker_trips: 1`, `cooldowns: 1` on the `IP_THROTTLE` line tell it from slow hosts.
* 3 `ip_throttled`: the breaker's SECOND trip.

Rows the pacer refused (in flight at the final trip or at the budget) are **never stamped**: they are
counted in `skipped_for_ip_throttle` / `skipped_for_budget` and lead tomorrow's queue.

## Challenge evidence per host

Since be#2640: a 403 challenge logs `crawl challenge: <host> returned 403 [... cf-mitigated=challenge]`
(a 429 one was already `crawl backoff: ...`), robots.txt answers feed the host backoff and the breaker,
and the `IP_THROTTLE` line carries `ip_throttle_challenge_top_hosts`. Use them to fill
`CRAWL_IP_CHALLENGE_EXCLUDED_HOSTS` from evidence, never by guessing.

## The crawl address: swap and rollback (explicit, never default)

`pivota-crawl-egress-ip` (34.82.199.35) was listed on 2026-10-16; `pivota-crawl-egress-ip-2`
(136.67.250.98) is reserved. Pace every crawler that runs nightly first, then:

```sh
CRAWL_SUBNET_CIDR=<existing cidr> CRAWL_NAT_SWAP_TO=pivota-crawl-egress-ip-2 infra/gcp/setup_crawl_egress.sh prod
# rollback
CRAWL_SUBNET_CIDR=<existing cidr> CRAWL_NAT_SWAP_TO=pivota-crawl-egress-ip   infra/gcp/setup_crawl_egress.sh prod
```

which runs `gcloud compute routers nats update pivota-crawl-nat --router pivota-crawl-router
--region us-west1 --nat-external-ip-pool=<address>`. Without `CRAWL_NAT_SWAP_TO` the script never
touches an existing NAT. Address-bound side effects: `crawl_identity.md` (the registered egress IP)
and `reap_cart_proofs.md` (gate (a)'s 24 h window restarts).

## Scheduling

The destination sweep's definition (`EXTERNAL_SEED_DESTINATION_SWEEP=true`) runs
`--limit 3500 --budget-seconds 3300` with `--max-retries 0` (mkcrawljob's default is 1). On an
EXISTING job the script updates only the image, the args, the retries and the timeout: env, secrets,
cpu and memory are left as they are, and the args keep the live job's retire choice (`--no-retire` or
not, read back from the job) unless `EXTERNAL_SEED_DESTINATION_SWEEP_RETIRE` is set on that run. If the
live args cannot be read, the run refuses (exit 2) rather than guess. Only a CREATE builds the full
definition.

`EXTERNAL_REFERRAL_REFRESH=true infra/gcp/setup_scheduler.sh prod <backend> <gateway>` reconciles the
refresh job with the rollout args `--limit 2000 --budget-seconds 3300 --host-concurrency 4` and
`--max-retries 0`, updating only the image, retries, timeout and args of the existing job (its
hand-set env and secrets are left alone). Without the flag the script does not touch it.
