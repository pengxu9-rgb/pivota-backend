# Platform Token/Credentials Format Reference

## 📋 Token 存储和解析规范

| 平台 | 存储格式 | 解析方法 | API 使用 | 状态 |
|------|----------|----------|---------|------|
| **Shopify** | JSON: `{"access_token":"shpat_..."}` | 解析 JSON → 提取 `access_token` | Header: `X-Shopify-Access-Token` | ✅ 已修复 |
| **Wix** | Plain: `Bearer xxx` 或 API key | 直接使用 | Header: `Authorization` | ✅ 正确 |
| **WooCommerce** | Colon-separated: `key:secret` | Split by `:` | Basic Auth: `(key, secret)` | ✅ 正确 |
| **BigCommerce** | Plain: token | 直接使用 | Header: `X-Auth-Token` | ✅ 正确 |
| **PrestaShop** | Plain: API key | 直接使用 | Query param: `?ws_key=` | ⚠️ 未实现详细测试 |
| **Square** | Plain: Bearer token | 直接使用 | Header: `Authorization: Bearer` | ⚠️ 未实现详细测试 |
| **Magento** | Plain: token | 直接使用 | Header: `Authorization: Bearer` | ⚠️ 未实现详细测试 |

---

## 🔍 检查结果

### ✅ 无需修改的平台

#### Wix
- **存储**: Plain text API key
- **第一个函数**: 直接用 `api_key`
- **第二个函数**: 直接用 `api_key`
- **一致性**: ✅ 两个函数完全一致

#### WooCommerce
- **存储**: `consumer_key:consumer_secret`
- **第一个函数**: `api_key.split(":", 1)` → 解析
- **第二个函数**: `api_key.split(":", 1)` → 解析
- **一致性**: ✅ 两个函数完全一致

#### BigCommerce
- **存储**: Plain text token
- **第一个函数**: 直接用 `api_key`
- **第二个函数**: 直接用 `api_key`
- **一致性**: ✅ 两个函数完全一致

---

## 🐛 已修复的问题

### Shopify（已修复）
- **问题**: 第二个函数缺少 JSON 解析
- **影响**: 发送 `{"access_token":"..."}` 给 Shopify → 401
- **修复**: 添加 JSON 解析逻辑
- **状态**: ✅ 已修复

---

## 📝 代码模式

### Pattern 1: Plain Text Token
```python
# 直接使用，无需解析
headers={"X-Auth-Token": api_key}
```
**适用**: Wix, BigCommerce, PrestaShop, Square, Magento

### Pattern 2: JSON Wrapped Token
```python
# 需要解析
token = api_key
if api_key.startswith("{"):
    parsed = json.loads(api_key)
    token = parsed.get("access_token")
headers={"X-Shopify-Access-Token": token}
```
**适用**: Shopify

### Pattern 3: Colon-Separated Credentials
```python
# 需要 split
creds = api_key.split(":", 1)
consumer_key, consumer_secret = creds[0], creds[1]
auth=(consumer_key, consumer_secret)
```
**适用**: WooCommerce

---

## 🎯 建议

### 对于新平台
在添加新平台支持时，确保**两个测试函数**使用相同的 token 解析逻辑：
1. `_test_single_store_api` (line 168)
2. `_test_single_store_api` (line 551) - 用于聚合测试

### Token 存储标准化
建议统一为 JSON 格式存储所有平台的 credentials：
```json
{
  "shopify": {"access_token": "shpat_..."},
  "wix": {"api_key": "..."},
  "woocommerce": {"consumer_key": "...", "consumer_secret": "..."},
  "bigcommerce": {"access_token": "..."}
}
```

这样：
- ✅ 统一的解析逻辑
- ✅ 支持多字段 credentials
- ✅ 易于扩展
- ✅ 类型安全

---

## 🔧 当前实现状态

### 完全实现的平台
- ✅ Shopify (已修复 JSON 解析)
- ✅ Wix
- ✅ WooCommerce
- ✅ BigCommerce

### 基础支持的平台
- ⚠️ PrestaShop (generic fallback)
- ⚠️ Square (generic fallback)
- ⚠️ Magento (generic fallback)

**Generic fallback**: 不执行实际 API 测试，直接标记为 "success"


