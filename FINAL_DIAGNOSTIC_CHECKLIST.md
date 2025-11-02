# PSP 配置问题 - 最终诊断清单

## 🎯 当前状态

**症状**: 配置显示成功，但数据库中没有记录

**影响**: Adyen, Checkout, PayPal 无法创建支付意图

**工作正常**: Stripe（通过环境变量）

---

## ✅ 已完成的修复

1. ✅ SQL VALUES 语法错误
2. ✅ Row 对象 .get() 错误  
3. ✅ 添加 logger 导入
4. ✅ **添加数据库事务**（最关键）
5. ✅ PayPal Client Secret 字段
6. ✅ Checkout processing_channel_id 验证
7. ✅ 优化 capabilities 配置

---

## 🔍 请帮我检查以下信息

### 1. 浏览器开发者工具检查

**步骤**:
1. 打开 Employee Portal
2. 按 F12 打开开发者工具
3. 切换到 "Network" 标签页
4. 点击 "Add PSP" 添加 Adyen
5. 填写信息并保存

**请回答**:
- 是否看到对 `/admin/psp/connect` 的 POST 请求？
- 请求的 Status Code 是多少？（200? 401? 500?）
- Response 内容是什么？
- Request Payload 是什么？

### 2. Railway 日志检查

**在 Railway Dashboard 中**:
1. 点击 backend 服务
2. 查看 "Deployments" 标签
3. 点击最新的部署
4. 查看 "Logs"

**搜索关键词**:
```
💾 Saving
✅ PSP saved successfully
✅ Verified in DB
✅ Transaction committed
❌ Failed to save PSP
```

**请回答**:
- 是否看到 "💾 Saving adyen PSP" 日志？
- 是否看到 "✅ Transaction committed" 日志？
- 是否有任何错误或异常？

### 3. Vercel 部署检查

**访问**: https://vercel.com (您的 Employee Portal)

**检查**:
- 最新部署的commit hash 是否是 `98a3ab0`？
- 部署状态是否是 "Ready"？
- 是否需要手动触发重新部署？

---

## 🧪 手动验证脚本

### 检查特定 PSP 是否在数据库

由于我们无法直接访问数据库，请在 Railway 的 PostgreSQL 插件中运行：

```sql
-- 查看所有 PSP 配置
SELECT 
    psp_id,
    merchant_id, 
    provider, 
    LENGTH(api_key) as api_key_len,
    account_id,
    CASE WHEN secret_key IS NOT NULL THEN 'YES' ELSE 'NO' END as has_secret,
    status,
    connected_at
FROM merchant_psps 
WHERE merchant_id = 'merch_208139f7600dbf42'
ORDER BY connected_at DESC;
```

**预期结果**（如果配置成功）:
```
provider  | api_key_len | account_id | has_secret | status
----------|-------------|------------|------------|--------
stripe    | 107         | acct_...   | NO         | active
adyen     | 25          | TestMerc.. | NO         | active
checkout  | 35          | pc_...     | NO         | active
paypal    | 80          | NULL       | YES        | active
```

---

## 🔧 临时解决方案

如果配置一直无法保存，我们可以**直接在数据库中插入记录**：

```sql
-- Adyen
INSERT INTO merchant_psps 
(psp_id, merchant_id, provider, name, api_key, account_id, capabilities, status, connected_at)
VALUES 
('psp_adyen_test123', 'merch_208139f7600dbf42', 'adyen', 'Adyen Account', 
 '您的Adyen API Key', 'TestMerchant', 'payments,refunds,payouts', 'active', NOW());

-- Checkout
INSERT INTO merchant_psps 
(psp_id, merchant_id, provider, name, api_key, account_id, capabilities, status, connected_at)
VALUES 
('psp_checkout_test123', 'merch_208139f7600dbf42', 'checkout', 'Checkout Account', 
 'sk_sbox_...', 'pc_...', 'payments,refunds', 'active', NOW());

-- PayPal  
INSERT INTO merchant_psps 
(psp_id, merchant_id, provider, name, api_key, account_id, secret_key, capabilities, status, connected_at)
VALUES 
('psp_paypal_test123', 'merch_208139f7600dbf42', 'paypal', 'PayPal Account', 
 '您的Client ID', NULL, '您的Client Secret', 'payments,refunds,payouts', 'active', NOW());
```

---

## 🚨 关键问题

我怀疑问题可能在于：

### 可能性 1: Employee Portal 缓存
- Vercel 的 Edge Network 可能缓存了旧版本
- 前端代码可能没有更新

**解决**: 
- 硬刷新页面（Ctrl+Shift+R）
- 或清除浏览器缓存

### 可能性 2: 认证问题
- API 调用可能因为认证失败而没有执行
- 但前端没有正确处理错误

**检查**: 
- 浏览器 Network 标签中的 Status Code
- 是否是 401 或 403？

### 可能性 3: CORS 问题
- 跨域请求被阻止
- 但 Stripe 能工作说明 CORS 应该是正常的

---

## 📋 下一步行动

请按优先级执行：

### 优先级 1: 检查浏览器 Network 标签
**最重要**！这能直接告诉我们 API 是否被调用，以及返回了什么。

### 优先级 2: 检查 Railway 日志
看看是否有保存尝试的日志

### 优先级 3: 直接在数据库插入
如果前两步都无法解决，可以手动插入测试

请告诉我您在 Network 标签页中看到了什么！



