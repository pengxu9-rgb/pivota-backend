# Revenue recovery contract 1.2.0

This change connects retained reports and new URL audits to the merchant recovery projection. It does not demonstrate better AI outcomes, recovered revenue, or a production rollout.

## Measurement

`selection.version=1`; methodology version 2. Each response observation is deposited as one canonical evidence row of the registered type `selection_response` (`db/audit_evidence.py`, the 14th member of `VALID_EVIDENCE_TYPES`), keyed by its `observation_id`; an unregistered type would be silently rewritten to `custom`. The unit is a product × provider × query × response, including replicate attempts. Identical observation IDs are deduplicated only within a report. Observation IDs are stable for the retained response ordering, not cross-run join keys.

Three disjoint tiers: dupe/alternative first, branded next, then recognized category intents; unfamiliar/custom axes remain unclassified. Failed responses never become negative answers. Missing answer evidence is unknown. Counts expose attempts, failures, eligible denominator, positives, unknowns and providers. Wilson 95% intervals describe eligible observed responses under an independence assumption. Provider and repeated-query correlation limits their interpretation; these intervals do not establish improvement.

The explicit-answer boolean contract (`explicit_answer_brand_mentioned_v1`) is separate from grounding-source visibility. Existing gateway probes request structured diagnostics and excerpts, not complete consumer answers. They do not reliably supply this boolean, so the new merchant panel will ordinarily show answer mentions as unmeasured until a versioned upstream answer-observation producer is implemented and validated. Do not infer absence from `correct_sku=false`, empty citations or an excerpt. Do not relabel source visibility as answer mention rate.

Historical reports without response observations remain unknown; the old two-bucket question-level citation aggregate is retained only for compatibility, not used as the new three-tier rate. A current score is a diagnostic distribution, not a conversion measure. Convert sales remains unverified.

## Persistence and compatibility

URL verifying writes canonical evidence/findings and reads back every generated projection before completion. Failures use the existing fail/refund path; catalog-only verification and enqueue stay separate. URL routing scores/findings are suppressed. Basis recording remains best effort; a missing basis disables comparison.

Completed runs with missing or outdated recovery projections are rebuilt read-only from retained report JSON on authenticated GET. This is a compatibility view, not a historical DB backfill. Ownership and paid-action filtering still apply. The Copilot context uses the same recovery builder.

Numerical movements require two complete comparable basis records and a current comparison-contract stamp. Old stored materiality flags and unguarded summary deltas are not accepted. The existing 15-point rule is a materiality heuristic, not a statistical significance test.

## Catalog and domains

The audit picker uses tenant-scoped `catalog_products` with pagination, matching the audit readiness source. Storefront metadata is optional enrichment. Requested product count and saved result count have distinct labels; missing per-product completion reasons are not invented.

The worker scheduler seeds inferred domains in bounded merchant pages before checking due rows every six hours. It records seed failures and observes a 180-second job deadline. HTTPS and robots fetches validate and pin public DNS addresses per redirect, preserve TLS SNI/Host, and bound response bytes. Declared/inferred/live status is not ownership proof.

This job is **opt-in and dormant by default**. `jobs/official_domain_liveness.run_official_domain_liveness_tick` returns `{"skipped": true}` unless `OFFICIAL_DOMAIN_LIVENESS_ENABLED=true`; the scheduler registers it either way, exactly as it registers `identity_reconcile_sweep`. It is opt-in because its first run seeds `merchant_official_domains` rows for every merchant in the catalog, and `official_domains` is one of `db.audit_basis.COMPARABILITY_FIELDS` — seeding moves attribution and makes each affected merchant's next re-audit a non-comparable pair. Arming procedure: (1) size the seed with a read-only dry run — `services.official_domain_liveness.seed_inferred_domains` is per-merchant, and `refresh_official_domain_liveness(merchant_id, dns_only=True, seed_inferred=False)` checks without seeding; (2) confirm the row count and the affected merchant set are what the dry run predicted; (3) set `OFFICIAL_DOMAIN_LIVENESS_ENABLED=true` on the worker service only. Unsetting it re-parks the job at the next tick.

## Validation and remaining rollout gates

Targeted backend regression suite: 406 passing tests, including historical compatibility/tenant access/paywall, JSON serialization-to-projection parity, URL persistence failure handling, scheduler isolation, redirects/private DNS, and existing audit modules. Frontend: real React renderer tests plus browser preview from backend fixtures. The populated mention fixture is synthetic and labeled accordingly. Whole-repository TypeScript has eight pre-existing errors, identical to the untouched base.

A dedicated local PostgreSQL 15 test now covers retained report JSONB → real canonical accessors → six projection inserts and updates → authenticated HTTP route → the actual React renderer. It found and fixed nondeterministic finding order, and now rejects swallowed empty reads in strict mode. This is still not a live worker run, paid audit execution, or deployed browser verification. No production records were backfilled and no deployment was performed. Homepage/share/export still use their legacy report contract (summary movement guard corrected); the new recovery panel is connected to catalog and URL report pages. Full consumer-answer capture, per-product skipped reasons, ownership coverage census and deterministic retest acceptance remain separate pending evidence.
