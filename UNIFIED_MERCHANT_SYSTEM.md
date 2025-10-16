# ✅ 统一商户系统 - 已完成

## 🎯 重构完成

我们已完全移除"Legacy Stores"概念，现在只有一个统一的 **Phase 2 商户系统**。

## 📊 变更内容

### 移除的功能
- ❌ Legacy Stores 加载逻辑
- ❌ 双重商户数据结构（configured stores + onboarded merchants）
- ❌ merchant_id 数字 ID 与字符串 ID 混用
- ❌ 环境变量配置的 Shopify/Wix 店铺

### 保留的功能
- ✅ Phase 2 商户注册流程（`/merchant/onboarding`）
- ✅ KYC 文档上传
- ✅ PSP 连接验证（Stripe/Adyen）
- ✅ API Key 生成
- ✅ 商户审核（Approve/Reject）
- ✅ 商户详情查看
- ✅ 商户删除（软删除）

## 🔧 技术改进

### 后端
1. **统一删除端点**
   ```
   DELETE /merchant/onboarding/delete/{merchant_id}
   ```
   - 接受 `merch_` 开头的字符串 ID
   - 执行软删除（`status='deleted'`）

2. **文档上传端点**
   ```
   POST /merchant/onboarding/kyc/upload/file/{merchant_id}
   ```
   - 支持 multipart 文件上传
   - 存储文档元数据到 `kyc_documents` JSON 字段

### 前端
1. **简化 AdminDashboard**
   - 移除 `merchants` 状态
   - 移除 legacy stores 加载逻辑
   - 直接让 `OnboardingAdminView` 管理自己的数据

2. **统一 OnboardingAdminView**
   - 移除 `legacyStores` prop
   - 所有商户使用相同操作按钮
   - 删除后自动刷新列表

3. **智能路由**
   - `merchantApi.uploadDocument()` 自动判断 ID 类型
   - `merch_` 开头走 Phase 2 endpoint
   - 数字 ID 走 legacy endpoint（向后兼容）

## 🧪 测试步骤

### 1. 注册新商户
1. 访问 http://localhost:5173
2. 点击 "Merchants" 标签页
3. 点击 "+ Merchant Onboarding Portal"
4. 填写表单：
   - Business Name: `Test Store`
   - Website: `https://test.com`
   - Region: `US`
   - Contact Email: `test@test.com`
5. 提交后会返回 `merchant_id`（如 `merch_abc12345`）

### 2. 上传 KYB 文档
1. 在 Merchants 列表中找到新商户
2. 点击 "Upload Docs" 按钮
3. 选择文档类型（Business License, Tax ID 等）
4. 上传 PDF/JPG/PNG 文件
5. 验证成功提示

### 3. 审核商户
1. 点击 "Review KYB" 按钮
2. 查看商户信息和上传的文档
3. 点击 "Approve Merchant" 或 "Reject"
4. 验证状态更新

### 4. 连接 PSP
1. 商户被批准后，点击 "Resume PSP Setup"
2. 选择 PSP（Stripe 或 Adyen）
3. 输入测试 API Key
4. 系统会实时验证 key 的有效性
5. 成功后生成 API Key 用于 `/payment/execute`

### 5. 删除商户
1. 点击 "Remove" 按钮
2. 确认删除
3. 验证商户从列表消失
4. 后端执行软删除（`status='deleted'`）

## 📝 当前状态

### 已部署
- ✅ 后端已推送到 Render
- ✅ 前端代码已提交

### 数据库
- ✅ `merchant_onboarding` 表完整
- ✅ 支持 `kyc_documents` JSON 字段
- ✅ 软删除逻辑

### API 端点
```
POST   /merchant/onboarding/register           # 注册商户
POST   /merchant/onboarding/kyc/upload/file/{merchant_id}  # 上传文档
POST   /merchant/onboarding/psp/setup          # 连接 PSP
GET    /merchant/onboarding/status/{merchant_id}  # 查询状态
GET    /merchant/onboarding/all                # 管理员查看所有
DELETE /merchant/onboarding/delete/{merchant_id}  # 删除商户
```

## 🎉 下一步

现在系统更简单、更一致了！所有商户都通过相同的流程：

```
注册 → 上传文档 → 审核 → 连接PSP → 获得API Key → 调用支付
```

如果需要将旧的 Shopify/Wix 店铺迁移，只需：
1. 通过注册表单手动创建新商户
2. 使用相同的店铺 URL 和配置
3. 连接对应的 PSP

## 🔍 错误诊断

如果遇到问题：

1. **删除失败**
   - 检查 merchant_id 是否以 `merch_` 开头
   - 查看浏览器控制台错误信息
   - 后端日志会显示详细原因

2. **上传失败**
   - 确认文件大小 < 10MB
   - 确认格式为 PDF/JPG/PNG
   - 检查 merchant_id 是否有效

3. **PSP 验证失败**
   - Stripe: 检查 key 是否以 `sk_test_` 或 `sk_live_` 开头
   - Adyen: 确认 key 有效且有权限

## 📞 支持

如有问题：
1. 查看浏览器 Console (`F12`)
2. 查看 Render 后端日志
3. 提供 merchant_id 和错误信息

