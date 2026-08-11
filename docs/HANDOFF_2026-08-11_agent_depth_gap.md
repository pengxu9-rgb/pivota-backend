# HANDOFF — the agent depth gap (Phase 2)

Written 2026-08-11 at the end of the session that fixed agent *readability*.
Start here; you do not need to re-read that session.

## The one-sentence problem

An outsider AI agent can now read Pivota's catalog cleanly — discovery, search,
resolve, cite, and crawlable PDPs all work and are live-verified — but per
product it gets **identity + price + availability + image + a description, and
rarely anything else**. The fields that would make an agent *prefer* Pivota over
a retailer's own listing — ratings, INCI, GTIN — are present on a small
minority. Closing that is Phase 2.

## Measured baseline (2026-08-10/11, re-measure before trusting)

Sampled 18 advertised products through `GET /agent/v1/citation/{sig}`:

| Field | Coverage |
|---|---|
| image | 100% |
| URL renderable | 100% |
| description (>200 chars) | 83% |
| substantiated claims | 33% |
| **aggregate_rating** | **6%** |
| **bullet_points** | **0%** in that sample |
| **usage_scenarios** | **0%** in that sample |

Corpus scale: **7,846** URLs advertised in `sitemap-products.xml`; **8,868**
rows in the canonical feed (`GET /api/canonical/products`); ~**11,122** rows in
`agent_pdp_view`. GTIN: `gtin13` non-empty on **1** of 11,122 view rows.

### A methodology warning that already burned me twice

**Measure at the SOURCE table, never at the serving view.** I twice concluded
"the data doesn't exist" from a view-level sample and was twice wrong:

- `bullet_points` read 0/39 at the view, so I called it a data gap. The source
  `product_enrichment` table holds **360 rows — 355 with bullet_points, 265 with
  usage_scenarios** — and 217 already serve. A sitemap-stratified sample simply
  missed a ~2–3% cohort. (0/39 only bounds prevalence below ~7.5%.)
- I also reported flags as "off" from code defaults when prod had them **on**.
  Always `railway variables --service <svc> --json` before claiming a flag state.

## The four workstreams, in recommended order

### 1. Enrichment propagation backfill — smallest, do it first

~**138** enriched rows (360 in `product_enrichment` minus 217 in the view)
predate the `SERVE_PDP_ENRICHMENT_ON_WRITE` flip and never reached
`agent_pdp_view`. That flag is now **`=1` in prod**, so new writes propagate;
only the historical set is stranded. One targeted re-assembly of the enriched
`content_key`s surfaces them — and as a free side effect repairs the stored
double-encoded `taxonomy_tags` on those rows (see "already fixed" below).

Entry points: `services/agent_pdp_view_assembler.py`
(`refresh_agent_pdp_view_for_content_key`), `scripts/backfill_agent_pdp_view.py`.

### 2. INCI ingestion — biggest content-depth jump available

`scripts/backfill_seed_inci.py` exists and its docstring states the situation:
the crawl captured INCI into `seed_data.inci_list` / `pdp_ingredients_raw` for
**~3,300 serving products, but only ~135 were ever ingested** into
`beauty_sku_ingredients`. The script feeds the rest through the same pipeline
(`ingest_crawled_inci_items` → `enrich_and_persist_product` → INCI-substantiated
claims) and re-materializes `agent_pdp_view`.

It is idempotent, skips `product_key`s that already carry `raw_inci`, and honors
ADR-001 source precedence (`may_write` guards against downgrading a
higher-authority INCI source). Validity gates: `MIN_INCI_LEN = 20`,
`MIN_INCI_COMMAS = 4` — marketing "key ingredients" bullets are not INCI.

**Run it as a dry run first and put the by-brand report in front of the user
before `--apply`.** Chunk the large run with `--limit`.

Why it matters beyond coverage: INCI is what ingredient-constrained queries
ground on, and that is the `ingredient_concern` tier the AEO portfolio calls
"the K-beauty wedge: constraint-dense long tail" — the tier we are most likely
to win.

Related scripts: `ingest_crawled_inci.py`, `ingest_canonical_inci.py`,
`attach_inci_minted_canonicals.py`, `backfill_ratings_inci.py`.

### 3. Ratings expansion — the pipe is proven, the data is thin

The full path **works end to end and is live** (verified: a PDP serves
`aggregateRating {ratingValue 4.6, reviewCount 148}` in JSON-LD, and
`aggregate_rating {value, count}` on both agent APIs). Only **186** products
have data.

- `scripts/backfill_ratings_inci.py` — the ~2,168 brand-official canonicals
  carry "rating 0% and INCI ~1.6%"; StyleKorean is currently the **only** rating
  source (Shopify `/products.json` exposes none), recoverable by re-crawling
  `catalog_offers.source_ref` for rows with a `stylekorean_global` offer.
- Then widen capture: `_parse_aggregate_rating`
  (`services/external_offers_service.py:~994`) already parses schema.org
  `aggregateRating`; other storefronts the crawler visits expose it too.

**Contract, non-negotiable (migration 186):** a rating is NEVER invented. NULL
means "no review data on the source page", not zero stars. Values clamp to
[0,5]; emit only with a positive review count.

### 4. GTIN capture — longest pole, start early

`gtin13` is effectively empty (1 of 11,122). Per
`PIVOTA-Agent/docs/gtin_enrichment_pipeline_scope.md` the well is dry at the
source: `catalog_skus.barcode` is populated for **0 of 5,229** merchant SKUs and
**10 of 12,687** external-seed SKUs.

Chain to build, in order:
1. Ingest Shopify variant `barcode` into `catalog_skus.barcode`.
2. Widen the gateway's `limitedVariantsSql` jsonb key whitelist
   (`PIVOTA-Agent/src/server.js:~5209`) — it currently **strips**
   `barcode`/`gtin`/`upc`/`ean` before JS ever sees them.
3. Emit `barcode` on the variant object in `buildVariants`
   (`PIVOTA-Agent/src/pdpBuilder.js:~2078`). The UI's `_readGtin`
   (`pivota-agent-ui/src/app/products/[id]/productJsonLd.ts:~490`) already reads
   `gtin13 || gtin || barcode` and validates 8/12/13/14-digit lengths — the
   consumer is built and waiting.
4. Only after data exists, restore the GTIN claim in `llms.txt`
   (`pivota-agent-ui/src/app/llms.txt/utils.ts`) — I removed it because it was
   false.

Why it matters: GTIN is the reconciliation primitive. Without it a comparison
agent cannot answer "is this the same product I saw elsewhere", which is the
core operation of the agents we want citing us.

## Already fixed — do not redo

- **`taxonomy_tags` double-encoding** (backend #1715, live): list members arrived
  as JSON-encoded *strings*. Fixed at write (`build_taxonomy_tags`) and read
  (`normalize_taxonomy_tags`, applied on both agent surfaces). Read-side repair
  is **permanent**, not transitional — the reconciler only re-assembles rows
  whose truth timestamps moved, so historical rows keep the broken *stored*
  shape until `scripts/backfill_agent_pdp_view.py` runs (workstream 1 does this).
- **`aggregate_rating` projection** (backend #1704 + gateway #1943): both agent
  APIs and the PDP JSON-LD. The sig-route needed
  `buildCatalogIdentityFromSignatureProductRef` to name the field — that builder
  returns a NEW object and silently drops anything unnamed. Remember this if you
  add fields to the PDP payload.
- **`resolve_product_candidates` sig lane** (gateway #1944), **citation search**
  (flag was already on), **410 for retired sigs**, **browse-page SSR**,
  **sitemap hygiene + IndexNow removal pings**.

## Known gaps that are NOT depth (don't get distracted)

- `availability: "unknown"` on some search cards — honest pass-through of a
  source page with no stock signal. Deliberate; leave it.
- `taxonomy_tags: []` on citation **search** rows — the search projection builds
  a light synthetic row that omits it. Cosmetic inconsistency, one line if you
  care: `_search_row_to_citation` in `routes/agent_citation_v1.py`.
- No shipping/returns on agent surfaces — data exists only in the flag-gated
  internal readiness exports; publishing it is a **product decision** (is it
  accurate per-merchant?), not a coding task.
- Backend `/openapi.json` is a 1.15 MB, 1,026-path uncurated dump with
  `security: []` and no tags, while the good curated
  `/agent/docs/openapi.json` exists and nothing links to it. Real
  discovery-quality gap, separate workstream.
- 364 of the advertised URLs are ~510-char chrome-only shells (7 brands, all
  `external_seed`) — `services/pdp_content_depth.py` documents this precisely.
  These cannot be fixed by rendering; they need content.

## How to verify you moved the needle

Re-run the same 18-product citation sample and the source-table counts. Then the
real scoreboard: `scripts/aeo_phase0_citation_baseline.py` — the demand-side
metric. **It has never had a committed result**; the repo's last written word is
"index presence still at zero". Commit the artifact this time.

Blocked on two external steps owned by the user (both in flight as of
2026-08-11): allowing web search in the GCP org policy
`constraints/vertexai.allowedPartnerModelFeatures` (default-deny) so the Claude
lane can run, and reading GSC coverage ~2 weeks after the 2026-08-10 sitemap
submission.

## Operational notes that will save you an hour

- **Railway public proxy is unreliable** (it was down for hours on 2026-08-10).
  Working in-cluster escape hatch:
  `printf 'cmd\nexit\n' | script -q /dev/null railway ssh --service web`
  — plain `railway ssh -- cmd` connects but relays no output.
- Prod DB via proxy from repo scripts needs
  `?sslmode=require` + `DB_POOL_ACQUIRE_TIMEOUT_SECONDS=60` +
  `DB_COMMAND_TIMEOUT_SECONDS` and a retry loop. See
  `docs/` neighbours and the memory note on this.
- macOS has **no `timeout(1)`** — `timeout N cmd` exits 127 and can look like a
  clean zero-match.
- The postgres dialect gates (`tests/test_*_postgres.py`) need a real Postgres.
  Local recipe: brew `postgresql@15` on port 55433, `initdb` with
  `LANG=C LC_ALL=C --locale=C`, and `-c unix_socket_directories=` (empty) because
  the scratch path exceeds the socket length limit.
- **Convention in this project:** dispatch an adversarial review before every
  merge. It caught a real, blocking defect in 4 of 6 PRs this session —
  including one my own tests structurally could not catch.
