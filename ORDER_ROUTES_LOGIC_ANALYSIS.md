# Order Routes 逻辑分析报告

## 📋 文件：`pivota_infra/routes/order_routes.py`

---

## ✅ 正确的逻辑

### 1. PSP 类型确定（第 235-254 行）
**流程：**
```
preferred_psp → merchant.psp_type → DB merchant_psps → 默认 "stripe"
```
✅ **正确**：优先级清晰，有合理的回退机制

### 2. PSP Key 查找（第 261-321 行）
**流程：**
```
1. DB merchant_psps 表查询
2. Stripe 特殊处理：merchant 表查询
3. 环境变量（已验证长度 > 10）
4. Mock key 分配（adyen/checkout）
```
✅ **正确**：多层回退机制，确保总能获得 key

### 3. Payment Intent 创建（第 322-381 行）
```python
if psp_key:
    adapter = get_psp_adapter(psp_type, psp_key, **kwargs)
    success, payment_intent, error = await adapter.create_payment_intent(...)
    if success and payment_intent:
        payment_intent_id = payment_intent.id
        client_secret = payment_intent.client_secret
        await update_payment_info(...)
    else:
        logger.error(f"Payment intent creation failed: {error}")
```
✅ **正确**：有成功/失败处理

---

## ⚠️  潜在问题

### 问题 1: 异常被全局捕获（第 382-389 行）
```python
except Exception as e:
    logger.error(f"Payment intent creation error: {e}")
    # payment_intent_id 和 client_secret 保持为 None!
```

**影响**：
- 如果适配器抛出**任何异常**，都会被捕获
- 订单创建继续进行，但 payment 信息为 null
- 用户看不到真正的错误原因

**示例**：
- Checkout 适配器之前抛出 `'Settings' object has no attribute 'checkout_mode'`
- 异常被捕获，日志记录，但订单照常返回（payment 为 null）

**建议修复**：
1. 在日志中明确标注异常类型
2. 或者在响应中包含错误信息
3. 或者对于致命错误，应该让订单创建失败

### 问题 2: 环境变量可能导致的边界情况

**场景**：如果 Railway 设置了：
```bash
ADYEN_API_KEY=""  # 空字符串
```

**修复前**：
```python
psp_key = getattr(settings, "adyen_api_key", None)
if psp_key:  # 空字符串为 truthy!
    logger.info(f"Using Adyen key from environment")
```
❌ **会使用空字符串，不会进入 mock key 逻辑**

**修复后**（第 295-302 行）：
```python
env_key = getattr(settings, "adyen_api_key", None)
if env_key and len(env_key) > 10:  # 验证不为空
    psp_key = env_key
```
✅ **已修复**

---

## 🔍 当前问题诊断

### 现象：Adyen 和 Checkout 返回 null

**已排除的原因**：
- ✅ 适配器本身工作正常（本地测试通过）
- ✅ Mock key 分配逻辑正确
- ✅ 环境变量验证已添加

**可能的原因**：

#### 1. 部署问题 ⏳
- Railway 可能还在使用旧版本代码
- 或者有缓存问题

**验证方法**：
```bash
# 检查部署的代码版本
curl https://web-production-fedb.up.railway.app/ | jq .version
```

#### 2. 数据库中有无效的 PSP 记录 🔍
**可能情况**：
- `merchant_psps` 表中有 adyen 记录
- 但 `api_key` 字段为空或无效
- 导致跳过 mock key 分配

**查询**：
```sql
SELECT provider, api_key, LENGTH(api_key) as key_len
FROM merchant_psps 
WHERE merchant_id = 'merch_208139f7600dbf42' 
  AND provider IN ('adyen', 'checkout');
```

#### 3. 适配器异常被捕获 🐛
- 即使我们修复了 `checkout_mode`，可能还有其他异常
- 异常在第 382 行被捕获，但不影响订单创建

**验证方法**：
- 查看 Railway 日志中的错误信息
- 搜索 "Payment intent creation error"

#### 4. 更新未生效 ⚡
- Git commit 已推送
- 但 Railway 可能：
  - 构建失败（未通知）
  - 使用了缓存的层
  - 需要手动重启

---

## 🛠️ 建议的修复

### 立即行动：

1. **添加更详细的错误响应**
```python
# 在 OrderResponse 中添加
error_message: Optional[str] = None

# 在异常处理中
except Exception as e:
    logger.error(f"Payment intent creation error: {e}")
    error_message = str(e)  # 保存错误信息

return OrderResponse(
    ...
    error_message=error_message if not payment_intent_id else None
)
```

2. **改进异常处理**
```python
except Exception as e:
    logger.error(f"Payment intent creation error: {e}")
    logger.error(f"Stack trace:", exc_info=True)  # 完整堆栈
    # 对于关键 PSP，可能应该让订单失败
    if psp_type in ["stripe"] and not psp_key.startswith("sk_mock"):
        raise HTTPException(
            status_code=500,
            detail=f"Payment system error: {str(e)}"
        )
```

3. **添加健康检查端点**
```python
@router.get("/health/psp-adapters")
async def check_psp_adapters():
    """Test all PSP adapters"""
    results = {}
    for psp in ["stripe", "adyen", "checkout"]:
        try:
            adapter = get_psp_adapter(psp, f"sk_mock_{psp}")
            results[psp] = "OK"
        except Exception as e:
            results[psp] = str(e)
    return results
```

---

## 📊 总结

### 逻辑矛盾：
- **无明显逻辑矛盾**
- 代码流程是正确的

### 主要问题：
1. ⚠️ **异常被静默捕获** - 导致很难调试
2. ✅ **环境变量验证** - 已修复
3. 🔍 **部署/数据库状态** - 需要验证

### 建议优先级：
1. 🔥 **高**：检查 Railway 日志中的异常
2. 🔥 **高**：检查数据库中的 PSP 记录
3. 🔥 **高**：验证部署的代码版本
4. 📝 **中**：改进错误处理和日志
5. 📝 **低**：添加健康检查端点

---

**更新时间**：2025-10-26 04:52 UTC
**最后提交**：89d9dc79 - fix: validate environment PSP keys before using them

