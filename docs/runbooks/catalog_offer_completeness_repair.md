# Catalog offer completeness repairs

Use stored, exact variant evidence. These tools do not fetch merchants, stamp a
price/cart proof, enable checkout, lift a withdrawal, or promote readiness.

## Targets and review

All commands default to staging and dry-run. Production requires explicit
`--environment production`, `PIVOTA_ENV=production`, and the current runtime
catalog database at `10.25.0.2/pivota_08220842_Zsqlgz`. Staging is pinned to
`10.122.0.3/pivota`. DSN target overrides and server identity mismatches are refused.

Run the desired command without `--apply`, review its complete manifest, and
record `plan_sha256`. Apply only after independent adversarial review and green
CI for the deployed code. A changed plan aborts before data changes; missing-offer
plans additionally revalidate each product under locks in one batch transaction.

```sh
python -m scripts.repair_missing_variant_offers --environment production --manifest
python -m scripts.repair_catalog_variant_prices --environment production
python -m scripts.repair_invalid_catalog_offer_links --environment production
```

Apply requires the reviewed hash and the tool's explicit contract:

```sh
python -m scripts.repair_missing_variant_offers --environment production --apply \
  --expect-contract missing-mirror-variant-offers-v1 --expect-plan-sha256 REVIEWED_SHA256
python -m scripts.repair_catalog_variant_prices --environment production --apply \
  --expect-contract repair-native-variant-money-v1 --expect-plan-sha256 REVIEWED_SHA256
python -m scripts.repair_invalid_catalog_offer_links --environment production --apply \
  --expect-contract withdraw-invalid-catalog-links-v1 --expect-plan-sha256 REVIEWED_SHA256
```

## Evidence and ownership

Missing mirror offers use their own variant amount, exact active seed attachment,
listing, seller and market. A deterministic canonical mirror offer remains a
valid listing template when its SKU was renamed; arbitrary sibling offers are
not templates. Existing and withdrawn offers are preserved.

Money repairs require exactly one active owned offer and either a consistent
current numeric variant ID or a unique bridge from the legacy numeric ID through
its stored SKU code to the current native variant. Conflicting numeric or string
aliases, duplicate codes, aggregate `offer_1` IDs, contradictory domains and
currency/market disagreement are refused. Never substitute the product amount.
SKU currency, money payload aliases, offer amounts and conservative stock state
change atomically. Existing market and readiness stay unchanged. Explicit
unavailability wins; this tool never upgrades stock. The price-age trigger must
be installed and enabled: a changed amount clears its old `price_checked_at`
instead of claiming a new read. Repairs retain their evidence fingerprint and
previous monetary values in payload provenance and writer audit records.

Generic link integrity is product/SKU identity and both suppression columns.
Offer sellers may legitimately differ from the product's anchor seller. Invalid
links are withdrawn with provenance; no seller deduplication or parent revival.

The former domain-based currency relabel tool is now an audit only. `--apply`
refuses before database or storefront access; its legacy SQL helper cannot write.
A store's base currency alone does not prove a captured amount's denomination.

## Verification and limits

Recount physical links, missing variants and positive offers in SKU currency;
check repaired payload aliases, explicit unavailable variants, price age and
unchanged withdrawals. Preserve the intentional `same_key_other_listing` and
`demo_retired_2026_07` withdrawals. Clean up temporary repair jobs after inspection.

Unverified source identities and contradictory captures remain unresolved rather
than inventing prices or routing. Repaired money is protected from legacy
canonical/promoter replays. A future native variant refresh must explicitly
supersede the reviewed evidence; this repair scan intentionally excludes already
healthy rows. Shared writer pre-read guards validate static physical links; the
repair transactions and canonical SQL add their own atomic checks, and this is
not a general concurrency guarantee for every catalog writer.
