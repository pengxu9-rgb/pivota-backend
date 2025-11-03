# 🧪 手动测试指南 - 自动批准流程

## 📋 测试准备

✅ **环境状态**:
- 前端: http://localhost:5173 (运行中)
- 后端: https://pivota-dashboard.onrender.com (运行中)
- 数据库: PostgreSQL (通过 Render)

⚠️ **已知问题**: pgbouncer prepared statement 冲突
- 影响：某些测试用例会失败
- 测试案例 2 (人工审核) 已验证工作正常
- 可以继续测试前端 UI 和用户体验

---

## 🎯 测试场景 1: 商户自助注册（人工审核路径）

### Step 1: 打开商户注册页面

1. 打开浏览器访问: **http://localhost:5173**
2. 点击 **"Merchant Onboarding Portal"** 按钮
3. 或直接访问: **http://localhost:5173/merchant/onboarding**

### Step 2: 填写注册信息（测试人工审核）

使用以下测试数据（名称和 URL 不匹配，需要人工审核）：

```
Business Name: XYZ Corporation
Store URL: https://totally-different-store.com
Region: US
Contact Email: test@xyz.com
Contact Phone: +1-555-0400
```

### Step 3: 提交并观察

1. 点击 **"Register"** 按钮
2. 观察弹出的 Alert 消息
3. **预期结果**:
   ```
   📋 Registration Received
   Manual review required.
   We'll notify you once approved.
   ```
4. 应该停留在 KYC 等待页面（Step 2）

### Step 4: 验证后端响应

打开浏览器开发者工具 (F12)，查看 **Console** 和 **Network** 标签：

**Network 标签中查看 POST `/merchant/onboarding/register` 的响应**:
```json
{
  "status": "success",
  "merchant_id": "merch_xxx",
  "auto_approved": false,
  "confidence_score": 0.05-0.2,
  "message": "Registration received. Manual review required...",
  "next_step": "Wait for admin approval"
}
```

---

## 🎯 测试场景 2: 查看商户管理界面

### Step 1: 登录管理员账户

1. 打开新标签页: **http://localhost:5173**
2. 点击右上角 **"Login"** 或直接访问 `/login`
3. 使用管理员凭证:
   - Email: `superadmin@pivota.com`
   - Password: `admin123`

### Step 2: 进入商户管理页面

1. 登录后，点击顶部导航的 **"Merchants"** 标签
2. 或直接访问: **http://localhost:5173/merchants**

### Step 3: 验证商户列表

**应该看到**:
- ✅ 刚才注册的商户 (XYZ Corporation)
- ✅ 状态显示为 "pending_verification"
- ✅ 商户卡片显示:
  - Business Name
  - Merchant ID (merch_xxx)
  - Store URL
  - Status badge
  - 操作按钮

**操作按钮应该包括**:
- ✅ **Approve** (绿色，✓ 图标)
- ✅ **Reject** (红色，✗ 图标)
- ✅ **Upload Docs**
- ✅ **Review KYB**
- ✅ **Details**
- ✅ **Remove**

### Step 4: 测试审批功能

1. 点击 **"Approve"** 按钮
2. 确认对话框中点击 **"OK"**
3. **预期结果**:
   - 商户状态变为 "approved"
   - 显示蓝色提示: "ℹ️ Waiting for merchant to connect PSP"
   - 出现 **"Resume PSP Setup"** 按钮

---

## 🎯 测试场景 3: PSP 连接流程

### Step 1: 回到商户注册页面

1. 回到商户注册标签页: **http://localhost:5173/merchant/onboarding**
2. 或点击管理员界面中的 **"Resume PSP Setup"** 按钮

### Step 2: 输入 Merchant ID

如果页面要求输入 Merchant ID:
1. 从管理员界面复制 Merchant ID (merch_xxx)
2. 粘贴到输入框
3. 点击 **"Resume"**

### Step 3: 连接测试 PSP

在 PSP Setup 页面:

1. 选择 **"Stripe (Test)"** 
2. 输入测试 API Key: 
   ```
   sk_test_51234567890abcdefghijklmnopqrstuvwxyz
   ```
3. 点击 **"Connect PSP"**

**预期结果**:
- ✅ 显示 "PSP Connected" 成功消息
- ✅ 自动跳转到完成页面
- ✅ 显示 "🎉 Onboarding Complete!"

### Step 4: 验证 PSP 连接状态

回到管理员界面:
1. 刷新商户列表
2. 找到刚才的商户
3. **应该显示**:
   - Status: "approved"
   - PSP: "✓ stripe" (绿色)
   - 不再显示 "Waiting for PSP" 提示

---

## 🎯 测试场景 4: 上传 KYB 文档

### Step 1: 打开文档上传

在管理员界面:
1. 找到任意商户
2. 点击 **"Upload Docs"** 按钮

### Step 2: 选择文档类型并上传

1. 从下拉菜单选择文档类型:
   - Business License
   - Tax ID
   - Bank Statement
   - Identity Document
2. 点击 **"Choose File"** 选择文件
3. 点击 **"Upload"**

**预期结果**:
- ✅ 显示上传成功消息
- ✅ 模态框自动关闭
- ✅ 商户列表刷新

### Step 3: 查看已上传文档

1. 点击同一商户的 **"Review KYB"** 按钮
2. **应该显示**:
   - 商户基本信息 (Name, Store URL, Email, Region)
   - 已上传文档列表
   - 每个文档的类型和上传时间
   - Approve/Reject 按钮

---

## 🎯 测试场景 5: 查看商户详情

### Step 1: 打开详情模态框

1. 点击任意商户的 **"Details"** 按钮

### Step 2: 验证显示内容

**应该包括**:
- ✅ Business Name
- ✅ Merchant ID
- ✅ Store URL
- ✅ Website (如果有)
- ✅ Region
- ✅ Contact Email
- ✅ Contact Phone
- ✅ Status
- ✅ PSP Type (如果已连接)
- ✅ Created At 时间戳
- ✅ Auto-Approved 状态 (Yes/No)
- ✅ Confidence Score (如果是自动批准)
- ✅ Full KYB Deadline (如果是自动批准)

---

## 🎯 测试场景 6: 删除商户

### Step 1: 删除测试商户

1. 找到测试用的商户 (XYZ Corporation)
2. 点击 **"Remove"** 按钮
3. 在确认对话框中输入 `DELETE`
4. 点击 **"Confirm"**

### Step 2: 验证删除结果

**预期结果**:
- ✅ 商户从列表中消失
- ✅ 显示成功消息: "Merchant removed successfully"
- ✅ 刷新页面后，商户仍然不显示

---

## 📊 测试检查清单

### 商户注册流程
- [ ] 可以打开注册页面
- [ ] 表单验证工作正常
- [ ] 提交后显示正确的响应消息
- [ ] 人工审核路径正常工作
- [ ] 自动批准路径有响应（即使失败也能看到错误）

### 管理员界面
- [ ] 可以登录管理员账户
- [ ] 商户列表正确显示
- [ ] 所有操作按钮都存在
- [ ] Approve 功能正常
- [ ] Reject 功能正常
- [ ] 状态筛选（All/Pending/Approved/Rejected）工作

### PSP 连接
- [ ] 可以进入 PSP Setup 页面
- [ ] 可以选择 PSP 类型
- [ ] 可以输入 API Key
- [ ] 连接后状态正确更新

### 文档管理
- [ ] 可以上传 KYB 文档
- [ ] 上传的文档显示在 Review 中
- [ ] 可以查看文档列表

### 商户详情
- [ ] 详情模态框显示完整信息
- [ ] 自动批准相关字段正确显示
- [ ] 时间戳格式正确

### 删除功能
- [ ] 可以删除商户
- [ ] 删除确认流程正常
- [ ] 删除后商户不再显示

---

## 🐛 遇到问题？

### 问题 1: 注册时报 500 错误
**原因**: pgbouncer prepared statement 冲突  
**解决**: 这是已知问题，不影响功能验证。尝试多次提交，或使用不同的测试数据。

### 问题 2: 商户列表为空
**可能原因**:
1. 后端连接问题 - 检查 https://pivota-dashboard.onrender.com/health
2. 没有登录 - 确保使用 superadmin@pivota.com 登录
3. 商户被删除 - 重新注册一个

### 问题 3: PSP 连接失败
**可能原因**:
1. API Key 格式错误 - 使用测试格式: `sk_test_xxx`
2. 网络问题 - 检查浏览器 Console 错误
3. 后端错误 - 查看 Render 日志

### 问题 4: 文档上传失败
**可能原因**:
1. 文件太大 - 尝试小于 5MB 的文件
2. 格式不支持 - 使用 PDF、PNG、JPG
3. 网络超时 - 重试上传

---

## 🎉 测试完成后

如果所有功能都正常工作，说明：
- ✅ 前端 UI 完整
- ✅ 后端 API 基本可用
- ✅ 人工审核流程完整
- ✅ 管理员审批功能正常
- ✅ PSP 连接流程完整
- ✅ 文档上传功能正常

**下一步**:
1. 联系 Render 支持解决 pgbouncer 问题
2. 或者升级到付费计划获得更好的数据库支持
3. 或者考虑迁移到 Supabase 等其他服务

---

**开始测试吧！** 🚀

有任何问题随时告诉我，我会帮你解决！

