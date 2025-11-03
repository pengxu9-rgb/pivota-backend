# 🔧 Fix: Database Persistence Issue

## Problem
Merchants disappear after each deployment because:
- Using SQLite: `sqlite:///./pivota.db`
- Render restarts container on deploy
- SQLite file is deleted → **All data lost**

## Solution: Use Supabase PostgreSQL

### Step 1: Get Supabase Database URL

1. Go to https://supabase.com/dashboard
2. Select your project
3. Go to **Settings** → **Database**
4. Find **Connection String** → **URI**
5. Copy the URL (looks like):
   ```
   postgresql://postgres:[PASSWORD]@db.xxxxx.supabase.co:5432/postgres
   ```

### Step 2: Add to Render Environment Variables

1. Go to https://dashboard.render.com
2. Click **pivota-dashboard**
3. Go to **Environment** tab
4. Add new variable:
   - **Key**: `DATABASE_URL`
   - **Value**: `postgresql://postgres:[PASSWORD]@db.xxxxx.supabase.co:5432/postgres`
5. Click **Save Changes**

### Step 3: Redeploy

Render will automatically redeploy. Wait ~5 minutes.

### Step 4: Test

1. Onboard a merchant
2. Upload documents
3. **Redeploy again** (to test persistence)
4. **Merchant should still be there!** ✅

---

## Alternative: Render PostgreSQL (Paid)

If you don't want to use Supabase:

1. In Render dashboard, create **New** → **PostgreSQL**
2. Copy the **Internal Database URL**
3. Add to environment variables
4. Redeploy

---

## Verification

After setting up PostgreSQL, check:

```bash
# Check what database is being used
curl https://pivota-dashboard.onrender.com/health

# Should show PostgreSQL connection instead of SQLite
```

---

## Why This Happens

**SQLite** (current):
- ✅ Simple, no setup
- ❌ File-based → deleted on container restart
- ❌ **NOT suitable for production**

**PostgreSQL** (recommended):
- ✅ Persistent across deployments
- ✅ Production-ready
- ✅ **Data survives restarts**

---

**Once DATABASE_URL is set to PostgreSQL, merchants will persist!** 🎯

