# AI Commerce Readiness Reference — Pivota Internal Test Merchant

_This file is a placeholder. Run the CLI with a live Gemini key to populate it:_

```bash
PROMOTIONS_ADMIN_KEY=...  \
PIVOTA_AGENT_INTERNAL_URL=https://pivota-agent-production.up.railway.app  \
PYTHONPATH=. python scripts/agent_center_bd_test_merchant_audit.py  \
  --output reports/test-merchant-bd-audit.md
```

The BD report's `discovery_lift.pivota_reference` field points to this
file as the canonical "post-onboarding reference" — a paired audit on
Pivota's internal Shopify test merchant (`merch_38fa56d5118b9974` @
`shop.myshopify.com`) covering the same probe modes the prospective
merchant's audit uses.

Refresh cadence: ~monthly, or after material changes to the test
merchant's catalog / Pivota PDP infrastructure. Cost is bounded at
~27 grounded Gemini calls per refresh (3 SKUs × 3 scan modes × 3 runs).

**This placeholder is intentionally minimal.** Do not edit by hand —
let the CLI overwrite it. Commit the regenerated file so BD has a
stable URL for the pitch deck.
