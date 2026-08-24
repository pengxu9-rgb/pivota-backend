-- Checkout probes are merchant-scoped; active routes must not create
-- concurrent audits for the same merchant.
CREATE UNIQUE INDEX IF NOT EXISTS uq_verification_runs_active_merchant_commerce_checkout
  ON verification_runs (merchant_id, verifier_id)
  WHERE merchant_id IS NOT NULL
    AND verifier_id = 'commerce_checkout_probe'
    AND status IN ('pending', 'claimed');
