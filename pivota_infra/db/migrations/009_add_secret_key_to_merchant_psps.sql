-- Add secret_key column to merchant_psps table for PayPal client_secret
ALTER TABLE merchant_psps ADD COLUMN IF NOT EXISTS secret_key TEXT;

-- Add index for provider to speed up lookups
CREATE INDEX IF NOT EXISTS idx_merchant_psps_provider ON merchant_psps(provider);

-- Update the status of any existing PayPal connections to inactive by default
UPDATE merchant_psps SET status = 'inactive' WHERE provider = 'paypal' AND status IS NULL;
