# Dead PDP links in the external-seed corpus — audit + fix

**Measured 2026-08-25** against prod (`external_product_seeds`, 11,352 active rows) and the
brands' own live storefronts. The measurement was read-only; no seed row was modified by it.

§1–§4 are the finding. §5 is the fix, **implemented in this change**. §6 is what it does not
cover.

Reproduce the measurement with `scripts/audit_external_seed_destination_liveness.py`; the same
mechanism runs on a schedule as `jobs/external_seed_destination_sweep`.

Scope note: this document does **not** touch the warm-handoff click lane. That lane's own
exposure is tracked separately under `docs/runbooks/outbound_warm_handoff_rollout.md`.

---

## 1. Headline

A published product link rots, and nothing in this system ever notices.

| | |
|---|---|
| Active seeds | **11,352** |
| …carrying a `/products/<handle>` | **10,670** across **286 hosts** |
| Hosts whose catalogue we could actually read | **44** (3,951 seeds — 37% of the handle-bearing corpus) |
| Seeds naming a handle the brand no longer lists | **491 / 3,951 = 12.4%** |
| …of those, confirmed **broken** for a shopper (§3) | **411 / 3,951 = 10.4%** |
| Seeds whose freshness clock is older than the `stale_snapshot` blocker allows | **11,352 / 11,352 = 100%** |
| Scheduled jobs that re-read a seed's destination | **0** |

The rate is not uniform and it is not small at the top of the corpus:

| host | delisted / seeds | |
|---|---|---|
| sigmabeauty.com | 72 / 192 | 37.5% |
| meritbeauty.com | 55 / 197 | 27.9% |
| wholesale.publicgoods.com | 53 / 181 | 29.3% |
| kyliecosmetics.com | 49 / 224 | 21.9% |
| iliabeauty.com | 40 / 112 | 35.7% |
| pixibeauty.com | 35 / 348 | 10.1% |
| mixsoon.us | 25 / 228 | 11.0% |
| glossier.com | 21 / 115 | 18.3% |
| cosrx.com | 12 / 148 | 8.1% |

**It decays with age.** Median age of a delisted seed is **99 days**; of a still-listed seed,
**56 days**. There is no repair loop, so the corpus can only get worse.

### This is live, on an anonymous route

`commerce.mcp.pivota.cc/agent/v1/products/search` (public, no auth) returns, today:

```
Pure Fit Cica Toner | https://www.cosrx.com/products/pure-fit-cica-toner
```

That URL answers **HTTP 404**. The seed behind it (`eps_…`, `cosrx.com`) was last touched
**2026-07-12**. The same route also emits `merchant_canonical_url` and `offers[].url` pointing
at the same dead address.

**570 `catalog_products` rows** (`catalog_track=external_referral`,
`readiness_tier=referral_only`) already carry one of the 491 delisted URLs as their
`canonical_url` — including 22 whose seed has since been set `inactive` and whose mirror row
was never retired.

---

## 2. Method, and the two ways it can lie

Handles come from `services.outbound_warm_handoff.extract_product_handle` over
`canonical_url or destination_url`; the host from `_host_of` on the same value. A locale
storefront (`nl.beautyofjoseon.com`) is treated as its own catalogue, because it is one.

**Stage 1 — catalogue join.** One `/products.json?limit=250&page=N` sweep per host yields
every handle the brand lists. Cheap: 44 requests covered 3,951 seeds.

**Stage 2 — probe the PDP.** *"Absent from `products.json`" is not "dead."* On cosrx.com, 5 of
12 delisted handles still render a full product page at 200 — published to the storefront,
excluded from both `products.json` and `sitemap_products_1.xml`. Stage 1 alone overstates the
damage, so every delisted URL is fetched and classified by what a shopper would actually get.

**Two failure shapes are reported separately and excluded from every rate.** Both were found
the hard way:

* **`bot_challenge`** — Cloudflare answers `429` with `cf-mitigated: challenge` on *every*
  path, `robots.txt` included. **213 of 286 hosts** from a non-crawl-egress client. This is not
  a rate limit: retrying and backing off cannot clear it, and the first version of this audit
  spent its whole run in exponential backoff against it. It is also not our user-agent —
  `PivotaAuditBot`, the httpx default, and no UA at all are all refused.
* **`incomplete`** — pagination that broke partway. Discarded entirely, not partially
  credited: the first run returned fentybeauty.com's first 3 pages as if they were the whole
  catalogue and reported **285 fabricated dead handles** on that host alone (plus 108 on
  palmofferonia.com). Every unread page is a false positive.

Coverage is therefore 37% of the handle-bearing corpus, and that is stated rather than
extrapolated. **The 12.4% is the rate among hosts that will talk to us, not a corpus-wide
estimate.**

---

## 3. What a shopper actually gets on a delisted link

All 491 delisted seed rows (490 distinct URLs) were fetched:

| verdict | n | share | is the link broken? |
|---|---:|---:|---|
| `dead_404` — HTTP 404/410 | **319** | 65.1% | **yes** |
| `redirected_off_product` — 200, but the final URL is a collection or marketing page | **92** | 18.8% | **yes** — the product is not there |
| `redirected_to_product` — 301 to a different `/products/<handle>` | 29 | 5.9% | no — repairable, see §5.3 |
| `live_delisted` — 200 on the same handle, absent from `products.json` *and* `sitemap_products_1.xml` | 26 | 5.3% | no |
| `unverifiable` — bot challenge / 429 / connect error | 24 | 4.9% | unknown |

**411 of 466 verifiable delisted links (88.2%) are broken for a shopper.**

`redirected_off_product` is not a soft landing. The top targets are
`kyliecosmetics.com/pages/kylies-looks` (14), `sigmabeauty.com/collections/brush-sets` (11),
`sigmabeauty.com/collections/influencer-sets` (7) — the buyer is dropped into a category page
and has to find the product themselves, if it still exists at all.

Against the **measured** denominator that gives:

| | |
|---|---|
| Confirmed-broken links | **411 / 3,951 measured seeds = 10.4%** |
| Delisted (upper bound, stage 1 only) | 491 / 3,951 = 12.4% |

For the six `OUTBOUND_WARM_HANDOFF_BRANDS`, only 4 of their hosts were readable
(`cosrx.com`, `mixsoon.us`, `medicube.us`, `nl.beautyofjoseon.com`; `beautyofjoseon.com`,
`skin1004.com` and `anua.us` all bot-challenge). On those: **40 / 400 delisted (10.0%)**, of
which **34 are hard 404s** and 6 are `live_delisted` — a **confirmed-dead rate of 8.5%**.

**The other direction, checked.** A control sample of 150 seeds whose handle *is* in the
brand's catalogue was probed the same way: 67 resolved, of which **66 were live and 1 was a
404** (`meritbeauty.com/products/pre-seeding-lip-liner-ext-eu` — a regional handle listed in
`products.json` whose PDP is gone). So the catalogue join misses roughly **1.5%** of dead
links, which makes 10.4% a floor rather than a ceiling.

**A note on the reported "60 of 438".** A stage-1 `products.json` join alone is an upper
bound, not a dead-link count: 5 of cosrx.com's 12 delisted handles serve a live product page.
The direction of the correction is small here — 85% of the six brands' delisted handles really
are 404s — but the two numbers are not the same measurement and should not be quoted
interchangeably.

---

## 4. Should something have caught this? — three lanes, none of which can

### 4.1 `stale_snapshot` is a clock, not an observation — and it is fail-open

`services/external_referral_readiness.py` declares a **blocker**:

```python
EXTERNAL_REFERRAL_STALE_DAYS = 7
...
if extracted_dt is not None and extracted_dt < (now - timedelta(days=7)):
    findings.append({"anomaly_type": "stale_snapshot", "severity": "blocker", ...})
```

Three problems, in increasing order of consequence:

1. **`extracted_dt is not None`** — a seed with no timestamp at all is treated as fresh. A
   missing observation reads as a good one.
2. **The clock is the wrong column.** `get_last_extracted_at` falls back to
   `row["updated_at"]`, so *any* writer — a PATCH from the employee console, a backfill —
   resets the freshness clock without anyone having looked at the page. It measures "when did
   we last write this row", not "when did we last see this URL".
   (`scripts/backfill_shopify_variant_ids.py:165` already documents dodging this.)
3. **It is a hard blocker that currently refuses the entire corpus.** Running the shipped gate
   (`should_block_external_referral_runtime`) against live prod rows: **400 / 400** sampled
   active seeds → `blocked`, reason `stale_snapshot`; **60 / 60** for the warm-handoff brands.
   100% of the active corpus is past the 7-day threshold. A blocker that fires on everything
   is not a safety property — it is an off switch nobody noticed, and the lanes that actually
   publish these URLs do not consult it (§4.3).

### 4.2 The refresh can correct a price. It cannot see a 404.

`_refresh_external_seed_by_id` (routes/employee_products.py) fetches the live page, and since
2026-08-22 it correctly overwrites a stale price/availability rather than `COALESCE`-ing them.
But it has no notion of the destination being *gone*:

```
resolve_external_offer(...)          # services/external_offers_service._fetch_html
  -> resp.raise_for_status()         # a 404 becomes an exception here
except Exception:
    return {"status": "degraded", "error": f"snapshot_failed: {exc}"}
```

A 404 is indistinguishable from a timeout, a TLS error, or a bot challenge. The seed is not
touched, stays `status='active'`, and is served again on the next request — forever. There is
no HTTP status recorded anywhere, no failure counter, and no path from "we fetched it and it
was gone" to any column a consumer reads.

The one anomaly that *sounds* like a dead-link check, `non_product_fallback_page`
(`services/external_seed_audit.detect_non_product_fallback`), is a regex over the **stored**
title/description/path. It never fetches anything.

### 4.3 The batch runner exists, has never been scheduled, and would not reach far enough

`jobs/external_referral_refresh.py` → `run_external_referral_refresh_batch` is real, tested,
and CLI-only:

* **Not registered anywhere.** `services/audit_scheduler.py` registers **32** jobs; none is
  this one. `infra/gcp/setup_scheduler.sh` has no entry either. (`docs/card-rail-readiness-audit.md`
  row A3 already called this out — it is still true.)
* **Reach is fine, throughput is not.** Its candidate query takes attached seeds first;
  measured on prod, **11,343 of 11,352** active seeds are attached, so reach is ~100%. But the
  default `--limit` is **500**, i.e. **23 days for one full pass** — against a `stale_snapshot`
  threshold of 7 days. The second arm (unattached seeds whose domain matches a
  `merchant_stores` domain) matches **0** rows in prod and is dead weight.
* Even at full reach it would not help, because of §4.2: a dead destination produces
  `degraded` and changes nothing.

### 4.4 …and two publishers bypass the gate entirely

`offers.resolve` does call the gate on every one of its three external paths
(`_append_external_offers_from_seed_rows`, plus the attached- and identity-retries). These do
not:

* the **gateway's `external_seeds` discovery provider** — `commerce.mcp` reads
  `external_product_seeds` straight from Postgres (see `infra/gcp/deploy_gateway.sh:161`) and
  serves `destination_url` / `merchant_canonical_url` / `offers[].url` with no readiness
  evaluation. This is the anonymous route that served the 404 above. *(separate repo)*
* the **seed → `catalog_products` mirror**, whose rows are read by
  `routes/pivota_canonical_routes` as `merchant_canonical_url`. That serializer already carries
  a `renderable` flag for *our* canonical URL, with a comment ending "The record was citable;
  the URL was dead." There is no equivalent signal for the merchant's own URL — the one that
  actually rots.

**Verdict: the lane is missing.** What exists is a timestamp threshold with no observation
behind it, a refresh that is blind to the failure mode, and a batch job that has never run on
a schedule.

---

## 5. The fix — IMPLEMENTED

Shipped in this change. (1)–(3) are the mechanism; (4) makes it run; (5) closes the serving
gap; (6) is the decision that had to be made either way.

### 5.1 Store the observation, not the write ✅

`db/migrations/200_external_seed_destination_liveness.sql` (+ the matching `db/schema_guard.py`
self-heal, because Railway deploys skip `db/migrations/`) adds to `external_product_seeds`:

| column | meaning |
|---|---|
| `destination_checked_at TIMESTAMPTZ` | when we last *reached the origin* for this URL |
| `destination_http_status INT` | what it answered |
| `destination_verdict TEXT` | `live` / `dead_404` / `redirected_off_product` / `redirected_to_product` / `live_delisted` / `unverifiable` |
| `destination_failure_streak INT NOT NULL DEFAULT 0` | consecutive *confirmed-dead* observations |

Written **only** by a fetch that got an answer from the origin. Never by a PATCH, never by a
backfill, never by `updated_at`. A `CHECK` constraint closes the verdict vocabulary, and a test
asserts the constraint and the Python enum cannot drift apart.

The readiness gate now reads `get_last_destination_check_at` — which has **no fallback** — so
NULL means "never verified" and raises a new `destination_never_verified` blocker. That is what
closes the fail-open: `if extracted_dt is not None and …` had made a missing observation read
as a good one.

`get_last_extracted_at` survives, still fed by `updated_at`, because the employee dashboards
render it as "when was this seed last extracted". It now carries a warning that it is not a
freshness signal.

### 5.2 Let the refresh see a dead link ✅

**The chain was worse than blind.** `raise_for_status()` threw, `resolve_external_offer` caught
it and returned the *cached* snapshot, `_refresh_external_seed_by_id` wrote that snapshot and
set `updated_at = NOW()` — so fetching a 404 made the row look **fresher** to the staleness
gate. Three changes break it:

* `services/external_offers_service` raises a typed
  `ExternalOfferUnavailable(status_code, url, final_url)` instead of a bare
  `httpx.HTTPStatusError`. Same control flow — every existing caller catches `Exception` — but
  the status now survives the throw.
* `resolve_external_offer` gains `raise_on_unavailable` (**default False**, i.e. today's
  behaviour exactly: serving still prefers a cached snapshot over nothing). A caller asking
  about the *link* rather than the *content* opts in. A transport failure keeps the fallback
  either way — a timeout says nothing about the product.
* `_fetch_html` gains an `observed` out-parameter carrying the status and the **final URL**, so
  a 301 onto a collection page — 92 of the 490 measured, and it answers 200 — is seen too.
* `_refresh_external_seed_by_id` records the observation and returns a `destination_refresh`
  report beside `price_refresh` / `availability_refresh`.

### 5.3 Retire only on confirmed death, and never on silence ✅

`services/external_seed_destination_liveness` sets `status='inactive'` (plus a dated note) when
**`destination_failure_streak >= 2`** and the verdict is in
`{dead_404, redirected_off_product}`. The 24h gap is enforced at increment time — a second look
inside `RETIREMENT_MIN_GAP` does not advance the streak — so one bad night retires nothing and
re-running a failed sweep is free.

**A dead verdict only advances the streak when stage 1 CORROBORATED it** — i.e. we read this
brand's `/products.json` successfully *and* the handle was absent from it. A probe on its own
never can, no matter how often it repeats.

Why repetition is not enough on its own: `/products/<slug>` is a URL *shape*, not a platform,
and a WAF that answers 404 to an unfamiliar client is byte-identical to a deleted product. A
WAF policy is also **more** persistent than a dead product, so it clears the 24h gap by
construction — the hysteresis filters transients, and this is not one.
`services/live_offer_verification._check_one` (#1868) refuses the same inference for the same
reason, requiring positive evidence of a Shopify storefront before it will call a 404 `gone`.
A successful catalogue read is that evidence, and the missing handle is a second, independent
witness.

The consequence: **only the sweep can retire.** `_refresh_external_seed_by_id` records its
observation — a real fact the serving gate acts on — but its verdicts are uncorroborated, so
they hold the streak where it is. That also closes an unflagged path: the refresh called
`retire_seed_for_dead_destination` directly, and `EXTERNAL_SEED_DESTINATION_SWEEP_RETIRE` gates
only the sweep, so `jobs/external_referral_refresh` could retire seeds with the sweep switched
off entirely.

**A verdict only ever describes the URL we actually serve.** The refresh fetches
`destination_url` while the gate and the sweep both resolve a seed to
`canonical_url or destination_url` — and the refresh itself rewrites `canonical_url` from the
fetched page, so the two drift apart by design. When they differ the refresh records nothing;
the sweep observes the served URL.

The mirrored `catalog_products` rows are withdrawn through the **existing** `suppressed_at`
control that `routes/pivota_canonical_routes` already honours, not a new serving flag. The
reason `external_seed_destination_dead` is deliberately **not** added to
`_TERMINAL_SUPPRESSION_REASONS`: a brand can republish a product, so this is reversible
containment and must answer 404, never a permanent 410.

**Everything else must be inert.** `unverifiable` — bot challenge, 429, 5xx, DNS, TLS, timeout,
`robots.txt` — increments a *coverage* metric and **writes no destination fact at all**: not the
verdict, not the status, not the clock. Freezing only the clock and the streak is not enough,
because the verdict is the field serving reads: a single 429 landing on a seed sitting at
`dead_404` with a full streak would otherwise overwrite the verdict, clear `destination_dead`,
and hand the 404 link back to the serving lane until another confirmed-dead observation
happened to arrive. This is not a
hypothetical guard rail: **213 of 286 hosts were unreadable** from a non-crawl-egress client
during this audit, and 99 of the 491 delisted URLs could not be probed even on hosts whose
`products.json` we had just read. A reaper that treated "cannot verify" as "dead" would have
deleted most of the corpus.

`live_delisted` and `redirected_to_product` also do not retire the seed — the link works. Both
reset the failure streak. `redirected_to_product` is a **repair signal** (rewrite
`canonical_url` to the target rather than dropping the row); acting on it automatically is not
in this change.

### 5.4 Two-stage sweep, on a schedule, from the crawl egress IP ✅

`jobs/external_seed_destination_sweep`, registered in `infra/gcp/setup_scheduler.sh` as a
**Cloud Run Job via `mkcrawljob`** (subnet `pivota-crawl`) on a `20 2 * * *` trigger. The
catalogue join is ~90× cheaper than probing every PDP (44 requests vs 3,951 seeds), so use
it as the candidate finder:

1. **Nightly, per host:** read `/products.json`; mark every seed whose handle is absent as a
   *candidate*. Stamp nothing for a host that returned `bot_challenge` / `incomplete` — record
   coverage instead.
2. **Then, per candidate only:** probe the PDP and write the verdict from §5.1.

Not a 33rd APScheduler job (`docs/card-rail-readiness-audit.md` §3.1): it crawls merchant
storefronts, so it must leave from the reserved crawl-egress NAT
(`infra/gcp/setup_crawl_egress.sh`) rather than a web dyno. Sized at **1,700 seeds/day** — a
full pass inside the 7-day window.

**Two switches, both defaulting off**, and the trigger is created paused like every other:
`EXTERNAL_SEED_DESTINATION_SWEEP` creates the job; `EXTERNAL_SEED_DESTINATION_SWEEP_RETIRE`
lets it withdraw seeds (the script refuses the second without the first). Observing is useful
on its own — until the sweep has run, every seed is `destination_never_verified` — so the first
production run should be observe-only.

Each run reports `dead_links_found`, `seeds_retired` and `hosts_unverifiable`, and
`coverage_alarm()` logs at WARNING when more hosts were unreadable than readable. **That is the
dial to alert on, not the dead-link count**: a run that cannot see its hosts reports zero dead
links and looks identical to a healthy one — which is the state in force from anywhere outside
the crawl egress (213 of 286 hosts).

### 5.5 Close the two ungated publishers — one done, one is another repo

* **`catalog_products` mirror ✅** — retirement suppresses every mirror row with
  `source_ref = <seed id>`, so the sig stops serving and `merchant_canonical_url` stops being
  emitted. Backfilling the 22 already-stale mirror rows whose seed went `inactive` before this
  existed is a one-off script, not shipped here.
* **Gateway discovery provider — NOT DONE** *(separate repo, PIVOTA-Agent)*. It reads
  `external_product_seeds` straight from Postgres, so the fix is a predicate, not a code path:
  `status = 'active' AND destination_verdict IS DISTINCT FROM 'dead_404' AND
  destination_verdict IS DISTINCT FROM 'redirected_off_product'`. It cannot drift from the
  backend's opinion because it *is* the backend's opinion — but until that PR lands, the
  anonymous `commerce.mcp` search lane keeps serving whatever the seed row says.

### 5.6 What the gate now refuses, and why that is not a regression ✅

The gate asks **two independent questions**, because one field cannot answer both:

| blocker | asks | cleared by |
|---|---|---|
| `stale_snapshot` | is the CONTENT (price, availability) recent? | a content refresh |
| `destination_stale` | has the LINK been re-verified recently? | a sweep pass |
| `destination_never_verified` | has the link EVER been verified? | a sweep pass |
| `destination_dead` | did we look, twice, a day apart, and find it gone? | repointing or retiring the seed |

**Collapsing those first two was a defect in the first draft of this change, and a serious one.**
Replacing the content-age check with the destination check reads as a strict improvement and is
not: the sweep stamps `destination_checked_at` from a catalogue read *without ever reading a
price*. So on the day the first full pass completed, ~11.3k rows carrying a median-56-to-99-day
old price would have flipped from blocked to healthy — a serving regression created by the
change meant to close one, and invisible to any measurement taken before the sweep ran.
`stale_snapshot` therefore keeps its own clock (`snapshot.extracted_at`, with the `updated_at`
fallback and the fail-open both removed), and the link check is a separate blocker.

**Today's verdicts do not change.** Before this, every one of the 11,352 active seeds was past
the 7-day threshold on the `updated_at` clock — measured by running the shipped gate against
live rows: 400/400 sampled seeds `blocked`, reason `stale_snapshot`. After this they are
`destination_never_verified` (all of them) and, for the ~2,793 rows with no
`snapshot.extracted_at` at all, `stale_snapshot` as well. Same answer, truthful reasons, and for
the first time **recoverable** ones: a sweep pass clears the first and a content refresh the
second, where nothing could clear the old one.

That the two clear separately is the point. A seed does not serve again until we have both read
its page *and* confirmed its link — which is exactly the pair that was missing when 10.4% of
measurable links were already dead.

Demoting staleness to `review` and letting the destination verdict carry the block on its own is
the better long-run shape. It is deliberately not done here: it would widen serving on the
strength of a lane that has not yet run once in production.

---

### 5.7 What an adversarial review of this change found ✅

Five defects, all in code that already had passing tests. Recorded because the shape repeats:

1. **A healthy 200 on a non-Shopify-shaped URL was a CONFIRMED-DEAD verdict.** `classify_destination`
   asked "did we land on the handle we asked for"; when the seed's URL carries no handle the
   question has no answer, and its absence fell straight through to `redirected_off_product`.
   **682 of the 11,352 active seeds** carry such a URL. The sweep never sees them
   (`group_by_host` drops handle-less rows), so only the refresh route reached it — the same
   route that could then retire.
2. **An `unverifiable` observation cleared the `destination_dead` blocker** by overwriting the
   verdict, even though it correctly froze the clock and the streak. See §5.3.
3. **The content-freshness gate was replaced rather than joined.** See §5.6.
4. **The `observed` out-param — the sole delivery mechanism for the live path — had no test.**
   Deleting all three lines that populate it left 181 tests green, because every refresh test
   stubs `resolve_external_offer` and hand-fills the dict. Now driven through the real
   `_fetch_html`.
5. **The mirror withdrawal matched on `source_ref` alone**, where the documented seed→product
   link is the `(source_ref, source_system)` pair, and omitted the `updated_at` bump every other
   suppression writer sets.

Two more, found while checking what had landed on `main` in the meantime: the migration was
numbered **199**, colliding with #1867's — the number was picked from a listing taken before the
branch was cut — and `resp.status_code >= 400` is not the predicate `raise_for_status()` used,
which throws on any non-2xx including an unfollowed 3xx.

---

## 6. Not done here

* **The gateway's discovery provider** (§5.5) — a different repo. Until it ships, the anonymous
  `commerce.mcp` search lane still serves dead seed URLs.
* **Backfilling the 22 orphaned mirror rows** whose seed was already `inactive`.
* **Acting on `redirected_to_product`** — the repair (rewrite `canonical_url` to the target) is
  recorded as a verdict but not applied.
* **Arming the sweep in production.** Both switches default off and the trigger is created
  paused; the first run should be `--no-retire`.
* Coverage of the measurement is 37% of the handle-bearing corpus. The remaining 63% needs a run
  from the reserved crawl-egress IP; the audit script takes `--host` and a seed dump so it can
  be pointed at them from there without change.
* The warm-handoff click lane is deliberately untouched.
