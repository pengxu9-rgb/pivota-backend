# 🚀 Load Simulator Guide for Lovable Dashboard

## 📊 Quick Start

Your load simulator is ready! Use these commands to generate realistic traffic for your Lovable dashboard.

## 🎯 Available Commands

### 1. **Continuous Load Simulation** (Recommended)
Generate ongoing realistic traffic:
```bash
cd pivota_infra
python3 simulate_load.py --mode continuous --rate 3
```
- **Rate 3**: 3 events per second (moderate load)
- **Rate 5**: 5 events per second (high load)
- **Rate 1**: 1 event per second (light load)

### 2. **Burst Simulation**
Generate a burst of events quickly:
```bash
python3 simulate_load.py --mode burst --burst-events 50 --burst-duration 10
```
- Generates 50 events over 10 seconds
- Great for testing dashboard responsiveness

### 3. **Realistic Traffic Patterns**
Simulate realistic business patterns:
```bash
python3 simulate_load.py --mode pattern
```
- Morning rush (high traffic)
- Quiet periods (low traffic)
- Evening peak (very high traffic)
- Night quiet (minimal traffic)

## 📊 What Gets Generated

### **Agents (5 types):**
- AGENT_001: TravelBot A
- AGENT_002: ShoppingBot B
- AGENT_003: FashionBot C
- AGENT_004: ElectronicsBot D
- AGENT_005: HomeBot E

### **Merchants (5 types):**
- MERCH_001: Shopify Fashion Store
- MERCH_002: Wix Electronics Store
- MERCH_003: Shopify Travel Store
- MERCH_004: Wix Home Decor Store
- MERCH_005: Shopify Beauty Store

### **PSPs (4 types):**
- stripe (fastest: 120-350ms)
- adyen (medium: 200-500ms)
- paypal (slower: 300-600ms)
- square (medium: 150-400ms)

### **Realistic Distribution:**
- 85% successful payments
- 10% failed payments
- 5% queued for retry
- Random amounts: $20-$300
- Multiple currencies: EUR, USD, GBP, CAD

## 🎨 Lovable Dashboard Testing

### **Start Load Simulation:**
```bash
# Terminal 1: Keep your FastAPI server running
# Terminal 2: Run load simulator
cd pivota_infra
python3 simulate_load.py --mode continuous --rate 3
```

### **Test Your Lovable Dashboard:**
1. **Connect to:** `http://localhost:8000/api/snapshot`
2. **WebSocket:** `ws://localhost:8000/ws/metrics`
3. **Watch real-time updates** as events flow in
4. **Test different views:** admin, agent, merchant roles

### **Dashboard Components to Test:**
- ✅ **Success Rate Chart** - Should show ~85% success
- ✅ **PSP Performance** - Different latencies per PSP
- ✅ **Agent Performance Table** - Multiple agents with varying stats
- ✅ **Merchant Performance** - Different merchant volumes
- ✅ **Live Event Feed** - Real-time event streaming
- ✅ **PSP Usage Pie Chart** - Distributed usage across PSPs

## 🔧 Troubleshooting

### **If events aren't showing:**
1. Make sure FastAPI server is running: `http://localhost:8000`
2. Check server logs for errors
3. Verify WebSocket connection in browser dev tools

### **If simulation stops:**
1. Check for import errors
2. Make sure you're in the `pivota_infra` directory
3. Verify all dependencies are installed

### **For high load testing:**
```bash
# Generate heavy load (10 events/second)
python3 simulate_load.py --mode continuous --rate 10

# Or simulate traffic spikes
python3 simulate_load.py --mode burst --burst-events 200 --burst-duration 20
```

## 📈 Expected Results

### **After 1 minute of simulation (rate=3):**
- ~180 total events
- ~153 successful payments
- ~18 failed payments
- ~9 retry attempts
- All 4 PSPs with different performance
- All 5 agents with varying activity
- All 5 merchants with different volumes

### **Real-time Updates:**
- WebSocket events every ~333ms (rate=3)
- Dashboard should update automatically
- Live event feed should show recent orders
- Metrics should update in real-time

## 🎯 Perfect for Lovable Testing!

Your load simulator generates:
- ✅ **Realistic payment events**
- ✅ **Multiple PSP performance patterns**
- ✅ **Varied agent/merchant activity**
- ✅ **Real-time WebSocket streaming**
- ✅ **Comprehensive metrics data**

**Ready to build your beautiful Lovable dashboard!** 🚀
