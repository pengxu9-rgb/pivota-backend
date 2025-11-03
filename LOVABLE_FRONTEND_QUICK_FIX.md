# 🔧 Lovable Frontend Quick Fix

## ❌ **Problem:**
- User exists in Supabase with correct user_roles
- Admin dashboard not showing the user
- PSP test button not working

## ✅ **Root Cause:**
Your Lovable frontend is trying to query Supabase directly, but likely hitting:
1. **RLS (Row Level Security) policies** blocking the queries
2. **Missing service role key** (using anon key which has restrictions)
3. **Wrong API endpoints** for PSP management

---

## 🚀 **QUICK FIX - Two Options:**

### **Option 1: Fix Supabase RLS Policies (Keep Current Setup)**

Add these RLS policies in Supabase:

```sql
-- Allow admins to read all user_roles
CREATE POLICY "Admins can read all user_roles"
ON user_roles
FOR SELECT
USING (
  EXISTS (
    SELECT 1 FROM user_roles ur
    WHERE ur.user_id = auth.uid()
    AND ur.role = 'admin'
    AND ur.approved = true
  )
);

-- Allow admins to update user_roles
CREATE POLICY "Admins can update user_roles"
ON user_roles
FOR UPDATE
USING (
  EXISTS (
    SELECT 1 FROM user_roles ur
    WHERE ur.user_id = auth.uid()
    AND ur.role = 'admin'
    AND ur.approved = true
  )
);

-- Allow admins to read all profiles
CREATE POLICY "Admins can read all profiles"
ON profiles
FOR SELECT
USING (
  EXISTS (
    SELECT 1 FROM user_roles ur
    WHERE ur.user_id = auth.uid()
    AND ur.role = 'admin'
    AND ur.approved = true
  )
);
```

**Then check browser console for errors!**

---

### **Option 2: Use FastAPI Backend (RECOMMENDED)**

Replace your Lovable components with the fixed versions I created:

#### **Step 1: Replace useAuth.ts**

Copy `lovable_components/useAuth_FIXED.ts` content and paste into your Lovable project at `src/hooks/useAuth.ts`

**Key changes:**
- Uses FastAPI backend instead of Supabase
- JWT token authentication
- No RLS policy issues

#### **Step 2: Replace AdminDashboard.tsx**

Copy `lovable_components/AdminDashboard_FIXED.tsx` content and paste into your Lovable project at `src/components/AdminDashboard.tsx`

**Key changes:**
- Fetches users from FastAPI `/auth/admin/users`
- Approves users via FastAPI `/auth/admin/users/{id}/approve`
- PSP test via FastAPI `/admin/psp/{id}/test`
- All authenticated with JWT token

#### **Step 3: Set Environment Variable**

In Lovable project settings:
```
VITE_API_URL=https://pivota-dashboard.onrender.com
```

#### **Step 4: Test**

1. Sign in with admin account (test@example.com / password123)
2. Go to Admin Dashboard
3. You should see pending users
4. Click Approve - should work
5. Test PSP - should work

---

## 🔍 **Why Option 2 is Better:**

### **Current Setup (Supabase Direct):**
```
Frontend → Supabase (RLS policies, limited control)
```

**Issues:**
- ❌ RLS policies can block queries
- ❌ Limited to Supabase features
- ❌ PSP management not in Supabase
- ❌ Analytics not in Supabase

### **Fixed Setup (FastAPI Backend):**
```
Frontend → FastAPI → Supabase (full control)
```

**Benefits:**
- ✅ No RLS policy issues
- ✅ Custom business logic
- ✅ PSP management working
- ✅ Analytics working
- ✅ Better error handling
- ✅ Already tested and working!

---

## 📋 **Immediate Action:**

### **Quick Test - Check Browser Console:**

1. Open your Lovable app
2. Open browser DevTools (F12)
3. Go to Console tab
4. Look for errors when loading Admin Dashboard

**Common errors you might see:**
- `Failed to fetch` - CORS or network issue
- `403 Forbidden` - RLS policy blocking
- `401 Unauthorized` - Authentication issue
- `404 Not Found` - Wrong endpoint

**Tell me what error you see and I'll help fix it!**

---

## 🚀 **Recommended Solution:**

**Use the FastAPI backend (Option 2)** because:
1. ✅ Already tested and working
2. ✅ No RLS policy configuration needed
3. ✅ PSP management works
4. ✅ Analytics works
5. ✅ Better error handling

**Just copy 2 files and set 1 environment variable!**

---

## 📞 **Next Steps:**

1. **Open browser console** in your Lovable app
2. **Tell me what errors you see**
3. I'll help you fix them quickly!

Or simply:
1. **Copy the fixed files** (useAuth_FIXED.ts and AdminDashboard_FIXED.tsx)
2. **Set VITE_API_URL** environment variable
3. **Test** - everything should work!



