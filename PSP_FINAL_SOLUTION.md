# PSP 配置问题 - 最终解决方案

## 📊 当前状态

**工作正常的 PSP**: 
- ✅ Stripe (2/4 = 50%)
- ✅ Checkout

**未工作的 PSP**:
- ❌ Adyen  
- ❌ PayPal

**已解决的问题**:
1. ✅ SQL VALUES 语法错误
2. ✅ 数据库事务未提交
3. ✅ Row 对象 .get() 错误
4. ✅ PSP 列表端点错误
5. ✅ PayPal Client Secret UI

---

## 🔍 最终诊断

### Adyen 和 PayPal 无法保存的可能原因：

#### 1. 数据库约束冲突
**可能性**：psp_id 已存在，ON CONFLICT 触发但更新失败

**验证方法**：在 Railway PostgreSQL 运行
```sql
SELECT psp_id, provider, merchant_id, status, connected_at
FROM merchant_psps 
WHERE merchant_id = 'merch_208139f7600dbf42'
ORDER BY connected_at DESC;
```

#### 2. 认证问题
**可能性**：Employee Portal 的 token 无效或过期

**症状**：
- 看到成功提示
- 但实际上 API 返回了 401/403
- 前端没有正确处理

**验证**：查看 Network 标签页中的实际 Status Code

#### 3. 前端缓存
**可能性**：Vercel 边缘缓存导致使用旧代码

**解决**：
- 强制刷新（Cmd+Shift+R）
- 清除浏览器缓存
- 在 Vercel Dashboard 手动触发重新部署

---

## 💡 临时解决方案：手动插入数据库

由于多次尝试都失败了，建议直接在数据库中插入配置：

### 在 Railway > PostgreSQL > Query 运行：

```sql
-- 1. 先查看现有配置
SELECT * FROM merchant_psps WHERE merchant_id = 'merch_208139f7600dbf42';

-- 2. 删除可能存在的失败记录（如果有）
DELETE FROM merchant_psps 
WHERE merchant_id = 'merch_208139f7600dbf42' 
  AND provider IN ('adyen', 'paypal')
  AND (api_key IS NULL OR api_key = '');

-- 3. 插入 Adyen（请替换 YOUR_ADYEN_API_KEY）
INSERT INTO merchant_psps 
(psp_id, merchant_id, provider, name, api_key, account_id, capabilities, status, connected_at)
VALUES 
('psp_adyen_' || extract(epoch from now())::text, 
 'merch_208139f7600dbf42', 
 'adyen', 
 'Adyen Account', 
 'YOUR_ADYEN_API_KEY',  -- 替换这里
 'YOUR_MERCHANT_ACCOUNT',  -- 替换这里，或用 NULL
 'payments,refunds,payouts', 
 'active', 
 NOW());

-- 4. 插入 PayPal（请替换凭据）
INSERT INTO merchant_psps 
(psp_id, merchant_id, provider, name, api_key, secret_key, capabilities, status, connected_at)
VALUES 
('psp_paypal_' || extract(epoch from now())::text,
 'merch_208139f7600dbf42', 
 'paypal', 
 'PayPal Account', 
 'YOUR_PAYPAL_CLIENT_ID',      -- 替换这里
 'YOUR_PAYPAL_CLIENT_SECRET',  -- 替换这里
 'payments,refunds,payouts', 
 'active', 
 NOW());

-- 5. 验证插入
SELECT psp_id, provider, 
       LENGTH(api_key) as key_len,
       CASE WHEN secret_key IS NOT NULL THEN 'YES' ELSE 'NO' END as has_secret,
       status
FROM merchant_psps 
WHERE merchant_id = 'merch_208139f7600dbf42'
ORDER BY connected_at DESC;
```

---

## 🧪 手动插入后的验证步骤

### 1. 立即测试支付

```bash
cd ~/Desktop/Pivota\ Infra/Pivota-cursor-create-project-directory-structure-8344
python3 test_all_psps_complete.py
```

**预期结果**：所有 4 个 PSP 都应该显示 "✅ Status: WORKING"

### 2. 检查 Employee Portal

刷新页面，应该看到 4 个 PSP：
- Stripe
- Adyen
- Checkout
- PayPal

---

## 🎯 根本解决方案

如果手动插入可以工作，说明问题在于 Employee Portal 的配置保存流程。

需要进一步调试：

### 检查 1: 浏览器 Network 标签页

配置时查看 `/admin/psp/connect` 请求：
- **Request Payload**: 确认 merchant_id、api_key、secret_key 都正确
- **Response**: 确认返回 200 和 success
- **实际的 Status Code**: 是否真的是 200？

### 检查 2: Railway 日志

搜索：
```
💾 Saving adyen PSP
💾 Saving paypal PSP
✅ PSP saved successfully
✅ Transaction committed
```

如果没有这些日志，说明 API 根本没被调用。

### 检查 3: Console 日志

配置后应该看到：
```
💾 Saving PSP: {provider: 'adyen', ...}
🔄 [Employee API] POST /admin/psp/connect  
✅ [Employee API] 200 /admin/psp/connect
✅ PSP save response: {...}
```

---

## ⏰ 建议行动

### 立即行动（推荐）：
**手动在数据库中插入** Adyen 和 PayPal 配置，然后测试支付是否工作

### 后续行动：
调试 Employee Portal 的配置保存流程，找出为什么自动保存失败

---

您想：
1. 先手动插入数据库验证支付功能？
2. 还是继续调试 Employee Portal 的配置保存问题？

我建议**先手动插入**，快速验证支付系统是否正常工作！

