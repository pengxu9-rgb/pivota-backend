# Merchant Onboarding Workflow

## 优化后的流程 (2025-10-16)

### ✅ 设计目标
- **快速注册**：商户可以快速完成注册，无需立即上传文档
- **灵活上传**：文档可以稍后由商户或管理员上传
- **支持重审**：拒绝后可以重新上传文档并再次审批

---

## 商户端流程

### 1. 快速注册 (Step 1)
**所需信息：**
- Business Name （必填）
- Store URL （必填，用于 KYB 和 MCP 集成）
- Region （必填：US/EU/APAC/UK）
- Contact Email （必填）
- Contact Phone （选填）

**完成后：**
- 获得 `merchant_id`（格式：`merch_xxxxx`）
- 状态自动设置为 `pending_verification`
- 进入 Step 2（等待审批）

### 2. 等待审批 (Step 2)
**展示内容：**
- Merchant ID
- 当前状态（pending_verification / approved / rejected）
- 下一步说明

**操作：**
- 检查状态（Refresh Status）
- 前往管理后台（Go to Dashboard）

**三种状态：**

#### 2.1 Pending Verification
```
📋 Next Steps:
1. Upload KYB Documents (Optional) - 您或我们的团队将上传所需文档
2. Admin Review - 我们的团队将审核您的文档并批准/拒绝
3. Connect Payment Provider - 批准后，连接您的 PSP 并开始处理付款
```

#### 2.2 Approved ✅
```
✓ KYC Approved!
您现在可以连接您的支付提供商
→ [Continue to PSP Setup]
```

#### 2.3 Rejected ❌
```
✗ KYC Rejected
您的申请被拒绝。请查看反馈并重新提交。

Rejection Reason: [显示拒绝原因]

请通过管理后台上传正确的文档，或联系我们的支持团队寻求帮助。
→ [Go to Dashboard]
```

### 3. PSP 设置 (Step 3)
只有在 KYC Approved 后才能访问

---

## 管理员端流程

### 1. 查看商户列表
路径：`/admin` → "Merchants" 标签

**显示信息：**
- Business Name
- Merchant ID
- Store URL
- Status (pending_verification / approved / rejected)
- PSP Connected
- Created At

### 2. 上传 KYB 文档
**操作：**
1. 点击商户卡片的 "Upload Docs" 按钮
2. 选择多个文件（支持 PDF, JPG, PNG）
3. 为每个文件选择文档类型：
   - Business License
   - Tax ID Document
   - Bank Statement
   - Proof of Address
   - Owner ID/Passport
   - Other Document
4. 点击 "Upload X Document(s)"

**结果：**
- 文档元数据存储在 `merchant_onboarding.kyc_documents` (JSONB)
- 逐个上传，显示进度
- 成功/失败统计

### 3. 审核 KYB
**操作：**
1. 点击 "Review KYB" 按钮
2. 查看商户信息：
   - Business Name
   - Store URL
   - Contact Email
   - Region
   - 已上传的文档列表
3. 决策：
   - **Approve**：批准商户，清除拒绝原因
   - **Reject**：拒绝商户，必须填写拒绝原因

### 4. 重新审批流程
**场景：**商户被拒绝后上传了新文档

**步骤：**
1. 商户/管理员通过 "Upload Docs" 上传新文档
2. 管理员再次点击 "Review KYB"
3. 查看新上传的文档
4. 点击 "Approve"
5. 系统自动清除旧的拒绝原因
6. 商户状态变为 `approved`

---

## API 端点

### 商户注册
```http
POST /merchant/onboarding/register
Content-Type: application/json

{
  "business_name": "Acme Inc.",
  "store_url": "https://acme.myshopify.com",
  "region": "US",
  "contact_email": "contact@acme.com",
  "contact_phone": "+1 555-123-4567"
}

Response:
{
  "status": "success",
  "merchant_id": "merch_abc123",
  "message": "Merchant registered. Please await KYC verification."
}
```

### 上传文档
```http
POST /merchant/onboarding/kyc/upload/file/{merchant_id}
Content-Type: multipart/form-data
Authorization: Bearer {token}

document_type: business_license
file: (binary)

Response:
{
  "status": "success",
  "message": "Document uploaded",
  "merchant_id": "merch_abc123",
  "document": {
    "name": "license.pdf",
    "content_type": "application/pdf",
    "size": 123456,
    "document_type": "business_license",
    "uploaded_at": "2025-10-16T10:30:00"
  }
}
```

### 批准商户
```http
POST /merchant/onboarding/approve/{merchant_id}
Authorization: Bearer {admin_token}

Response:
{
  "status": "success",
  "message": "Merchant Acme Inc. approved",
  "merchant_id": "merch_abc123"
}
```

### 拒绝商户
```http
POST /merchant/onboarding/reject/{merchant_id}?reason=Missing+tax+documents
Authorization: Bearer {admin_token}

Response:
{
  "status": "success",
  "message": "Merchant Acme Inc. rejected",
  "merchant_id": "merch_abc123",
  "reason": "Missing tax documents"
}
```

### 获取商户详情
```http
GET /merchant/onboarding/details/{merchant_id}
Authorization: Bearer {token}

Response:
{
  "status": "success",
  "merchant": {
    "merchant_id": "merch_abc123",
    "business_name": "Acme Inc.",
    "store_url": "https://acme.myshopify.com",
    "website": null,
    "platform": "US",
    "status": "approved",
    "kyb_documents": [
      {
        "name": "license.pdf",
        "document_type": "business_license",
        "uploaded_at": "2025-10-16T10:30:00",
        ...
      }
    ],
    "psp_connected": false,
    "psp_type": null,
    "created_at": "2025-10-16T10:00:00"
  }
}
```

---

## 数据库表结构

### `merchant_onboarding` 表关键字段
```sql
CREATE TABLE merchant_onboarding (
  id SERIAL PRIMARY KEY,
  merchant_id VARCHAR(50) UNIQUE NOT NULL,  -- merch_xxxxx
  business_name VARCHAR(255) NOT NULL,
  store_url VARCHAR(500) NOT NULL,          -- 新增必填字段
  website VARCHAR(500),                      -- 可选
  region VARCHAR(50),                        -- US, EU, APAC, UK
  contact_email VARCHAR(255) NOT NULL,
  contact_phone VARCHAR(50),
  status VARCHAR(50) DEFAULT 'pending_verification',  -- approved, rejected
  rejection_reason TEXT,                     -- 拒绝原因
  kyc_documents JSONB,                       -- 文档元数据数组
  psp_connected BOOLEAN DEFAULT FALSE,
  psp_type VARCHAR(50),
  verified_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);
```

---

## 文档上传失败排查

### 常见失败原因
1. **认证失败**：Token 过期或无效
2. **文件太大**：超过 10MB
3. **网络超时**：上传时间过长
4. **后端错误**：500 错误

### 排查步骤
1. 查看浏览器控制台错误信息
2. 检查网络请求的响应状态
3. 尝试单个文件上传
4. 检查文件大小和格式
5. 查看后端日志（Render）

### 改进建议
- [ ] 增加文件大小验证（前端）
- [ ] 添加上传进度条
- [ ] 实现断点续传
- [ ] 使用对象存储（S3/Cloudflare R2）

---

## 总结

**优势：**
✅ 商户注册流程简化，提高转化率
✅ 灵活的文档上传时机
✅ 支持拒绝后重新审批，提高审批效率
✅ 清晰的状态展示和用户引导

**下一步：**
1. 等待 Render 部署完成（约 2-3 分钟）
2. 测试完整的注册-上传-审批流程
3. 收集用户反馈并持续优化

