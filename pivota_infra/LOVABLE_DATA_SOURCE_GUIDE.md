# 📊 Lovable Dashboard Data Source Guide

## 🔗 API Endpoints for Lovable

**Base URL:** `http://localhost:8000`

### 1. **Snapshot API** - Main dashboard data
```http
GET /api/snapshot
GET /api/snapshot?role=admin
GET /api/snapshot?role=agent&id=AGENT_001
GET /api/snapshot?role=merchant&id=MERCH_001
```

**Response Structure:**
```json
{
  "summary": {
    "total": 150,
    "success": 120,
    "fail": 25,
    "retries": 5
  },
  "psp": {
    "stripe": {
      "success_count": 80,
      "fail_count": 10,
      "retry_count": 2,
      "avg_latency": 210.5,
      "total": 92
    },
    "adyen": {
      "success_count": 30,
      "fail_count": 8,
      "retry_count": 2,
      "avg_latency": 280.3,
      "total": 40
    }
  },
  "agent": {
    "AGENT_001": {
      "success_count": 45,
      "fail_count": 8,
      "retry_count": 2,
      "avg_latency": 220.1,
      "total": 55,
      "agent_name": "TravelBot A"
    }
  },
  "merchant": {
    "MERCH_001": {
      "success_count": 50,
      "fail_count": 10,
      "retry_count": 2,
      "avg_latency": 235.2,
      "total": 62,
      "merchant_name": "Shopify Fashion Store"
    }
  },
  "psp_usage": {
    "stripe": 92,
    "adyen": 40,
    "paypal": 18
  },
  "timestamp": 1700000000,
  "window_size_seconds": 3600,
  "total_events": 150
}
```

### 2. **Recent Events API** - Live event feed
```http
GET /api/recent-events
GET /api/recent-events?limit=100
```

**Response Structure:**
```json
{
  "events": [
    {
      "type": "payment_result",
      "order_id": "ORD_001",
      "agent": "AGENT_001",
      "agent_name": "TravelBot A",
      "merchant": "MERCH_001",
      "merchant_name": "Shopify Store A",
      "psp": "stripe",
      "status": "succeeded",
      "latency_ms": 250,
      "attempt": 1,
      "amount": 49.99,
      "currency": "EUR",
      "timestamp": 1700000000
    }
  ],
  "count": 1,
  "timestamp": 1700000000
}
```

### 3. **Connection Stats API** - WebSocket info
```http
GET /api/connection-stats
```

**Response Structure:**
```json
{
  "total_connections": 5,
  "connections_by_role": {
    "admin": 2,
    "agent": 2,
    "merchant": 1
  },
  "timestamp": 1700000000
}
```

## 🌐 WebSocket for Real-time Updates

**WebSocket URL:** `ws://localhost:8000/ws/metrics`

**Optional Authentication:** `ws://localhost:8000/ws/metrics?token=your_jwt_token`

### WebSocket Message Types:

#### 1. **Initial Snapshot** (on connect)
```json
{
  "type": "snapshot",
  "data": { /* full snapshot object */ },
  "timestamp": 1700000000
}
```

#### 2. **Live Events** (real-time updates)
```json
{
  "type": "event",
  "event": {
    "type": "payment_result",
    "order_id": "ORD_001",
    "agent": "AGENT_001",
    "agent_name": "TravelBot A",
    "merchant": "MERCH_001",
    "merchant_name": "Shopify Store A",
    "psp": "stripe",
    "status": "succeeded",
    "latency_ms": 250,
    "attempt": 1,
    "amount": 49.99,
    "currency": "EUR",
    "timestamp": 1700000000
  },
  "snapshot": { /* updated snapshot */ },
  "timestamp": 1700000000
}
```

#### 3. **Client Messages** (send to server)
```json
// Request fresh snapshot
{
  "type": "snapshot_request"
}

// Ping for connection health
{
  "type": "ping"
}
```

## 🧪 Test Data Generation

### Generate Sample Data
```http
POST /api/test/generate-sample-data
```
Generates 50 realistic sample events for testing.

### Generate Live Events
```http
POST /api/test/generate-live-events?count=10
```
Generates live events for real-time testing.

### Get Sample Snapshot
```http
GET /api/test/sample-snapshot
```
Returns a realistic sample snapshot with proper data structure.

## 📊 Dashboard Components Data Mapping

### **Success Rate Chart**
- **Data Source:** `snapshot.summary.success / snapshot.summary.total`
- **Update:** Real-time via WebSocket events

### **PSP Performance Chart**
- **Data Source:** `snapshot.psp[psp_name]`
- **Metrics:** success_count, fail_count, avg_latency, total

### **Agent Performance Table**
- **Data Source:** `snapshot.agent`
- **Columns:** agent_name, success_count, fail_count, avg_latency, total

### **Merchant Performance Table**
- **Data Source:** `snapshot.merchant`
- **Columns:** merchant_name, success_count, fail_count, avg_latency, total

### **Live Event Feed**
- **Data Source:** WebSocket events with `type: "event"`
- **Display:** order_id, agent_name, merchant_name, psp, status, latency_ms, timestamp

### **PSP Usage Pie Chart**
- **Data Source:** `snapshot.psp_usage`
- **Key-Value:** psp_name -> usage_count

## 🎨 Lovable Integration Tips

1. **Initial Load:** Call `/api/snapshot` on component mount
2. **Real-time Updates:** Connect to WebSocket and listen for `type: "event"`
3. **Refresh:** Send `{"type": "snapshot_request"}` via WebSocket
4. **Error Handling:** Check `timestamp` field for data freshness
5. **Role-based Views:** Use role and id parameters for filtered data

## 🔄 Data Refresh Strategy

1. **Initial Load:** REST API call to `/api/snapshot`
2. **Real-time Updates:** WebSocket events
3. **Manual Refresh:** WebSocket message `{"type": "snapshot_request"}`
4. **Fallback:** Periodic REST API calls every 30 seconds

## 📱 Responsive Data Structure

All endpoints return consistent data structures that work well for:
- **Desktop Dashboards:** Full data with all metrics
- **Mobile Views:** Filtered data by role/entity
- **Real-time Feeds:** Live event streams
- **Historical Analysis:** Timestamp-based data

## 🚀 Ready for Production

The API is fully functional with:
- ✅ CORS enabled for Lovable
- ✅ Real-time WebSocket updates
- ✅ Role-based data filtering
- ✅ Comprehensive error handling
- ✅ Sample data generation
- ✅ Production-ready schemas
