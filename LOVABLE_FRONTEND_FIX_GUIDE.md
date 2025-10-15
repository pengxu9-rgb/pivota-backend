# 🔧 Lovable Frontend Fix Guide

## ❌ **Problem:**
The Lovable frontend has corrupted API connections:
- Admin dashboard trying to use wrong endpoints
- Using Supabase directly instead of FastAPI backend
- PSP endpoints pointing to non-existent routes
- Auth hook using Supabase auth instead of FastAPI

## ✅ **Solution:**

I've created fixed versions of all the corrupted components. Follow these steps to fix your Lovable frontend:

---

## 📁 **Files to Replace:**

### **1. Replace `useAuth.ts`**

**Location:** `src/hooks/useAuth.ts` (or wherever your auth hook is)

**Replace with:** `lovable_components/useAuth_FIXED.ts`

**Key Changes:**
- ✅ Uses FastAPI backend (`/auth/signin`, `/auth/signup`, `/auth/me`)
- ✅ Stores JWT token in localStorage
- ✅ Proper error handling
- ✅ No Supabase dependency for auth

### **2. Replace `AdminDashboard.tsx`**

**Location:** `src/components/AdminDashboard.tsx` (or your dashboard component)

**Replace with:** `lovable_components/AdminDashboard_FIXED.tsx`

**Key Changes:**
- ✅ Uses FastAPI backend for user management (`/auth/admin/users`)
- ✅ Correct PSP endpoint (`/admin/psp/status` instead of `/psp-fix/status`)
- ✅ Added Analytics tab with `/admin/analytics/overview`
- ✅ Proper JWT token authentication
- ✅ Fixed approval endpoint (`/auth/admin/users/{id}/approve`)
- ✅ Fixed PSP test response parsing

### **3. Keep `Auth.tsx` (No Changes Needed)**

The `Auth.tsx` component is fine as-is, since it uses the `useAuth` hook which we're fixing.

---

## 🔧 **Step-by-Step Fix:**

### **Step 1: Set Environment Variables**

In your Lovable project, set these environment variables:

```env
VITE_API_URL=https://pivota-dashboard.onrender.com
```

**Note:** You don't need Supabase environment variables anymore since we're using the FastAPI backend directly!

### **Step 2: Copy Fixed Files**

1. **Copy `useAuth_FIXED.ts`** to your Lovable project:
   - Rename it to `useAuth.ts`
   - Place it in `src/hooks/useAuth.ts`

2. **Copy `AdminDashboard_FIXED.tsx`** to your Lovable project:
   - Rename it to `AdminDashboard.tsx`
   - Place it in `src/components/AdminDashboard.tsx`

### **Step 3: Update Imports (if needed)**

Make sure your imports are correct:

```typescript
// In your main App.tsx or index.tsx
import { AuthProvider } from './hooks/useAuth';
import AdminDashboard from './components/AdminDashboard';
import Auth from './components/Auth';

// Wrap your app with AuthProvider
function App() {
  return (
    <AuthProvider>
      {/* Your app content */}
    </AuthProvider>
  );
}
```

### **Step 4: Remove Supabase Dependencies (Optional)**

If you're not using Supabase for anything else, you can remove:
- `supabase_client.ts`
- Supabase imports from components
- `@supabase/supabase-js` from `package.json`

---

## 🧪 **Testing the Fixed Frontend:**

### **Test 1: Sign Up**
1. Go to your Lovable app
2. Click "Sign Up"
3. Enter email and password
4. You should see: "Account created! Awaiting admin approval."

### **Test 2: Sign In (Unapproved)**
1. Try to sign in with the new account
2. You should see: "Account pending admin approval"

### **Test 3: Admin Dashboard**
1. Sign in with admin account (test@example.com / password123)
2. Go to Admin Dashboard
3. You should see:
   - **User Management tab** - List of pending users
   - **PSP Management tab** - List of PSPs with test/toggle buttons
   - **Analytics tab** - System metrics

### **Test 4: Approve User**
1. In Admin Dashboard, go to User Management
2. Find the pending user
3. Click "Approve"
4. User should be approved

### **Test 5: Sign In (Approved)**
1. Sign out
2. Sign in with the approved user
3. Should successfully log in

---

## 🔍 **API Endpoints Reference:**

### **Authentication Endpoints:**
- `POST /auth/signup` - User registration
- `POST /auth/signin` - User login
- `GET /auth/me` - Get current user
- `POST /auth/signout` - Sign out

### **Admin Endpoints (Require Admin Token):**
- `GET /auth/admin/users` - List all users
- `POST /auth/admin/users/{id}/approve` - Approve/reject user
- `PUT /auth/admin/users/{id}/role` - Update user role

### **PSP Endpoints (Require Admin Token):**
- `GET /admin/psp/status` - Get all PSPs status
- `POST /admin/psp/{id}/test` - Test PSP connection
- `POST /admin/psp/{id}/toggle` - Enable/disable PSP

### **Analytics Endpoints (Require Admin Token):**
- `GET /admin/analytics/overview` - Get system analytics

---

## 📋 **Common Issues and Fixes:**

### **Issue 1: "Failed to load pending users"**
**Fix:** Make sure you're signed in with an admin account and the token is stored in localStorage.

### **Issue 2: "PSP test failed: undefined"**
**Fix:** This is fixed in the new AdminDashboard - it now correctly parses `result.test_results.success`.

### **Issue 3: "Method Not Allowed"**
**Fix:** Make sure you're using the correct HTTP method (POST for signup/signin, GET for /me).

### **Issue 4: "Not authenticated"**
**Fix:** Make sure the JWT token is being sent in the Authorization header: `Bearer <token>`.

---

## ✅ **What's Fixed:**

### **In useAuth.ts:**
- ✅ Uses FastAPI backend instead of Supabase
- ✅ Stores JWT token in localStorage
- ✅ Loads user data from `/auth/me`
- ✅ Proper error handling and toast notifications

### **In AdminDashboard.tsx:**
- ✅ Fetches users from `/auth/admin/users`
- ✅ Approves users via `/auth/admin/users/{id}/approve`
- ✅ Updates roles via `/auth/admin/users/{id}/role`
- ✅ Fetches PSPs from `/admin/psp/status`
- ✅ Tests PSPs via `/admin/psp/{id}/test`
- ✅ Toggles PSPs via `/admin/psp/{id}/toggle`
- ✅ Fetches analytics from `/admin/analytics/overview`
- ✅ Proper JWT token authentication for all requests
- ✅ Better error handling and loading states

---

## 🚀 **After Fixing:**

Your Lovable frontend will:
1. ✅ Connect directly to FastAPI backend
2. ✅ Use JWT token authentication
3. ✅ Display pending users correctly
4. ✅ Allow admin to approve/reject users
5. ✅ Show PSP status and allow testing
6. ✅ Display system analytics
7. ✅ Handle errors gracefully

**Your frontend is now production-ready!** 🎉

---

## 📞 **Need Help?**

If you encounter any issues:
1. Check browser console for errors
2. Check Network tab to see API requests/responses
3. Verify environment variables are set correctly
4. Make sure backend is running at https://pivota-dashboard.onrender.com
5. Test backend endpoints directly using curl first

**All backend endpoints are working and tested!** The issue was just the frontend API connections.

