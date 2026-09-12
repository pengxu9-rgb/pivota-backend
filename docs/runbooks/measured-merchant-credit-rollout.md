# Fractional merchant text metering rollout

Pricing: verified model usage cost × 1.6 / USD 0.01 per credit, persisted with eight decimal places. Audits keep frozen fixed quotes. Text generation has a maximum charge of 1 credit, requires that amount in the existing wallet, and never initiates an external overage card charge inside its result transaction.

1. Apply migration 222 in a transaction using the established production migration runner. It preserves existing integer values and starts `merchant_metering_controls.measured_text_v1` disabled. If the five-second lock timeout fires, investigate and retry; do not remove the lock guard.
2. Deploy web, worker and proof-issuer through the normal exact-SHA test gates. Until activation, new paid text generation is temporarily unavailable; deck export falls back to the existing report without the AI summary. Existing saved measured results remain readable. Follow-up actions must not create empty tasks due to a disabled metering policy.
3. Verify all three service image SHAs match the tested release. Only then activate with `UPDATE merchant_metering_controls SET enabled=TRUE, updated_at=NOW() WHERE policy_version='measured_text_v1'`.
4. Publish the matching frontend with the maximum-cost disclosure and fractional usage readout. Verify ask, draft, summary, replay and failure behavior from a logged-in merchant session.
5. Verify expired subscription cleanup produces a merchant_credit_adjustments receipt and preserves persistent purchased/granted credits. Do not invent historical token usage or rewrite old debit records. ANUKO's legacy manual grant is not a verified Stripe purchase.

Rollback: disable the policy before rolling services back. Do not revert NUMERIC columns to integers or deploy pre-fractional wallet/refund code after fractional transactions exist. Prefer a forward fix; retained numeric balances and usage receipts must remain exact.

Local verification requires a dedicated localhost recovery_contract_test database with migrations applied and the policy enabled. Run tests/integration/test_fractional_credit_postgres.py with RUN_RECOVERY_POSTGRES=1. These tests refuse remote databases.
