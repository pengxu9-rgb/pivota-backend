# Card-Rail Transaction Readiness Audit

**Date:** 2026-08-21 · **Mode:** read-only · **Scope:** `pivota-backend` (this repo) + `PIVOTA-Agent` gateway (read-only, worktree `_worktrees/pivota-agent-search-zeros-20260820` @ `a89706a70`) + live production surfaces (`api.pivota.cc`, commit `eeb5f6cd`).

**Measured against:** *A front-end agent sends Pivota a natural-language purchase intent in beauty or 3C. Pivota returns a ranked recommendation plus a machine-executable execution spec. The agent completes a real card purchase on the merchant's storefront using that spec. The outcome is logged back and attributable to the specific recommendation.*

> **Platform assumption:** Pivota is mid-migration from Railway to Google Cloud (decided 2026-08-19; prod cutover **Sep 8–12**, soak to Sep 26). **Every new scheduled job and every new database object recommended below targets GCP, not Railway** — Cloud Run Jobs + Cloud Scheduler via `infra/gcp/setup_scheduler.sh`, and Cloud SQL `pivota-prod:us-west1:pivota-pg`. Section 3 covers what the migration changes about this audit; it changes more than the deployment target.

### How this audit was measured, and what it could not measure

| Source | Status |
|---|---|
| Code / schema / migrations, this repo | Full read |
| Gateway (`PIVOTA-Agent`) reco + warm-handoff lanes | Full read (local worktree, 2026-08-20 HEAD; not `origin/main`) |
| Live prod diagnostics — `/health`, `/__catalog_health`, `/__trust_health`, `/__catalog_invariants`, `/__scheduler_health` | Full read |
| Live prod public index — `/api/canonical/products` (9,095 rows enumerated; 3,000 detail records pulled) | Full read |
| **Direct production Postgres** | **Blocked** by the session's command classifier on every attempt. Every count below therefore comes from a live HTTP surface or a code path, never from SQL. Null-rates that are only visible in SQL are marked *not measurable here*. |
| Live merchant storefronts | 90 SKUs / 37 domains + 70 index domains probed (read-only GETs) |

Estimates are labelled **[est.]** with their sample size. Where a capability does not exist, it says *does not exist* — not "partial".

---

## 1. Verdict

**Yes — the October milestone is reachable, but the biggest obstacle is not the one the brief assumes, and the pilot should probably not be Key Entry.** The premise that "the merchant has integrated nothing" is **false for this index**: I probed the 70 highest-volume merchant domains in Pivota's own serving corpus and **66 of 70 (94.3%, covering 91.3% of sampled SKUs) serve a valid `/.well-known/ucp` profile and answer an *anonymous* MCP `create_checkout` / `get_checkout` call today** — Shopify has shipped the Universal Commerce Protocol platform-wide, so a live priced checkout (line items, shipping, tax, discounts, `continue_url`) is one unauthenticated call away per SKU, with zero merchant onboarding. Pivota already has a working client for exactly this (`services/ucpWarmHandoff.js` + `services/outbound_warm_handoff.py`), **live in production, allowlisted to 6 brands**. The real obstacle is on our side: **the index is structurally stale and the serving contract emits no execution spec.** There is no per-row crawl-freshness field, **no scheduled re-crawl job among the 31 live scheduler jobs**, the newest row in the entire 9,095-row canonical index is 13.1 days old, and the one refresh routine that exists writes price with `COALESCE(price_amount, :price_amount)` — so it *structurally cannot correct a stale price*. Measured consequence: on a 90-SKU live sample, **31.1% of index records would produce a wrong or unexecutable spec today** (12.3% price mismatch, 11.1% out-of-stock-but-listed, 6.7% dead PDP). The fix is not more crawling — it is to stop serving a remembered price at all and resolve price/stock/total live through the UCP endpoint the merchant is already running, at recommendation time.

**One new obstacle that only exists because of the GCP migration, and it is not small.** All Cloud Run services and Jobs deploy with `--vpc-egress all-traffic` behind a single Cloud NAT holding **one reserved IP** (`--nat-all-subnet-ip-ranges --nat-external-ip-pool` in `infra/gcp/setup_egress_nat.sh`). Prod is **`8.231.167.230`** — **the same address being handed to Antom/Adyen for payment IP-allowlisting, reserved specifically so it never changes.** I measured that ~50 requests across 37 Cloudflare-fronted merchant domains in about a minute is enough to trip a cross-domain, IP-level 429 lasting ~15 minutes. Put the re-crawl and the live-verification hop on Cloud Run as currently configured and **crawler traffic shares an IP reputation — and a NAT port pool — with the payment path, on an address you cannot rotate without a partner re-allowlisting cycle.** This must be separated before either capability ships. See §3.

---

## 2. Gap table

Effort: **S** ≤ 1 week · **M** 2–4 weeks · **L** > 4 weeks (one engineer).

### A. Index data layer

| Capability | What exists today | What the milestone requires | Gap | Effort | Blocking? |
|---|---|---|---|---|---|
| **A1. Merchant + SKU schema** | Full 4-level chain: `catalog_merchants` → `catalog_products` (60+ cols) → `catalog_skus` (variant grain, `source_variant_id`, `barcode`, `visible_attributes`) → `catalog_offers` (`list_price`, `merchant_effective_price`, `availability`, `inventory_quantity`, `market`, `price_confidence`). Plus `catalog_price_snapshots` / `catalog_inventory_snapshots` time series. | Same, plus a transactability dimension (see B) | Schema is more than adequate. Nothing to build here. | — | No |
| **A2. Coverage** | Live `/__catalog_health` (2026-08-21): **14,124** catalog rows, **9,689** serving corpus, **6,834** public. By source: `external_product_seeds_mirror_v1` 5,456 public / `catalog_enrichment_agent_v1` 1,378 public / **`shopify_products_sync` 1,537 rows, ALL `retired_by_design`**. Canonical index: 9,095 rows, 8,655 serving-eligible + renderable. **199 unique merchant domains** in a 3,000-row sample; top 23 domains = 50% of SKUs, top 66 = 80%. Category: beauty-dominant (`resolved_vertical` exists but its distribution is SQL-only, *not measurable here*). | ~100 merchants, beauty + 3C | **0% of the public corpus is merchant-integrated** — every serving row is crawl-derived (`platform = 'external_seed'` on 119/119 in a detail sample). 3C/electronics presence is not measurable from the public surface; assume near-zero given the source mix. Merchant count is fine; **category coverage for 3C is the gap.** | M (3C seeding) | Yes, for 3C only |
| **A3. Freshness** | `content_changed_at` over all 9,095 canonical rows: **p10 14.5d, p50 14.6d, p75 14.6d, p95 88.1d, min 13.1d, max 88.1d. 0% within 7 days.** Only 2 of the intake paths write this column (`brand_authored_intake`, `audit_index_intake`); the dominant mirror (`scripts/mirror_external_seeds_to_catalog_products.py`) is insert-only and explicitly "does not overwrite existing rows". `/__trust_health`: **14,128 / 14,128 trust rows stale > 24h.** `external_offer_snapshots.last_checked_at` exists but only for the OG-scraper cache (`EXTERNAL_OFFER_MAX_AGE_DAYS = 7`). | Per-record crawl recency, and a serving-time freshness gate | **There is no per-row crawl-recency field for the lane that carries 80% of the corpus.** `content_changed_at` is effectively row-creation time. **No scheduled re-crawl exists**: 31 live scheduler jobs, none of which re-fetches a merchant PDP for price/stock. `jobs/external_referral_refresh.py` exists but is CLI-only and is not registered with the scheduler. **Build it as a Cloud Run Job + Cloud Scheduler trigger in `infra/gcp/setup_scheduler.sh` — not as a 32nd APScheduler entry** (§3.1). | S (field + job) | **Yes** |
| **A4. Refresh correctness** | `routes/employee_products.py:4443` — the only price-refresh writer: `price_amount = COALESCE(price_amount, :price_amount)`, same for `availability`, `title`, `image_url`. | A refresh that corrects drift | **A refresh can never change a price that is already set.** Scheduling the job as-is would fix nothing. One-line class of fix, outsized value. | S | **Yes** |
| **A5. Accuracy spot-check** | **Measured 2026-08-21, n = 90 SKUs across 37 domains** (K-beauty D2C cohort, `cohorts/kbeauty_d2c_expansion.json`, stratified ≤3/domain), live re-fetch via Shopify `/products/{handle}.js` where available else JSON-LD: reachable 81/90. **Price mismatch 10/81 = 12.3%** (deltas +4.7% to +427.8%; two cases were index-high because a live promo was running, eight index-low). **Live out-of-stock while index says in-stock 9/81 = 11.1%.** **Dead PDP (404) 6/90 = 6.7%.** **Union — spec would be wrong or unexecutable: 28/90 = 31.1%.** Separately, **35/81 (43%) of live PDPs carried a `compare_at_price`** (an active markdown the index does not model at all). | ≤ ~2% effective error after live verification | **31.1% is the number to design against.** It is not fixable by re-crawling on any cadence we would actually run — the 43% promo rate means list price is wrong on nearly half of PDPs at any moment. | — | **Yes** (drives F) |
| **A6. Variant granularity** | Schema supports it (`catalog_skus.source_variant_id`). **Actual coverage: 838 / 2,992 = 28.0%** of serving rows carry any variant with a `variant_id` (3,000-row census). In the live sample, **34/81 = 42% of products are genuinely multi-variant** (Size / Shade / Color / Format). Numeric Shopify variant IDs were recoverable live for **81/81 (100%)** of reachable Shopify PDPs. | Address a specific purchasable variant | For ~72% of served rows we can name a product family but **not a purchasable variant**. The data is trivially recoverable live (`/products/{handle}.js` or UCP `catalog.lookup`) — it is simply not being collected. | S–M | **Yes** |

### B. Merchant transactability profile

| Capability | What exists today | What the milestone requires | Gap | Effort | Blocking? |
|---|---|---|---|---|---|
| **B1. The field class itself** | **Does not exist.** No column, table, or service anywhere models a crawled domain's transactability. `merchant_commerce_readiness_state` (foundation/discover/signals/execute) exists but is keyed to *onboarded* merchants and requires a connected store + PSP — inapplicable to a 100%-crawled cohort. | A per-domain profile | Build from scratch. | M | **Yes** |
| **B2. E-commerce platform** | Detection exists only for OAuth-connected stores (`_SUPPORTED_COMMERCE_PLATFORMS = {shopify, wix, woocommerce, bigcommerce}`). For crawled domains `catalog_products.platform` is the literal `'external_seed'` for 100% of served rows. | Per-domain platform | Not stored, not derivable from artifacts. **Measured live: 81/81 reachable cohort PDPs are Shopify (100%).** One GET derives it. | S | No (derivable cheaply) |
| **B3. Bot-management vendor** | **Does not exist.** No vendor detection anywhere. `external_offer_snapshots.evidence` stores only `{provider, fetchedAt, snapshotId, description, variants, image_urls}` — **no HTTP status, no response headers, no CDN identity**. Nothing is derivable without new probing. | Per-domain vendor + current block state | **Measured live: 90/90 cohort domains and 69/70 top index domains are Cloudflare-fronted; 1 is Akamai.** More important than the label: **~50 requests spread over 37 Cloudflare domains in about one minute tripped a *cross-domain* IP-level 429 that persisted ~15 minutes** — including on domains that had answered 200 moments earlier. A naive verification hop from one egress IP will throttle itself out of service — **and on GCP that one egress IP is the partner-allowlisted payment address** (§3.2). | S (probe) / M (egress isolation) | **Yes** |
| **B4. Guest checkout** | **Does not exist**, and is **not derivable without executing a checkout** — Shopify exposes no public signal for it. | Known per domain | Only two honest paths: (a) resolve it via UCP `create_checkout` (which succeeds or fails on the merchant's real rules), or (b) accept it as unknown and let the outcome graph learn it. Recommend (a). | S via UCP | No |
| **B5. Checkout field inventory / field-name map** | **Does not exist.** | A fill map per domain | Only needed if Key Entry is the pilot mode. **If the pilot uses the UCP lane instead, this whole row disappears** — the merchant prices and collects. Flagged per the brief: this is the single row where Key Entry vs API-based changes the answer most. | L (Key Entry) / — (UCP) | Only under Key Entry |
| **B6. Promo/coupon field presence** | `services/promo_terms.py` + `catalog_payment_incentives` (incl. `card_network`, `issuer_name`, `wallet_type` — a card-network-aware incentive model, unexpectedly relevant here). No per-domain coupon-field detection. | Codes + application order | UCP advertises `dev.ucp.shopping.discount` as a first-class capability on 66/70 domains — apply codes through the protocol, don't scrape a field. | S via UCP | No |
| **B7. Shipping + tax computable pre-checkout** | **Does not exist for crawled merchants.** A real landed-price engine *does* exist (`services/shopify_pricing_service.py` → `subtotal / discount_total / shipping_fee / tax / total` + `discount_evidence`), but it calls `POST /admin/api/{v}/checkouts.json` with `resolve_shopify_admin_access_token` — i.e. **it requires the merchant's OAuth token** and returns `"Shopify credentials missing"` for every crawled domain. | Landed total before handoff | The capability exists but is bound to the wrong precondition. **UCP `create_checkout` returns the same numbers anonymously** — and `dev.ucp.shopping.fulfillment` is advertised on 66/70 domains. Re-point, don't rebuild. | M | **Yes** |
| **B8. TAP support signals** | **Does not exist**, and the repo says so plainly: `docs/TARGET_ARCHITECTURE.md` — "x402 / Visa TAP / Mastercard Agent Pay: **Not implemented anywhere.** No placeholder code pretends otherwise." | Detect + record | But the adjacent signal is live and strong: `/.well-known/ucp` on **66/70 (94.3%)** of top index domains, all advertising `checkout`, `cart`, `discount`, `fulfillment`, `order`, `catalog.search`, `catalog.lookup` over `https://{shop}.myshopify.com/api/ucp/mcp`. Store the profile + capability set per domain. | S | No |
| **B9. Cart-prefill URL** | **Exists and is production-proven.** `services/outbound_links_service.py` builds `https://{host}/cart/{numeric_variant}:{qty}` with `attributes[pivota_click_id]` (survives into Shopify `note_attributes`). Separately the **warm-handoff lane** (`services/outbound_warm_handoff.py` + gateway `src/services/ucpWarmHandoff.js`) turns a crawled product into a **pre-built cart on the brand's own checkout** via anonymous UCP `create_cart`, returning `continue_url`. **`OUTBOUND_WARM_HANDOFF_ENABLED=true` in prod**, allowlisted to `cosrx.com, beautyofjoseon.com, skin1004.com, anua.us, medicube.us, mixsoon.us`. | Cart-prefill for the pilot cohort | The mechanism works. The gap is **reach** (6 brands of ~200) and **the numeric variant id**, which we hold for only 28% of rows (A6). | S (widen allowlist) | No |
| **B10. Derivable without re-crawling?** | **Almost nothing.** Stored artifacts carry no status, headers, or platform. | — | **Essentially 100% of B needs new probing.** Measured probe cost: 2 GETs/domain, ~1.3 s + ~1.6 s wall clock, plus 1 UCP well-known GET and 1 MCP `tools/list` POST (~0.5 s each). **≈4 requests and ~4 s per domain — but paced to ≤ ~1 req/s per egress IP** (see B3). ~200 domains ≈ 15 min single-threaded. Trivially cheap; the constraint is pacing, not compute. | S | — |

### C. Decision layer

| Capability | What exists today | What the milestone requires | Gap | Effort | Blocking? |
|---|---|---|---|---|---|
| **C1. Ranking logic + signals** | Two ranked lanes. (a) `services/beauty_external_ranking.py` — purely lexical/semantic: `category_anchor ≤0.28`, `active_ingredient ≤0.28`, `concern ≤0.24`, `formula ≤0.12`, `form_factor ≤0.10`. (b) `services/agent_ranking_service.py` — `w_rel 0.6 / w_quality 0.2 / w_enrichment 0.2 / w_business 0.0` over content-quality + enrichment features. | Relevance + executability | **Neither lane consumes price, merchant identity, freshness, stock, or completion probability.** `w_business` is wired and defaults to **0.0**. | — | No (but see C3) |
| **C2. Landed price** | List price only on the serving path. The landed-price engine exists but is OAuth-gated (B7). | Item + shipping + tax + stacked promos | Blocked on B7. | M | **Yes** |
| **C3. Completion-probability term** | Does not exist. | A term in the score | **Clean insertion point already present**: `RankedExternalBeautyCandidate.ranking_score_breakdown` (a dict) and `AgentRankingConfig.w_business` (already plumbed, weight 0). Add `p_complete` as a feature, set `w_business > 0`. The *scoring* change is S; **earning the prior is the real work** — it needs G's outcome graph, so it is chicken-and-egg. Seed it with the B-profile (UCP-reachable + fresh + variant-resolved) until outcomes accumulate. | S (plumbing) / M (prior) | No |
| **C4. Attribution-availability term** | `partner_type ∈ {none, affiliate, partner, unknown}` on `external_product_seeds` and `outbound_link_rules`; `utm_template` defaults to `utm_source=pivota&utm_medium={{tool}}&utm_campaign={{market}}`. **No affiliate-network integration exists** — the only network code is a *denylist* (`AFFILIATE_HOST_SUFFIXES` in `outbound_warm_handoff.py`, which deliberately refuses to warm-hand affiliate links because it would forfeit the commission). | A scoring term | The field exists; its population and its use as a ranking signal do not. | S | No |

### D. API surface

| Capability | What exists today | What the milestone requires | Gap | Effort | Blocking? |
|---|---|---|---|---|---|
| **D1. Endpoint inventory** | Live OpenAPI: **1,011 paths / 1,078 operations** on the backend (internal lane). External door is the gateway: **14 MCP tools**, **9 UCP tools** (`search_catalog`, `get_product`, `get_offers`, `get_intel`, `recommend_products`, `create/update/get/complete_checkout_session`, `create_payment_link`, `get_order`, `request_after_sales`, …). Per `ADR-021`, `/agent/v1/*` is internal, not the external contract. | One documented contract | Surface is large but the external door is coherent. | — | No |
| **D2. Auth + rate limits** | `X-API-Key` or `Authorization: Bearer` (`routes/agent_auth.py`); global `RATE_LIMIT_RPM` default **1000 / 60s** plus a per-agent `rate_limit`; anonymous ceiling + per-IP buckets in `middleware/rate_limiter.py`. | Same | Adequate. | — | No |
| **D3. Latency** | Measured 2026-08-21 from an external client (includes network RTT): canonical PDP detail **p50 279 ms / p95 644 ms** (n=30); list-500 **p50 359 ms** (n=8); `/health` **p50 274 ms** (n=10). The reco lane's own metadata reported **`latency_ms: 7652`** on a live probe. | p95 within the agent's budget | Read paths are fast. **The recommendation lane at ~7.6 s is the outlier** and leaves almost no room for a live-verification hop on top. | M | **Yes** (with F) |
| **D4. `intent → recommendation + execution spec`** | **Does not exist as one call.** Closest is `recommend_products`. Worse, the three lanes disagree on what a product even is: **MCP search** returns `merchant_id`, `merchant_name`, **`destination_url`** (real merchant PDP, e.g. `…/products/niacinamide-serum?Size=30ml&Option=Single`), `merchant_canonical_url`; **UCP search** returns none of those — its `url` and every `variants[].id` are `https://agent.pivota.cc/products/{sig}`; **`recommend_products`** returns `merchant_id: "external_seed"` (a routing token, not a merchant) and `product_ref: null`. | One call | **Collapse to `recommend_products`** — it already owns intent parsing, ranking, and a verification hook. Extend its per-item payload with the execution spec (E). Do not add a second call the agent must chain. | M | **Yes** |
| **D5. UCP profile + self-hosted agent endpoints** | Live and reusable. Pivota's own `/ucp/mcp` and `/agent/shop/v1/invoke` are protocol-agnostic; `create_checkout_session` is PSP-rail-specific but the discovery half is not. | Reusable | Discovery/read is fully reusable. The transact half is PSP-only and should be left alone — the card rail's transact leg belongs on the merchant's UCP endpoint, not ours. | — | No |
| **D6. Idempotency + correlation ID** | `X-Request-Id` is accepted from upstream and echoed on every response (`middleware/structured_logging.py:73–130`). `Idempotency-Key` is enforced on the money path (gateway `submit_delegated_payment`, `after_sales_cases`, `agent_events`). `commerce_interactions` already carries `trace_id`, `prompt_id`, `result_id`, `click_id`, `quote_id`, `order_id`. | An ID minted at recommendation time that survives Pivota → agent → Reap → back | **The spine exists but nothing mints a `recommendation_id` at recommend time and stamps it on the spec.** `click_id` is minted at *redirect-build* time, one hop too late, and only on the `/r?token=` path. **Confirmed: small change, outsized value — flag high.** | S | **Yes** |

### E. Execution spec emitter

There is no execution-spec emitter. `services/checkout_handoff_descriptor.py` is the nearest thing and is not it: it refuses to emit unless `commerce_path == "pivota_direct_quote_first"` **and** `allows_pivota_order` **and** `allows_psp_creation` — i.e. it is PSP-rail-only by construction, and it emits identity keys plus a Pivota-hosted `handoff_url`, never a merchant URL.

| Spec field | Emittable today? | Detail |
|---|---|---|
| Canonical PDP URL with variant preselected | **Partly — one query away** | `merchant_canonical_url` present on **99.7%** of serving rows (n=2,992) and often already variant-qualified. But variant identity is held for only **28%** (A6), so "with the variant preselected" is true for ~28%. |
| Cart-prefill URL | **Partly — exists, narrow reach** | `shopify_cart_base_url()` + warm-handoff `continue_url`. Live for 6 allowlisted brands; blocked elsewhere by the missing numeric variant id, not by missing code. |
| Promo codes + application order | **No — new data** | `promo_terms.py` models terms, not per-merchant codes or ordering. UCP `dev.ucp.shopping.discount` is the cheap route. |
| Expected line-item total | **No for crawled merchants** | Engine exists, OAuth-gated (B7). |
| Expected grand total (abort-on-mismatch) | **No for crawled merchants** | Same. This is the field that makes the agent safe; without it there is nothing to abort against. |
| Checkout field map / fill hints | **Does not exist** | Only needed under Key Entry. |
| Recommended rail (card / ACP / UCP / PSP) | **Does not exist** | `offer_mode` and `commerce_path` exist as adjacent concepts but neither is a rail recommendation. Now cheaply derivable: UCP-reachable → protocol rail; else → Key Entry. |
| Affiliate / tracking params in the URL | **Exists, but not on the lane the agent uses** | Attribution is stamped only inside `_make_external_redirect_url`, which returns an **opaque `{base}/r?token=…`**. The MCP lane hands the agent the **raw `destination_url` with no click id at all**. So an agent that drives checkout from `destination_url` — which it must — **loses attribution entirely.** |
| Expiry timestamp on the spec | **No** | `quotes.expires_at` exists on the PSP rail; nothing analogous on the read/handoff path. `services/serving_freshness.py` emits `fresh_until` on a 1 h TTL but only describes the *projection's* age, not a spec's validity. |

**Verdict: E is the biggest single build.** Effort **M–L**. **Blocking.**

### F. Live-verification hop

| Capability | What exists today | What the milestone requires | Gap | Effort | Blocking? |
|---|---|---|---|---|---|
| **F1. A live-check path** | Two things that look like one and are not. (a) The reco lane's `verifyPrice` (gateway `src/server.js:30541`) — but it calls **Pivota's own internal `get_pdp_v2`**, not the merchant. `price_verified: true` therefore means *"consistent with Pivota's own projection"*, **not** *"matches the merchant's live price"*. Budget: ≤8 checks, 2.5 s race, 2 s axios timeout, degrade to snapshot with `price_verified: false`. (b) `services/external_offers_service.resolve_external_offer` — a **genuine** live fetch (httpx, 10 s total / 5 s connect, OG + JSON-LD, 7-day cache), but it is not on any serving path and its only writer COALESCEs (A4). | Re-verify price + availability against the live page before handoff | **A live-verification hop against the merchant does not exist on the serving path.** The flag that reads like one is measuring us against ourselves. | M | **Yes** |
| **F2. Latency budget** | Reco lane observed **7.65 s** end-to-end. Live PDP fetch measured **~1.3 s median** per merchant page; anonymous UCP `tools/list` **~0.5 s**. | A budget an agent will accept | Realistic target: **≤3 s for the whole reco+verify turn.** That means the 7.65 s lane must come down first — verification cannot be bolted onto it. Recommended shape: verify **only the top-K (K=3) shortlist**, in parallel, hard-capped at **1.5 s**, against the merchant's UCP endpoint (cheaper and more authoritative than scraping HTML), with a **per-domain result cache of 60–120 s** to absorb repeat queries. On GCP the cache belongs in Memorystore (`pivota-redis`, already provisioned, `10.25.7.196`) so it is shared across autoscaled instances rather than per-process. **This hop is request-path traffic and therefore egresses on the shared NAT IP — it is the primary consumer of the isolation in §3.2.** | M | **Yes** |
| **F3. Fallback on failure/timeout** | The reco lane's existing degradation is the right shape and should be reused: unverified items keep the snapshot, are marked `price_verified: false`, and (when a constraint is enforced) are dropped rather than shown. | Explicit policy | **Recommended:** never silently degrade on the card rail. Verified → emit spec with `expected_total` and a 5-min expiry. Unverified → **demote, and emit the spec with `expected_total: null` + `confidence: "unverified"`** so the agent can decide. Verification says out-of-stock or 404 → **drop and take next-best merchant** (we have 199 domains and duplicate coverage of many products). Do not return an unverified item as rank 1. | S | No |

### G. Telemetry / outcome graph

| Capability | What exists today | What the milestone requires | Gap | Effort | Blocking? |
|---|---|---|---|---|---|
| **G1. Event logging** | Stronger than expected. `commerce_interactions` is a genuine funnel spine: `prompt_id, result_id, click_id, quote_id, checkout_id, order_id, refund_id, canonical_product_id, canonical_variant_id, trace_id, agent_id, protocol_name, llm_provider, llm_model, status, first/last_occurred_at`. Plus `commerce_interaction_events`, `surface_click_events` (impressions, clicks, `destination_url`, `context` JSONB), `commerce_attribution_edges` (click → order), `funnel_events`, `agent_ranking_log` (full feature vector per ranked item), `agent_product_events`. Attribution closure runs live (`external_conversion_poll`, every 15 min). | Query → recommendation → handoff → outcome | The spine is ~70% there. **What is missing is exactly the card-rail-specific half**: no `recommendation_id` minted at recommend time (D6); **no quoted-vs-actual total anywhere**; **no transaction failure-reason vocabulary** (grep finds `reason_code` only in readiness/quality contexts); no per-hop latency; **no ingestion endpoint for an agent-reported outcome** — `POST /agent/v1/events/*` accepts clicks and offer-selections only. | S–M | Near-blocking |
| **G2. Minimal event schema** | — | — | Proposed below. | S | — |
| **G3. Join point for agent + Reap outcomes** | `commerce_attribution_edges` already joins `click_id → order_id`. | A three-way join | **Join on `recommendation_id`**, carried in the spec, echoed by the agent, and (if Reap will) stamped on the authorization. Fall back to `click_id` (already order-surviving on Shopify via `note_attributes`) when the agent drops it. | S | No |

**Delivery-path warning for G3 (from the migration session, 2026-08-21).** If outcome or attribution events are ever pushed outward rather than polled, they must not reuse the `pivota-acp` webhook outbox as-is. It was genuinely broken until today (fixed in pivota-acp #34/#35): the dispatcher crashed on its first row and the task died permanently; the sender swallowed every outcome, so an upstream **500 was recorded as delivered**; and with no destination configured every row would have been marked sent having gone nowhere.

**The design property that failure teaches — and it is the reason this hop is specified pull-first.** The outbox failed *quietly* because every layer swallowed its own outcome: a sender returning `None` on success, on transport error, and on an upstream 500 alike, so the caller could not distinguish **delivered** from **rejected** from **never attempted**. The rule is not "prefer pull" — it is that **the transport must be able to say which of those three happened.** A pull design gets that property for free (the absence of a row is unambiguous); a push design must be built to preserve it. If the outcome hop ever does go push, that is the acceptance criterion.

Two conditions before `DISABLE_WEBHOOK_OUTBOX=false` is flipped: `OPENAI_WEBHOOK_URL` and `MERCHANT_WEBHOOK_SECRET` must be set. And the `Idempotency-Key` header added in that fix is **documentation of intent, not a contract** — adding a header does not establish one. Nothing may depend on it for outcome de-duplication until the receiver confirms it de-duplicates on that key.

**Proposed minimal event schema** — one row per handoff, `card_rail_outcomes`:

```
recommendation_id   text  PK      -- minted at recommend time, in the spec, echoed back
trace_id            text          -- joins to commerce_interactions
agent_id            text
merchant_domain     text
sig_id / product_key / variant_id text
rail                text          -- ucp_protocol | key_entry | psp | acp
quoted_item_total   numeric       -- what the spec promised
quoted_grand_total  numeric
quoted_currency     text
quoted_at           timestamptz
spec_expires_at     timestamptz
actual_item_total   numeric       -- agent- or Reap-reported
actual_grand_total  numeric
outcome             text          -- completed | abandoned | failed | aborted_on_mismatch
failure_reason      text          -- bot_blocked | out_of_stock | price_mismatch |
                                  -- variant_unavailable | checkout_error | payment_declined |
                                  -- guest_checkout_required | shipping_unsupported |
                                  -- pdp_404 | spec_expired | agent_timeout
latency_ms          jsonb         -- {recommend, verify, handoff, agent_checkout, authorization}
auth_outcome        text          -- from Reap, if shared
reported_by         text          -- agent | reap | pivota_poller
occurred_at         timestamptz
```

`failure_reason` is the compounding asset: it is what turns C3's completion-probability term from a guess into a measurement.

### H. Compliance and hygiene

| Capability | What exists today | What the milestone requires | Gap | Effort | Blocking? |
|---|---|---|---|---|---|
| **H1. No card data** | **Verified clean.** Grepped every `Column(...)` in `db/` and every migration for `card_number\|pan\|cvv\|cvc\|cryptogram\|track2\|security_code\|expiry` — **zero matches**. The only `card`-shaped column in the schema is `catalog_payment_incentives.card_network` (merchant-side incentive metadata: "5% off with Visa"), which is not cardholder data. `orders.payment_method_id` holds a PSP token reference, not a credential. ACP `delegate_payment` is a permanent **501 that never reads the request body**; a schema-guard test asserts no `acp_delegate_allowances` column matches `number\|cvc\|pan\|cryptogram`. | Same | **Holds by construction *in this repo*, and the construction is documented and tested — but the row as first written overstated its scope, and H5 is the counter-example.** My evidence was `db/` and `db/migrations/` in pivota-backend; my conclusion was phrased as a Pivota-wide red line. It is not one: `pivota-acp` is a Pivota-operated production service whose live, unauthenticated `delegate_payment` contractually accepts `number`/`cvc`/`cryptogram` today. **Corrected claim: no cardholder data reaches *pivota-backend's* schema, verified. "Pivota holds no cardholder data anywhere" is not established by this audit and is contradicted by H5 until that service is decommissioned.** | — | No (this repo) / see **H5** |
| **H2. Buyer PII** | `buyer_addresses` stores **full plaintext shipping addresses** (`recipient_name, line1, line2, city, region, postal_code, country, phone`). Access is confined to `routes/buyer_api.py`, always scoped to `principal.user_id` — it is buyer-owned CRUD for the PSP rail. It is **not** on any card-rail handoff path today. `surface_click_events` stores `ip` and `user_agent`. | No buyer PII through the card-rail handoff | **Not blocking today, but it is a landmine.** On the card rail the agent collects and fills the address on the merchant's site; Pivota must never receive it. **Recommend an explicit assertion**: the execution-spec emitter and the outcome endpoint must reject any payload containing address/PII fields, with a test. Also note the UCP in-chat priced preview deliberately uses a **synthetic address, no PII** — keep that property. | S | No (but harden) |
| **H3. Crawler posture — robots** | Respected in `services/brand_product_discovery.py`, `services/co_occurrence_finder.py`, `services/bd_brand_signals.py` (`RobotFileParser`, permissive on fetch error, `blocked` classification). **Not checked in `services/external_offers_service._fetch_html`** — which is precisely the fetcher a live-verification hop would reuse. UA is `Mozilla/5.0 (compatible; PivotaBot/1.0; +https://pivota.cc)`. | Politeness at verification volume | **Gap:** `_fetch_html` has no robots check, **no per-domain rate limit, and no backoff**. At F2's volumes that is the component most likely to get us blocked — and post-migration it would be getting *the payment egress IP* blocked (§3.2), which upgrades this from a politeness issue to a payments-availability issue. | S | **Blocking** (post-migration) |
| **H4. Currently blocked / unusable domains** | Measured: **`www.ulta.com`** — 404 on `/.well-known/ucp`, Akamai-fronted, retailer not brand (147 SKUs in the 3,000-row sample, the #2 domain). **`toocoolforschool.us`** (33), **`theordinary.com`** (19, Salesforce Commerce Cloud), **`thankyoufarmer.us`** (14, 409). Plus **6.7% of sampled PDPs are already 404** (`poopourri.com`, `podl.us` ×2, `ponds.us`, `goongbe.us`, `forbeaut.us`). | An exclusion list | **Exclude Ulta, The Ordinary, toocoolforschool.us, thankyoufarmer.us from the pilot cohort.** Also treat 404-rate per domain as a cohort filter — it is a direct proxy for index rot. | S | No |
| **H5. A retired service still running with a live payment credential** *(surfaced by the migration + launch-readiness sessions, 2026-08-21; recorded here because it is squarely an H item)* | `pivota-acp` is **Online on Railway carrying a real Adyen API key**, retired by ADR-021 and never decommissioned. Confirmed inert *as a destination*: no custom domain (only `pivota-acp-production.up.railway.app`), `acp.pivota.cc/health` returns `"service":"PIVOTA-Agent"`, and **zero references across all four GCP prod services**. Tomorrow's cutover does not change that. **Two separate questions live here and must not be merged** — see below. | No live payment credential on an unreferenced, unmonitored service | **Not a card-rail blocker and not a cutover blocker.** Recorded so the card-rail workstream does not re-adopt the service. **Owner: cutover/infra session; already escalated to Peng.** | S | No |
**H5 UPDATE 2026-08-21, late — a PCI exposure on a retired service. Corrected once; this is the settled version.**

> **Retraction of the first version of this block.** It claimed the endpoint was **unauthenticated** and called it an "open card intake." **That was wrong.** I inferred *enforcement* from the live `openapi.json` (`securitySchemes` empty, no per-operation `security`). An OpenAPI document describes **documentation, not enforcement**: FastAPI emits `securitySchemes` only when the `Security`/dependency machinery is used, so a plain required `Header(...)` plus a manual check is **invisible to the spec and fully effective at runtime**. That is exactly the scope defect described below the H5 detail — third instance in one evening, and mine. Caught by the migration session; verified here before accepting.

**What is actually true**, verified against `pivota-acp` `origin/main` and the live service's env (key **names** only):

- **The route is service-token gated.** `pivota_infra/src/acp/router.py:538` declares `Authorization: str = Header(...)` — Ellipsis, so a request without it 422s — and line 545 calls `_require_auth()`, which rejects a non-`Bearer` header with 401 and then, **if any of `ACP_SERVICE_TOKEN` / `ACP_API_KEY` / `ACP_SERVICE_TOKENS` is configured**, requires an exact match. **`ACP_SERVICE_TOKEN` is set on the live service**, so strict mode is active and an arbitrary bearer gets 401. *(The fail-open-when-unconfigured branch is a latent hazard, but it is not the live state.)*
- **The PAN/CVC path is real and still deployed.** `router.py:594` passes the **entire** request — `await create_delegate_token(vt_id, body.dict())` — and `delegate_service.py` does `INSERT INTO delegate_tokens (id, payload, created_at)` with `json.dumps(request)`. Raw `number` and `cvc` into a JSONB column, exactly as `db/migrations/192_acp_delegate_allowances.sql:53–58` describes the retired service doing. **It was never removed.**
- **It cannot reach a database today.** The live service has **no `DATABASE_URL`** — zero of its 30 env keys match `database`. So `HAS_DB` is false, the `INSERT` branch is unreachable, and it falls through to `_MEMORY_TOKENS`: **in-process, dies with the process, never written to disk.**
- **Only one sink, not two.** `store_idempotency(...)` looks like a second copy but `session_service.py` stores only `_hash_body(body)` — a hash. (Checked by the migration session specifically to avoid handing me a worse claim than the true one.)

**Accurate severity.** Not an open card intake on the internet. A **retired, service-token-gated** endpoint whose deployed code would place PAN + CVC in **process memory** if called by a token holder — on a service nobody monitors, that nothing routes to, whose token exists and is presumably known to something. That is a genuine PCI exposure and **still the strongest single argument for decommissioning**, well past the dormant Adyen credential and past the `/pay` mock. It is **not** a stop-the-world finding.

**Not verified, deliberately:** runtime behaviour was established by reading the deployed code and the env, **not** by POSTing card-shaped data to a production endpoint. That remains the right refusal — the code read settles it without needing to.

**H5 detail — the two questions, and a correction to the basis on which one of them was closed.**

*Question 1: is the Adyen credential on an armed path?* The stated basis for "no" was **(a)** `ACP_ENABLE_REAL_CAPTURE=false` and **(b)** *"the live tree has no `checkout-live.adyen.com` endpoint."* I verified both against `pivota-acp` `origin/main` @ `087daf4` (2026-08-21, current — the only local checkout, `~/dev/pivota-acp-revert`, sits on an Apr-21 branch and is four months stale, so a working-tree grep there proves nothing):

- **(a) holds, and is stronger than stated.** `pivota_infra/src/connectors/_real_capture.py:25` defaults the flag to `false`, and the module docstring is explicit that the ACP real-capture flow *"only calls the pivota-backend Agent API"* — the merchant is charged via its own PSP inside the backend, further gated by the backend kill-switch. **The ACP capture path does not touch pivota-acp's own PSP connectors at all.**
- **(b) is false.** `checkout-live.adyen.com` **is** in that tree — `psp/connectors.py:126` and `psp/production_connectors.py:127` — and `psp.connectors` is imported by `orchestrator/payment_orchestrator.py:13` and `routes/payment_routes.py:170` in the same repo. Whether those routes are mounted on the running service is **not verified from here** (third repo, not this workstream's to own).

**Net: the conclusion survives, on better evidence than it was given — but only for the ACP flow, not for the service as a whole.** "Not armed via ACP" is established; "nothing on that box can reach the key" is not. Peng closed *rotating the key* as unnecessary partly on basis (b). That decision may well still be right — the flag default and the platform-agnostic capture path are real controls — but it deserves ten minutes from whoever owns pivota-acp to confirm those two payment routes are unmounted, rather than resting on a leg that does not hold.

*Question 2: should a service ADR-021 retired still be running at all?* Independent of Question 1, and unaffected by it. This is the open item.

| **H6. Secrets hygiene** *(incidental finding, not in the brief)* | Two production internal shared secrets read from the live `web` service env are **short, low-entropy, keyboard-mashed placeholder-grade strings** (`EXTERNAL_OFFERS_INTERNAL_KEY`, `RECOMMENDATIONS_INTERNAL_KEY`). Values deliberately not reproduced here. | Real secrets | These gate internal service-to-service calls. **Rotate to generated high-entropy values before the pilot widens.** | S | No |

---

## 3. GCP migration constraints on this work

The migration is not just a change of deployment target for these recommendations — it changes what is safe, what is cheap, and when things can land. Source of truth for all of this is `infra/gcp/` on `origin/main` (PRs #1773, #1777, merged 2026-08-19/20).

### 3.1 Where a scheduled re-crawl actually goes

`infra/gcp/setup_scheduler.sh` already codifies three distinct scheduling shapes, and a re-crawl must pick the right one:

| Shape | Mechanism | Fits a re-crawl? |
|---|---|---|
| Sub-minute drainers (5–10 s ticks) | In-process APScheduler on a dedicated `worker` Cloud Run service, `min=max=1`, `--ingress internal` | No — a crawl does not need sub-minute cadence |
| The 8 periodic backend jobs | Same `worker` process, gated by `AUDIT_WORKER_ENABLED` | Possible, but it would put outbound crawl traffic inside the service that also drains the audit/executor queues |
| **True cron** | **Cloud Run Job + Cloud Scheduler trigger** | **Yes — this is the right shape.** A freshness sweep is a batch process that exits, which is exactly what Jobs are for, and it gets its own CPU, memory, timeout, and pool sizing. |

Concretely: add a `catalog-freshness-sweep` Cloud Run Job alongside `relgraph-sync` and `reviews-invitation-send`, plus a `sched` entry. Four rules that the existing script learned the hard way and a new job must inherit:

- **Create it inert.** `WORKERS` and `PAUSED` are opt-in and both fail closed (`PAUSED` accepts only `0`/`1`; `WORKERS` only `true`/`false`). On 2026-08-20 a `setup_scheduler.sh prod` run nearly armed a duplicate set of drainers against production data because one script had been made opt-in and the other had not. A crawl job that fires against a prod snapshot during the pre-cutover window would hammer real merchant sites from the prod egress IP.
- **Pin the pool.** A Job inherits no pool sizing; `db/database.py` defaults to 5..20. `reviews-invitation-send` pins `DB_POOL_MIN_SIZE=1 / MAX=4` for exactly this reason. See §3.3.
- **Strip the key before appending it.** `gcloud --env-vars-file` resolves a duplicate key to the **first** occurrence, so any new env var must be `grep -vE`'d out of the ported file first or the override is silently inert.
- **`--args` splits on commas.** Use the `"^|^-c|$SCRIPT"` form for anything containing them.

### 3.2 🚨 The egress-IP collision — the most important new finding

`infra/gcp/setup_egress_nat.sh` creates **one** Cloud Router + Cloud NAT with **one reserved address**, applied with `--nat-all-subnet-ip-ranges`. Every Cloud Run service and every Job in the project deploys with `--vpc-egress all-traffic`, so they all share it:

| Env | Reserved egress IP | Also used for |
|---|---|---|
| prod | **`8.231.167.230`** | **Antom / Adyen partner IP-allowlisting** |
| staging | `136.66.216.216` | (never give this to a partner) |

The script's own comment says the address is reserved so that *"the address partners allowlist never changes."* That property is exactly what makes sharing it with a crawler dangerous:

1. **Reputation coupling.** My measurement shows a modest crawl rate triggers cross-domain Cloudflare 429s against the source IP. Cloudflare, and the WAF/anti-abuse reputation services behind these merchants, would be scoring the same address that carries payment traffic. A crawl that earns a bad reputation degrades the payment path.
2. **No escape hatch.** The normal remedy for a throttled crawl IP is to rotate it. Here you cannot — partners have pinned it, and rotating means a coordinated re-allowlisting cycle with Antom and Adyen.
3. **NAT port exhaustion.** A single NAT IP has a finite port pool shared across all instances. A fan-out verifier opening many short-lived TLS connections is precisely the workload that exhausts it, and the symptom would surface as connection failures on *every* outbound path from the project — including payments.

**Agreed fix (M), reconciled with the migration session 2026-08-21:** a **dedicated subnet with its own Cloud Router + Cloud NAT and its own reserved IP** (`--nat-custom-subnet-ip-ranges` rather than `--nat-all-subnet-ip-ranges`), with the crawl Job and the verification path deployed onto that subnet via `--subnet`. **`8.231.167.230` stays the payment egress; the new IP goes to crawl.** Ships as its own PR against `infra/gcp/setup_egress_nat.sh` — the script hardcodes the all-ranges shape, so it needs a **parameter**, not an in-place edit — reviewed by the migration session. **Target Aug 25**; not before Sat 2026-08-22, because prod egress is not touched the day before a cutover. The alternative — routing crawl traffic through an external proxy pool — also works and additionally solves the per-domain pacing problem, at the cost of a third-party dependency on the request path.

**This is a prerequisite for backlog items 6, 8 and 14, not a follow-up to them.**

Two corrections to the framing above, both from that reconciliation:

- **Port exhaustion is the harder-edged reason, not reputation.** Cloud NAT's port pool is per-IP, so a burst crawl can starve the payment path's outbound ports *even with a perfectly clean reputation*. Reputation damage is the slow failure; port starvation is the fast one.
- **Nothing today requires `8.231.167.230` to be allowlisted.** Antom is not integrated, and Adyen is test-mode connectors with no source-IP allowlist. Calling it a live dependency (as the first draft of this audit did) overstated it — it is an *output to have ready* for PSP onboarding. What makes the timing real is the direction of travel: a partner document quoting a fixed egress IP for Antom's engineering team is being prepared now, so the address is about to become externally committed. Isolating crawl egress before that ships is materially cheaper than after.

### 3.3 Cloud SQL — what the new schema lands on

Prod is already provisioned: `pivota-prod:us-west1:pivota-pg`, **POSTGRES_17**, `db-custom-2-7680` (2 vCPU / 7.5 GB), **REGIONAL HA**, PITR on, deletion protection on, private IP `10.25.0.2`. The new objects this audit proposes — `card_rail_outcomes`, a per-domain transactability table, and a `last_crawled_at` column on the catalog — land here as ordinary startup migrations. Three constraints apply:

- **Connection budget.** `max_connections = 200` and the pool math is already tight (the prod sizing bug — 20 instances × 20 connections — was found and fixed during migration). A crawl Job must pin a small pool (1..4, as `reviews-invitation-send` does), not open the 5..20 default.
- **Write pattern is new.** Today the instance serves a read-heavy catalog. A nightly sweep that rewrites `catalog_offers.list_price` / `availability` across thousands of rows is sustained write + WAL traffic on a 2-vCPU **synchronously-replicated** REGIONAL instance, and PITR retains that WAL. Size this before enabling it; it may be the first thing that justifies a bigger tier.
- **`DB_STATEMENT_TIMEOUT_SECONDS=30` is live on prod.** A Cloud Run Job inherits the ported env, so a batch sweep would inherit the 30 s ceiling and be cancelled mid-statement. Use the `unbounded_statement_timeout()` escape hatch or unset the var for the job — and note the escape hatch currently has **zero call sites**, so this is untested in production.
- **Startup DDL races on an empty database.** The first Cloud Run deploy failed on a concurrent `CREATE TABLE IF NOT EXISTS` (`pg_type_typname_nsp_index` duplicate key). Adding new tables widens that window. Author the new DDL defensively.

### 3.4 Timing — the September cut line collides with the cutover

Cutover is **Sep 8–12**, soak **Sep 12–26**, first real charge late September. The demo milestone sits on top of it. Sequencing that actually works:

- **Now → Sep 6 (parallel-stack window).** Land the code-only items — they are platform-agnostic and ride the cutover for free: `recommendation_id` (#1), the `COALESCE` fix (#2), variant backfill (#4), execution-spec v0 (#5), attribution on the agent-visible lane (#12). Author the crawl Job and the egress subnet, and **rehearse both on the live GCP staging stack**, which already holds a restored production dump.
- **Sep 8–12 (cutover).** Change freeze on anything card-rail. Do not introduce a new scheduled job into a DNS flip.
- **Sep 12–26 (soak).** Un-pause the crawl Job on prod GCP, on its own egress IP, at low rate. Turn on the live-verification hop behind a flag for the Tier-0 six brands.
- **Late Sep → Oct.** Widen to Tier 1/2 and enable landed totals.

**Do not build any of the scheduled work on Railway.** It is throwaway, and worse, `setup_scheduler.sh` reproduces Railway's job set exactly — a job added to Railway now and not to that script silently fails to migrate.

### 3.5 Smaller migration traps that touch this work

- **`sslmode=require` means different things in Python and Node.** asyncpg encrypts without CA verification; node-pg maps it to `rejectUnauthorized: true` and fails against Cloud SQL's per-instance CA. The gateway uses a separate `DATABASE_URL_NOVERIFY` secret. Since `verifyPrice` lives on the **gateway** (Node), any DB access the verification hop needs must use the no-verify DSN.
- **`/healthz` is unreachable on Cloud Run** — the Google frontend answers it with its own 404 before the container sees it. Any health/uptime check for a new service must use `/health`.
- **Direct `*.run.app` URLs are closed** (`--ingress internal-and-cloud-load-balancing`). Test prod only through the LB: `curl --resolve <host>:443:34.8.67.235`.
- **Never add a `Dockerfile` at the repo root.** It flips Railway's builder from Railpack to Docker for all 8 services building from this repo, which already caused a prod deploy failure once. The Cloud Run Dockerfile lives at `infra/gcp/Dockerfile`.

---

## 4. Prioritized backlog

> **Status reconciliation — re-measured 2026-08-24 against `origin/main` and against live GCP.**
>
> **Correction to an earlier pass of this section:** it reported items 3a, 3 and 14 as "not
> started" on the strength of a grep against `infra/gcp/setup_egress_nat.sh` and
> `setup_scheduler.sh`. That was a scope error — the work landed in *new* files those greps
> could not see (`setup_crawl_egress.sh`, `migrate_payment_nat_to_default_subnet.sh`,
> `jobs/scheduled_ucp_reprobe_job.py`, `routes/store_audit_probe_internal.py`). The corrected
> state is below. Where a row says "not started", it now means a positive check was run, not
> that one filename lacked a match.
>
> | Item | State | Evidence |
> |---|---|---|
> | 3a — dedicated crawl egress | **DONE, provisioned and live in BOTH projects.** `pivota-crawl-nat` is `LIST_OF_SUBNETWORKS` on `pivota-crawl`, with reserved IPs `IN_USE`: staging `34.11.177.234`, prod `34.82.199.35`. The payment NAT was narrowed to `default` while retaining its address (staging `136.66.216.216`, prod `8.231.167.230`). Confirmed by the read-only guard `migrate_payment_nat_to_default_subnet.sh <env> --check` in both. The design also solves a constraint this audit missed: Cloud NAT permits only one `ALL_SUBNETWORKS_ALL_IP_RANGES` NAT per VPC/region, so the crawl script hard-fails until the payment NAT is narrowed first. | live `gcloud`, 2026-08-24 |
> | 3 — per-domain UCP probe + stored profile | **built, shipped inert.** Isolated probe lane with its own least-privilege identity, migration 196 (`store_audit_execution_routes`), receipt endpoint closed unless `STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED` **and** a dedicated key are both set. Triggers created `PAUSED`. Not a stored capability table yet — it writes `execution_routes` + `acceptance_signal` evidence. | `routes/store_audit_probe_internal.py`, `docs/commerce-index-crawl-lane.md` |
> | 14 — scheduled freshness / re-probe | **built, default-off selector.** `jobs/scheduled_ucp_reprobe_job.py` + `scripts/run_scheduled_ucp_reprobes.py`; `PAUSED=0` is deliberately *rejected* while Store Audit UCP is present, so arming it cannot resume unrelated Scheduler jobs. | same |
> | 2 — refresh can correct a stale price | **shipped** | #1812 |
> | 1 — `recommendation_id` | **half** — minted per item and per set (#2080), logged on lane outage (#2081). The accept-it-back half is item 10 and does not exist. | #2080, #2081 |
> | 4 — numeric variant-id backfill | **swept 2026-08-25; 60.5% and that is close to the ceiling.** Coverage **10.0% → 60.5%** (6,514 / 10,763 active rows whose URL contains `/products/`). **1,807 rows remain eligible and most are not recoverable by this producer**: they concentrate in hosts that are dead, redirecting, or bot-fronted — `comfortzone.us` (180, ConnectError), `paulmitchell.com` (103, 301), `toocoolforschool.us` (97), `fentybeauty.com` (64), `kyliecosmetics.com` (50), plus Akamai-fronted majors (sephora, glossier, urbandecay, hudabeauty, jomalone, toofaced) that serve nothing to a crawler. The sweep was STOPPED on evidence, not finished: after several hours the responsive hosts began returning 429 and the producer's abort-after-8-consecutive-blocks rule fired — the correct answer is to let the scheduled re-crawl job pick these up over time on the `pivota-crawl` subnet, not to push through a throttle. | measured live |
> | 5 — execution spec v0 | **shipped this pass.** `merchant_domain`, `pdp_url`, `cart_url`, `variant_id`, `rail`, `expires_at`, `tracking` now ride on every external offer, composed by the same function the redirect signs. `recommendation_id` is deliberately **not** in it: it is minted in the gateway's `recommend_products`, not at `offers.resolve`, and stamping a fabricated one would defeat the join it exists for. | this PR |
> | 12 — attribution on the agent's own lane | **shipped this pass.** One click id now spans the signed token, `pdp_url` and `cart_url`. | this PR |
> **Item 8 is scoped to the offers fetcher, and that is NOT the same as "safe to run crawl jobs
> on `pivota-crawl`".** The reserved egress IP is shared by every outbound lane, so a ban earned
> anywhere takes all of them down. Measured 2026-08-25 — merchant-crawling lanes that still
> bypass the gate:
>
> | lane | robots | pacing |
> |---|---|---|
> | `services/executor_agents/sitemap_freshness.py` | none | none |
> | `services/executor_agents/canonical_pdp_enrichment.py` | none | none |
> | `services/curated_brand_feed.py` | none | none |
> | `services/brand_product_discovery.py` | root-only (never sees path rules) | none |
> | `services/bd_cold_start_service.py` | root-only | none |
> | `services/co_occurrence_finder.py` | root-only | none |
>
> Routing these through `services/crawl_politeness` is the remaining work before the crawl subnet
> carries real traffic. Cheap — the gate is one `await` — but it is six call sites, not one.
>
> | 8 — rate-limit + robots the fetcher | **shipped.** `services/crawl_politeness.py`: per-host pacing (`CRAWL_MIN_INTERVAL_SECONDS`, default 1/s), robots.txt obeyed on the FULL path with `Crawl-delay` honoured whenever it is slower than our floor, and exponential 429/503 backoff honouring `Retry-After` only ever to lengthen. `_fetch_html` is gated on it. Two things worth carrying: the wait is BOUNDED (`CrawlPaced`) because `POST /api/offers/external/resolve` has no auth dependency and is a live path — an unbounded stall there would be a new regression, not politeness; and the pre-existing `_robots_allows` (`services/brand_product_discovery.py:493`) asks about the site ROOT, so a `Disallow: /products/` never bit the paths it guards. That helper is UNCHANGED here and still has that defect. | this PR |
> | 6 — live-verify top-3 | **not started.** Note the v2 index design partly supersedes the framing: public crawl carries authority 45 and "never auto-publishes checkout-sensitive facts", and checkout is specified to always live-validate. | `docs/commerce-index-v2.md` |
> | 7, 10 (Wave 3) · 9, 11, 13, 15, 16 (Wave 4) | **not started** | verified by grep |


Ordered by (blocking × effort), then sequenced against the **Sep 8–12 cutover** (§3.4). The **Where** column is load-bearing: it is what keeps this work from being built twice.

### Wave 1 — now → Sep 6. Code-only, platform-agnostic, rides the cutover for free

| # | Item | Effort | Where | Why first |
|---|---|---|---|---|
| 1 | **Mint a `recommendation_id`** at recommend time; put it in the spec; accept it back on an outcome endpoint. | S | App code (gateway + backend) | Every other measurement joins on it. Cheapest high-leverage change in the audit. |
| 2 | ✅ **DONE 2026-08-22** — **`_refresh_external_seed_by_id` can now correct a stale price.** Branch `claude/cardrail-seed-refresh-price`; 8 tests, all verified to fail against `origin/main`. Not the one-line fix this row predicted: the resolution moved out of SQL into Python (the suite stubs the statement executor, so a `COALESCE` in SQL is untestable), amount+currency are now applied as a **pair** (they are extracted from independent sources, so a fetch can yield an amount with no currency — applying it alone would redenominate the offer), and the function now returns `price_refresh` / `availability_refresh` drift reports that `run_external_referral_refresh_batch` aggregates. Curated `title`/`image_url` keep existing-wins precedence. | S | App code | Without it every downstream freshness fix is inert. |
| 4 | **Backfill numeric variant IDs** for the pilot cohort from Shopify `/products/{handle}.js` or UCP `catalog.lookup`. | S–M | One-shot script, run from a laptop or a staging Job — **not** a prod scheduled job yet | Unblocks cart permalinks *and* variant-accurate specs. Currently 28% coverage. |
| 5 | **Execution-spec v0** on `recommend_products`: `{recommendation_id, merchant_domain, pdp_url, cart_url, variant_id, rail, expires_at, tracking}`. No totals yet. | M | App code | This is the milestone's actual deliverable shape. |
| 12 | **Attribution on the lane the agent actually uses** — stamp `pivota_click_id` / `utm_content` onto `destination_url` in the MCP/UCP payloads, not only inside `/r?token=`. | S | App code | Without it the pilot generates revenue we cannot see. Promoted from October: it is code-only, so it is free to land in this window. |

### Wave 2 — now → Sep 6. Built and rehearsed on **GCP staging**, shipped inert

| # | Item | Effort | Where | Why |
|---|---|---|---|---|
| **3a** | **Dedicated crawl egress**: own subnet + Cloud Router + Cloud NAT + reserved IP; `--nat-custom-subnet-ip-ranges`. **Agreed + scheduled: land Aug 25**, own PR parameterising `infra/gcp/setup_egress_nat.sh`, reviewed by the migration session. Not before Sat Aug 22. | M | `infra/gcp/setup_egress_nat.sh` (parameterised, not edited in place) | **Prerequisite for 3, 6, 8 and 14.** Without it those items share the payment path's NAT IP and port pool (§3.2). |
| 3 | **Per-domain UCP probe + store the profile** (`/.well-known/ucp` → shop host → MCP endpoint → capability set). ~200 domains, paced ≤1 req/s. | S | Cloud Run Job + Scheduler, on the 3a subnet. Created `PAUSED`. | Turns B from "does not exist" into a populated table in a day, and decides rail selection per domain. |
| 8 | **Rate-limit + robots the fetcher** (`_fetch_html`): per-domain token bucket, backoff on 429, robots check. | S | App code, but only meaningful once 3a exists | Item 6 will get us IP-banned without it — measured, not theoretical. |
| 6 | **Live-verify the top-3 only**, in parallel, 1.5 s cap, against the merchant's UCP endpoint; Memorystore-backed 60–120 s cache; degrade per F3. | M | Request path — needs 3a **and** 8 | Takes the 31.1% wrongness down to near-zero on the items that matter. |

### — Sep 8–12: cutover freeze. Ship nothing card-rail. —

### Wave 3 — Sep 12–26 (soak). Un-pause on prod GCP, at low rate

| # | Item | Effort | Where | Why |
|---|---|---|---|---|
| 7 | **Widen `OUTBOUND_WARM_HANDOFF_BRANDS`** from 6 to the Tier-1 cohort. **Read [docs/runbooks/outbound_warm_handoff_rollout.md](./runbooks/outbound_warm_handoff_rollout.md) first** — newly-minted answers for the added brands now degrade to `cart_prefilled: null` rather than a wrong `false`, but the widening still retroactively falsifies `false` answers on redirect tokens minted up to 7 days earlier and not yet expired. | S | Env var | The cart-prefill path is already proven; it is an allowlist edit plus monitoring — plus the `cart_prefilled` conflict, which must be resolved before or with the widening. |
| 14 | **Scheduled freshness sweep** with a real per-row `last_crawled_at`, prioritized by serving rank. | M | Cloud Run Job + Scheduler on the 3a subnet; pinned pool 1..4; statement-timeout escape hatch (§3.3) | Live verification covers the top-3; the long tail still needs a floor. Promoted from October — it is the thing the migration most changes, so build it once, on GCP. |
| 10 | **`card_rail_outcomes` table + `POST /agent/v1/outcomes`** with the `failure_reason` vocabulary. | S–M | Cloud SQL migration + app code | The compounding asset. Starts paying off only after it has traffic, so it must exist before the first real transactions. |

### Wave 4 — late Sep → Oct. First real transactions

| # | Item | Effort | Where | Why |
|---|---|---|---|---|
| 9 | **Landed total via UCP `create_checkout`** (synthetic address, no PII) → `expected_item_total` / `expected_grand_total` in the spec. | M | Request path, 3a subnet | The agent has nothing to abort against until this exists. |
| 11 | **Promo/discount codes in the spec**, in application order, via `dev.ucp.shopping.discount`. | M | Request path | 43% of live PDPs are running a markdown; ignoring this systematically overquotes. |
| 13 | **`p_complete` into ranking**: set `w_business > 0`, feed the B-profile prior, then swap in measured outcomes. | S + M | App code | Turns the outcome graph into ranking lift. |
| 15 | **Bring the reco lane under ~3 s.** | M | App code | 7.65 s + verification is not a shippable turn. Note the migration already bought some of this for free — staging Cloud Run answered `/__catalog_health` in **1.8 s vs 5.8 s on Railway**; re-measure the reco lane post-cutover before optimising. |
| 16 | **3C / electronics cohort seeding.** | M–L | Pipeline | The milestone names beauty *and* 3C; today the corpus is beauty-dominant. |

### Explicitly deferred

- Checkout field maps / fill hints (**B5**) — only if Key Entry survives as the pilot mode.
- Guest-checkout detection (**B4**) — resolve it through UCP or learn it from `failure_reason`.
- Bot-vendor labelling beyond "Cloudflare: yes/no" — the label adds little; the *pacing* is what matters.
- Anything resembling browser-based cart execution — out of scope by instruction, and the UCP lane makes it unnecessary for 94% of the cohort anyway.
- **Any of the above built on Railway.** `infra/gcp/setup_scheduler.sh` reproduces Railway's job set exactly; a job added to Railway now and not to that script silently fails to migrate.

---

## 5. Pilot cohort recommendation

**Filter criteria (each measured, not assumed):**

1. Domain appears in the live serving corpus (`serving_eligible = true AND renderable = true`) — base population **8,655 SKUs / ~199 domains** (3,000-row census).
2. Domain is in the **top 66 by SKU count** — this is where the concentration is: top 23 domains carry 50% of SKUs, top 66 carry 80%.
3. Domain serves a valid `/.well-known/ucp` profile **and** its MCP endpoint answers an anonymous `tools/list` with `create_checkout` — **66 of the top 70 pass (94.3%), covering 91.3% of sampled SKUs**.
4. Platform = Shopify — **100%** of reachable cohort PDPs (n=81), and implied by (3).
5. Exclude domains with observed live defects: 404 PDPs, or index-vs-live price/stock drift in sampling.
6. Exclude non-Shopify retailers and Salesforce Commerce sites.

**Resulting cohort:**

| Tier | Domains | Basis |
|---|---|---|
| **Tier 0 — demo, week 1** | **6** | Already live on warm handoff: `cosrx.com`, `beautyofjoseon.com`, `skin1004.com`, `anua.us`, `medicube.us`, `mixsoon.us`. All 6 confirmed UCP + anonymous `create_checkout`. Zero new integration work. |
| **Tier 1 — September demo** | **~25** | Tier 0 plus the highest-SKU UCP-reachable domains that showed **zero defects** in live sampling — e.g. `paulmitchell.com` (96 SKUs), `pixibeauty.com` (95), `roundlab.com` (54), `tonymoly.us` (51), `naturium.com` (35), `meritbeauty.com` (34), `centellian24usa.com` (33), `us.nuxe.com` (33), `age20s.com`, `itsskin.us`, `iunik.com`, `equlib.us`, `hugrab.us`, `slowpure.com`, `milktouch.us`, `parkjunbeautylab.com`, `seapuri.us`, `lador.us`, `fwee.us`, `houseofbalance.us`, `tangleangel.com`, `oiad.us`, `todaywith.jp`, `tieut.jp`. |
| **Tier 2 — October, first real transactions** | **~66** | All UCP-reachable domains in the top 70. **≈2,237 SKUs in the 3,000-row sample → ≈6,400 SKUs extrapolated across the 8,655-row serving corpus [est.]**. This is the "order-of-100" target, and it is already 94% reachable. |
| **Excluded** | 4 + rot | `www.ulta.com` (Akamai, no UCP, retailer — 147 SKUs, the single biggest exclusion), `theordinary.com` (Salesforce Commerce), `toocoolforschool.us`, `thankyoufarmer.us`. Plus any domain whose 404 rate exceeds ~10% in the pre-pilot probe (`podl.us`, `poopourri.com`, `ponds.us`, `forbeaut.us` are current candidates). |

**Caveat on the defect filter:** the per-domain clean/defect rates come from **n = 2–3 SKUs per domain**. That is enough to *exclude* a domain confidently (a 404 is a 404) but not enough to *certify* one. Re-probe each Tier-1 domain at n ≥ 20 before the demo — at the pacing in H3 that is roughly one hour of wall clock for the whole tier.

---

## 6. Open questions for Reap

1. **Which VIC payment mode is the pilot actually on?** This audit found that 94% of our merchant cohort exposes a live, anonymous, protocol-based checkout. That makes **API/protocol mode viable today** and makes Key Entry the *harder* path, not the safer one. Is Reap's agent card able to settle against a merchant-hosted UCP checkout, or does the credential only work through a browser form fill?
2. **Does the VIC credential survive a `continue_url` handoff?** Our warm-handoff lane returns a pre-built cart URL on the merchant's own storefront. Does the agent complete there with the tokenized card, and does that count as a VIC transaction?
3. **Who owns agent registration and credentialing** — Reap, Visa, or Pivota? Pivota roots agent identity in DID/VC per ADR-012 and issues its own API keys. If Reap issues the agent identity, we need a mapping rule, not a second identity system.
4. **Will authorization outcomes be shared back, and on what key?** We can carry a `recommendation_id` all the way to the merchant's order (Shopify persists cart attributes into `note_attributes`). Can Reap stamp or echo it? If not, what is the join key — and what is the latency before an outcome is visible?
5. **What is the merchant universe and geography?** Our corpus is US-market beauty, Shopify D2C, with a small JP/GB/KR tail (118 rows excluded as `foreign_market`). Does VIC acceptance cover these merchants' acquirers, and does it cover JP/KR at all?
6. **Decline and 3DS behaviour.** On a step-up challenge at a long-tail merchant, what does the agent see, and what should Pivota record as `failure_reason`?
7. **Does Reap need a landed total in advance**, or will it authorize an open amount and capture the merchant's? This decides whether backlog item #9 is blocking for October or merely valuable.

---

## 7. Decisions needed from you

*(Listed, not made.)*

0. **`pivota-acp` decommissioning — a security decision rather than a cleanup one, but not an emergency.** A retired production service still serves `POST /agentic_commerce/delegate_payment`, whose deployed code would put raw PAN + CVC into **process memory** if called. It is **service-token gated** (not open — an earlier version of this line said otherwise and was wrong; see **H5**) and it has **no database**, so nothing is persisted. The exposure is a token-holder-reachable card intake on a service nobody monitors and nothing routes to. Three options, in the order I'd take them: **(a)** stop the service — no custom domain, zero references across all four GCP prod services, and ACP runs in-process in pivota-backend, so nothing breaks; **(b)** keep it up but drop that route; **(c)** accept it deliberately and in writing. **Do this soon after the cutover, not during it. Rotation of the Adyen key stays closed on your existing call** — a separate and genuinely dormant issue; do not let the two merge.
1. **Pilot rail: UCP protocol lane vs Key Entry.** The evidence says protocol. It also means the pilot leans on Shopify's UCP rollout rather than on browser automation, which is a strategic bet as much as a technical one. **This decision cascades into B5, B7, E, and F.**
2. **Do we serve a remembered price at all?** The alternative — never quote a price we have not verified in the last N seconds — is cleaner, honest, and slower. It also shrinks what the index is *for*.
3. **Acceptable p95 for a reco+verify turn.** I have assumed ≤3 s. If Minds will tolerate 5 s, item #15 leaves the September cut.
4. **Verification egress strategy — now a GCP decision, and the sharpest one.** One IP will not survive the volume (measured), and post-migration that IP is `8.231.167.230`, the address Antom and Adyen allowlist. Three options: **(a)** dedicated subnet + own Cloud NAT + own reserved IP (recommended — clean isolation, all in-house, ~M); **(b)** external proxy pool (also solves per-domain pacing, adds a third-party dependency on the request path); **(c)** accept a much lower verification rate and verify only Tier-0. **Not deciding this is the same as choosing (c) by accident**, because the rate limit will enforce it.
5. **Does the card-rail crawl work block the cutover, or wait behind it?** My recommendation is Wave 2 built inert on staging and un-paused during the soak — but that puts the first real freshness sweep in the same fortnight as the first real charge. The alternative is delaying the sweep to October and accepting the 31.1% wrongness for the demo, mitigated only by top-3 live verification.
6. **Cloud SQL sizing.** `db-custom-2-7680` REGIONAL HA was sized for today's read-heavy catalog. A nightly price/stock rewrite is a new sustained synchronous-replication write pattern. Size up pre-emptively, or turn the sweep on at low rate and watch? (Prod HA is already ~$450–500/mo accepted; a tier bump is real money.)
7. **Beauty-only September, or force 3C in.** The corpus is beauty; 3C needs seeding (item #16, M–L) and will not be ready at the same quality.
8. **Attribution stance.** Stamping `pivota_click_id` on the raw `destination_url` we hand agents makes attribution work — and makes it visible to the merchant. Acceptable?
9. **Whether Pivota tells the agent when it is *not* confident.** F3 recommends emitting `confidence: "unverified"` rather than silently degrading. That surfaces our own staleness to a partner.
10. **Whether to re-point `services/shopify_pricing_service.py` at UCP** or build a second pricing engine beside it. Re-pointing risks the PSP rail; building beside it risks the two drifting — the failure mode ADR-021 exists to prevent.

---

## Appendix — measurement provenance

| Finding | Source | n |
|---|---|---|
| 14,124 / 9,689 / 6,834 corpus counts | `GET api.pivota.cc/__catalog_health` | full |
| 14,128 trust rows, all stale >24h | `GET /__trust_health` | full |
| 9,095 canonical rows, freshness percentiles, blocker mix | `GET /api/canonical/products` paginated | full |
| 199 merchant domains, 28.0% variant coverage | `GET /api/canonical/products/{sig}` | 3,000 |
| 31 scheduler jobs, none a re-crawl | `GET /__scheduler_health` | full |
| 31.1% wrong-spec rate; 12.3% price, 11.1% stock, 6.7% dead | live PDP + Shopify `.js` probe | 90 SKUs / 37 domains |
| 100% Cloudflare, 100% Shopify | same probe | 90 / 81 |
| Cross-domain 429 after ~50 req/min | control re-probe of domains that had just returned 200 | 8 + 12 |
| 94.3% UCP + anonymous `create_checkout` | `/.well-known/ucp` + MCP `tools/list` | top 70 index domains |
| API latency p50/p95 | timed client-side, external network | 30 / 8 / 10 |
| Reco lane 7.65 s, `price_verification` block | `recommend_probe.json` (live, 2026-08-21) | 1 |
| All schema, flag, and code claims | direct file read at the cited path | — |

No writes were made to any database, storefront, or merchant cart during this audit. Merchant-side calls were limited to `GET` on public pages/profiles and one read-only JSON-RPC `tools/list` per shop.
