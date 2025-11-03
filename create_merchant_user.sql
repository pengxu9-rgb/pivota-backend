-- Create user account for merchant: yao.wang@chydan.com
-- Password will be: Merchant123!
-- Password hash is bcrypt hash of "Merchant123!"

-- First, check if user already exists
-- Run this query first to see if user exists:
SELECT email, role FROM users WHERE email = 'yao.wang@chydan.com';

-- If user doesn't exist, run this to create:
INSERT INTO users (email, password_hash, full_name, role, active)
VALUES (
    'yao.wang@chydan.com',
    '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5aqfyH2T0vGEC',  -- Merchant123!
    'Yao Wang',
    'merchant',
    true
)
ON CONFLICT (email) DO UPDATE SET
    password_hash = EXCLUDED.password_hash,
    role = 'merchant',
    active = true;

-- Verify the user was created:
SELECT email, full_name, role, active, created_at FROM users WHERE email = 'yao.wang@chydan.com';

-- Also find their merchant_id:
SELECT merchant_id, business_name, status FROM merchant_onboarding WHERE contact_email = 'yao.wang@chydan.com';



