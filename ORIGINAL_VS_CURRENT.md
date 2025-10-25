# Original Working System vs Current Broken State

## What Was Working Before (admin-dashboard folder)

### Original Admin Dashboard Features:
1. **MerchantTable Component** (`admin-dashboard/src/components/MerchantTable.tsx`)
   - ✅ Search and filter merchants
   - ✅ View Details modal
   - ✅ KYB Review modal with document display
   - ✅ Upload Documents modal
   - ✅ Delete merchant
   - ✅ Connect Shopify
   - ✅ Sync Products
   
2. **API Client** (`admin-dashboard/src/lib/api.ts`)
   - ✅ `merchantApi.getAll()` - Get all merchants
   - ✅ `merchantApi.getDetails(merchantId)` - Get merchant details
   - ✅ `merchantApi.delete(merchantId)` - Delete merchant
   - ✅ `merchantApi.updateKYB(merchantId, status, reason)` - Approve/reject KYB
   - ✅ `merchantApi.uploadDocuments(merchantId, files)` - Upload documents
   - ✅ `integrationsApi.connectShopify()` - Connect Shopify
   - ✅ `integrationsApi.syncShopifyProducts()` - Sync products

3. **Backend Endpoints** (pivota_infra/routes/)
   - ✅ GET `/merchant/onboarding/all` - List all merchants
   - ✅ GET `/merchant/onboarding/details/{merchantId}` - Get merchant details
   - ✅ DELETE `/merchant/onboarding/delete/{merchantId}` - Delete merchant
   - ✅ POST `/merchant/onboarding/kyb/{merchantId}` - Update KYB status
   - ✅ POST `/merchant/onboarding/upload/{merchantId}` - Upload KYB documents
   - ✅ POST `/integrations/shopify/connect` - Connect Shopify
   - ✅ POST `/integrations/shopify/products/sync` - Sync products

## What Got Broken (pivota-portals-v2/employee-portal)

### Issues in New Employee Portal:

1. **Directory Confusion**
   - Created TWO employee portal directories
   - `/pivota-employee-portal` (wrong one I was editing)
   - `/pivota-portals-v2/employee-portal` (actual deployed one)

2. **Missing Features**
   - No "Create New Merchant" button visible/working
   - View Details not showing stores/PSPs/stats
   - KYB Review not displaying uploaded documents properly
   - Upload Docs button not working
   - PSP connections returning "Not Found"
   - Agents page showing no data
   - PSP page not working
   - Transactions page empty

3. **Frontend-Backend Mismatch**
   - Frontend calling different endpoint URLs than backend provides
   - Column name mismatch (store_name vs name)
   - Response format differences

4. **Deployment Issues**
   - Vercel not picking up latest code
   - Created duplicate Vercel projects
   - Code committed but not deployed

## Root Causes

1. **Rebuilt from scratch** instead of copying working admin-dashboard code
2. **Wrong directory** - edited `/pivota-employee-portal` instead of `/pivota-portals-v2/employee-portal`
3. **Lost reference** to original working implementation
4. **Deployment confusion** - multiple portals, unclear which is deployed

## Solution Path

### Option 1: Copy Working Code (RECOMMENDED)
1. Copy all components from `admin-dashboard/src/components/` to `pivota-portals-v2/employee-portal/app/dashboard/merchants/`
2. Copy API client from `admin-dashboard/src/lib/api.ts` and merge with existing
3. Test each feature one by one
4. Deploy to Vercel

### Option 2: Fix Current Implementation
1. Fix each broken feature individually (slow, error-prone)
2. Continue debugging (what we've been doing, not working well)

### Option 3: Revert to Working State
1. Use admin-dashboard as the employee portal temporarily
2. Rebuild portals later when system is stable

## Recommendation

**Copy the working MerchantTable and API code** from admin-dashboard to employee-portal, since:
- It was already tested and working
- It has all the features you need
- It talks to the same backend
- Faster than debugging broken code

Would you like me to do this?





