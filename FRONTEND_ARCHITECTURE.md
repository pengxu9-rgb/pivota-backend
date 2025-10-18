# Pivota Frontend Architecture
**Production-Ready Agent & Merchant Portals**

---

## 🎯 FRONTEND OVERVIEW

We need **TWO separate frontends**:

### 1. **Agent Portal** (For AI Agents)
- Simple API integration interface
- Order creation and tracking
- Analytics dashboard
- API key management

### 2. **Merchant Dashboard** (For Merchants)
- Business overview and analytics
- Order management
- PSP/MCP connection setup
- Product catalog management

---

## 🏗️ ARCHITECTURE DESIGN

### Technology Stack Recommendation:

**Option A: React + TypeScript (Modern SPA)**
```
Frontend: React 18 + TypeScript
State: React Query (for API calls) + Zustand (for global state)
Styling: Tailwind CSS + shadcn/ui components
Routing: React Router v6
Charts: Recharts or Chart.js
Build: Vite
Deployment: Vercel or Railway (same as backend)
```

**Option B: Next.js (Full-Stack Framework)**
```
Framework: Next.js 14 (App Router)
Language: TypeScript
Styling: Tailwind CSS + shadcn/ui
API Routes: Next.js API routes (optional, we have backend)
Deployment: Vercel
```

**Recommendation**: **Next.js** for faster development and better SEO

---

## 📱 AGENT PORTAL - PAGES & FEATURES

### 1. **Dashboard** (`/`)
```typescript
Features:
- Overview metrics (orders, revenue, success rate)
- Recent orders list
- Quick actions (create order, search products)
- Usage statistics (API calls, rate limits)

Components:
- MetricsCard (total orders, GMV, conversion rate)
- RecentOrdersTable
- QuickActionButtons
- UsageGauge (daily quota visualization)
```

### 2. **Product Search** (`/products`)
```typescript
Features:
- Search products across all authorized merchants
- Filter by merchant, category, price
- Product details modal
- Add to cart functionality

Components:
- ProductSearchBar
- ProductGrid/ProductList
- ProductCard
- FilterSidebar
- AddToCartButton
```

### 3. **Create Order** (`/orders/new`)
```typescript
Features:
- Multi-step order creation wizard
- Product selection
- Customer info input
- Shipping address form
- Order review and submit

Components:
- OrderWizard (Stepper component)
- ProductSelector
- CustomerForm
- ShippingAddressForm
- OrderSummary
- SubmitButton
```

### 4. **Order Tracking** (`/orders`)
```typescript
Features:
- List all orders (with filters)
- Order detail view
- Fulfillment tracking
- Refund requests

Components:
- OrdersTable (sortable, filterable)
- OrderDetailModal
- TrackingTimeline
- RefundRequestForm
```

### 5. **Analytics** (`/analytics`)
```typescript
Features:
- Performance metrics over time
- Conversion funnel
- Top merchants
- Revenue trends

Components:
- AnalyticsCharts (line, bar, pie)
- DateRangePicker
- MetricsComparison
- ExportButton
```

### 6. **Settings** (`/settings`)
```typescript
Features:
- API key display (show once)
- Rate limits and quotas
- Webhook configuration
- Profile settings

Components:
- APIKeyDisplay (copy to clipboard)
- QuotaSettings
- ProfileForm
```

---

## 🏪 MERCHANT DASHBOARD - PAGES & FEATURES

### 1. **Dashboard** (`/`)
```typescript
Features:
- Sales overview
- Order statistics
- Revenue charts
- Recent activity

Components:
- SalesMetrics
- RevenueChart (daily/weekly/monthly)
- OrderStatusPieChart
- RecentActivityFeed
```

### 2. **Orders** (`/orders`)
```typescript
Features:
- All orders list
- Filter by status, date, amount
- Order details
- Fulfill orders
- Process refunds

Components:
- OrdersDataTable
- OrderDetailPanel
- FulfillOrderButton
- RefundModal
- ExportCSV
```

### 3. **Products** (`/products`)
```typescript
Features:
- Sync products from Shopify/Wix
- View product catalog
- Inventory status
- Product performance

Components:
- ProductsGrid
- SyncButton
- InventoryBadge
- ProductAnalytics
```

### 4. **Settings** (`/settings`)
```typescript
Features:
- Business profile
- PSP connection (Stripe/Adyen setup)
- MCP connection (Shopify/Wix)
- API keys
- Webhooks

Components:
- BusinessProfileForm
- PSPConnector (Stripe/Adyen switcher)
- MCPConnector (Platform selector)
- APIKeyManager
- WebhooksList
```

### 5. **Analytics** (`/analytics`)
```typescript
Features:
- Revenue trends
- Order conversion rates
- Agent performance
- Product analytics

Components:
- RevenueOverTime
- ConversionFunnel
- AgentLeaderboard
- TopProductsTable
```

### 6. **Onboarding** (`/onboarding`) - First-time setup
```typescript
Steps:
1. Business information
2. Connect payment processor (Stripe/Adyen)
3. Connect e-commerce platform (Shopify/Wix)
4. Sync products
5. Complete!

Components:
- OnboardingWizard
- ProgressIndicator
- StepConnectPSP
- StepConnectMCP
- ProductSyncStatus
```

---

## 🔐 AUTHENTICATION FLOW

### Agent Portal:
```
1. Login with API Key
2. Store in localStorage/cookie
3. Add to all API requests as X-API-Key header
4. Auto-refresh on token expiry
```

### Merchant Dashboard:
```
1. Login with email/password (JWT)
2. Store JWT token
3. Add to requests as Authorization: Bearer <token>
4. Refresh token before expiry
```

---

## 🎨 UI/UX DESIGN PRINCIPLES

### Design System:
- **Colors**: 
  - Primary: Blue (#3B82F6)
  - Success: Green (#10B981)
  - Warning: Yellow (#F59E0B)
  - Error: Red (#EF4444)
- **Typography**: Inter or SF Pro
- **Spacing**: Tailwind's spacing scale
- **Components**: shadcn/ui (accessible, customizable)

### Key UX Principles:
1. **Fast**: Optimistic updates, skeleton loaders
2. **Clear**: Show order status at a glance
3. **Forgiving**: Undo actions, confirmation dialogs
4. **Responsive**: Mobile-first design
5. **Accessible**: WCAG 2.1 AA compliance

---

## 📦 PROJECT STRUCTURE

```
pivota-frontend/
├── src/
│   ├── app/                     # Next.js app router
│   │   ├── (agent)/            # Agent portal routes
│   │   │   ├── page.tsx        # Dashboard
│   │   │   ├── products/
│   │   │   ├── orders/
│   │   │   └── settings/
│   │   ├── (merchant)/         # Merchant portal routes
│   │   │   ├── page.tsx        # Dashboard
│   │   │   ├── orders/
│   │   │   ├── products/
│   │   │   ├── analytics/
│   │   │   └── settings/
│   │   └── layout.tsx
│   ├── components/
│   │   ├── ui/                 # shadcn components
│   │   ├── agent/              # Agent-specific components
│   │   ├── merchant/           # Merchant-specific components
│   │   └── shared/             # Shared components
│   ├── lib/
│   │   ├── api/                # API client functions
│   │   ├── hooks/              # Custom React hooks
│   │   └── utils/              # Helper functions
│   ├── types/                  # TypeScript types
│   └── config/                 # App configuration
├── public/
├── package.json
└── tailwind.config.js
```

---

## 🔌 API INTEGRATION

### API Client Setup:
```typescript
// lib/api/client.ts
const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'https://web-production-fedb.up.railway.app';

export const apiClient = {
  // Agent endpoints
  agent: {
    searchProducts: (params) => fetch(`${API_BASE}/agent/v1/products/search?${params}`),
    createOrder: (data) => fetch(`${API_BASE}/agent/v1/orders/create`, {method: 'POST', body: data}),
    trackOrder: (orderId) => fetch(`${API_BASE}/agent/v1/fulfillment/track/${orderId}`),
    getAnalytics: (days) => fetch(`${API_BASE}/agent/v1/analytics/summary?days=${days}`)
  },
  
  // Merchant endpoints
  merchant: {
    getOrders: (params) => fetch(`${API_BASE}/orders/merchant/${params.merchantId}`),
    getAnalytics: () => fetch(`${API_BASE}/merchant/analytics`),
    syncProducts: (merchantId) => fetch(`${API_BASE}/products/${merchantId}?force_refresh=true`),
    processRefund: (orderId, data) => fetch(`${API_BASE}/orders/${orderId}/refund`, {method: 'POST', body: data})
  }
};
```

---

## 🚀 DEVELOPMENT TIMELINE

### Week 1: Backend Completion + Frontend Setup
**Days 1-2**: Backend APIs
- ✅ Fulfillment tracking API
- ✅ Refund processing  
- ⏳ API documentation
- ⏳ Testing

**Days 3-4**: Frontend Setup
- Initialize Next.js project
- Set up Tailwind + shadcn/ui
- Create basic layouts
- API client setup

**Days 5-7**: Agent Portal
- Dashboard with metrics
- Product search
- Order creation wizard
- Order tracking

### Week 2: Merchant Dashboard + Polish
**Days 1-3**: Merchant Dashboard
- Business dashboard
- Order management
- PSP/MCP connection UI
- Analytics

**Days 4-5**: Integration & Testing
- Connect all APIs
- Test all user flows
- Bug fixes

**Days 6-7**: Polish & Launch Prep
- UI/UX refinements
- Performance optimization
- Documentation
- Deploy frontend

---

## 📝 IMMEDIATE NEXT STEPS

### Today (Remaining):
1. ✅ Commit fulfillment & refund APIs
2. Create API documentation
3. Initialize frontend project

### Tomorrow:
1. Build Agent Portal skeleton
2. Implement product search UI
3. Create order wizard

### This Week:
Complete Option 1 + Basic Frontend

Would you like me to:
1. **Start building the frontend now?**
2. **Create API documentation first?**
3. **Test the new fulfillment/refund APIs?**
