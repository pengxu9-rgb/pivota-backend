# PSP 配置未保存问题诊断

## 🔍 问题现象

**症状**: 
- 前端显示 "✅ PSP connected" 成功提示
- 但数据库中没有对应的记录
- 订单创建时 payment_intent_id 和 client_secret 为 null

**影响的 PSP**:
- ❌ Adyen
- ❌ Checkout
- ❌ PayPal

**正常的 PSP**:
- ✅ Stripe（通过环境变量配置）

---

## 🛠️ 已实施的修复

### 1. SQL 语法错误（提交 264fc6ef）
```python
# 修复前
VALUES ({', '.join(':' + col if col != 'status' else "'active'" for col in base_cols)})
# 会生成错误的 SQL

# 修复后  
base_vals = [":psp_id", ":merchant_id", ..., ":status", ...]
VALUES ({', '.join(base_vals)})
```

### 2. Row 对象访问错误（提交 fe1def1c）
```python
# 修复前
verify.get('secret_key')  # Row 对象没有 .get() 方法

# 修复后
verify_dict = dict(verify)
verify_dict.get('secret_key')
```

### 3. 添加详细日志（提交 fe1def1c）
```python
logger.info(f"💾 Saving {provider} PSP for merchant {merchant_id}")
logger.info(f"✅ PSP saved successfully: {psp_id}")
logger.info(f"✅ Verified in DB: ...")
```

### 4. PayPal Client Secret 字段（提交 98a3ab0）
- Employee Portal Configure 表单现在包含 Client Secret 字段

---

## 🔍 可能的原因

### 原因 1: 部署延迟 ⏳
**症状**: 代码已推送但 Railway 还在使用旧版本

**检查方法**:
1. 访问 Railway Dashboard
2. 查看 Deployments 标签页
3. 确认最新的提交 hash 是否是 `fe1def1c`

**解决方案**:
- 等待 Railway 完成部署（可能需要 3-5 分钟）
- 或手动触发重新部署

### 原因 2: 数据库事务问题 💾
**症状**: INSERT 执行了但没有 COMMIT

**可能原因**:
- 数据库使用了事务但没有提交
- Railway 的数据库连接配置问题

**检查**:
```python
# 在 admin_api.py 第 366 行
await database.execute(query_str, params)
# 是否真的提交了？
```

### 原因 3: 错误被静默捕获 🐛
**症状**: 异常被 catch 但前端仍返回成功

**检查 Railway 日志**:
搜索关键词：
- "❌ Failed to save PSP"
- "Failed to connect PSP"
- "Exception"
- "Error"

### 原因 4: 前端发送了错误的数据 📡
**症状**: API 调用的 payload 不正确

**验证**:
打开浏览器开发者工具（F12）→ Network 标签页
查看 `/admin/psp/connect` 请求的：
- Request Payload
- Response

---

## 🧪 验证步骤

### Step 1: 确认后端已部署最新代码

```bash
# 检查提交 hash
cd pivota_infra
git log --oneline -1
# 应该显示: fe1def1c fix: handle Row object properly...
```

### Step 2: 等待足够长的时间

```bash
# Railway 部署通常需要 2-5 分钟
# 建议等待 5 分钟后再尝试
sleep 300
```

### Step 3: 清除浏览器缓存

- 刷新 Employee Portal 页面（Ctrl+Shift+R 或 Cmd+Shift+R）
- 确保使用最新的前端代码

### Step 4: 重新配置一个 PSP

使用以下测试数据（Adyen）:
```
Provider: adyen
Merchant ID: merch_208139f7600dbf42
API Key: test_key_12345678  # 至少 8 字符
Account ID: TestMerchant
```

观察：
- 浏览器控制台是否有错误？
- Network 标签页中 API 调用是否成功？
- 返回的响应是什么？

### Step 5: 立即测试

配置后立即运行：
```bash
bash check_merchant_psps.sh
```

如果仍然显示 NULL，说明保存失败。

---

## 📝 临时解决方案：直接使用环境变量

如果配置一直无法保存，可以临时在 Railway 中设置环境变量：

```bash
# Railway Environment Variables
ADYEN_API_KEY=your_adyen_test_key
CHECKOUT_API_KEY=your_checkout_key  # 需要添加到 settings.py
PAYPAL_CLIENT_ID=your_client_id     # 需要添加到 settings.py
PAYPAL_CLIENT_SECRET=your_secret    # 需要添加到 settings.py
```

但这不是长期方案，我们需要修复数据库保存功能。

---

## 🚨 下一步调试

如果重新配置后仍然失败：

1. **查看 Railway 日志**
   - 在 Railway Dashboard 中查看实时日志
   - 搜索 "💾 Saving" 和错误信息

2. **提供以下信息**:
   - 浏览器控制台的完整错误（如果有）
   - Network 标签页中的 API 响应
   - 配置时使用的具体值（隐藏敏感信息）

3. **验证数据库连接**:
   - Railway 数据库是否正常运行
   - 是否有连接限制

---

**创建时间**: 2025-10-26 07:20 UTC  
**最新修复**: fe1def1c

