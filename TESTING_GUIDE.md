# 🧪 Merchant Onboarding System - Testing Guide

## Prerequisites

### 1. Backend Running
- **Render**: https://pivota-dashboard.onrender.com (should be deployed)
- **Local** (optional): `uvicorn main:app --reload`

### 2. Frontend Running
- **Local**: http://localhost:3000 ✅ (Currently running)

### 3. Test Accounts
You'll need:
- **Admin account**: For approval
- **Employee/Agent account**: For onboarding merchants

---

## 🎯 Test Scenario: Complete Merchant Onboarding Flow

### Step 1: Login as Admin
1. Go to http://localhost:3000
2. Login with your admin account (`superadmin@pivota.com` or `admin@pivota.com`)
3. Navigate to **Merchants** tab

### Step 2: Onboard a Test Merchant
1. Click **"Onboard Merchant"** button
2. Fill in the form:

```
Business Information:
- Business Name: "LuxeStyle Fashion"
- Legal Name: "LuxeStyle Fashion LLC"
- Platform: Shopify
- Store URL: https://luxestyle.myshopify.com
- Business Type: E-commerce
- Country: United States

Contact Information:
- Contact Email: merchant@luxestyle.com
- Contact Phone: +1-555-0123

Business Metrics:
- Expected Monthly Volume: 50000

Description: "Premium fashion e-commerce store"
```

3. Click **"Submit Application"**
4. **Expected Result**: Alert with merchant ID
   ```
   ✅ Merchant "LuxeStyle Fashion" onboarded successfully!
   
   Merchant ID: 1
   
   Next steps:
   1. Upload KYB documents
   2. Wait for admin approval
   ```

### Step 3: Check Backend (Verify Merchant Created)
Open browser dev tools → Network tab → Check the API response:

**Request**:
```
POST https://pivota-dashboard.onrender.com/merchants/onboard
```

**Expected Response**:
```json
{
  "status": "success",
  "message": "Merchant application submitted successfully",
  "merchant_id": 1,
  "next_step": "Upload KYB documents"
}
```

### Step 4: Upload KYB Documents (API Test)
Since document upload UI isn't in the modal yet, test via API:

```bash
# Create a test PDF file
echo "Test Business License Document" > business_license.pdf

# Upload document (replace TOKEN and merchant_id)
curl -X POST https://pivota-dashboard.onrender.com/merchants/1/documents/upload \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F "document_type=business_license" \
  -F "file=@business_license.pdf"
```

**Expected Response**:
```json
{
  "status": "success",
  "message": "Document uploaded successfully",
  "document_id": 1,
  "file_name": "business_license.pdf"
}
```

### Step 5: View Merchant in Dashboard
1. Refresh the Merchants tab
2. You should see "LuxeStyle Fashion" listed with:
   - Status: `pending` or `approved`
   - Platform: Shopify
   - Store URL

### Step 6: Review KYB (Admin Action)
1. Find the merchant in the list
2. Click **"Review KYB"** button
3. **Expected**: Modal shows:
   - Business Name: LuxeStyle Fashion
   - Platform: Shopify
   - Verification Status
   - Documents list (if uploaded)

### Step 7: Approve Merchant
1. In the KYB Review modal, click **"Approve"**
2. **Expected**: Success message
   ```
   ✅ Merchant LuxeStyle Fashion approved successfully
   ```
3. Modal closes, merchant status changes to "approved"

### Step 8: Test Rejection (Optional)
1. Onboard another merchant
2. Click **"Reject"** in KYB modal
3. Enter rejection reason: "Incomplete documentation"
4. **Expected**: Merchant status changes to "rejected"

---

## 🧪 API Testing (Alternative Method)

If frontend has issues, test backend directly:

### 1. Get Auth Token
```bash
# Login
curl -X POST https://pivota-dashboard.onrender.com/auth/signin \
  -H "Content-Type: application/json" \
  -d '{"email": "superadmin@pivota.com", "password": "YOUR_PASSWORD"}'

# Save the token from response
TOKEN="eyJ..."
```

### 2. Test Merchant Onboarding
```bash
curl -X POST https://pivota-dashboard.onrender.com/merchants/onboard \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "business_name": "Test Store",
    "legal_name": "Test Store Inc",
    "platform": "shopify",
    "store_url": "https://test.myshopify.com",
    "contact_email": "test@store.com",
    "contact_phone": "+1234567890",
    "business_type": "ecommerce",
    "country": "US",
    "expected_monthly_volume": 10000,
    "description": "Test merchant"
  }'
```

### 3. List Pending Merchants
```bash
curl https://pivota-dashboard.onrender.com/merchants/?status=pending \
  -H "Authorization: Bearer $TOKEN"
```

### 4. Approve Merchant
```bash
curl -X POST https://pivota-dashboard.onrender.com/merchants/1/approve \
  -H "Authorization: Bearer $TOKEN"
```

### 5. Reject Merchant
```bash
curl -X POST https://pivota-dashboard.onrender.com/merchants/2/reject \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"status": "rejected", "rejection_reason": "Incomplete docs"}'
```

---

## ✅ Expected Test Results

### Frontend Checklist:
- [ ] Modal opens when clicking "Onboard Merchant"
- [ ] Form accepts all required fields
- [ ] Submit shows success alert with merchant ID
- [ ] Merchant appears in list after refresh
- [ ] KYB Review modal opens with merchant details
- [ ] Approve button works and updates status
- [ ] Reject button works with reason input

### Backend Checklist:
- [ ] `/merchants/onboard` creates merchant record
- [ ] Database tables `merchants` and `kyb_documents` exist
- [ ] Document upload saves file to `uploads/kyb/{merchant_id}/`
- [ ] `/merchants/?status=pending` returns pending merchants
- [ ] Approve endpoint updates status to "approved"
- [ ] Reject endpoint updates status to "rejected"

### Database Verification (if accessible):
```sql
-- Check merchant was created
SELECT * FROM merchants ORDER BY created_at DESC LIMIT 1;

-- Check documents
SELECT * FROM kyb_documents WHERE merchant_id = 1;
```

---

## 🐛 Troubleshooting

### Issue 1: Modal Doesn't Open
**Check**: Browser console for errors
**Fix**: Refresh page, check if Vite HMR caused issues

### Issue 2: API Returns 401 Unauthorized
**Check**: Token in localStorage
**Fix**: Login again to refresh token

### Issue 3: Merchant Not Appearing in List
**Check**: Network tab → API response
**Fix**: The merchants list might be from `/admin/merchants/kyb/status` (old endpoint) instead of `/merchants/` (new endpoint)

### Issue 4: File Upload Fails
**Check**: File size, backend logs
**Fix**: Ensure `uploads/kyb/` directory exists with write permissions

### Issue 5: Backend Not Deployed
**Check**: Render dashboard
**Fix**: Deploy latest code to Render

---

## 📊 What to Check

After testing, verify:

1. **Database**:
   - Merchants table has records
   - Status transitions work (pending → approved/rejected)
   - Documents are linked to merchants

2. **API**:
   - All endpoints respond correctly
   - Authentication works
   - Admin-only endpoints reject non-admins

3. **Frontend**:
   - Modals render properly
   - Forms submit successfully
   - Success/error messages display
   - Data refreshes after actions

---

## 🚀 Next Steps After Testing

Once testing confirms everything works:

1. **Deploy Backend** (if not already)
2. **Document any bugs found**
3. **Build Phase 2**: PSP Connection System
4. **Build Phase 3**: API Key Management
5. **Build Phase 4**: Webhook System

---

**Ready to test? Start at http://localhost:3000 and follow Step 1!** 🎯

