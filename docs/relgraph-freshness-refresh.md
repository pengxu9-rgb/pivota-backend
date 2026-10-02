# Bounded recommendation freshness handoff

The paired gateway change provides `scripts/audit-relgraph-freshness.js`: a read-only,
aggregate-only audit and explicit private IDs-only worklist creation. It selects stale
selected anchors, serving-safe approved endpoints and uncovered anchors using the gateway's
canonical coverage/cooldown owner. The normal recommendation path does not fetch from an
origin or invoke an LLM for freshness.

Consume a private `relgraph.freshness_refresh.v1` manifest in this repository:

```sh
python -m scripts.ops.relgraph_freshness_refresh --manifest /private/operator/refresh.json
python -m scripts.ops.relgraph_freshness_refresh --manifest /private/operator/refresh.json --apply --authorize-operator-refresh --budget-seconds 120 --host-concurrency 2
```

Default execution is read-only: no origin request, seed/offer/page write or label mutation.
The apply command requires both explicit flags, a manifest at most 24 hours old, at most 200
products/seeds, supported market and native currency, and the existing offer dual-write flag.
It rechecks current live sync, merchant/store activity, test sources, market, currency,
suppression and quarantine. A merchant disconnected after planning is excluded. The owner
seed selector repeats its own guards on the restricted IDs; no URL is accepted as input.
Every origin proof and restricted seed selection also requires an exact attachment. A
populated conflicting attachment defeats ID fallback. Unattached rows may bind only the
legacy `external_seed` namespace or globally external `ext_` IDs; native store-local IDs
alone cannot establish freshness or authorize origin work for another listing.

The existing refresh batch retains origin politeness, host/IP breakers, and origin/product/
currency validation. The crawl deadline is hard (maximum 600 seconds); page recomputation
uses the existing expression/writer afterward with a separate 30-second deadline. Prior
writes can commit before cancellation, so a timeout reports partial writes possible and no
blind retry runs. A skipped, cached, unavailable-price, failed, or incomplete projection/page
outcome is degraded; successful counters alone cannot establish fresh stored prices. A final
bounded read-only replan (at most 30 additional seconds) checks the manifest's current page,
offer, origin and eligibility state. It emits only remaining-work aggregates. An old/null
price clock retained by the mirror owner, an unresolved attached listing, or a failed/timed-out
recheck is degraded with `complete:false`; no second refresh attempt runs. Counter overcounts
also cannot qualify as complete. Success requires both actual owner results and settled DB truth.
Only aggregate counts are printed. The isolated CLI suppresses owner diagnostics that can
carry product rows or URLs. Relationship labels, including human approvals, are untouched.

A stale positive/negative page check is unknown confidence, not evidence of a broken page.
`pdp_will_render_computed_at` records database-truth content-route/serving validation, not an
HTTP crawl. The periodic PDP reconciler now includes unchanged rows with missing, future or
old timestamps, by default after 48 hours and at most 2,000 per pass. The value and timestamp
are still written together by `pdp_renderability_store`; age counters are separate from drift.
Null/future timestamps precede past timestamps so a corrupt future date cannot sit forever
behind a continuously stale catalog. Current invalid prices or unknown stock are origin work;
current explicit unavailable stock is a conclusive result. Unowned offers and products filtered
by the current apply guards are counted and cannot report complete freshness.
`PDP_WILL_RENDER_RECONCILE_STALE_HOURS` is clamped 1–144; the row limit is capped at 2,000.

Fresh offer price proof remains `price_checked_at`; availability/origin proof remains the
matching seed's successful `last_crawled_at`. Attempts, `updated_at`, and cached fallback do
not advance proof. The targeted stale tier is 48 hours. A wrong native currency is excluded;
amounts/currency are never relabelled. Native/merchant offers without an external seed remain
owned by their existing native refresh pipeline; this tool cannot claim to check them.

No new scheduler/IaC resource is added, armed or executed. The existing worker must use the
reviewed backend image before it can run the changed reconciler. Gateway/backend images have
different runtimes and ephemeral filesystems: keep the private manifest in a controlled
operator workspace or supply an explicitly approved private file to the backend job. A
gateway job's `/tmp` cannot be read by a separate backend job. This implementation performs
no cross-image export, production extraction, deploy, cron tick or production write.

Validation: the unit file is discovered by the backend test sweep. The real PostgreSQL files
`test_relgraph_freshness_refresh_postgres.py` and `test_pdp_will_render_reconciler_postgres.py`
are automatically discovered by the existing `postgres-dialect-gate.yml`; the script/service/
job/test changes trigger that workflow. They use synthetic fixtures and verify real driver
bindings, canonical attachment, market/currency, disconnection, suppression/quarantine and
unchanged true/false timestamp refresh through the existing owner writer.
