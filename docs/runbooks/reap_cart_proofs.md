# Reap cart proofs (two daily jobs)

> **⛔ HARD GATE — DO NOT EXECUTE OR `--enable` EITHER JOB, AND DO NOT RUN EITHER WRITER BY HAND
> (`scripts/ops/run_oneoff_job.sh`), IN ANY ENVIRONMENT, until ALL of these hold:**
>
> **(a)** the crawl-IP incident is closed. Shopify's edge has been throttling our single crawl NAT
> IP (34.82.199.35) across all shops since ~00:00Z 2026-09-30. "Closed" means **at least 24 h with
> no crawl-backoff 429 bursts and no IP-throttle breaker trips** on the crawlers already sharing that
> address -- the external-referral refresh, the external-seed destination sweep,
> `tierb-cart-link-eligibility` and `merchant-purchasability-sweep` -- **plus Peng's go**;
>
> **(b)** #2474 (the shared Shopify-edge pacer in `crawl_politeness`) is merged, **both lanes are
> wired through it** (the follow-up PR to #2471), and **`CRAWL_SHOPIFY_EDGE_PACER_ENABLED=true` is
> set on both jobs** (check with `gcloud run jobs describe`).
>
> This includes the dark dry run: a dry run fetches exactly as hard as an apply. Provisioning the
> jobs DARK (step 1) contacts no store and is allowed; executing them (step 2 onward) is not.

The Reap cart-link lane buys through a Shopify cart permalink and refuses any row without a
**fresh storefront proof**. Two writers author those proofs, and two Cloud Run Jobs run them on a
schedule:

| Job | Lane | Writer (the only proof logic) | Writes | Valid for | Schedule (UTC) |
|---|---|---|---|---|---|
| `reap-cart-proof-enrichment` | `enrichment` | `jobs/enrichment_cart_variant_proof.py` (#2464) | `enrichment_cart_variant_proofs`, one row per (product_key, sku_key): `ok` or a recorded refusal | 72 h | 06:41 daily |
| `reap-cart-proof-mirror` | `mirror` | `scripts/backfill_shopify_variant_ids.py` (#2459) | `external_product_seeds.seed_data.snapshot.shopify_cart_proof` and `shopify_cart_variant_proofs` (sole or named variant; JSON `null` revokes), plus stamped variant ids | 7 days | 10:13 daily |

| Piece | Where |
|---|---|
| Entry point (both jobs) | `python -m jobs.reap_cart_proof_refresh mirror\|enrichment --on-crawl-egress --budget-seconds N` |
| Provisioning | `infra/gcp/setup_reap_cart_proof_jobs.sh staging\|prod <backend-tag> [--enable\|--disable]` |
| Domains, mirror | every domain on `config/tierb_cart_link_merchants.json` (42), **stalest first** (below) |
| Domains, enrichment | `ENRICHMENT_DOMAINS` in the entry point: stila, jsmbeauty.sg, bluemercury, tarte, MAC (last) |
| Where each store got to | `reap_cart_proof_refresh_cursors` (migration 250, `db/reap_cart_proof_refresh_cursors.py`) |
| Readers | mirror: `services.shopify_variant_identity.verified_cart_variant_id`; enrichment: `services.reap_enrichment_cart_proof.verify_enrichment_cart_proof`, behind `REAP_AGENTIC_CART_LINK_ENRICHMENT_ENABLED` |

**No table here is created at boot.** Migration 250 is NOT applied by any deploy or startup: all
four services run with `SKIP_HEAVY_STARTUP_INIT=true`, so `db/migrations/` never runs in prod. The
cursor table exists only once the first APPLY run's `ensure_table()` has created it (a dry run on
an environment where no apply has run reads "no cursors" and writes nothing); the enrichment proof
table likewise appears on its first writer apply (or its reader's first flag-on request).

The entry point owns no proof logic. It walks each domain page by page by calling the writer's
own entry point on the writer's own cursor (mirror: `run(limit=25)`; enrichment: `run_domain(limit=250)`,
so its writes commit page by page), inside a wall-clock budget, and prints one report line.

**Order and resume.**
- Every APPLY run stores, per (lane, store), the cursor to resume from, after every page and when
  the store ends. A store the budget (or a block, or a crash) cut is resumed from there next run;
  a store walked to its end starts from its first page next run. A dry run reads the cursors (it
  walks what the next apply would) and writes nothing.
- Mirror order: stores no run has walked to the end go first; then by the earlier of the oldest
  **still-valid** proof among the store's active seeds (one SQL read; lapsed proofs are ignored) and
  the store's last completed walk; then by name. A completed walk moves a store to the back, so no
  store starves while the budget covers a day's work. Seeds are matched to stores exactly as the
  backfill's `--domain` filter matches them (case-sensitive; exact or a `.`-subdomain), so a seed
  whose `domain` column the backfill would never select does not move a store either.
- **Both lanes: a store that blocked us is backed off.** When a store ends `aborted_on_block`, its
  row gets `blocked_until`: it is skipped (`backed_off`) until then, and walked LAST on the first run
  after that. The back-off is shorter than the lane's proof life, so one block never guarantees a
  lapse: **mirror 3 days** (proofs live 7 days), **enrichment 1 day** (proofs live 72 h). One store
  that always blocks us costs one threshold's worth of requests per back-off and never the other
  stores' refresh. **No store is backed off in a run whose IP-throttle breaker tripped** (below):
  every store that run backed off is re-recorded without `blocked_until` (`ip_block: true` in the
  report) -- during an address-level throttle a store that "blocked us" was not the problem.
- **A poison page does not pin the cursor.** A store that crashes at the SAME resume cursor on two
  runs in a row has that cursor reset to NULL (`crash_cursor_reset` in the report, a WARNING in the
  log), so the rows before it are walked again; read `error` to fix the page itself.

**Blocks, and the IP-throttle breaker.**
- **A store**: aborted (`aborted_on_block`, backed off) when its consecutive block-shaped answers
  (429/403/5xx/transport errors; a clean answer resets the count, a challenge page or an unsent
  request does not touch it) reach the writer's own threshold T (mirror 8, enrichment 5), **or**
  when it is walked to its end with at least one block and **no clean answer at all** (a store we
  never read is not `done` and gets no `last_completed_at`). Then **the pass moves on**.
- **The address**: one IP-throttle breaker per run (`services.crawl_ip_throttle.IpThrottleBreaker`,
  #2473), fed every response's headers through `crawl_politeness.note_response`. **A store counts
  toward a trip only once its own run of 429s (or 503 + Retry-After) reaches 3 in a row** (a clean
  answer from it ends the run): one transient 429, timeout or 502 on a healthy store is not a block,
  and a tiny store aborted after one error does not count either. It trips (`trip_reason:
  blocks_after_health`) when **3 counting stores within 15 minutes include one that was healthy** --
  it answered cleanly earlier this run, or **the cursor table says a previous run walked it cleanly
  and its last run did not blame it** -- **and no other store has answered cleanly since they began**.
  So a run that starts already blocked (the 09-30 throttle began hours before both lanes' slots) trips
  on the third previously-healthy store; stores the table already blames (`aborted_on_block` last
  time, forgiven or not), which `defer_blocked` walks last and back to back, are no evidence and never
  trip it; and blockers walked between stores that still answer do not trip it. **Four first-contact
  stores** (no cursor row) counting with nothing answering since also trip it (`nothing_answering`):
  a new lane or new stores meeting a blocked address, at most once per store ever. **What does not
  stop:** a run that starts blocked when no store has health on record and none is first contact
  walks every store to its own threshold (8) and backs each off for 3 days. Thresholds are for
  lanes that walk ONE store at a time (#2473's defaults, 10 hosts in 60 s, suit the many-host refresh
  and could never trip here). A trip **stops the whole pass** (`ip_throttled`, exit 1) and
  **forgives the back-offs of that run**. This is what keeps healthy stores from being backed off
  under the 2026-09-30 pattern -- a rate throttle that let ~29% through, so each store's 429s come in
  runs between its answers: three such stores trip the breaker long before anyone is backed off.
- **The breaker counts STORES, not hostnames** (a store's apex, `www.` twin and subdomains are one).
  Three stores that refuse us from their first request, walked back to back and followed by a store
  that answers, are three store-level blocks: **no trip, and each keeps its back-off**. A trip
  forgives only the back-offs recorded **within the breaker window** (15 min) before it; an earlier,
  unrelated block keeps its back-off.
- **A second run-level stop for blocks that are not 429s.** 403s, 5xx and transport errors
  (connection resets, timeouts) never trip the throttle breaker, yet an IP-level block looks exactly
  like that (the 2026-08-21 shape; the 2026-09-28 NAT drops). A second, store-keyed breaker counts
  those, with the same rule. Its trip also stops the pass without backing off stores and forgives back-offs within its
  window (`block_breaker` in the report, with `trip_reason`). One store that genuinely 403s
  everything is aborted and backed off on its own; stores that had been answering and then block one
  after another stop the pass.
- **Redirects are followed one hop at a time**, each hop gated, paced and reported like the first
  request (at most 3 hops, https only, the same storefront only), with ONE patience deadline for
  the whole request. A redirect off the storefront (or off https) is never requested: the backfill
  reports it as `http_421` (the enrichment writer's `host_redirected`), counted in
  `off_storefront_redirects`, and it is **not** in `most_blocked_domains`.
- **Held requests.** A request `crawl_politeness` does not release within 60 s (a Retry-After or
  backoff hold, a Crawl-delay over the cap, the shared edge slot) is not sent and is counted. The page
  stops its store as `held_by_politeness` (exit 4) WITHOUT advancing the cursor past it: the store is
  neither `done` nor `aborted_on_block`, gets no back-off, and resumes before the held rows next run.
  A request robots.txt disallows, or one to a host whose Crawl-delay is over the cap, is also not
  sent, but that refusal is permanent: counted apart (`robots_disallowed`, `crawl_delay_too_long`),
  not a hold -- the store advances past it. In the backfill's own report a held request shows as
  `http_425` and a permanent refusal as `http_451` (local answers, never sent, never counted as a
  block).
- Mirror: the client handed to the backfill classifies every answer with the backfill's own rules
  and, once the store has tripped or the breaker has, answers 429 locally without sending.
  Enrichment: the writer's `should_stop` is the breaker, so it stops asking the moment it trips.

**Pacing: every request goes through `crawl_politeness` and the shared Shopify-edge pacer (#2474).**
Both lanes wait for `crawl_politeness.before_request` before every request: robots.txt, the host's own interval, any
Retry-After / backoff hold a previous answer armed, and (with `CRAWL_SHOPIFY_EDGE_PACER_ENABLED`,
set on both jobs by the setup script) a slot of the ONE aggregate budget every crawl job on the
crawl IP shares (default 2 req/s). Both lanes mark their hosts Shopify-served (every target is a Tier
B Shopify store) and teach the pacer from response headers. A request the gate will not release
within 60 s is not sent (mirror: counted as `not_sent_by_politeness`, the row is left for the next
run). With the pacer on, **a dry run writes one thing**: its slot leases in `crawl_egress_pacer`
(migration 251), the shared schedule itself. Nothing else.

## The egress rule (read this first)

**Both jobs run only on the crawl subnet `pivota-crawl` (NAT 34.82.199.35).** Never on `worker`,
`web` or any job on the `default` subnet: their egress is 8.231.167.230, **the address payment
partners allowlist**, and NAT port exhaustion is per IP. The entry point refuses to start without
`--on-crawl-egress` (exit 2), and the setup script refuses to create a job when the subnet is
missing. If you run a writer by hand with `scripts/ops/run_oneoff_job.sh`, `SUBNET=pivota-crawl`
is **not optional** (the runner's default is `default`).

## Dark, dry run, apply

One environment variable decides whether a run writes: **`REAP_CART_PROOF_APPLY`** (1/true/yes/on
applies; anything else, unset included, is a dry run). The setup script sets it and the triggers
together:

| | `REAP_CART_PROOF_APPLY` | triggers | a scheduled run | a hand `gcloud run jobs execute` |
|---|---|---|---|---|
| dark (new jobs, or `--disable`) | `false` | paused | none | **a dry run**: fetches every store, writes nothing |
| armed (`--enable`) | `true` | resumed | writes | writes |

**A plain re-run (no flag) keeps the current state**: the script reads both jobs' gate and trigger
with `describe` before touching either, and re-images them (a newer `<backend-tag>`) without arming
or disarming. It refuses, writing nothing, when the two jobs are split (one armed, the other dark
**or missing** -- a plain re-run never creates a job armed) or a job's gate and trigger disagree (a
failed earlier run): pass `--enable` or `--disable` then. A missing job next to a dark one is
created dark. A trigger the script creates is paused immediately (gcloud has no `create --paused`),
before any other write.
`--disable` prints a loud `DISARMING` line and pauses both triggers before it touches either job;
`--enable` resumes the triggers last, so a failure part-way leaves nothing firing.

**A dark job is not inert when executed by hand.** Unlike the Tier B job (dark = contacts nobody),
a hand execution of a dark cart-proof job crawls, exactly as hard as a real run (both writers gate
the write, not the crawl). That is deliberate: it is the dry run on the real job definition. Do not
start one by hand inside another crawl's window (02:20–04:20, 05:15–06:15 UTC) or while a
scheduled run of the other cart-proof job is in flight: they share stores.

## Preconditions (all four, before ANY execution, dry runs included)

In addition to the HARD GATE at the top (the crawl-IP incident closed with >= 24 h of normal reads,
and #2474 merged):

1. **The mirror lane goes through `crawl_politeness`.** Its requests honour Retry-After and the
   per-host backoff and take the shared Shopify-edge slot (the follow-up PR to #2471, which also
   wires #2474). The backend tag the jobs run must contain that PR: re-run the setup script with it.
2. **Both lanes feed the shared pacer and the breaker.** They mark their hosts Shopify-served, learn
   from and report response headers, and the setup script sets `CRAWL_SHOPIFY_EDGE_PACER_ENABLED=true`
   (and `CRAWL_SHOPIFY_EDGE_LEASE=2`, a cap on the demand-sized lease) on both jobs: check with
   `gcloud run jobs describe`. `CRAWL_SHOPIFY_EDGE_RPS` must NOT be set on them: every crawl job shares
   one rate (#2474), the global default.
3. **The IP-throttle breaker is what stops a pass**, and a trip backs nothing off (this runbook,
   "Blocks").
4. **The mirror budget is sized from the prod census** (PR body: 2,092 candidates over the 42 Tier B
   domains, ~6,500 s at the lane's pacing; budget 10,800 s, task timeout 15,000 s).

## Procedure, per environment

Staging first. Staging has its own Postgres (10.122.0.3) and may hold no enrichment rows: the
enrichment report then shows `products: 0` per domain and nothing is fetched. The backend tag must
be an image in `us-west1-docker.pkg.dev/pivota-shared/pivota/backend` that contains this code.

```sh
ENV=staging; PROJECT=pivota-staging; TAG=<backend sha>

# 1. provision DARK (creates or updates both jobs; gate false; triggers paused). Contacts no store.
infra/gcp/setup_reap_cart_proof_jobs.sh $ENV $TAG

# ⛔ STOP HERE until the HARD GATE at the top of this runbook (>= 24 h with no crawl-backoff 429 bursts
#    and no breaker trips on the referral refresh, the destination sweep, Tier B and the purchasability
#    sweep, plus Peng's go; #2474 merged, both lanes wired through it, and
#    CRAWL_SHOPIFY_EDGE_PACER_ENABLED=true on both jobs) and the PRECONDITIONS 1-4 above all hold.

# 2. FIRST DRY RUN: ONE SMALL STORE PER LANE, not all 42. `--args` replaces the job's args for this
#    execution only (the job definition is untouched). judydoll.com has 3 mirror candidates (prod
#    census 2026-09-30); stilacosmetics.com is the smallest enrichment store (~124 products, a few
#    /products.json pages in `auto` mode).
gcloud run jobs execute reap-cart-proof-mirror --project $PROJECT --region us-west1 --wait \
  --args=-m,jobs.reap_cart_proof_refresh,mirror,--on-crawl-egress,--budget-seconds,600,--only,judydoll.com
gcloud run jobs execute reap-cart-proof-enrichment --project $PROJECT --region us-west1 --wait \
  --args=-m,jobs.reap_cart_proof_refresh,enrichment,--on-crawl-egress,--budget-seconds,600,--only,stilacosmetics.com
#    Read both reports: `ip_throttle.ip_throttled` must be false, `throttle_diagnostics.responses`
#    ~0, `shopify_edge_pacer.enabled` true with `granted` > 0, and the store `done`.

# 2b. Only then, the FULL dry run of each job on its real definition (fetches; writes only its pacer leases)
gcloud run jobs execute reap-cart-proof-enrichment --project $PROJECT --region us-west1 --wait
gcloud run jobs execute reap-cart-proof-mirror     --project $PROJECT --region us-west1 --wait

# 3. read the reports (below). Then ONE APPLY per job, still dark (the override is for this
#    execution only; the job keeps REAP_CART_PROOF_APPLY=false and its trigger stays paused)
gcloud run jobs execute reap-cart-proof-enrichment --project $PROJECT --region us-west1 --wait \
  --update-env-vars REAP_CART_PROOF_APPLY=true
gcloud run jobs execute reap-cart-proof-mirror --project $PROJECT --region us-west1 --wait \
  --update-env-vars REAP_CART_PROOF_APPLY=true

# 4. ARM (gate true on both jobs, both triggers resumed)
infra/gcp/setup_reap_cart_proof_jobs.sh $ENV $TAG --enable
```

Step 3's one-off apply writes cursors too (it is an apply), so step 4's first scheduled run resumes
wherever step 3 stopped.

Then prod with `ENV=prod PROJECT=pivota-prod`. **Steps 3 and 4 write to production: Peng's go
first.** Announce each prod apply to the owner of the lane it writes (the Reap rail).

`--wait` returns non-zero for any non-zero exit; the exit code alone does not say which (gcloud
reports every failure as 1). Read the report line for the job's own code.

**Scope (`--only`) is kept across re-runs.** A job limited to some stores, e.g. the mirror at
`--only judydoll.com`, keeps that list on a plain re-run, on `--enable` and on `--disable`. On
2026-10-08 an `--enable` silently widened a judydoll-only mirror to all 42 stores; this is the
fix. To change the scope:

- `MIRROR_ONLY=judydoll.com,fentybeauty.com` (or `ENRICHMENT_ONLY=...`) sets it;
- `MIRROR_ONLY=all` removes it.

The script prints `== scope: <job> only=...` for each job before writing. It refuses, writing
nothing, when a job's current args cannot be read.

A backend deploy **never re-images these jobs**: they run `<backend-tag>` until the setup script is
re-run with a newer one (no flag: the current armed/dark state is kept).

## Reading the report

Each run prints a `REAP_CART_PROOF_PROGRESS {...}` line as each domain ends, then one
`REAP_CART_PROOF_REPORT {...}` line. Text-prefixed, so both land in `textPayload`:

```sh
gcloud logging read \
  'resource.type=cloud_run_job AND resource.labels.job_name=reap-cart-proof-mirror
   AND textPayload:"REAP_CART_PROOF_REPORT"' \
  --project pivota-prod --limit 1 --freshness 2d --format 'value(textPayload)'
```

If the task timeout's SIGTERM arrives, the job prints the REPORT line with what it has
(`"terminated": "SIGTERM"`, the store in flight `terminated`) BEFORE it lets go of the database, and
exits non-zero (4, or 1/3 if those apply) even if nothing had finished. Every page already
checkpointed stays checkpointed; the in-flight page is lost and redone next run. **What that page
costs, in requests:** mirror, at most 25 seeds (25 `.js` requests plus their redirect hops, ~1.5 min at 3 s). Enrichment is
250 PRODUCTS, but a product can hold many storefront handles: MAC's first 250 products are ~1,600
handles (the folded shades), which in the `.js` fallback is ~1,600 requests, **~80 minutes**, of
work thrown away (in normal `auto` listing mode it is a few listing pages).

Top level: `lane`, `mode` (`dry_run` / `apply`), `budget_s`, `elapsed_s`, `exit_code`,
`status_counts`, `domains_order`, and `domains.<domain>` = `{status, pages, elapsed_s, writer, …}`.

| `status` | meaning |
|---|---|
| `done` | walked to its end |
| `aborted_on_block` | this store answered T consecutive 429/403/5xx/transport errors, or never answered cleanly at all; it is backed off (mirror 3 days, enrichment 1 day) and the pass moved on. With `ip_block: true`, the breaker tripped later in the same run and the back-off was forgiven |
| `held_by_politeness` | crawl_politeness held one or more of this store's requests past 60 s: they were NOT sent, the store stopped there WITHOUT advancing its cursor past them, no back-off. `not_sent_by_politeness` counts them |
| `ip_throttled` | the IP-throttle breaker tripped during (or right before) this store: **the whole pass stopped** here, nothing is backed off, and the store resumes from its cursor next run. The top-level `ip_throttle` block has the breaker's evidence (hosts, first/last 429, Retry-After and server histograms) |
| `backed_off` | skipped: this store blocked us within its lane's back-off (`backed_off` at the top level lists until when) |
| `crashed` | the writer raised; `error` says what; the next domain still ran. `crash_cursor_reset: true`: the second crash in a row at this cursor, which was reset |
| `cursor_stuck` | the writer returned the cursor it was given; the domain was stopped rather than looped |
| `budget_stopped` | the budget ran out while walking this domain; `last_cursor` is where it stopped and where the next run resumes |
| `not_reached` | the budget (or an abort) ended the pass before this domain |
| `terminated` | the SIGTERM arrived while this domain was in flight |

Per domain, `start_cursor` is where this run resumed it (absent = its first page) and
`checkpoint_error` says a cursor could not be stored (the proofs were still written; the next run
re-walks from the older cursor). Top level, `resumed` lists the stores resumed mid-walk, and
`block_streak_short_circuited` (mirror) counts requests answered locally after the streak tripped.

| exit | meaning | do |
|---|---|---|
| 0 | every store walked to its end (`backed_off` stores are listed, not counted) | nothing; glance at `backed_off` |
| 1 | the WHOLE PASS stopped: the IP-throttle breaker tripped (`ip_throttled`) | the crawl address is being throttled: read `ip_throttle`, check the other crawl jobs (refresh, destination sweep, Tier B, purchasability sweep), and do not re-run until they read normally again |
| 2 | bad arguments, no `--on-crawl-egress`, a missing merchant list, or the writer refused the domain list; nothing attempted | fix the job args or `config/tierb_cart_link_merchants.json` |
| 3 | a writer crashed (or `cursor_stuck`), or the pass itself could not plan or start (`REAP_CART_PROOF_CRASH` on stderr, no report) | read the job log |
| 4 | a store was left unwalked: the budget, the task timeout's SIGTERM, or one store that blocked us (`aborted_on_block` without `pass_abort`; it is backed off, see the lever below) | a store that blocked us: check that store alone; nothing else was affected. Otherwise: the cut store resumes from its cursor and the stalest stores go first next run. **This is not self-healing on its own**: it only catches up if one day's budget covers one day's work. Exit 4 on consecutive days means the budget is too small: raise the budget AND the task timeout together in the setup script, and read `reap_cart_proof_refresh_cursors` (below) to see which stores are behind |

A non-zero exit fails the execution. `--max-retries 0`: nothing re-runs automatically.

**Mirror** (`writer` is the backfill's report, summed over pages): `candidates` (the first dry run
is the measurement this lane was sized without: seeds walked per domain), `cart_proofs`
(`sole_variant` / `named_variant` proofs computed, dry run too), `rows_with_new_ids`,
`variant_ids_stamped`, `write_conflicts` (the refresh job rewrote the row under us: skipped, never
forced), `fetch_outcomes` (`dead_handle` = the seed's URL is gone; `not_json` = a challenge page or
a themed soft-404; `http_421` = a redirect off the storefront, never followed; `http_425` / `http_451`
= held / refused by crawl_politeness, not sent), `most_blocked_domains`. A proof the fetch no longer supports is written as JSON
`null` (revoked) by the same run.

**Enrichment** (`writer.pages[]` is the writer's own per-domain report, see the docstring of
`jobs/enrichment_cart_variant_proof.py`): `outcomes` (written: `ok`, `product_changed`,
`parent_stub`, `variant_gone`, `currency_not_market`, …), `skipped` (nothing written:
`skip_transient:*`, `skip_listing_incomplete`), `written`, `write_older_than_stored`,
`currency_read` / `currency_problems`, `price_check` + `price_drift_samples`, and per host
`hosts.<host>.source_mode` and `listing` (`complete`, `stop`). Watch `listing.stop == "page_cap"`:
a store with more than ~25,000 published products cannot be paged to its end (100 pages x 250),
so `auto` mode leaves its unlisted handles as `skip_listing_incomplete` every day; bluemercury is
the store to check. The fix is a per-handle run for that store (`--source products_js_v1`, below).

## Reading the tables

```sql
-- enrichment: outcome mix and age per store
SELECT shop_host, outcome, count(*), min(checked_at), max(checked_at)
  FROM enrichment_cart_variant_proofs GROUP BY 1, 2 ORDER BY 1, 2;

-- mirror: proofs per store and the oldest one (valid 7 days)
SELECT domain,
       count(*) FILTER (WHERE jsonb_typeof(seed_data->'snapshot'->'shopify_cart_proof') = 'object') AS proofs,
       count(*) FILTER (WHERE seed_data->'snapshot'->'shopify_cart_proof' = 'null'::jsonb) AS revoked,
       min(seed_data->'snapshot'->'shopify_cart_proof'->>'checked_at') AS oldest
  FROM external_product_seeds WHERE status = 'active' GROUP BY 1 HAVING count(*) FILTER
       (WHERE seed_data->'snapshot' ? 'shopify_cart_proof') > 0 ORDER BY 1;
```

```sql
-- where each store got to, when it was last walked to its end, its block back-off, its crash count
SELECT lane, domain, next_cursor, last_status, last_completed_at, blocked_until, crash_count, updated_at
  FROM reap_cart_proof_refresh_cursors ORDER BY lane, last_completed_at NULLS FIRST, domain;

-- CLEAR A STORE'S BLOCK BACK-OFF (it is walked on the next run, LAST, because its last run blocked).
-- Do it once you know the store answers again (e.g. a one-store dry run by hand), not blindly.
UPDATE reap_cart_proof_refresh_cursors SET blocked_until = NULL
 WHERE lane = '<mirror|enrichment>' AND domain = '<domain>';

-- forget a store's cursor (it restarts at its first page next run); only with the triggers paused
DELETE FROM reap_cart_proof_refresh_cursors WHERE lane = 'mirror' AND domain = '<domain>';
```

Prod Postgres is private: run SQL through `scripts/ops/run_oneoff_job.sh -c "<program>"` (a
throwaway job on the default subnet; it does not fetch from merchants, so the default subnet is
right for it).

## Clearing a `product_changed` row (enrichment)

`product_changed` means the storefront handle now holds a **different Shopify product** than the
one the proof recorded (or a placeholder's live title no longer matches the catalog title). The row
keeps the recorded `shopify_product_id`, so the refusal is **sticky**: no later run turns it into
an `ok` for the new product. Clearing it is a person's decision:

1. Find the rows:
   ```sql
   SELECT product_key, sku_key, shop_host, handle, shopify_product_id, checked_at
     FROM enrichment_cart_variant_proofs WHERE outcome = 'product_changed' ORDER BY shop_host, product_key;
   ```
2. For each, open `https://<shop_host>/products/<handle>.js` and compare with the catalog row
   (`catalog_products.title`, the sku's `source_variant_id`). If the live product is **not** the
   catalog's product, leave the row: it is doing its job; the catalog row needs fixing instead.
3. If it **is** the same product (a re-created listing, a retitle the catalog has now followed),
   delete that one row, and only while it still says `product_changed`:
   ```sql
   DELETE FROM enrichment_cart_variant_proofs
    WHERE product_key = '<product_key>' AND sku_key = '<sku_key>' AND outcome = 'product_changed';
   ```
   The next run records the live product id afresh. Until then the sku has no proof and the
   purchase lane refuses it, which is the safe direction.

## Running a writer by hand (outside the jobs)

> **⛔ STOP: the HARD GATE at the top of this runbook applies here too.** A hand run with
> `scripts/ops/run_oneoff_job.sh` crawls the same stores from the same crawl IP; do not run either
> writer by hand until every condition there holds.

Only for a targeted fix; the jobs are the normal path. **Prefer the job with `--only <store>`** (step 2
above): a hand run of `scripts/backfill_shopify_variant_ids.py` does NOT go through
`crawl_politeness`, the shared Shopify-edge pacer or the IP breaker (only the job's mirror client
does), so it should stay a single seed. Always `SUBNET=pivota-crawl`, and
re-list the whole `ENV_VARS` (it replaces the runner's defaults):

```sh
# enrichment, one store, per-handle (e.g. a store whose listing hits page_cap); dry run first
SUBNET=pivota-crawl TASK_TIMEOUT=7200s \
ENV_VARS=PIVOTA_ENV=production,DB_STATEMENT_TIMEOUT_SECONDS=30,DB_COMMAND_TIMEOUT_SECONDS=600 \
  scripts/ops/run_oneoff_job.sh -m jobs.enrichment_cart_variant_proof --on-crawl-egress \
  --domain bluemercury.com --source products_js_v1

# mirror, one seed
SUBNET=pivota-crawl \
  scripts/ops/run_oneoff_job.sh scripts/backfill_shopify_variant_ids.py \
  --seed-id <epsv_...> --domain <domain> --limit 1
```

Add `--apply` to write. Do not run either inside the scheduled window of the matching job.

## Disarm

```sh
# both jobs dark again (triggers paused first, then gate false); prints a loud DISARMING line.
# A re-run WITHOUT --disable keeps an armed job armed.
infra/gcp/setup_reap_cart_proof_jobs.sh prod <backend-tag> --disable

# fastest, without the script (the job's env still says apply until the script is re-run):
gcloud scheduler jobs pause reap-cart-proof-enrichment-cron --location us-west1 --project pivota-prod
gcloud scheduler jobs pause reap-cart-proof-mirror-cron     --location us-west1 --project pivota-prod

# stop a run in flight (writes already committed stay; enrichment commits per store at its end):
gcloud run jobs executions list --job reap-cart-proof-mirror --region us-west1 --project pivota-prod --limit 3
gcloud run jobs executions cancel <execution> --region us-west1 --project pivota-prod
```

Disarming stops new proofs; it does not remove the ones already written. They age out on their
own (72 h enrichment, 7 days mirror). To stop purchases relying on them **now**, turn off the
reader instead: `REAP_AGENTIC_CART_LINK_ENRICHMENT_ENABLED` (enrichment rows) or
`REAP_AGENTIC_CART_LINK_ENABLED` (the whole cart-link lane) on `web`.

## Schedules and timeouts

The crawl address's daily neighbours, as their longest windows: external-seed-destination-sweep
(script 02:20–04:20 with its retry; **live prod reads 03:15, no retry, to 04:15** — the trigger
has drifted from `setup_scheduler.sh`), tierb-cart-link-eligibility 03:30–04:00, and
external-referral-refresh (live only, no script here) 05:15–06:15. The hourly purchasability sweep
(:07–:27) and the every-5/10-minute probe and drain lanes cannot be avoided by a multi-hour run,
only not coincided with.

- **enrichment**, 06:41, budget 3,600 s, task timeout 10,800 s. A normal pass is minutes
  (`/products.json` listing pages). The timeout covers the fallback where every store is read one
  `.js` per handle at 3 s: 962 handles before MAC (2,886 s, inside the budget) and MAC's ~1,870
  (5,610 s), 8,496 s in all. MAC is last so it can never starve the other four.
- **mirror**, 10:13, budget 10,800 s, task timeout 12,600 s (the budget plus one 50-seed page).
  At 3 s per seed that is ~3,600 seeds a day. **Not measured**: the first dry run's
  `writer.candidates` per domain is the measurement; if the pass is cut (exit 4) on consecutive
  days, raise both numbers in the setup script.

## Known limits

- **A HAND run of the mirror writer is not polite** (the scheduled job is). Since #2476, the scheduled
  `reap-cart-proof-mirror` job sends every request, redirect hops included, through
  `crawl_politeness` (robots.txt, `Retry-After`, per-host backoff, the shared Shopify-edge pacer). It
  aborts a store after 8 block-shaped answers in a row (or no clean answer at all), and stops the pass
  only through the lane breakers above. A hand run of `scripts/backfill_shopify_variant_ids.py` gets
  none of that. It paces itself (1 s global, 3 s per store), ignores robots.txt and `Retry-After`,
  and has one block counter for the whole run: it aborts the run after 8 block-shaped answers in a
  row, across every store it touches. That is why hand runs stay under this runbook's hard gate and
  single-seed. Making the script itself polite is that script's change, not this job's.
- **bluemercury may exceed `/products.json`'s 100-page cap** (see "Reading the report").
- **The enrichment writer re-reads a store's listing for every 250-product page** in `auto` mode
  (MAC and tarte are two pages each): a few extra listing requests, in exchange for page-by-page
  commits and a budget that can stop inside a store.
- **A PARTIAL throttle escapes the lane breaker** (#2476 review, P2, measured). The breaker's
  "still answering" veto means a clean answer from any store outside the counting window cancels the
  trip. So once roughly 1 walked store in 3 or more still reads cleanly, a throttle on the rest never
  stops the pass. Measured with every cursor row healthy (T = a throttled store, F = a free one, in
  walk order):

  | walk order | result |
  |---|---|
  | `TF` | no trip; 6 of 12 stores backed off |
  | `TTF` | no trip; 8 of 12 backed off |
  | `TTFTFF` | no trip |
  | `TTTF` | trips after 19 requests |

  The throttled stores are aborted and backed off, and their rows then say `aborted_on_block`. A
  blamed store is never evidence again, so each serves its 3-day back-off and is walked last (about
  112 requests per cycle) until the address recovers. The 2026-09-30 shape (~97% of answers
  throttled) does trip. **What to watch:** many `aborted_on_block` stores in one run that exits 4
  rather than 1. Treat that as a possible partial IP throttle: check the other crawl jobs' breakers
  before trusting the back-offs. **Possible tightening, not implemented:** count a store as "still
  answering" only after several consecutive clean answers since the window began.
- **A table with every row present and none healthy never trips** (#2476). This means every store was
  blamed last time, or was walked but never to its end, and none is first contact. If such a run
  starts blocked, it walks every store to its threshold and backs each off. That costs about 272–336
  mirror requests once per 3-day back-off, not nightly. It only arises if the lane has never walked
  cleanly from this address, and #2475's hard gate (≥ 24 h of normal reads first) makes the first
  runs healthy.

`tests/test_setup_reap_cart_proof_jobs.py` re-derives the neighbour windows from their scripts (and
pins the two live-only ones) and fails if a schedule or timeout moves into one.


Buyer-selected multi-variant mirror products
-------------------------------------------

A named catalog SKU may use `snapshot.shopify_cart_variant_proofs[shopify_variant_id]`.
The existing mirror writer authors all selector proofs from one complete products.js response,
records the response time, and replaces the map on every successful fetch to revoke removed,
unavailable or contradictory choices. The reader checks the source, same product URL, host,
freshness, selected identity and captured-id agreement. A selector proof never prices a product
placeholder: each size still needs its own catalog offer, and the Reap quote must reconcile
that price before a buyer can approve payment. The no-selector sole-variant rule is unchanged.

For the KraveBeauty Matcha cleanser staging check, the stored mirror SKUs use `::v::` keys,
not the enrichment lane's `::v:` keys. Both SKU rows exist, but their own offers and storefront
proofs are absent, and US Tier B eligibility has expired. Refresh remains subject to the hard
gate above; never stamp a proof timestamp or eligibility clock without measuring it.
