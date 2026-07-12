# Decision needed: are `merch_obs_` observed sellers "first-party" for the trust gate?

**Status:** DECIDED — **Option C** (explicit observed-seller tier). Implemented in
pivota-backend `services/catalog_trust_policy.py` + PIVOTA-Agent
`src/services/catalogTrustPolicy.js`. The IPS-gate portion is zero serving-loss
(all merch_obs_ rows have IPS today); observed sellers stay exempt from the
identity-coverage shadow gates, made explicit rather than an accident of the
merchant_id string. Follow-up: run identity verification over the cohort, then
tighten the coverage exemption if desired.
**Audit findings:** #2/#3 from the ADR-009 legacy-merchant audit (2026-07-11).
**Owners of the code:** `services/catalog_trust_policy.py` (backend) + `src/services/catalogTrustPolicy.js` (agent) — identical twins.

## The bug
Both trust policies decide "first-party vs scraped third-party" with:
```
is_first_party = merchant_id != 'external_seed'
```
Under ADR-009, external seeds no longer live under the literal `external_seed`
merchant — they mirror under per-brand **observed sellers** (`merch_obs_…`). So
`merch_obs_` scraped supply now evaluates as **first-party**, which is almost
certainly not intended: it's crawled brand-site content, the exact thing the
identity pipeline exists to verify.

This flag controls two gates:

1. **Missing-IPS block** (`catalog_trust_policy.py:536`, `catalogTrustPolicy.js:412`)
   — a non-first-party row with **no `index_pipeline_state`** is blocked
   (`INDEX_NOT_SERVING_ELIGIBLE`); first-party rows pass. The c1.v0.4 fix added
   this after finding 80 external-seed products serving with public trust and no
   IPS quality-gate.

2. **Identity shadow gates** (`catalog_trust_policy.py:565`, `catalogTrustPolicy.js:437`)
   — first-party rows are **exempt** from the identity-coverage shadow gates
   (`IDENTITY_CONFIDENCE_NULL`, `IDENTITY_LIVE_READ_DISABLED`, and the
   `status='unknown'` shadow). The pipeline exists to verify scraped third-party
   content; first-party merchants are treated as their own source of truth.
   `review_required` / `IDENTITY_CONFLICT` still hard-gate everyone.

## Measured impact (prod, 2026-07-11)
- **1,359** `merch_obs_` external-seed catalog products.
- Missing-IPS gate: **0 impact today** — all 1,359 have an IPS row (the
  enrichment created them). This gate only matters for *future* onboards in the
  window before IPS materializes.
- Identity shadow gate: **this is the whole trade-off.** Per the code comment,
  external-mirror rows "largely" have `identity=None` → derived status
  `unknown`, confidence `null`. Under the current first-party classification
  they get `IDENTITY_NOT_APPLICABLE_FIRST_PARTY` and **serve**. If reclassified
  non-first-party, the same rows get `IDENTITY_CONFIDENCE_NULL` + `shadow=true`
  and are **withheld from public reads** until identity-verified.
  → Reclassifying strictly would dark most of the just-enriched 926-product
  cohort until the identity pipeline covers them. (Exact count = # of
  `merch_obs_` rows with no identity row / unknown status — measure before
  implementing.)

## The real question
**Do we require identity-pipeline verification before publicly serving a
brand's own crawled D2C content?**

A `merch_obs_` observed seller is genuinely dual-natured:
- **First-party-ish:** it IS the brand's own store (e.g. `goongbe.us` = GOONGBE),
  so the content is brand-authoritative, not a random reseller scrape.
- **Third-party-ish:** we obtained it by crawling without the brand's
  API/cooperation, so it's exactly the "scraped content" the identity pipeline
  was built to verify (wrong product, stale, misattribution).

## Options

**A. Treat `merch_obs_` as non-first-party (gate strictly).**
- Correct pure-trust posture: don't publicly serve unverified scraped content.
- Cost: darks most of the enriched cohort on public reads until identity
  verification runs; effectively gates the ADR-009 "unblock merch_obs_ serving"
  goal behind identity coverage. Would need an identity-verification push on the
  cohort to recover serving.

**B. Keep `merch_obs_` as first-party (status quo).**
- Serves the enriched cohort now; matches ADR-009 #1770 intent ("unblock
  merch_obs_ trust + serving").
- Cost: the "trust bypass" the audit flagged — crawled content serves publicly
  without identity-pipeline verification, and the c1.v0.4 missing-IPS protection
  is off for `merch_obs_` (latent: only bites future pre-IPS onboards, 0 today).
- Also fragile: "first-party" is an *accident* of `merchant_id != 'external_seed'`,
  not a deliberate decision.

**C. Introduce an explicit "observed_seller" trust tier (recommended).**
- Discriminate on `platform='external_seed'` (or a truth-tier), not `merchant_id`,
  and give observed sellers their own posture:
  - **Apply** the missing-IPS/quality gate (they pass it — 1,359/1,359 have IPS,
    926 eligible) → closes the c1.v0.4 latent hole for future onboards.
  - **Keep** the hard identity gates that are explicit signals
    (`review_required`, `IDENTITY_CONFLICT`) — those still hard-gate.
  - **Exempt** from the identity-*coverage* shadow gates (null-confidence /
    unknown-status), on the basis that a brand's own D2C crawl is
    brand-authoritative — same treatment first-party gets today, but stated
    deliberately rather than emerging from a merchant_id accident.
- Net: preserves serving of the enriched cohort, closes the IPS hole, makes the
  trust posture explicit and auditable. Pairs well with a follow-up to run
  identity verification on the cohort over time (then tighten if desired).

## Recommendation
**C**, with the IPS-gate portion shippable immediately (zero serving loss, closes
a latent hole) and the identity-coverage posture made explicit. If the trust bar
is "no unverified scraped content serves, full stop," then **A** — but that's a
deliberate choice to dark the cohort pending identity verification, and should be
paired with an identity-verification plan so the ingestion work isn't stranded.

**Do not** ship a blind `merchant_id → platform` swap here (unlike the other
ADR-009 fixes): for these two gates that swap = Option A's behavior, which
darks the cohort. The direction is a decision, not a mechanical fix.
