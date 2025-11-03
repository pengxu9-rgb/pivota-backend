# PSP 配置数据流完整链路分析

## 📊 涉及的数据库表

### 核心表：`merchant_psps`

```sql
CREATE TABLE merchant_psps (
    psp_id TEXT PRIMARY KEY,
    merchant_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    name TEXT,
    api_key TEXT,
    account_id TEXT,
    secret_key TEXT,  -- For PayPal
    capabilities TEXT,
    status TEXT,
    connected_at TIMESTAMP
);
```

**这是唯一存储 PSP 配置的表！**

---

## 🔄 完整数据流链路

### 链路 1: Employee Portal 配置保存

```
前端 (Employee Portal)
  ↓
pivota-employee-portal/app/dashboard/psps/page.tsx
  Line 318: await employeeApi.connectPSPAdmin(data)
  ↓
pivota-employee-portal/lib/api-client.ts  
  Line 282: this.client.post('/admin/psp/connect', data)
  ↓
后端 API: POST /admin/psp/connect
  ↓
pivota_infra/routes/admin_api.py
  Line 267: async def admin_connect_psp(...)
  ↓
数据库操作:
  Line 329: async with database.transaction():
  Line 368: await database.execute(query_str, params)
  ↓
写入表: merchant_psps
```

### 链路 2: 订单创建时读取 PSP

```
测试脚本/Agent API
  ↓
POST /agent/v1/orders/create
  ↓
pivota_infra/routes/agent_api.py
  Line 442: await create_new_order(...)
  ↓
pivota_infra/routes/order_routes.py
  Line 264-271: 查询 merchant_psps 表
  
  SQL查询:
  SELECT api_key, account_id, secret_key 
  FROM merchant_psps
  WHERE merchant_id = :merchant_id 
    AND provider = :provider 
    AND status = 'active'
  ORDER BY connected_at DESC
  LIMIT 1
  ↓
如果找到: 使用 PSP 创建 payment intent
如果未找到: 返回 null
```

### 链路 3: Employee Portal 加载 PSP 列表

```
前端 (Employee Portal)
  ↓
pivota-employee-portal/app/dashboard/psps/page.tsx
  Line 29: await employeeApi.getAllPSPs()
  ↓
pivota-employee-portal/lib/api-client.ts
  Line 258: this.client.get('/psps/all')
  ↓
后端 API: GET /psps/all
  ↓
pivota_infra/routes/employee_dashboard_routes.py
  Line 310: async def get_all_psps(...)
  Line 319-327: SQL查询
  
  SQL查询:
  SELECT p.psp_id, p.provider, p.name, p.status, p.merchant_id,
         p.connected_at, p.capabilities,
         p.api_key, p.account_id, p.secret_key,
         m.business_name as merchant_name
  FROM merchant_psps p
  LEFT JOIN merchant_onboarding m ON p.merchant_id = m.merchant_id
  ORDER BY p.connected_at DESC
  ↓
返回 PSP 列表到前端
```

---

## 🔍 关键发现

### ✅ 所有端点都操作同一个表

- **写入**: `admin_api.py` → `merchant_psps` 表
- **读取（订单）**: `order_routes.py` → `merchant_psps` 表  
- **读取（列表）**: `employee_dashboard_routes.py` → `merchant_psps` 表

**结论**: 表是正确的，不存在"调用了错误的表"的问题。

---

## 🐛 可能的问题点

### 问题点 1: 保存端点有两个！

**发现**: 系统中有**多个 PSP 保存端点**：

1. **`/admin/psp/connect`** (admin_api.py Line 267)
   - Employee Portal 应该调用这个
   - 使用事务 ✅
   - 有详细日志 ✅

2. **`/merchant/integrations/psp/connect`** (merchant_api_extensions.py Line 182)
   - Merchant Portal 调用这个
   - 使用事务 ✅
   - 有日志 ✅

3. **`/merchant/onboarding/setup-psp`** (employee_store_psp_fixes.py Line 332)
   - 也是 Employee 端点
   - **没有使用事务！** ❌

**可能**: Employee Portal 可能调用了错误的端点？

让我检查 employeeApi.connectPSPAdmin 实际调用的是哪个：

已确认：Line 282 调用 `/admin/psp/connect` ✅ 正确

### 问题点 2: 读取时的 SQL 查询条件

**保存时**: 没有特殊条件，直接 INSERT

**读取时（订单创建）**: 
```sql
WHERE merchant_id = :merchant_id 
  AND provider = :provider 
  AND status = 'active'  ← 这个条件！
```

**可能问题**: 如果保存时 status 不是 'active'，读取时就找不到！

让我检查保存时 status 的值：
- Line 342 (admin_api.py): `"status": "active"` ✅ 正确

### 问题点 3: 事务回滚

**可能**: 保存过程中抛出异常，事务被回滚

**检查点**：
- Line 381-383: 如果验证失败，会 `raise Exception`
- 这会导致事务回滚！

```python
if verify:
    logger.info(...)
else:
    logger.error(...)
    raise Exception("PSP not found after insert")  ← 这里！
```

**如果验证查询失败（即使 INSERT 成功），也会回滚！**

---

## 🎯 诊断建议

### 请在 Railway 日志中搜索：

1. **保存尝试日志**:
```
💾 Saving adyen PSP
💾 Saving paypal PSP
```

2. **执行日志**:
```
Executing UPSERT query in transaction
✅ PSP UPSERT executed
```

3. **验证日志**:
```
✅ Verified in DB (in transaction)
✅ Transaction committed
```

4. **错误日志**:
```
❌ Failed to verify PSP
❌ Failed to save PSP
PSP not found after insert
```

### 如果日志中有 "PSP not found after insert"

说明：
- INSERT 执行了
- 但验证查询失败
- 导致抛出异常
- 事务回滚
- 数据没有保存

**解决方案**: 移除验证失败时的 raise Exception

---

## 🛠️ 建议的修复

### Option 1: 移除验证失败时的异常

```python
if verify:
    logger.info(...)
else:
    logger.error(...)
    # 不要 raise，只记录警告
    # raise Exception("PSP not found after insert")
```

### Option 2: 改进验证逻辑

可能验证查询本身有问题（比如 psp_id 不匹配）

---

## 📋 请提供以下信息

1. **Railway 日志**: 搜索 "adyen" 或 "paypal"，复制相关日志
2. **Employee Portal**: PSP 列表中现在能看到 Adyen 和 PayPal 吗？
3. **浏览器 Console**: 配置时的完整日志

这些信息能帮我准确定位问题！


