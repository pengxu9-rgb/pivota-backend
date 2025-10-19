# Pivota Portals - Deployment Status & Features

**Last Updated:** 2025-10-19  
**Status:** ✅ All 3 portals deployed to Vercel

---

## 🏪 Merchant Portal (merchant.pivota.cc)

### ✅ Deployed Features
- **Login System**: Fixed to use `token` field from backend
- **Real Data Loading**: 
  - Loads merchant-specific data using `merchant_id`
  - Connects to real Shopify stores (e.g., chydantest.myshopify.com)
  - Real PSP connections (Stripe, Adyen, etc.)
  - Real orders and products from backend API

### 📊 Dashboard Tabs

#### 1. Overview
- Stats cards: Total Orders, Revenue, Customers, Products
- Recent orders table
- Growth metrics with real data

#### 2. Orders
- Full order list with filtering
- Export to CSV
- Order detail view (`/orders/[orderId]`)
- Refund capability

#### 3. Products
- Product grid display
- Sync from Shopify
- Add new products
- Update stock levels

#### 4. Integration ⭐
**Store Connections:**
- Connect multiple Shopify stores (OAuth or manual)
- Connect multiple Wix stores
- WooCommerce (coming soon)
- Display connected stores: chydantest.myshopify.com
- Test store connections
- Sync products from each store
- Disconnect stores

**PSP Management:**
- Connect YOUR OWN PSP accounts:
  - Stripe (sk_test_... or sk_live_...)
  - Adyen
  - Mollie
  - PayPal
  - Checkout.com
  - Square
- Display connected PSPs with metrics
- Test PSP connections
- Disconnect PSPs
- Webhook secret configuration
- Sandbox mode toggle

**Routing Configuration:**
- Display routing rules when multiple PSPs connected
- Automatic fallback messaging
- Success rate improvements

**API & Webhooks:**
- Merchant API key display/copy/regenerate
- Webhook URL configuration
- Webhook signing secret (display/rotate)
- Event subscription selection:
  - order.created
  - order.updated
  - payment.completed
  - payment.failed
  - product.out_of_stock
- Send test webhook
- Delivery logs with replay capability
- Code examples (cURL, Express)

#### 5. Settings
- Business name, email, phone, store URL
- Notification preferences
- Save settings

### 🔑 Test Account
- Email: `merchant@test.com`
- Password: `Admin123!`
- Expected Data: Should show chydantest.myshopify.com connections

---

## 👔 Employee Portal (employee.pivota.cc)

### ✅ Deployed Features
- **Login System**: Fixed to use `token` field
- **Comprehensive Merchant Management**

### 📊 Dashboard Features

#### Merchants Tab
**Merchant Table with Actions:**
- View Details
- Review KYB (Know Your Business)
- Upload Documents for KYB
- **Connect Shopify** (on behalf of merchant)
  - OAuth or manual
  - Multiple stores support
- **Connect Wix** (on behalf of merchant)
  - API key + Site ID
  - Multiple stores support
- **Add Additional Store** (Shopify/Wix)
- **Test Store Connection**
- **Disconnect Store**
- **Connect PSP** (on behalf of merchant)
  - All PSP types: Stripe, Adyen, Mollie, PayPal, Checkout.com, Square
  - Webhook secret (optional)
  - Sandbox mode toggle
- **Test PSP Connection**
- **Delete Merchant**

**Filters & Search:**
- Search by business name, email, merchant ID
- Filter by status: all/active/pending/suspended

#### PSP Management Tab
- View all PSPs
- PSP metrics
- Add new PSP
- Test PSP connections

#### Routing Rules Tab
- Configure payment routing
- Enable/disable rules
- A/B testing

#### Analytics Tab
- Dashboard analytics
- Revenue metrics
- Success rates

### 🔑 Test Account
- Email: `employee@pivota.com`
- Password: `Admin123!`

---

## 🤖 Agent Portal (agents.pivota.cc)

### ✅ Deployed Features
- **Login System**: Fixed to use `token` field
- **8-Step Onboarding Flow**
- **Complete Settings Page** ⭐ NEW

### 📊 Dashboard Features

#### Main Dashboard
- API call statistics
- Order metrics
- GMV tracking
- Success rate
- Recent activity log
- Recent orders

#### Quick Links
- Integration guide
- Analytics dashboard
- **Settings** (fully functional)

### ⚙️ Settings Page (NEW)

**Profile Information:**
- Agent name
- Email
- Company/Organization
- Description

**API Credentials:**
- API Key (display/copy/regenerate)
- Webhook URL configuration
- Webhook signing secret (display/rotate)

**Notification Preferences:**
- Email on new order
- Email on API errors
- Webhook on new order
- Webhook on payment
- Daily summary report

**Security:**
- Two-Factor Authentication (enable option)
- Change password
- Delete account

### 🎯 Onboarding Flow
8 steps with graceful fallbacks:
1. Basic Information
2. API Connection Test
3. Product Search Test
4. Order Creation Test
5. Payment Processing Test
6. Webhook Setup
7. Review Configuration
8. Complete Setup

Features:
- Auto-save progress to localStorage
- Resume from last step (with confirmation)
- Reset progress button
- Test each integration step

### 🔑 Test Account
- Email: `agent@test.com`
- Password: `Admin123!`

---

## 🚀 Deployment Status

### Git Commits (Latest)
- **Merchant Portal**: `407ca2f` - Trigger redeploy with latest features
- **Employee Portal**: `1f90c02` - Trigger redeploy with all management features
- **Agent Portal**: `2972a98` - Add comprehensive Settings page

### Vercel Deployment
All three portals are connected to GitHub and auto-deploy on push.
**Deployment time:** ~2-3 minutes after push

---

## 🔧 Backend Integration

### API Endpoints Used
- `POST /api/auth/login` - Returns `{ success, token, user }`
- `GET /api/merchants` - List all merchants
- `GET /api/merchants/{id}` - Merchant details
- `GET /api/analytics/dashboard` - Dashboard analytics
- `GET /api/orders` - List orders
- `GET /api/products` - List products
- `GET /api/integrations/stores` - Connected stores
- `POST /api/integrations/shopify/connect` - Connect Shopify
- `POST /api/integrations/wix/connect` - Connect Wix
- `GET /api/psp/list` - List PSPs
- `POST /api/psp/setup` - Setup PSP
- `GET /api/webhooks/config` - Webhook configuration
- `POST /api/webhooks/test` - Test webhook

### Authentication
- Backend returns: `{ success: true, token: "JWT...", user: {...} }`
- Frontend stores: `localStorage.setItem('merchant_token', data.token)`
- Headers: `Authorization: Bearer ${token}`

---

## 📝 Known Issues & Next Steps

### Merchant Portal
- ✅ Login fixed (use `token` not `access_token`)
- ✅ Real data loading implemented
- ✅ Webhook configuration UI added
- 🔄 **Pending**: Verify backend returns real Shopify store data for test merchant

### Employee Portal
- ✅ Login fixed
- ✅ All merchant management features added
- ✅ Multi-platform store connection support
- ✅ PSP connection with webhook/sandbox options

### Agent Portal
- ✅ Login fixed
- ✅ Settings page implemented
- ✅ Onboarding with graceful fallbacks

---

## 🧪 Testing Checklist

### For Merchant Portal (merchant@test.com)
- [ ] Login successfully
- [ ] See real data from chydantest.myshopify.com
- [ ] View connected stores in Integration tab
- [ ] View connected PSPs with real credentials
- [ ] Copy API key
- [ ] Configure webhook URL
- [ ] Send test webhook

### For Employee Portal (employee@pivota.com)
- [ ] Login successfully
- [ ] View merchant list
- [ ] Open "More Actions" dropdown for a merchant
- [ ] Connect Shopify store on behalf of merchant
- [ ] Connect PSP with webhook secret
- [ ] Test PSP connection
- [ ] Review KYB documents

### For Agent Portal (agent@test.com)
- [ ] Login successfully
- [ ] View dashboard with stats
- [ ] Navigate to Settings
- [ ] Copy API key
- [ ] Configure webhook URL
- [ ] Update profile information
- [ ] Change notification preferences

---

## 📞 Support

If Vercel deployment doesn't show latest features:
1. Check Vercel dashboard for build status
2. Verify GitHub integration is active
3. Check build logs for errors
4. Force redeploy with empty commit: `git commit --allow-empty -m "trigger deploy" && git push`

