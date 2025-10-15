# 🔧 Fix Supabase User Creation Issue

## ❌ **Problem:**
When trying to create users via the API, you get the error:
```
"User not allowed"
```

## ✅ **Solution:**

The issue is that **Email signup is disabled** in your Supabase project settings. Here's how to fix it:

### **Step 1: Enable Email Provider in Supabase**

1. Go to your Supabase Dashboard: https://supabase.com/dashboard
2. Select your project: `jagukdffqmlnktrmionh`
3. Navigate to **Authentication** → **Providers**
4. Find **Email** in the list of providers
5. Click on **Email** to expand settings
6. **Enable** the Email provider if it's disabled
7. Under **Email Auth Settings**, make sure these are configured:
   - ✅ **Enable Email Signup** - Turn this ON
   - ✅ **Confirm Email** - You can turn this OFF for testing (or keep ON if you want email confirmation)
   - ✅ **Secure Email Change** - Optional
8. Click **Save**

### **Step 2: Check Auth Settings**

1. Still in **Authentication** section
2. Go to **Settings** (under Authentication)
3. Check these settings:
   - **Site URL**: Set to your Lovable app URL or `http://localhost:3000` for testing
   - **Redirect URLs**: Add your app URLs
   - **JWT Expiry**: Default (3600 seconds) is fine

### **Step 3: Verify Service Role Key**

Make sure you're using the **Service Role Key** (not the Anon Key) in your Render environment variables:

```
SUPABASE_SERVICE_ROLE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImphZ3VrZGZmcW1sbmt0cm1pb25oIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2MDQzMDUxNywiZXhwIjoyMDc2MDA2NTE3fQ.MI4SeubivXaHDsxUWyvxSJ7yAd8iutxPnuElz8LPrvs
```

### **Step 4: Test User Creation**

After enabling Email signup, test again:

```bash
curl -X POST https://pivota-dashboard.onrender.com/auth/signup \
  -H "Content-Type: application/json" \
  -d '{"email": "newuser@example.com", "password": "password123"}'
```

**Expected response:**
```json
{
  "status": "success",
  "message": "Account created successfully. Awaiting admin approval.",
  "user_id": "...",
  "role": "employee",
  "approved": false
}
```

---

## 🔍 **Alternative: Manual User Creation**

If you prefer to create users manually (for now), you can do it directly in Supabase:

### **Method 1: Via Supabase Dashboard**

1. Go to **Authentication** → **Users**
2. Click **Add user** (or **Invite user**)
3. Enter email and password
4. Click **Create user**
5. Then run this SQL to set role and approval:

```sql
-- Set the user's role and approval status
UPDATE user_roles 
SET role = 'employee', approved = false 
WHERE user_id = 'USER_ID_FROM_SUPABASE';
```

### **Method 2: Via SQL**

Run this in Supabase SQL Editor:

```sql
-- This creates a user directly (requires service role)
-- Note: This bypasses the normal auth flow
INSERT INTO auth.users (
  instance_id,
  id,
  aud,
  role,
  email,
  encrypted_password,
  email_confirmed_at,
  created_at,
  updated_at,
  confirmation_token,
  email_change,
  email_change_token_new,
  recovery_token
) VALUES (
  '00000000-0000-0000-0000-000000000000',
  gen_random_uuid(),
  'authenticated',
  'authenticated',
  'newuser@example.com',
  crypt('password123', gen_salt('bf')),
  NOW(),
  NOW(),
  NOW(),
  '',
  '',
  '',
  ''
);
```

**Note:** This is complex and not recommended. It's better to fix the Email provider settings.

---

## 🎯 **Recommended Approach:**

1. ✅ **Enable Email signup in Supabase** (Step 1 above)
2. ✅ **Test user creation via API**
3. ✅ **If it works, you're done!**
4. ❌ **If it still fails, check the service role key**

---

## 📝 **What I've Already Fixed in the Code:**

I've updated `utils/supabase_client.py` to:
- ✅ Provide better error messages
- ✅ Auto-confirm emails (bypass email confirmation)
- ✅ Add user metadata for role

The code is ready. You just need to enable Email signup in Supabase settings!

---

## 🚀 **After Fixing:**

Once you enable Email signup in Supabase, user creation will work automatically through your Lovable frontend!

Users will be able to:
1. Sign up with email/password
2. Wait for admin approval
3. Sign in after approval
4. Access the system based on their role

**Need help?** Let me know if you encounter any issues after enabling Email signup!

