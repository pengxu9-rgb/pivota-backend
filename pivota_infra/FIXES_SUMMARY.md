# Employee Portal Fixes - October 20, 2025

## Issues Fixed

### 1. ✅ Sync Products Error (FIXED)
**Issue**: Column "store_name" does not exist
**Fix**: Changed SQL query in `routes/employee_store_psp_fixes.py` line 286 to use `name as store_name` instead of `store_name`
**Commit**: e85122bf

### 2. ✅ View Details Button (FIXED)
**Issue**: View Details modal not showing stores, PSPs, and stats
**Fix**: Enhanced `/merchant/onboarding/details/{merchant_id}` endpoint to fetch and return:
- Connected stores from `merchant_stores` table
- Connected PSPs from `merchant_psps` table  
- Transaction stats from `orders` table
**Commit**: 4090728b

### 3. 🔄 Review KYB Files (IN PROGRESS)
**Issue**: Review KYB does not show uploaded files
**Status**: The endpoint `/merchant/onboarding/details/{merchant_id}` already returns `kyc_documents` array
**Action Needed**: Verify frontend is correctly displaying the documents from `actionData.documents`

### 4. 🔄 Upload Docs Button (IN PROGRESS)
**Issue**: Upload Docs button not working
**Status**: Frontend has the modal and calls `employeeApi.uploadMerchantDocuments()`
**Action Needed**: Verify backend endpoint exists at `/merchant/onboarding/upload/{merchantId}`

### 5. ✅ Create New Merchant Button (EXISTS)
**Status**: Button and modal already exist in the code (line 440+ in merchants/page.tsx)
**Action Needed**: Test if backend endpoint `/merchant/onboarding/create` works correctly

### 6. 🔄 PSP Connections (Adyen, Wix, Stripe)
**Issue**: "Not Found" errors when connecting PSPs
**Status**: Backend endpoint exists at `/merchant/onboarding/setup-psp`
**Possible Causes**:
- Frontend might be calling wrong URL
- Merchant not found in database
- PSP table schema mismatch
**Action Needed**: Add more detailed error logging to identify exact failure point

### 7. 🔄 Agents Page
**Issue**: No data showing, cannot onboard agents
**Status**: Frontend calls `/agents` endpoint which exists in `routes/employee_agent_mgmt.py`
**Action Needed**: Verify agents table has data and endpoint returns correct format

### 8. 🔄 PSP Page
**Issue**: Buttons not working, wrong data sources
**Action Needed**: Audit PSP page frontend and backend endpoints

### 9. 🔄 Transactions Page
**Issue**: Empty data
**Action Needed**: Check if `orders` table has data and `/employee/transactions` endpoint works

## Next Steps
1. Wait for Railway deployment (2-3 minutes)
2. Test each fixed feature
3. Investigate remaining issues with detailed logging
4. Continue fixing based on test results






