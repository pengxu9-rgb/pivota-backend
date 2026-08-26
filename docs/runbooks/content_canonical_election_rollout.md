# Rollout: one canonical URL per content_key

Ships the page-side half of the duplicate-URL fix pivota-agent-ui#280 started.

## What is broken today

474 `content_key`s serve identical content — same title, same Product JSON-LD,
confirmed by 24 live fetches — under 2 to 7 sitemap-eligible sig URLs (551
redundant URLs; groups of 2×425, 3×25, 4×22, 5×1, 7×1). The largest cohort is
Path-C minted canonicals sharing a content_key with the external_seed MIRROR row
they were minted from; P3 (PIVOTA-Agent#1828 + pivota-backend#1585) made both
sides renderable at once.

Every one of those pages emits a **self-referential** `<link rel="canonical">`.
Two URLs, identical content, each declaring itself canonical: Google picks one
arbitrarily and may well pick the one the sitemap omits.

\#280 fixed the sitemap (one URL per content_key, sticky on incumbency). It could
not fix the pages, because a PDP has no way to know which sibling the sitemap
picked.

## The invariant

> the sig the sitemap advertises == the sig every sibling PDP canonicalises at

Breaking it is **worse than the duplicate**: advertising URL A while A's own page
points its canonical at B tells the crawler to drop the URL we just submitted.

The two surfaces cannot each compute it. #280's winner rule is anchored on
*incumbency* — presence in the committed `public/sitemap-products.xml` — which is
real index equity (a pure hex/lexicographic ordering would have swapped 183
already-indexed URLs the moment P3 landed) but lives in a git artifact a PDP
request cannot consult.

So the backend became the source of truth. It elects once, stores the answer in
`content_canonical_election`, and both surfaces read it.

## Order of operations

The steps are ordered by a hard dependency: **the gateway's SQL joins
`content_canonical_election`, so the table must exist before PIVOTA-Agent
deploys.** Do not reorder 1 and 4.

### 1. Merge pivota-backend

`db/schema_guard.py` creates the table inside `ensure_required_schema_light()`,
which runs *before* the `SKIP_HEAVY_STARTUP_INIT` bail — so prod self-heals on
boot even in fast mode, unlike `db/migrations/`. It is best-effort and wrapped in
a 12s timeout, so verify rather than assume:

```bash
psql "$DATABASE_URL" -c "\d content_canonical_election"
```

If it is absent, apply `db/migrations/181_content_canonical_election.sql` by hand
through the public proxy before continuing.

At this point the feed emits `canonical_sig_id: null` for every row and nothing
has changed: the sitemap falls through to its own layers 1-3, exactly as today.

### 2. SEED the election from the live sitemap

**Run this before any plain sweep.** A plain sweep against an empty table elects
lexicographically and moves 183 already-indexed URLs — the precise regression
\#280 exists to prevent.

```bash
DATABASE_URL=... python scripts/elect_content_canonicals.py \
  --seed-from-sitemap https://agent.pivota.cc/sitemap-products.xml
```

Dry run first (that is the default). Expected against the 2026-07-26 corpus:

```
"content_keys":        4528
"duplicate_groups":     474
"redundant_urls":       551
"moved_from_sitemap":  ~340-360  <-- READ THIS ONE, and trust the run over this doc
"replacements":           0      <-- structurally always 0 on a seed; ignore it
"reasons": { "sole_candidate": 4054, "dedupe_keeper": ~360, "sitemap_incumbent": ~114 }
```

## ⚠️ THE SEED MOVES ~350 LIVE URLs. That is a decision, not a no-op.

Two independent measurements against prod, four hours apart, got **340** and
**357**. Neither is authoritative — the corpus moves, and the tombstone set is
derived by probing live PDPs — which is exactly why `moved_from_sitemap` exists:
the dry run prints the real number for the corpus you are about to apply to.
**Read it there, not here.** What both measurements agree on, and what matters:
every single moved URL moves because the currently-advertised sig is a step-5
dedupe TOMBSTONE, and **zero** move for any other reason. The incumbency seed
holds perfectly everywhere else.

An earlier version of this runbook called `"replacements": 0` the acceptance
criterion and claimed the seed moved zero URLs. **Both were wrong**, and the
gate could never have caught it: `replaced` is the PREVIOUSLY STORED winner, so
on a seed run — the one run where hundreds of URLs move at once — the table is
empty and `replacements` is structurally zero no matter what happens. The
`moved_from_sitemap` counter exists because of that; it compares each elected
winner against the URL the live sitemap advertises today, and it prints
**before** `--apply`.

Measured by fetching all 1,025 duplicate-group member PDPs live (#1833 is
deployed, so a member whose live `rel=canonical` is not itself IS a tombstone,
and the target is its keeper): **~350 of 4,528 sitemap URLs, about 7.5%,
change** — every one because the currently-advertised sig is a step-5 dedupe
TOMBSTONE, so the keeper is elected instead. Sitemap size stays 4,528.

**Why this is right, and why it is not optional.** PIVOTA-Agent#1833 is already
merged and deployed, so *today, in production*, those 340 URLs already serve
`rel=canonical` pointing at a different URL. The contradiction — we advertise A
while A disavows itself for B — is the CURRENT state, not something these PRs
introduce. The only choice is which side to resolve it on:

* **Follow the keeper (what this does).** Equity consolidates onto the row the
  dedupe pipeline explicitly designated on 2026-07-10 via `keeper_product_key`.
  The old URLs keep answering 200 with `rel=canonical` at the new one — the
  standard consolidation pattern, and better than a 301 while the row still
  serves.
* **Follow incumbency instead.** Requires reverting #1833 as well, or the live
  contradiction simply stays. Incumbency is an accident of crawl order;
  `keeper_product_key` is a deliberate row-level decision — and 6 of these groups
  are serving up to six self-canonical duplicate 200s of one product.

Re-run with `--apply` once `moved_from_sitemap` matches what you expect.

### 3. Confirm convergence

```bash
DATABASE_URL=... python scripts/elect_content_canonicals.py
```

Must report `"replacements": 0` and `"new_elections": 0`. A steady-state sweep
over an unchanged corpus writes nothing; if it does not, something upstream is
flapping `renderable` and that is the bug to chase, not this.

From here on the sweep runs itself: the `content-canonical-election` **Cloud Run
Job**, triggered by the `content-canonical-election-cron` Cloud Scheduler entry
every 6h at :52 UTC, auto-applying and logging a WARNING whenever a live URL
moves. It elects newly-arrived content_keys and re-elects only where the stored
winner has stopped being a candidate.

It ran as `.github/workflows/content-canonical-election.yml` until 2026-08-26.
That lane is gone and cannot come back: Cloud SQL `pivota-pg` has no public IP,
so a GitHub-hosted runner has no route to it. The workflow only ever worked
because its `DATABASE_URL` repo secret pointed at Railway's public proxy, and
it began failing the moment Railway was decommissioned (2026-08-25).

To read a run:

```bash
gcloud logging read \
  'resource.type=cloud_run_job AND resource.labels.job_name=content-canonical-election' \
  --project pivota-prod --limit 200 --freshness 7d
```

To run one by hand — dry-run first, since the Job's baked args include `--apply`:

```bash
gcloud run jobs execute content-canonical-election --region us-west1 --project pivota-prod --wait \
  --args 'scripts/elect_content_canonicals.py,--seed-from-sitemap,https://agent.pivota.cc/sitemap-products.xml'
```

**A stale election is guarded, not harmless.** Both readers — the feed
(`_elected_canonical_sig_column`) and `get_pdp_v2`'s `cce_valid` lateral —
intersect the stored winner with the live electable set and degrade to "no
election" rather than naming a sig that no longer renders. That closes the
dangerous failure (advertising URL B while B's page canonicalises at a dead A),
but a guarded stale election still means those content_keys quietly lose their
cross-canonical and go back to being duplicates. **The sweep interval is
therefore the duplicate-exposure window** — that is why it is scheduled rather
than run by hand, and why raising the interval is a real trade rather than a
tidy-up.

### 4. Merge PIVOTA-Agent

`get_pdp_v2` starts emitting `canonical.data.content_canonical_route_id`. Nothing
consumes it yet.

The gateway joins `content_canonical_election` in its **primary**
signature-resolution query, which is why step 1 comes first. It is not a
tripwire, though: on `undefined_table` it latches the table as missing, warns
once, and retries without the join, so every PDP keeps serving its own
self-referential canonical. Getting the order wrong costs the feature for a
deploy cycle, not the PDPs. If that warning shows up in prod logs, go back to
step 1 — the table did not get created — and restart the service to clear the
latch.

### 5. Merge pivota-agent-ui — founder-gated

Two changes land together:

* the PDP emits the elected sig as `<link rel="canonical">` when it is not
  itself the winner, and
* the sitemap takes the backend's answer as layer 0 of its dedup.

Verify on a preview deploy before merging (visual/SEO changes need the preview
gate). Pick any group from step 2 and fetch both members:

```bash
curl -s https://<preview>/products/<losing_sig>  | grep -o '<link rel="canonical"[^>]*>'
curl -s https://<preview>/products/<winning_sig> | grep -o '<link rel="canonical"[^>]*>'
```

Expected: **both** name the winner. The winner stays self-referential; the loser
now points away from itself. That is the whole fix.

## Verifying after the next sitemap cron

```bash
curl -s https://agent.pivota.cc/sitemap-products.xml \
  | grep -c '<loc>'
```

The URL count should not drop — #280 already deduped the sitemap, so layer 0
agrees with layer 1 by construction on the seeded corpus. A *drop* here means the
election and the incumbency disagree, i.e. the seed did not run or ran against a
stale sitemap. Re-run step 2.

## Rollback

Ordered inverse of rollout, and safe at every point:

* Revert pivota-agent-ui → pages go back to self-referential canonicals and the
  sitemap back to layer 1. No URL moves.
* Revert PIVOTA-Agent → the field disappears; agent-ui's fallback is self.
* Truncating `content_canonical_election` is also safe *while agent-ui is
  reverted* — every surface falls back to the pre-181 ordering. Do not truncate
  with agent-ui live: the sitemap would fall back to incumbency while pages that
  cached the old tag still point at the winner.

Note `elected_at <> updated_at` identifies every content_key whose URL has ever
moved. On a healthy corpus that set stays empty.
