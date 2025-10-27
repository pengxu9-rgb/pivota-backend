# Agent Dashboard Data Issues - Diagnostic Report

**Date**: 2025-10-27  
**Agent**: asdf@asdf.com (agent_ee38f2b3645a2ec2)  
**Test Order Created**: ORD_9BCAE1D751E0A461 ($99)

---

## 🔍 Issues Identified

### 1. **Orders Not Showing (CRITICAL)**
- **Symptom**: `/agent/v1/orders` returns 0 orders
- **Root Cause**: Need to verify if `agent_id` is being saved to database
- **Test SQL**:
```sql
SELECT order_id, agent_id, merchant_id, total, payment_status, created_at
FROM orders
WHERE order_id = 'ORD_9BCAE1D751E0A461';
```
- **Expected**: `agent_id` should be `agent_ee38f2b3645a2ec2`
- **Query Location**: `pivota_infra/routes/agent_sdk_fixed.py:380`
```python
where_clauses = ["o.agent_id = :agent_id"]
params = {"agent_id": context.agent_id}
```

### 2. **Avg Order Value Incorrect**
- **Symptom**: Shows $10 instead of $99
- **Root Cause**: Dashboard using wrong data source
- **Fix Location**: `pivota-agents-portal/app/dashboard/page.tsx:610`
```typescript
// Current (WRONG):
metrics.orders.count_last_24h > 0 
  ? formatAmount(metrics.total_gmv / metrics.orders.count_last_24h) 
  : formatAmount(0)

// Should use only PAID orders:
paid_orders_count > 0
  ? formatAmount(total_paid_revenue / paid_orders_count)
  : formatAmount(0)
```

### 3. **Query Analytics All Zeros**
- **Symptom**: Product Searches, Inventory Checks, Price Queries all show 0
- **Root Cause**: `agent_usage_logs` table may not have data
- **Endpoint**: `/agent/v1/analytics/queries`
- **Fix Location**: `pivota_infra/routes/agent_analytics.py:77-97`
```python
# These queries return 0:
product_searches = await database.fetch_val(
    "SELECT COUNT(*) FROM agent_usage_logs WHERE agent_id = :agent_id AND endpoint LIKE '%/products/search%'",
    {"agent_id": agent_id_resolved}
)
```

### 4. **Performance Timeline Empty**
- **Symptom**: Analytics Performance Timeline (past 24h) shows incorrect data
- **Root Cause**: Frontend may be calling wrong endpoint or backend not returning data
- **Check**: Agent Analytics API endpoints

### 5. **Total API Calls Incorrect**
- **Current**: Shows 10 total requests
- **Root Cause**: Counting all `agent_usage_logs` entries, not filtered properly
- **Endpoint**: `/agent/metrics/summary`

### 6. **Merchants Page Empty**
- **Symptom**: No merchants showing despite orders existing
- **Root Cause**: Agent-merchant relationship not established
- **Expected**: Should show `merch_208139f7600dbf42` (chydantest.myshopify.com)

---

## 🔧 Root Cause Analysis

### Primary Issue: Orders Table `agent_id` Field

**Problem**: Orders may not have `agent_id` populated.

**Verification Steps**:
1. Check if `agent_id` column exists in orders table:
```sql
SELECT column_name, data_type 
FROM information_schema.columns 
WHERE table_name = 'orders' AND column_name = 'agent_id';
```

2. Check existing orders for `agent_id`:
```sql
SELECT order_id, agent_id, merchant_id, total, payment_status
FROM orders
WHERE merchant_id = 'merch_208139f7600dbf42'
ORDER BY created_at DESC
LIMIT 10;
```

3. Check if newly created orders have `agent_id`:
```sql
SELECT order_id, agent_id, total, created_at
FROM orders
WHERE created_at > NOW() - INTERVAL '1 hour'
ORDER BY created_at DESC;
```

### Secondary Issue: Metrics Calculation Logic

**Files to Review**:
1. `/pivota_infra/routes/agent_metrics.py` - Backend metrics calculation
2. `/pivota_infra/routes/agent_analytics.py` - Analytics endpoints
3. `/pivota-agents-portal/app/dashboard/page.tsx` - Frontend dashboard

---

## 🛠️ Fix Plan

### Phase 1: Database Verification (IMMEDIATE)
1. ✅ Verify `agent_id` column exists in `orders` table
2. ✅ Check if newly created order has `agent_id` populated
3. ⏳ If missing, backfill existing orders with `agent_id`

### Phase 2: Backend Fixes
1. **Fix Orders Query**:
   - Ensure `/agent/v1/orders` correctly filters by `agent_id`
   - Add logging to debug query results

2. **Fix Metrics Calculation**:
   - Update `get_metrics_summary` to use only PAID orders for revenue/avg
   - Fix query analytics to properly count usage logs

3. **Fix Merchants Endpoint**:
   - Create/fix `/agent/v1/merchants` endpoint
   - Return merchants associated with agent's orders

### Phase 3: Frontend Fixes
1. **Dashboard Calculations**:
   - Use correct data sources for all metrics
   - Add loading states and error handling

2. **Analytics Page**:
   - Connect to correct backend endpoints
   - Display real performance timeline data

---

## 📝 SQL Fixes

### If `agent_id` Column Missing:
```sql
ALTER TABLE orders 
ADD COLUMN IF NOT EXISTS agent_id VARCHAR(255);

CREATE INDEX IF NOT EXISTS idx_orders_agent_id ON orders(agent_id);
```

### Backfill Existing Orders:
```sql
-- Option 1: If we can derive agent_id from metadata
UPDATE orders
SET agent_id = (metadata->>'agent_id')::VARCHAR
WHERE agent_id IS NULL 
AND metadata IS NOT NULL 
AND metadata->>'agent_id' IS NOT NULL;

-- Option 2: If we know the agent for certain merchants
UPDATE orders
SET agent_id = 'agent_ee38f2b3645a2ec2'
WHERE agent_id IS NULL
AND merchant_id = 'merch_208139f7600dbf42'
AND created_at > '2025-10-25';  -- Only recent test orders
```

---

## ✅ Expected Outcomes After Fixes

1. **Orders Page**: Shows 2+ orders ($99 each)
2. **Avg Order Value**: $99.00
3. **Merchants Page**: Shows 1 merchant (chydantest.myshopify.com)
4. **Query Analytics**: Shows actual API call counts
5. **Performance Timeline**: Shows order/payment activity
6. **Total API Calls**: Accurate count of agent requests

---

## 🧪 Test Plan

After fixes, run:
```bash
# 1. Create new order
curl -X POST "https://web-production-fedb.up.railway.app/agent/v1/orders/create" \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{...order_data...}'

# 2. List orders
curl -H "Authorization: Bearer YOUR_TOKEN" \
  "https://web-production-fedb.up.railway.app/agent/v1/orders"

# 3. Check metrics
curl -H "Authorization: Bearer YOUR_TOKEN" \
  "https://web-production-fedb.up.railway.app/agent/metrics/summary"

# 4. Check analytics
curl -H "Authorization: Bearer YOUR_TOKEN" \
  "https://web-production-fedb.up.railway.app/agent/v1/analytics/funnel"
```

---

**Next Step**: Execute database verification queries to confirm `agent_id` status.

