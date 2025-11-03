-- Find merchant by email: yao.wang@chydan.com

-- Check merchant_onboarding table
SELECT 
    merchant_id, 
    business_name, 
    contact_email, 
    status, 
    created_at,
    psp_connected,
    mcp_connected
FROM merchant_onboarding 
WHERE contact_email = 'yao.wang@chydan.com' 
   OR business_email = 'yao.wang@chydan.com';

-- Check users table
SELECT 
    user_id,
    email, 
    full_name, 
    role, 
    active,
    created_at
FROM users 
WHERE email = 'yao.wang@chydan.com';

-- If merchant exists, get their login credentials info
-- Note: Password is hashed, cannot retrieve plaintext
SELECT 
    u.email,
    u.role,
    m.merchant_id,
    m.business_name,
    'Password is hashed - use password reset if needed' as note
FROM users u
LEFT JOIN merchant_onboarding m ON u.email = m.contact_email
WHERE u.email = 'yao.wang@chydan.com';



