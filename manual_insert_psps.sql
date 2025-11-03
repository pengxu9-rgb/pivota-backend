-- Manual PSP Configuration Insert
-- Run this in Railway PostgreSQL console

-- First, check current PSPs
SELECT psp_id, merchant_id, provider, status, connected_at 
FROM merchant_psps 
WHERE merchant_id = 'merch_208139f7600dbf42'
ORDER BY connected_at DESC;

-- Delete any existing test PSPs to start clean (optional)
-- DELETE FROM merchant_psps WHERE merchant_id = 'merch_208139f7600dbf42' AND provider IN ('adyen', 'checkout', 'paypal');

-- Insert Adyen
INSERT INTO merchant_psps 
(psp_id, merchant_id, provider, name, api_key, account_id, capabilities, status, connected_at)
VALUES 
('psp_adyen_manual_001', 'merch_208139f7600dbf42', 'adyen', 'Adyen Account', 
 'YOUR_ADYEN_TEST_API_KEY_HERE', 'YOUR_MERCHANT_ACCOUNT', 'payments,refunds,payouts', 'active', NOW())
ON CONFLICT (psp_id) DO UPDATE SET
    api_key = EXCLUDED.api_key,
    account_id = EXCLUDED.account_id,
    status = EXCLUDED.status;

-- Insert Checkout
INSERT INTO merchant_psps 
(psp_id, merchant_id, provider, name, api_key, account_id, capabilities, status, connected_at)
VALUES 
('psp_checkout_manual_001', 'merch_208139f7600dbf42', 'checkout', 'Checkout Account', 
 'YOUR_CHECKOUT_SECRET_KEY', 'YOUR_PROCESSING_CHANNEL_ID', 'payments,refunds', 'active', NOW())
ON CONFLICT (psp_id) DO UPDATE SET
    api_key = EXCLUDED.api_key,
    account_id = EXCLUDED.account_id,
    status = EXCLUDED.status;

-- Insert PayPal
INSERT INTO merchant_psps 
(psp_id, merchant_id, provider, name, api_key, account_id, secret_key, capabilities, status, connected_at)
VALUES 
('psp_paypal_manual_001', 'merch_208139f7600dbf42', 'paypal', 'PayPal Account', 
 'YOUR_PAYPAL_CLIENT_ID', NULL, 'YOUR_PAYPAL_CLIENT_SECRET', 'payments,refunds,payouts', 'active', NOW())
ON CONFLICT (psp_id) DO UPDATE SET
    api_key = EXCLUDED.api_key,
    secret_key = EXCLUDED.secret_key,
    status = EXCLUDED.status;

-- Verify inserts
SELECT psp_id, provider, 
       LENGTH(api_key) as api_key_len, 
       account_id,
       CASE WHEN secret_key IS NOT NULL THEN 'YES' ELSE 'NO' END as has_secret,
       status 
FROM merchant_psps 
WHERE merchant_id = 'merch_208139f7600dbf42'
ORDER BY connected_at DESC;

-- Expected result: Should see adyen, checkout, paypal with active status


