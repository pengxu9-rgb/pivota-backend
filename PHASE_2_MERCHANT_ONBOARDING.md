# 🚀 Phase 2: Merchant Onboarding & PSP Connection

## ✅ Completed Features

### Backend Infrastructure

#### 1. **Merchant Onboarding Database** (`db/merchant_onboarding.py`)
- ✅ Merchant registration with business details
- ✅ KYC status tracking (pending → approved → connected)
- ✅ PSP connection management
- ✅ API key generation & storage
- ✅ Document upload simulation (JSON blob)

#### 2. **Payment Router Database** (`db/payment_router.py`)
- ✅ Links merchants to their PSPs
- ✅ Stores encrypted PSP credentials
- ✅ Enables unified `/payment/execute` routing
- ✅ Supports multi-PSP configurations

#### 3. **Onboarding API Endpoints** (`routes/merchant_onboarding_routes.py`)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/merchant/onboarding/register` | POST | Register new merchant |
| `/merchant/onboarding/kyc/upload` | POST | Upload KYC documents |
| `/merchant/onboarding/psp/setup` | POST | Connect PSP & get API key |
| `/merchant/onboarding/status/{id}` | GET | Check onboarding status |
| `/merchant/onboarding/all` | GET | Admin: List all merchants |
| `/merchant/onboarding/approve/{id}` | POST | Admin: Approve KYC |
| `/merchant/onboarding/reject/{id}` | POST | Admin: Reject KYC |

### Frontend Components

#### 4. **Merchant Onboarding Dashboard** (`/merchant/onboarding`)
- ✅ 4-step wizard (Register → KYC → PSP → Complete)
- ✅ Real-time status updates
- ✅ Auto-approve KYC simulation (5s delay)
- ✅ PSP selection (Stripe/Adyen/ShopPay)
- ✅ API key generation & display
- ✅ Progress indicator UI

#### 5. **Admin Onboarding View** (Admin Dashboard → Onboarding tab)
- ✅ View all merchant onboardings
- ✅ Filter by status (pending/approved/rejected)
- ✅ Manual KYC approve/reject
- ✅ PSP connection status
- ✅ Quick actions for each merchant

---

## 📋 How It Works

### Merchant Onboarding Flow

```
1. REGISTER
   └─> POST /merchant/onboarding/register
       ├─> Creates merchant record
       ├─> Generates unique merchant_id
       └─> Triggers auto-KYC (5s background task)

2. KYC VERIFICATION
   └─> Status: pending_verification
       ├─> Auto-approved after 5s (simulated)
       ├─> OR Admin manual approval
       └─> Status → approved

3. PSP SETUP
   └─> POST /merchant/onboarding/psp/setup
       ├─> Connect Stripe/Adyen/ShopPay
       ├─> Generate merchant API key
       ├─> Register in payment router
       └─> Status → PSP connected

4. COMPLETE
   └─> Merchant receives:
       ├─> Unique merchant_id
       ├─> API key (pk_live_...)
       └─> Can now use /payment/execute
```

### API Key Usage

Once a merchant completes onboarding, they receive an API key:

```bash
# Example API call to execute payment
curl -X POST https://pivota-dashboard.onrender.com/payment/execute \
  -H "X-Merchant-API-Key: pk_live_abc123..." \
  -H "Content-Type: application/json" \
  -d '{
    "order_id": "ORDER-001",
    "amount": 100.00,
    "currency": "USD"
  }'
```

The payment router automatically:
1. Validates the API key
2. Retrieves merchant's PSP configuration
3. Routes payment to correct PSP
4. Returns unified response

---

## 🎨 Frontend Access

### Merchant Dashboard
- **URL**: `/merchant/onboarding`
- **Access**: Public (no auth required for onboarding)
- **Features**:
  - Business registration form
  - KYC document upload
  - PSP connection wizard
  - API key display

### Admin Dashboard
- **URL**: `/admin` → **Onboarding (Phase 2)** tab
- **Access**: Admin only
- **Features**:
  - View all onboarding merchants
  - Filter by status
  - Approve/reject KYC
  - Monitor PSP connections

---

## 🧪 Testing Instructions

### 1. Register a New Merchant

**Frontend**: Navigate to `/merchant/onboarding`

**Backend API**:
```bash
curl -X POST https://pivota-dashboard.onrender.com/merchant/onboarding/register \
  -H "Content-Type: application/json" \
  -d '{
    "business_name": "Test Store Inc",
    "website": "https://teststore.com",
    "region": "US",
    "contact_email": "merchant@teststore.com",
    "contact_phone": "+1-555-1234"
  }'

# Response:
{
  "status": "success",
  "merchant_id": "merch_a1b2c3d4",
  "message": "Merchant registered. KYC verification in progress.",
  "next_step": "Upload KYC documents or wait for auto-verification"
}
```

### 2. Wait for KYC Auto-Approval (5 seconds)

```bash
# Check status
curl https://pivota-dashboard.onrender.com/merchant/onboarding/status/merch_a1b2c3d4

# After 5 seconds:
{
  "status": "success",
  "merchant_id": "merch_a1b2c3d4",
  "kyc_status": "approved",
  "psp_connected": false,
  ...
}
```

### 3. Connect PSP

```bash
curl -X POST https://pivota-dashboard.onrender.com/merchant/onboarding/psp/setup \
  -H "Content-Type: application/json" \
  -d '{
    "merchant_id": "merch_a1b2c3d4",
    "psp_type": "stripe",
    "psp_sandbox_key": "sk_test_YOUR_STRIPE_KEY"
  }'

# Response:
{
  "status": "success",
  "merchant_id": "merch_a1b2c3d4",
  "api_key": "pk_live_xyz789...",  # SAVE THIS!
  "psp_type": "stripe",
  "message": "Stripe connected successfully. Registered in payment router.",
  "next_step": "Use this API key to call /payment/execute with header X-Merchant-API-Key"
}
```

### 4. Admin Actions

```bash
# List all merchants (admin only)
curl -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  https://pivota-dashboard.onrender.com/merchant/onboarding/all

# Manually approve KYC
curl -X POST -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  https://pivota-dashboard.onrender.com/merchant/onboarding/approve/merch_a1b2c3d4

# Reject with reason
curl -X POST -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  "https://pivota-dashboard.onrender.com/merchant/onboarding/reject/merch_a1b2c3d4?reason=Missing+documents"
```

---

## 📊 Database Schema

### `merchant_onboarding` Table
```sql
- id: Integer (Primary Key)
- merchant_id: String (Unique) - e.g., "merch_a1b2c3d4"
- business_name: String
- website: String
- region: String (US/EU/APAC/UK)
- contact_email: String
- contact_phone: String
- status: String (pending_verification | approved | rejected)
- psp_connected: Boolean
- psp_type: String (stripe | adyen | shoppay)
- psp_sandbox_key: String (Encrypted in production)
- api_key: String (pk_live_...)
- api_key_hash: String
- kyc_documents: JSON
- created_at: DateTime
- verified_at: DateTime
```

### `payment_router_config` Table
```sql
- id: Integer (Primary Key)
- merchant_id: String (FK → merchant_onboarding)
- psp_type: String
- psp_credentials: String (Encrypted)
- routing_priority: Integer
- enabled: Boolean
- created_at: DateTime
```

---

## 🔗 Integration Points

### With Existing Systems

1. **Authentication**: 
   - Admin endpoints use existing `require_admin` from `routes/auth_routes.py`
   - Merchant onboarding is public (no auth)

2. **Payment Execution**:
   - Merchants use `X-Merchant-API-Key` header
   - Payment router looks up merchant → PSP mapping
   - Routes to correct adapter (Stripe/Adyen)

3. **Admin Dashboard**:
   - New "Onboarding (Phase 2)" tab
   - Links to `/merchant/onboarding` for new merchant flow
   - Shows onboarding stats in Overview

---

## 🎯 Next Steps (Phase 3)

### Suggested Enhancements

1. **Employee Dashboard** (separate from Admin)
   - View assigned merchants
   - Upload documents on behalf of merchants
   - Communicate with merchants

2. **Agent Dashboard**
   - View onboarded merchants
   - Access stock/inventory data
   - Track commissions

3. **Real KYC Integration**
   - Replace JSON blob with actual file uploads (S3/Cloudinary)
   - Integrate with KYC providers (Stripe Identity, Onfido)
   - Document verification workflow

4. **Multi-PSP Routing**
   - Smart routing based on:
     - Transaction amount
     - Merchant region
     - PSP success rates
     - Cost optimization

5. **Webhook Integration**
   - PSP webhook handlers
   - Real-time transaction status updates
   - Merchant notifications

---

## 🚨 Important Notes

### Security

- ⚠️ **API keys are shown only once** during PSP setup
- ⚠️ PSP credentials stored in plain text (encrypt in production!)
- ⚠️ Use `secrets` module for key generation
- ⚠️ Hash API keys with SHA256 for verification

### Performance

- Auto-KYC approval uses background tasks (no blocking)
- Payment router uses indexed lookups (merchant_id)
- Database tables auto-created on startup

### Deployment

1. Backend changes are auto-deployed to Render
2. Frontend needs rebuild: `cd simple_frontend && npm run build`
3. Environment variables:
   - No new variables needed for Phase 2
   - Existing PSP keys are reused

---

## 📝 Changelog

### Phase 2 - October 2025

- ✅ Added merchant onboarding flow
- ✅ Implemented KYC auto-approval simulation
- ✅ Created PSP setup wizard
- ✅ Generated merchant API keys
- ✅ Built payment router database
- ✅ Added admin onboarding management
- ✅ Created React onboarding dashboard
- ✅ Integrated with existing admin panel

---

## 🆘 Troubleshooting

### Issue: KYC not auto-approving
- Check backend logs for background task execution
- Manually approve via admin dashboard or API

### Issue: PSP setup fails
- Verify PSP credentials are valid
- Check merchant KYC is approved
- Ensure merchant hasn't already connected a PSP

### Issue: API key not working
- Verify header is `X-Merchant-API-Key: pk_live_...`
- Check merchant is registered in payment_router_config
- Ensure PSP is connected

---

## 📞 Support

For issues or questions:
1. Check `/merchant/onboarding/status/{merchant_id}`
2. View admin dashboard Onboarding tab
3. Check backend logs in Render
4. Review API docs at `/docs`

