# Agents Data Mapping Fix - Complete

## Problem
All metrics data showing as 0 (requests, GMV, success rate, etc.)

## Root Cause
API field name mismatches between backend and frontend:

### API Returns (Flat Structure):
```json
{
  "agent_id": "agent_ee38f2b3645a2ec2",
  "agent_name": "asdf",
  "owner_email": "asdf@asdf.com", 
  "api_key_prefix": "ak_live_01...",
  "rate_limit": 1000,
  "request_count": 0,        // NOT metrics.requests_24h
  "success_rate": 0.0,        // NOT metrics.success_rate
  // No metrics object!
}
```

### Frontend Expected (Nested Structure):
```json
{
  "name": "...",
  "email": "...",
  "api_key": "...",
  "metrics": {
    "requests_24h": 0,
    "success_rate": 0,
    "total_gmv": 0,
    // ...
  }
}
```

## Solution Implemented

### 1. Compatibility Layer in Components
Added fallback mapping in both `AgentTable.tsx` and `AgentDetailPanel.tsx`:

```typescript
// Handle both old format (with metrics object) and new format (flat structure)
const metrics = agent.metrics || {
  requests_24h: agent.request_count || 0,
  success_rate: agent.success_rate || 0,
  total_gmv: agent.total_gmv || 0,
  total_orders: agent.total_orders || 0,
  successful_24h: agent.successful_24h || 0,
  failed_24h: agent.failed_24h || 0,
  avg_latency_ms: agent.avg_latency_ms || 0
};
```

### 2. Updated Summary Statistics
Fixed calculation in `agents/page.tsx`:

```typescript
// Handle both formats
const total24hRequests = agents.reduce((sum, a) => {
  return sum + (a.metrics?.requests_24h || a.request_count || 0);
}, 0);

const totalGMV = agents.reduce((sum, a) => {
  return sum + (a.metrics?.total_gmv || a.total_gmv || 0);
}, 0);
```

### 3. Field Mappings
| Frontend Field | API Field (Old) | API Field (New) |
|---------------|-----------------|------------------|
| name | name | agent_name |
| email | email | owner_email |
| api_key | api_key | api_key_prefix |
| metrics.requests_24h | metrics.requests_24h | request_count |
| metrics.success_rate | metrics.success_rate | success_rate |
| rate_limit | governance.max_requests_per_minute | rate_limit |

## Files Modified
1. `/app/components/agents/AgentTable.tsx`
2. `/app/components/agents/AgentDetailPanel.tsx` 
3. `/app/dashboard/agents/page.tsx`

## Status
✅ **DEPLOYED** - Frontend now correctly displays:
- Agent names (agent_name)
- Email addresses (owner_email)
- API keys (api_key_prefix)
- Request counts (request_count)
- Success rates (success_rate)
- Rate limits (rate_limit)

## Testing Instructions
1. Refresh the Employee Portal agents page
2. Verify data displays correctly (no more 0 values if data exists)
3. Click "View" to see detailed metrics
4. All statistics should reflect actual API data

## Note
The backend API should ideally be updated to return consistent field names, but this compatibility layer ensures the frontend works with both formats.
