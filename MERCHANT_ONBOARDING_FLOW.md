# Merchant Onboarding & KYB Flow

## 🎯 Complete Workflow

### 1. Employee/Agent Onboards Merchant
**Endpoint**: `POST /merchants/onboard`

```bash
curl -X POST https://pivota-dashboard.onrender.com/merchants/onboard \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "business_name": "My Store",
    "legal_name": "My Store LLC",
    "platform": "shopify",
    "store_url": "https://mystore.com",
    "contact_email": "owner@mystore.com",
    "contact_phone": "+1234567890",
    "business_type": "ecommerce",
    "country": "US",
    "expected_monthly_volume": 50000,
    "description": "Online clothing store"
  }'
```

**Response**:
```json
{
  "status": "success",
  "message": "Merchant application submitted successfully",
  "merchant_id": 123,
  "next_step": "Upload KYB documents"
}
```

---

### 2. Employee Uploads KYB Documents
**Endpoint**: `POST /merchants/{merchant_id}/documents/upload`

```bash
curl -X POST https://pivota-dashboard.onrender.com/merchants/123/documents/upload \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F "document_type=business_license" \
  -F "file=@business_license.pdf"
```

**Supported Document Types**:
- `business_license` - Business registration/license
- `tax_id` - Tax ID document
- `bank_statement` - Recent bank statement
- `proof_of_address` - Utility bill or lease agreement
- `identity_proof` - Owner's ID/passport
- `other` - Other supporting documents

**Response**:
```json
{
  "status": "success",
  "message": "Document uploaded successfully",
  "document_id": 456,
  "file_name": "business_license.pdf"
}
```

---

### 3. Admin Reviews KYB Application
**Get Pending Merchants**: `GET /merchants/?status=pending`

```bash
curl https://pivota-dashboard.onrender.com/merchants/?status=pending \
  -H "Authorization: Bearer ADMIN_TOKEN"
```

**Get Merchant Details**: `GET /merchants/{merchant_id}`

```bash
curl https://pivota-dashboard.onrender.com/merchants/123 \
  -H "Authorization: Bearer ADMIN_TOKEN"
```

**Response**:
```json
{
  "status": "success",
  "merchant": {
    "id": 123,
    "business_name": "My Store",
    "status": "pending",
    "verification_status": "pending",
    ...
  },
  "documents": [
    {
      "id": 456,
      "document_type": "business_license",
      "file_name": "business_license.pdf",
      "verified": false
    }
  ]
}
```

---

### 4. Admin Verifies Documents (Optional)
**Endpoint**: `POST /merchants/documents/{doc_id}/verify`

```bash
curl -X POST https://pivota-dashboard.onrender.com/merchants/documents/456/verify \
  -H "Authorization: Bearer ADMIN_TOKEN"
```

---

### 5. Admin Approves/Rejects Merchant

#### Approve:
**Endpoint**: `POST /merchants/{merchant_id}/approve`

```bash
curl -X POST https://pivota-dashboard.onrender.com/merchants/123/approve \
  -H "Authorization: Bearer ADMIN_TOKEN"
```

**Response**:
```json
{
  "status": "success",
  "message": "Merchant My Store approved successfully",
  "merchant_id": 123
}
```

#### Reject:
**Endpoint**: `POST /merchants/{merchant_id}/reject`

```bash
curl -X POST https://pivota-dashboard.onrender.com/merchants/123/reject \
  -H "Authorization: Bearer ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "status": "rejected",
    "rejection_reason": "Incomplete documentation"
  }'
```

---

## 📁 Database Tables Created

### `merchants` table:
- `id` - Primary key
- `business_name` - Business display name
- `legal_name` - Legal entity name
- `platform` - shopify/wix/custom
- `store_url` - Store website
- `contact_email` - Contact email
- `contact_phone` - Contact phone
- `business_type` - Type of business
- `country` - Country code
- `expected_monthly_volume` - Expected monthly revenue
- `description` - Business description
- `status` - pending/approved/rejected/active
- `verification_status` - pending/verified/rejected
- `volume_processed` - Total volume processed
- `created_at` - Creation timestamp
- `updated_at` - Last update timestamp
- `approved_by` - Admin user ID who approved
- `approved_at` - Approval timestamp

### `kyb_documents` table:
- `id` - Primary key
- `merchant_id` - Foreign key to merchants
- `document_type` - Type of document
- `file_name` - Original filename
- `file_path` - Server file path
- `file_size` - File size in bytes
- `uploaded_at` - Upload timestamp
- `verified` - Boolean verification status
- `verified_at` - Verification timestamp
- `verified_by` - Admin user ID who verified

---

## 🖥️ Frontend Integration

### In Admin Dashboard (`/admin`):

1. **Onboard Merchant**: Click "Onboard Merchant" button in Merchants tab
2. **Fill Form**: Complete the merchant onboarding form
3. **Submit**: Application is created with `merchant_id`
4. **Upload Documents**: Use the merchant ID to upload KYB documents
5. **Review**: Admin can review in KYB Review modal
6. **Approve/Reject**: Admin clicks approve or reject with reason

### Files Updated:
- ✅ `/pivota_infra/db/merchants.py` - Database tables and operations
- ✅ `/pivota_infra/routes/merchant_routes.py` - API endpoints
- ✅ `/pivota_infra/main.py` - Router registration + table creation
- ✅ `/simple_frontend/src/services/api.ts` - Frontend API client
- ✅ `/simple_frontend/src/pages/AdminDashboard.tsx` - UI integration

---

## 🚀 Next Steps

### To Test:
1. **Deploy backend** to Render (tables will be auto-created)
2. **Login as employee/agent** to onboard a merchant
3. **Upload KYB documents** for the merchant
4. **Login as admin** to review and approve/reject

### Document Upload UI (Coming Soon):
- Add file upload modal after merchant onboarding
- Show merchant's document list
- Allow drag-and-drop for multiple documents
- Preview uploaded documents

---

## 📝 Notes

- Documents are stored in `/uploads/kyb/{merchant_id}/` directory
- Only authenticated users can onboard merchants
- Only admins can approve/reject merchants
- File uploads support PDF, JPG, PNG formats
- Maximum file size: 10MB per document

