# Admin API 完整性检查报告

## 📄 文件：`pivota_infra/routes/admin_api.py`

---

## ✅ 已修复的问题

### 1. **SQL 语法错误**（第 340-350 行）
**问题**：
```python
# 错误的生成方式
VALUES ({', '.join(':' + col if col != 'status' else "'active'" for col in base_cols)})
```
会生成类似：`:psp_id, :merchant_id, ..., 'active', ...`（混合了参数和字面值）

**修复**：
```python
# 正确的方式
base_vals = [":psp_id", ":merchant_id", ..., ":status", ...]
params["status"] = "active"
VALUES ({', '.join(base_vals)})
```

### 2. **缺少 logger**（第 14-16 行）
**问题**：使用了 `logger.info()` 但没有导入

**修复**：
```python
import logging
logger = logging.getLogger(__name__)
```

### 3. **添加了详细日志**（第 311-365 行）
- 记录 API key 长度
- 记录是否有 secret_key
- 执行后验证数据是否真的保存

---

## 🔍 逻辑完整性检查

### `admin_connect_psp` 函数（第 263-378 行）

#### ✅ 输入验证
```python
Line 277-278: ✅ 验证 provider 在支持列表中
Line 279-280: ✅ 验证 api_key 长度 >= 8
Line 283-284: ✅ PayPal 特殊验证（需要 secret_key）
Line 298-299: ✅ 验证 merchant_id 存在
```

#### ✅ PSP ID 处理
```python
Line 287-296: ✅ 如果有 psp_id 但无 merchant_id，从数据库获取
Line 302-303: ✅ 如果没有 psp_id，生成新的
```

#### ✅ 数据库操作
```python
Line 316-351: ✅ 动态构建 INSERT/UPDATE 查询
              ✅ 支持 secret_key（PayPal）
              ✅ 使用 UPSERT（ON CONFLICT）
Line 357-365: ✅ 验证保存结果
Line 368-375: ✅ 更新 merchant_onboarding 标志
```

#### ✅ 错误处理
```python
Line 377-378: ✅ 捕获异常并返回 HTTP 500
```

---

## ⚠️ 潜在改进点

### 1. **Capabilities 逻辑不够完整**（第 305-308 行）
```python
capabilities = payload.get("capabilities") or (
    ["payments", "refunds", "payouts"] if provider == "stripe" else
    ["payments", "refunds"]
)
```

**问题**：
- PayPal 也支持 payouts，但被归类为 `["payments", "refunds"]`
- Checkout 可能也有不同的 capabilities

**建议**：
```python
DEFAULT_CAPABILITIES = {
    "stripe": ["payments", "refunds", "payouts", "subscriptions"],
    "adyen": ["payments", "refunds", "payouts"],
    "checkout": ["payments", "refunds"],
    "paypal": ["payments", "refunds", "payouts"]
}
capabilities = payload.get("capabilities") or DEFAULT_CAPABILITIES.get(provider, ["payments"])
```

### 2. **Account ID 可选性不明确**（第 272, 325 行）
```python
account_id = payload.get("account_id")  # 可能是 None
...
"account_id": account_id or None,  # 多余的 or None
```

**对于不同 PSP**：
- Checkout: `account_id` 是**必需的**（processing_channel_id）
- Adyen: `account_id` 可能是 merchant_account
- Stripe: `account_id` 可选
- PayPal: `account_id` 可选

**建议**：
```python
# 验证 Checkout 必需的 account_id
if provider == "checkout" and not account_id:
    raise HTTPException(400, "Checkout.com requires processing_channel_id in account_id field")
```

### 3. **错误信息可以更详细**（第 378 行）
```python
raise HTTPException(status_code=500, detail=f"Failed to connect PSP: {e}")
```

**建议**：
```python
import traceback
logger.error(f"Failed to save PSP: {e}", exc_info=True)
raise HTTPException(
    status_code=500, 
    detail={
        "error": "PSP_SAVE_FAILED",
        "message": f"Failed to connect {provider}",
        "details": str(e),
        "psp_id": psp_id if psp_id else None
    }
)
```

---

## 🔄 依赖的其他端点

### 相关端点检查：

#### 1. `/admin/psp/{psp_id}/test`（第 380-412 行）
✅ **正常** - 测试 PSP 连接

**潜在问题**：
- 只检查环境变量配置的 PSP（`get_configured_psps()`）
- 不会检查数据库中的 PSP

**建议**：
```python
# 应该先查询数据库
psp_record = await database.fetch_one(
    "SELECT * FROM merchant_psps WHERE psp_id = :psp_id",
    {"psp_id": psp_id}
)
if psp_record:
    # 使用数据库中的 API key 进行测试
    ...
```

#### 2. `/admin/psp/{psp_id}/toggle`（第 414-430 行）
⚠️ **问题** - 只更新内存，不更新数据库

```python
# 当前代码：只返回成功，但没有真正更新
return {"status": "success", ...}
```

**建议**：
```python
await database.execute(
    "UPDATE merchant_psps SET status = :status WHERE psp_id = :psp_id",
    {"status": "active" if enable else "inactive", "psp_id": psp_id}
)
```

---

## 🔐 安全性检查

### ✅ 认证
- 所有端点都使用 `Depends(require_admin)`
- 只有 admin 角色可以访问

### ✅ 输入验证
- Provider 白名单验证
- API key 长度验证
- PayPal secret_key 验证

### ⚠️ SQL 注入风险
```python
# 使用参数化查询 - 安全
await database.execute(query_str, params)
```
✅ **安全** - 所有查询都使用参数化

### ⚠️ 敏感信息泄露
```python
# Line 363: 日志中记录 API key 长度（安全）
logger.info(f"api_key_len={len(verify['api_key'])}")
```
✅ **安全** - 不记录完整 API key

---

## 📊 完整性评分

| 方面 | 评分 | 说明 |
|-----|------|------|
| 输入验证 | 9/10 | ✅ 大部分验证完整，Checkout account_id 可加强 |
| 错误处理 | 7/10 | ✅ 有异常捕获，但错误信息可以更详细 |
| 数据库操作 | 9/10 | ✅ 使用 UPSERT，有验证查询 |
| 日志记录 | 8/10 | ✅ 关键步骤有日志，可以更详细 |
| 安全性 | 9/10 | ✅ 认证、参数化查询都正确 |
| 代码质量 | 8/10 | ✅ 逻辑清晰，但有改进空间 |

**总体评分**: 8.3/10 ✅

---

## 🎯 建议的改进（优先级排序）

### 高优先级 🔥
1. ✅ **已修复**：SQL VALUES 语法错误
2. ✅ **已修复**：添加 logger 导入
3. ✅ **已修复**：添加保存后验证

### 中优先级 📝
4. 为 Checkout 添加 account_id 必需验证
5. 改进 capabilities 的 PSP 特定配置
6. 修复 toggle endpoint 实际更新数据库

### 低优先级 💡
7. 更详细的错误响应
8. PSP test endpoint 支持数据库配置的 PSP
9. 添加 API key 验证（实际调用 PSP API 检查有效性）

---

## 📝 其他发现

### 未使用的导入
```python
Line 6: from fastapi import status  # 未使用
```

### 硬编码值
```python
Line 27: "adyen_merchant_account": settings.adyen_merchant_account  # 默认值
Line 404: "response_time": 145  # 硬编码
Line 649: "active_agents": 1  # TODO注释说明需要从数据库获取
```

---

## ✅ 结论

文件整体质量良好，主要的 SQL 语法错误已修复。当前的实现：

1. ✅ **核心功能正确**：PSP 连接/更新逻辑完整
2. ✅ **安全性良好**：认证和 SQL 参数化都正确
3. ✅ **错误处理存在**：有异常捕获和日志
4. ⚠️ **有改进空间**：错误信息、验证逻辑可以更完善

**可以安全使用**，建议后续迭代时处理中低优先级的改进点。

---

**检查时间**: 2025-10-26 05:10 UTC  
**文件版本**: 264fc6ef  
**检查者**: AI Assistant


