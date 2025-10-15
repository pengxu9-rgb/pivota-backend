# 🚨 FIX LOVABLE FRONTEND NOW - Step by Step

## ❌ **Problem:**
- PSP fetch failing (endpoint doesn't exist in Supabase)
- Users not showing (RLS blocking)
- Everything broken because frontend is using wrong architecture

## ✅ **Solution:**
Replace 2 files in your Lovable project. That's it!

---

## 📋 **STEP-BY-STEP FIX:**

### **Step 1: Open This File**
Open: `lovable_components/useAuth_FIXED.ts`

### **Step 2: Copy ALL Content**
Select all (Ctrl+A / Cmd+A) and copy (Ctrl+C / Cmd+C)

### **Step 3: Paste in Lovable**
In your Lovable project:
1. Find or create: `src/hooks/useAuth.ts`
2. Delete all existing content
3. Paste the copied content
4. Save

---

### **Step 4: Open This File**
Open: `lovable_components/AdminDashboard_FIXED.tsx`

### **Step 5: Copy ALL Content**
Select all (Ctrl+A / Cmd+A) and copy (Ctrl+C / Cmd+C)

### **Step 6: Paste in Lovable**
In your Lovable project:
1. Find or create: `src/components/AdminDashboard.tsx`
2. Delete all existing content
3. Paste the copied content
4. Save

---

### **Step 7: Set Environment Variable**
In Lovable project settings:
1. Go to Settings → Environment Variables
2. Add new variable:
   - Name: `VITE_API_URL`
   - Value: `https://pivota-dashboard.onrender.com`
3. Save

---

### **Step 8: Restart Lovable Dev Server**
Click the restart/refresh button in Lovable

---

### **Step 9: Test**
1. Sign in with: test@example.com / password123
2. Go to Admin Dashboard
3. You should see:
   - ✅ Pending users (if any exist with approved=false)
   - ✅ PSP list with Test/Enable buttons
   - ✅ Analytics tab with metrics

---

## 🎯 **What Changed:**

### **Before (Broken):**
```typescript
// useAuth.ts - Using Supabase directly
const { data, error } = await supabase.auth.signUp({ email, password });

// AdminDashboard.tsx - Wrong endpoint
const response = await fetch(`${API_URL}/psp-fix/status`);

// AdminDashboard.tsx - Supabase query (blocked by RLS)
const { data, error } = await supabase
  .from('user_roles')
  .select('*')
  .eq('approved', false);
```

### **After (Fixed):**
```typescript
// useAuth.ts - Using FastAPI backend
const response = await fetch(`${API_URL}/auth/signin`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ email, password }),
});

// AdminDashboard.tsx - Correct endpoint
const response = await fetch(`${API_URL}/admin/psp/status`, {
  headers: { 'Authorization': `Bearer ${token}` },
});

// AdminDashboard.tsx - FastAPI endpoint (no RLS issues)
const response = await fetch(`${API_URL}/auth/admin/users`, {
  headers: { 'Authorization': `Bearer ${token}` },
});
```

---

## ✅ **After Fix, You'll Have:**

1. ✅ **Working User Management**
   - See pending users
   - Approve/reject users
   - Change user roles

2. ✅ **Working PSP Management**
   - See all PSPs
   - Test PSP connections
   - Enable/disable PSPs

3. ✅ **Working Analytics**
   - Total transactions
   - Total volume
   - Success rate
   - Active PSPs

4. ✅ **Working Authentication**
   - JWT token-based
   - Proper error handling
   - Role-based access

---

## 🔍 **Why This Fixes Everything:**

### **PSP Data Location:**
- ❌ NOT in Supabase
- ✅ IN FastAPI backend at `/admin/psp/status`

### **User Management:**
- ❌ Supabase direct queries blocked by RLS
- ✅ FastAPI backend has proper permissions

### **Authentication:**
- ❌ Supabase auth limited features
- ✅ FastAPI JWT tokens with custom logic

---

## 📞 **If It Still Doesn't Work:**

1. **Check browser console** (F12) for errors
2. **Verify environment variable** is set correctly
3. **Make sure you're signed in** with admin account
4. **Check that backend is running**: https://pivota-dashboard.onrender.com/

---

## 🚀 **This Will Fix Everything!**

Just replace those 2 files and set the environment variable.

**Your frontend will work perfectly!**

