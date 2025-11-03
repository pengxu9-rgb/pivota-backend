# Fix Database Persistence on Railway

## The Problem
Railway is using SQLite which gets wiped on each deployment. This is why orders and merchants disappear!

## Solution: Add PostgreSQL to Railway

### Option 1: Railway PostgreSQL (Recommended - FREE)

1. **Go to your Railway project**

2. **Add PostgreSQL Service:**
   - Click "New" → "Database" → "Add PostgreSQL"
   - Railway will automatically create a database

3. **Connect to your app:**
   - Railway will automatically add `DATABASE_URL` to your app
   - It will look like: `postgresql://postgres:xxxxx@xxxx.railway.internal:5432/railway`

4. **Redeploy your app:**
   - Railway will automatically redeploy
   - The app will detect PostgreSQL and use it instead of SQLite

### Option 2: External Database (Supabase/Neon - FREE tier)

1. **Create free database at:**
   - Supabase: https://supabase.com (Free tier)
   - Neon: https://neon.tech (Free tier)
   - Aiven: https://aiven.io (Free trial)

2. **Get connection string:**
   ```
   postgresql://user:password@host:5432/database
   ```

3. **Add to Railway:**
   - Go to your service variables
   - Add: `DATABASE_URL=postgresql://...`

4. **Redeploy**

## After Adding Database:

1. **Tables will be created automatically** on first run

2. **Re-register your merchant:**
   - Go to: https://your-frontend/
   - Register merchant again
   - Add PSP credentials
   - Connect Shopify

3. **Test order creation:**
   ```bash
   python3 test_shopify_integration.py
   ```

## Why This Fixes Everything:

- ✅ Orders will persist
- ✅ Merchants stay registered
- ✅ Payment intents work
- ✅ Shopify orders can be created
- ✅ Data survives deployments

## Quick Check:

After adding PostgreSQL, check if it's working:

```bash
curl https://web-production-fedb.up.railway.app/health
```

The response should show the app is healthy and connected to PostgreSQL.
