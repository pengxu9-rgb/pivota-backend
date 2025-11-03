# 🎯 Ready to Test - Merchant Onboarding System

## ✅ What's Built (Locally)

### Backend Files Created:
- ✅ `pivota_infra/db/merchants.py` - Database tables & operations
- ✅ `pivota_infra/routes/merchant_routes.py` - API endpoints
- ✅ `pivota_infra/main.py` - Updated with merchant router

### Frontend Files Updated:
- ✅ `simple_frontend/src/services/api.ts` - Added `merchantApi`
- ✅ `simple_frontend/src/pages/AdminDashboard.tsx` - Integrated real API calls
- ✅ `simple_frontend/src/index.css` - Fixed modal backdrop styling

### Modals Created:
- ✅ `MerchantOnboardingModal.tsx` - Onboarding form
- ✅ `KYBReviewModal.tsx` - KYB review interface
- ✅ `MerchantDetailsModal.tsx` - Merchant details viewer

---

## 🚀 How to Test (2 Options)

### Option A: Test Locally (Backend + Frontend)

#### 1. Start Backend Locally:
```bash
cd pivota_infra
uvicorn main:app --reload --port 8000
```

#### 2. Update Frontend API URL:
Edit `simple_frontend/src/services/api.ts`:
```typescript
const API_BASE_URL = 'http://localhost:8000';  // Change from Render URL
```

#### 3. Test the Flow:
1. Open http://localhost:3000
2. Login as admin
3. Click "Onboard Merchant"
4. Fill form → Submit
5. Check browser console for API response
6. See if merchant appears in list

---

### Option B: Deploy to Render (Recommended)

#### 1. Commit Changes:
```bash
git add .
git commit -m "Add merchant onboarding system with KYB flow"
git push origin main
```

#### 2. Wait for Render Deploy:
- Go to https://dashboard.render.com
- Check deployment logs
- Wait for "Live" status

#### 3. Test on Render:
1. Open http://localhost:3000
2. Frontend will call https://pivota-dashboard.onrender.com
3. Test the complete flow

---

## 🧪 Quick Test Script

### Test via API (No Frontend Needed):

```bash
# 1. Login and get token
TOKEN=$(curl -s -X POST https://pivota-dashboard.onrender.com/auth/signin \
  -H "Content-Type: application/json" \
  -d '{"email": "superadmin@pivota.com", "password": "YOUR_PASSWORD"}' \
  | jq -r '.access_token')

# 2. Onboard merchant
curl -X POST https://pivota-dashboard.onrender.com/merchants/onboard \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "business_name": "Quick Test Store",
    "legal_name": "Quick Test LLC",
    "platform": "shopify",
    "store_url": "https://quicktest.com",
    "contact_email": "test@quicktest.com",
    "contact_phone": "+1234567890",
    "business_type": "ecommerce",
    "country": "US",
    "expected_monthly_volume": 5000,
    "description": "Test store"
  }'

# 3. List merchants
curl https://pivota-dashboard.onrender.com/merchants/ \
  -H "Authorization: Bearer $TOKEN"
```

---

## ⚠️ Current Status

### Backend:
- ✅ Code written locally
- ❌ **NOT deployed to Render yet**
- ❌ Database tables not created yet (need deployment)

### Frontend:
- ✅ Running on http://localhost:3000
- ✅ All modals working
- ⚠️ Will call Render backend (which doesn't have merchant endpoints yet)

### What This Means:
**You can test the UI locally, but API calls will fail until backend is deployed.**

---

## 📋 Testing Checklist

### Frontend UI Test (Works Now):
- [ ] Open http://localhost:3000
- [ ] Login as admin
- [ ] Go to Merchants tab
- [ ] Click "Onboard Merchant" → Modal opens ✅
- [ ] Fill form → Form accepts input ✅
- [ ] Click submit → **API call will fail** ❌ (backend not deployed)

### Full Integration Test (After Deploy):
- [ ] Commit & push code
- [ ] Wait for Render deploy
- [ ] Open http://localhost:3000
- [ ] Complete merchant onboarding
- [ ] Upload documents (via API)
- [ ] Review & approve merchant
- [ ] Verify merchant status changes

---

## 🔄 Recommended Next Steps

### Step 1: Deploy Backend ⭐
```bash
git add pivota_infra/db/merchants.py
git add pivota_infra/routes/merchant_routes.py
git add pivota_infra/main.py
git commit -m "Add merchant onboarding backend"
git push origin main
```

### Step 2: Test Frontend
- Wait for deploy to complete
- Test at http://localhost:3000
- Merchant onboarding should work end-to-end

### Step 3: Document Issues
- Note any bugs or missing features
- Decide which missing components to build next:
  - PSP Connection
  - API Key Management
  - Webhook System

---

## 🎯 What You Can Test RIGHT NOW

Even without deployment, you can test:

1. **Frontend Modal UI**:
   - Click "Onboard Merchant" button
   - Fill the form
   - See if it looks good
   - Check if backdrop is visible

2. **Frontend Code Review**:
   - Check console for any errors
   - Verify modal closes properly
   - Test form validation

3. **Backend Code Locally**:
   - Run `cd pivota_infra && uvicorn main:app --reload`
   - Test endpoints with curl
   - Check database tables get created

---

**What would you like to do?**

A) **Test frontend UI now** (modals, forms) → Open http://localhost:3000
B) **Deploy backend first** → Commit & push to Render
C) **Run backend locally** → Test complete flow on localhost
D) **Skip testing** → Build remaining features (PSP, API keys)

Let me know and I'll guide you! 🚀
