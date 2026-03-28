## Commerce Budget FX Release Gate Audit

Date: 2026-03-28

### Scope

This change fixes the remaining production runtime blocker behind the `Shopping Search Release Gate` after `PIVOTA-Agent` had already moved deploy verification to the authenticated invoke rail.

Target query:

- `vitamin c serum under €30`

### What This Patch Changes

- Keeps the main search path on `cache_multi_intent`; no resolver/error fallback is introduced.
- Preserves existing `x402_exchange_rates` direct and reverse snapshot lookup.
- Adds a bounded latest-rate fallback when snapshot lookup is unavailable:
  - env: `AGENT_SHOP_BUDGET_FX_LATEST_FALLBACK_ENABLED`
  - env: `AGENT_SHOP_BUDGET_FX_LATEST_BASE_URL`
  - env: `AGENT_SHOP_BUDGET_FX_LATEST_TIMEOUT_SECONDS`
- Emits `budget_fx_source=latest_rate_api` when the latest-rate fallback is used.
- Preserves the unresolved contract when both snapshot lookup and latest-rate fallback are unavailable.

### Related Chains Checked

Fixed in this PR:

- `source=search` strict multi-constraint EUR budget path
- cross-currency external seed budget filtering on the main cache-stage rail
- backend quick reliability suite baseline bug in external seed filter products

Explicitly preserved:

- `shopping_agent` + snapshot-backed FX path
- unresolved contract when no FX source is available
- exact lookup / merchant routing contracts unrelated to budget FX

Not changed here:

- `PIVOTA-Agent` workflow deploy verification rail
- Aurora runtime public-route smoke
- public `/api/gateway` legacy probe behavior

### Baseline Issue Found During Audit

While validating this fix on a clean `origin/main` backend worktree, `tests/test_external_products.py::test_shop_gateway_find_products_multi_matches_external_seeds_with_stopwords` was already failing because external seed filter products were constructed with `visible_attributes=None`, which is invalid for `StandardProduct`.

This PR fixes that baseline issue by preserving an empty dictionary instead of `None`.

### Verification

- `python3 -m pytest -q tests/test_external_products.py`
- `bash scripts/run_agent_reliability_suite.sh quick`

Expected production outcome after deploy:

- `vitamin c serum under €30`
- `status=200`
- `total > 0`
- `budget_fx_applied=true`
- `budget_fx_unresolved=false`
- no resolver/error fallback
