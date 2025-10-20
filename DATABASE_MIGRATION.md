# 🔄 从演示数据迁移到真实数据库

## ✅ 重大改进完成

### 之前的问题
- ❌ 使用硬编码的 DEMO 数据
- ❌ 商户ID固定为 `merch_208139f7600dbf42`（不存在于数据库）
- ❌ 新添加的商店/PSP存储在内存中，重启后丢失
- ❌ 无法支持多个真实商户

### 现在的解决方案
- ✅ 使用真实的 SQLite 数据库
- ✅ `merchant@test.com` 映射到真实商户 `merch_6b90dc9838d5fd9c`
- ✅ 商店和PSP持久化存储在数据库表中
- ✅ 支持生产环境的多商户

---

## 📊 数据库变更

### 新建表

#### 1. `merchant_stores` 表
```sql
CREATE TABLE merchant_stores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id TEXT UNIQUE NOT NULL,
    merchant_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    name TEXT NOT NULL,
    domain TEXT NOT NULL,
    api_key TEXT,
    status TEXT DEFAULT 'connected',
    connected_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_sync DATETIME,
    product_count INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

#### 2. `merchant_psps` 表
```sql
CREATE TABLE merchant_psps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    psp_id TEXT UNIQUE NOT NULL,
    merchant_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    name TEXT NOT NULL,
    api_key TEXT,
    account_id TEXT,
    status TEXT DEFAULT 'active',
    connected_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    capabilities TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### 初始数据

已为 `merch_6b90dc9838d5fd9c` 插入真实数据：

**商店**:
- ✅ Shopify: `chydantest.myshopify.com` (4 products)

**PSP**:
- ✅ Stripe: Stripe Account (已连接)

---

## 🔧 代码变更

### 后端更改

#### 1. `routes/auth_routes.py`
- ✅ `merchant@test.com` 账号包含真实的 `merchant_id: merch_6b90dc9838d5fd9c`
- ✅ Login 响应包含 `merchant_id` 字段
- ✅ JWT token 包含 `merchant_id` claim

#### 2. `routes/merchant_dashboard_routes.py`
- ✅ 添加数据库连接函数
- ✅ `GET /merchant/{merchant_id}/integrations` 从数据库读取商店
- ✅ `GET /merchant/{merchant_id}/psps` 从数据库读取PSP
- ✅ 移除 DEMO_MERCHANT_DATA 依赖（保留用于向后兼容）

#### 3. `routes/merchant_api_extensions.py`
- ✅ `get_merchant_id_from_user` 从 JWT token 读取真实 merchant_id
- ✅ `POST /merchant/integrations/store/connect` 写入数据库
- ✅ `POST /merchant/integrations/psp/connect` 写入数据库

### 前端更改

#### 1. `merchant-portal/app/login/page.tsx`
- ✅ 从登录响应中存储 `merchant_id`
- ✅ 不再硬编码 merchant_id

---

## 🧪 测试步骤（部署后）

### 1. 等待部署完成
- ⏳ Railway 后端部署：2-3 分钟
- ⏳ Vercel 前端部署：2-3 分钟

### 2. 清除浏览器数据
```
重要！清除旧的 localStorage 数据：
1. 打开开发者工具 (F12)
2. Application → Local Storage
3. 删除所有 merchant_* 相关的键
4. 刷新页面
```

### 3. 重新登录测试

访问：https://merchant.pivota.cc/login

```
Email: merchant@test.com
Password: Admin123!
```

**验证登录响应包含 merchant_id**:
- 打开 Network 标签
- 查看 `/auth/signin` 响应
- 应该包含 `"merchant_id": "merch_6b90dc9838d5fd9c"`

### 4. 测试集成页面

#### 查看现有商店和PSP
- 应该能看到：
  - ✅ Shopify: chydantest.myshopify.com (4 products)
  - ✅ Stripe: Stripe Account

#### 添加 Wix 商店
1. 点击 "Connect Store"
2. 选择 Wix
3. 输入信息
4. 点击确认
5. **应该立即在列表中看到新添加的 Wix 商店**

#### 添加 Adyen PSP
1. 点击 "Add PSP"
2. 选择 Adyen
3. 输入 API Key
4. 点击确认
5. **应该立即在列表中看到新添加的 Adyen PSP**

---

## 🎯 预期结果

### Integrations 页面应该显示：

**Stores (2个)**:
1. Shopify - chydantest.myshopify.com (4 products)
2. Wix - [你添加的 Wix 商店]

**PSPs (2个)**:
1. Stripe - Stripe Account
2. Adyen - Adyen Account

---

## 🔍 故障排查

### 如果商店/PSP仍然不显示

1. **确认后端已部署**
```bash
curl -s https://web-production-fedb.up.railway.app/health
```

2. **确认数据库有数据**
```bash
# 在 Railway 上运行
sqlite3 pivota.db "SELECT * FROM merchant_stores;"
sqlite3 pivota.db "SELECT * FROM merchant_psps;"
```

3. **确认 merchant_id 正确**
- 检查 localStorage 中的 `merchant_id`
- 应该是 `merch_6b90dc9838d5fd9c`

4. **清除浏览器缓存**
- Cmd+Shift+R 强制刷新

---

## 📈 系统升级

从：
- ❌ 演示系统（硬编码数据）

到：
- ✅ 生产系统（真实数据库）
- ✅ 支持多商户
- ✅ 数据持久化
- ✅ 可扩展架构

---

**部署时间**: 等待中  
**预计完成**: 5 分钟后



