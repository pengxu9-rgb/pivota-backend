# Dead PDP links in the external-seed corpus — audit + proposed fix

**Measured 2026-08-25** against prod (`external_product_seeds`, 11,352 active rows) and the
brands' own live storefronts. Read-only throughout; no seed row was modified.

Reproduce with `scripts/audit_external_seed_destination_liveness.py` (added by this change).

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

## 5. Proposed fix

Five pieces. (1)–(3) are the fix; (4) makes it run; (5) closes the serving gap.

### 5.1 Store the observation, not the write

Migration adding to `external_product_seeds`:

| column | meaning |
|---|---|
| `destination_checked_at TIMESTAMPTZ` | when we last *reached the origin* for this URL |
| `destination_http_status INT` | what it answered |
| `destination_verdict TEXT` | `live` / `dead_404` / `redirected_off_product` / `redirected_to_product` / `live_delisted` / `unverifiable` |
| `destination_failure_streak INT NOT NULL DEFAULT 0` | consecutive *confirmed-dead* observations |

Written **only** by a fetch that got an answer from the origin. Never by a PATCH, never by a
backfill, never by `updated_at`. Then point `get_last_extracted_at`'s staleness check at
`destination_checked_at` and drop the `updated_at` fallback — a NULL there must read as
"never verified", which is the honest answer for the whole corpus today.

Removing the `updated_at` fallback also removes the fail-open: `extracted_dt is not None`
becomes a real condition rather than an accident.

### 5.2 Let the refresh see a dead link

`_fetch_html` already knows the status code and throws it away.

* Raise a typed `ExternalOfferUnavailable(status_code=…, url=…)` from
  `services/external_offers_service` instead of letting the bare `httpx.HTTPStatusError`
  escape (or attach `http_status` to the snapshot).
* In `_refresh_external_seed_by_id`, catch it and write `destination_verdict` +
  `destination_http_status` + `destination_checked_at`, incrementing or resetting
  `destination_failure_streak`. Return a `destination_refresh` report next to the existing
  `price_refresh` / `availability_refresh` so `run_external_referral_refresh_batch` can
  aggregate a **dead-link rate per run** — the number that justifies the job existing.
* A `raise_for_status()` on a 404 must stop meaning the same thing as a socket timeout.

### 5.3 Retire only on confirmed death, and never on silence

`status='inactive'` (plus a note) when **`destination_failure_streak >= 2`** with the
observations at least 24h apart and the verdict in `{dead_404, redirected_off_product}`.

**Everything else must be inert.** `unverifiable` — bot challenge, 429, 5xx, DNS, TLS, timeout,
`robots.txt` — must increment a *coverage* metric and never the failure streak. This is not a
hypothetical guard rail: **213 of 286 hosts were unreadable** from a non-crawl-egress client
during this audit, and 99 of the 491 delisted URLs could not be probed even on hosts whose
`products.json` we had just read. A reaper that treated "cannot verify" as "dead" would have
deleted most of the corpus.

`live_delisted` and `redirected_to_product` must also not retire the seed — the link works.
`live_delisted` is worth a review-severity anomaly (the brand has unlisted it, so it is
probably being wound down), and `redirected_to_product` is a **repair signal**: rewrite
`canonical_url` to the redirect target instead of dropping the row.

### 5.4 Two-stage sweep, on a schedule, from the crawl egress IP

The catalogue join is ~90× cheaper than probing every PDP (44 requests vs 3,951 seeds), so use
it as the candidate finder:

1. **Nightly, per host:** read `/products.json`; mark every seed whose handle is absent as a
   *candidate*. Stamp nothing for a host that returned `bot_challenge` / `incomplete` — record
   coverage instead.
2. **Then, per candidate only:** probe the PDP and write the verdict from §5.1.

Register it as a **Cloud Run Job + Cloud Scheduler** entry in `infra/gcp/setup_scheduler.sh` —
not as a 33rd APScheduler job (`docs/card-rail-readiness-audit.md` §3.1) — so it runs on the
reserved crawl-egress NAT (`infra/gcp/setup_crawl_egress.sh`) rather than from a web dyno.
Size it for a **full pass inside the 7-day `stale_snapshot` window**: ~1,700 seeds/day, which
stage 1 makes trivial. Raise `run_external_referral_refresh_batch`'s default limit accordingly
and drop the `merchant_stores` arm (0 rows).

Publish three counters per run — `dead_links_found`, `seeds_retired`, `hosts_unverifiable` —
and alert on the third. A crawl lane that quietly stops being able to see 74% of its hosts is
the failure mode to watch for, and it is the one currently in force.

### 5.5 Close the two ungated publishers

* **`catalog_products` mirror:** propagate the verdict onto the mirrored row and stop serving
  `merchant_canonical_url` for a confirmed-dead destination — the same "tell the truth about
  the link" treatment `renderable` already gives our own canonical URL. Also retire mirror rows
  whose seed went `inactive` (22 already stale).
* **Gateway discovery provider** *(separate repo, PIVOTA-Agent):* it reads the seed table
  directly, so give it the column, not a code path — a `destination_verdict NOT IN
  ('dead_404','redirected_off_product')` predicate in its query is the whole fix, and it cannot
  drift from the backend's opinion because it *is* the backend's opinion.

### 5.6 The `stale_snapshot` decision that has to be made either way

Once §5.1 lands, the gate stops being vacuous — and it will still refuse 100% of the corpus
until the first full sweep completes. Someone has to choose, explicitly:

* keep it a **blocker** and accept that the external lane serves nothing until the sweep has
  run (safe, and honest about what we actually know); or
* demote it to **review** and let the *destination verdict* — a fact, not a clock — be the
  blocker.

The second is the better shape, but it is only available *after* §5.1–§5.4 exist. Until then,
the 7-day clock is the only staleness signal there is, and it is being ignored by exactly the
lanes that publish.

---

## 6. Not done here

* No seed rows were modified, no columns added, no jobs registered. This is a measurement and a
  proposal.
* Coverage is 37% of the handle-bearing corpus. The remaining 63% needs a run from the reserved
  crawl-egress IP; the audit script takes `--host` and a seed dump so it can be pointed at them
  from there without change.
* The warm-handoff click lane is deliberately untouched.
