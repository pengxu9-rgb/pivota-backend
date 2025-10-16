# 🧪 自动批准流程测试指南

## ✅ 环境准备

- **后端**: https://pivota-dashboard.onrender.com (✅ 运行中)
- **前端**: http://localhost:5173 (✅ 运行中)
- **测试时间**: $(date)

---

## 🎯 测试目标

验证商户注册的智能自动批准功能：
1. ✅ URL 和名称匹配 → 自动批准 → 立即连接 PSP
2. ❌ URL 或名称不匹配 → 人工审核
3. ⏰ 自动批准后设置7天 KYB 截止日期

---

## 📋 测试案例

### 测试案例 1: 自动批准（Shopify 店铺）✅

**预期结果**: 应该自动批准

**输入数据**:
```
Business Name: Acme Store
Store URL: https://acme-shop.myshopify.com
Region: US
Contact Email: acme@test.com
Contact Phone: +1-555-0100
```

**验证点**:
- [ ] URL 格式正确（https://）
- [ ] 是 Shopify 已知平台
- [ ] 商家名称 "acme" 与 URL "acme-shop" 高度匹配
- [ ] **置信度应该 > 0.8**
- [ ] **应该立即显示批准消息**
- [ ] **自动跳转到 PSP Setup 页面**

**预期响应**:
```
✅ Registration Approved!
✅ Auto-approved (Confidence: 85-95%)
📅 Complete full KYB within 7 days
You can now connect your PSP!
```

---

### 测试案例 2: 自动批准（独立域名，高匹配）✅

**预期结果**: 应该自动批准

**输入数据**:
```
Business Name: Tech Gadgets
Store URL: https://techgadgets.com
Region: US
Contact Email: info@techgadgets.com
Contact Phone: +1-555-0200
```

**验证点**:
- [ ] 名称 "techgadgets" 与域名完全匹配
- [ ] **置信度应该 > 0.9**
- [ ] **应该自动批准**

---

### 测试案例 3: 自动批准（中等匹配，边界情况）⚠️

**预期结果**: 应该自动批准（边界情况）

**输入数据**:
```
Business Name: Best Shop LLC
Store URL: https://bestshop-online.com
Region: US
Contact Email: contact@bestshop.com
Contact Phone: +1-555-0300
```

**验证点**:
- [ ] 名称 "bestshop" 在域名中存在
- [ ] **置信度应该 0.5-0.7**
- [ ] **应该自动批准**（匹配分数 > 0.3）

---

### 测试案例 4: 人工审核（名称不匹配）❌

**预期结果**: 需要人工审核

**输入数据**:
```
Business Name: XYZ Corporation
Store URL: https://totally-different-store.com
Region: US
Contact Email: info@xyz.com
Contact Phone: +1-555-0400
```

**验证点**:
- [ ] 名称 "xyz" 与域名 "totallydifferentstore" 不匹配
- [ ] **匹配分数 < 0.3**
- [ ] **应该显示人工审核消息**
- [ ] **停留在 KYC 等待页面**（不跳转到 PSP Setup）

**预期响应**:
```
📋 Registration Received
Manual review required.
We'll notify you once approved.
```

---

### 测试案例 5: 人工审核（URL 无效）❌

**预期结果**: 需要人工审核

**输入数据**:
```
Business Name: Invalid Store
Store URL: https://this-does-not-exist-123456789.com
Region: US
Contact Email: test@invalid.com
Contact Phone: +1-555-0500
```

**验证点**:
- [ ] URL 无法访问
- [ ] 不是已知电商平台
- [ ] **应该显示人工审核消息**

---

## 🔍 详细测试步骤

### Step 1: 打开商户注册页面

1. 打开浏览器访问: **http://localhost:5173**
2. 点击 **"Merchant Onboarding Portal"** 按钮
3. 或直接访问: **http://localhost:5173/merchant/onboarding**

### Step 2: 填写注册表单（测试案例 1）

```
Business Name: Acme Store
Store URL: https://acme-shop.myshopify.com
Region: US
Contact Email: acme@test.com
Contact Phone: +1-555-0100
```

### Step 3: 提交并观察响应

点击 **"Register"** 按钮后，观察：

**自动批准的标志**:
1. ✅ 弹出 Alert 框显示批准消息
2. ✅ 显示置信度分数（Confidence: XX%）
3. ✅ 提示 "Complete full KYB within 7 days"
4. ✅ 1秒后自动跳转到 PSP Setup 页面

**人工审核的标志**:
1. ⚠️ 弹出 Alert 框显示审核消息
2. ⚠️ 显示 "Manual review required"
3. ⚠️ 停留在 KYC 等待页面（Step 2）

### Step 4: 验证后端数据

打开浏览器开发者工具 (F12)，查看 **Network** 标签：

**查看 POST `/merchant/onboarding/register` 的响应**:
```json
{
  "status": "success",
  "message": "✅ Registration approved! ...",
  "merchant_id": "merch_xxx",
  "auto_approved": true,
  "confidence_score": 0.85,
  "validation_details": {
    "url_valid": true,
    "url_message": "✅ Shopify store detected",
    "name_match": true,
    "match_score": 0.85,
    "match_message": "High similarity: 'acme' matches 'acme-shop'"
  },
  "full_kyb_deadline": "2025-10-23T10:30:00",
  "next_step": "Connect PSP"
}
```

### Step 5: 连接 PSP（自动批准后）

如果自动批准，应该自动跳转到 PSP Setup 页面：

1. 选择 PSP 类型: **Stripe** (测试用)
2. 输入测试 API Key: `sk_test_xxx`
3. 点击 **"Connect PSP"**
4. 验证连接成功

### Step 6: 验证管理员视图

1. 在新标签页打开: **http://localhost:5173**
2. 使用管理员账号登录:
   - Email: `superadmin@pivota.com`
   - Password: `admin123`
3. 点击 **"Merchants"** 标签
4. 找到刚才注册的商户

**应该显示**:
```
商户卡片:
┌─────────────────────────────────────┐
│ Acme Store                [approved]│
│ ID: merch_xxx                        │
│ 🏪 https://acme-shop.myshopify.com   │
│                                      │
│ Auto-Approved: Yes (85% confidence)  │
│ KYB Deadline: 2025-10-23            │
│ PSP: ✓ stripe                        │
│                                      │
│ [Upload Docs] [Review KYB]          │
│ [Details] [Remove]                   │
└─────────────────────────────────────┘
```

---

## ✅ 测试检查清单

### 自动批准流程
- [ ] 测试案例 1 (Shopify 高匹配) → 自动批准
- [ ] 测试案例 2 (独立域名完全匹配) → 自动批准
- [ ] 测试案例 3 (中等匹配边界) → 自动批准
- [ ] 显示正确的置信度分数
- [ ] 自动跳转到 PSP Setup
- [ ] 可以成功连接 PSP

### 人工审核流程
- [ ] 测试案例 4 (名称不匹配) → 人工审核
- [ ] 测试案例 5 (URL 无效) → 人工审核
- [ ] 显示人工审核消息
- [ ] 停留在 KYC 等待页面
- [ ] 不自动跳转到 PSP Setup

### 管理员视图
- [ ] 商户列表显示自动批准状态
- [ ] 显示置信度分数
- [ ] 显示 KYB 截止日期
- [ ] 所有操作按钮正常工作

### 数据库验证
- [ ] `auto_approved` 字段正确设置
- [ ] `approval_confidence` 字段有正确的值
- [ ] `full_kyb_deadline` 设置为7天后
- [ ] `status` 字段为 "approved" (自动批准)

---

## 🐛 常见问题排查

### 问题 1: 所有商户都需要人工审核
**可能原因**:
- 匹配阈值设置过高
- URL 验证服务不可用

**检查**:
```bash
# 查看后端日志
curl https://pivota-dashboard.onrender.com/
```

### 问题 2: 没有自动跳转到 PSP Setup
**可能原因**:
- 前端没有正确处理 `auto_approved` 响应
- `setTimeout` 没有执行

**检查**:
- 打开浏览器控制台查看错误
- 检查 Network 响应中的 `auto_approved` 字段

### 问题 3: 管理员看不到自动批准信息
**可能原因**:
- 后端没有返回 `auto_approved` 和 `approval_confidence`
- 前端没有显示这些字段

**检查**:
```bash
# 测试 API
curl https://pivota-dashboard.onrender.com/merchant/onboarding/all \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN"
```

---

## 📊 预期指标

**自动批准率**: 70-80%
- Shopify/Wix 等已知平台 → 90%+ 自动批准
- 独立域名 → 60-70% 自动批准
- 可疑/不匹配 → 需要人工审核

**置信度分布**:
- 高置信度 (0.8-1.0): 50-60%
- 中置信度 (0.5-0.8): 20-30%
- 低置信度 (0.3-0.5): 10-15%
- 需要审核 (< 0.3): 10-20%

---

## 🚀 开始测试！

**当前状态**: 
- ✅ 后端部署完成
- ✅ 前端运行中
- ✅ 准备开始测试

**请按照上述步骤逐个测试，并告诉我结果！**

如果遇到任何问题，请提供：
1. 具体的错误消息
2. 浏览器控制台的输出
3. Network 请求的响应内容

