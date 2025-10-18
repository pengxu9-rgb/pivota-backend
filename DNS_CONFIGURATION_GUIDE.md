# 🌐 Pivota 域名配置指南

## 📋 需要配置的域名

| 域名 | 用途 | 前端项目 | 状态 |
|------|------|----------|------|
| `pivota.cc` | 宣传主页 | lovable-website | ✅ 已部署 |
| `agents.pivota.cc` | Agent 门户 | 待创建 | ⏳ 待配置 |
| `merchants.pivota.cc` | Merchant 门户 | 待创建 | ⏳ 待配置 |
| `employee.pivota.cc` | Employee 门户 | 待创建 | ⏳ 待配置 |

---

## 🎯 Step 1: 获取 Vercel CNAME 记录

### 方法 A：通过 Vercel Dashboard（推荐）

#### 1. 登录 Vercel
访问：https://vercel.com/dashboard

#### 2. 为宣传主页配置域名（pivota.cc）
```
1. 进入项目: lovable-website
2. 点击 "Settings" → "Domains"
3. 添加域名: pivota.cc
4. Vercel 会显示 CNAME 记录，类似：
   
   Type: CNAME
   Name: @  (或留空)
   Value: cname.vercel-dns.com
   
   或者使用 A 记录：
   Type: A
   Name: @
   Value: 76.76.21.21 (Vercel 的 IP)
```

#### 3. 为子域名配置（待前端项目创建后）
当你创建好三个独立的前端项目后，重复上述步骤：

**agents.pivota.cc:**
```
1. 在 Vercel 创建新项目: pivota-agents-portal
2. Settings → Domains → 添加 "agents.pivota.cc"
3. Vercel 会提供 CNAME，例如：
   Type: CNAME
   Name: agents
   Value: cname.vercel-dns.com
```

**merchants.pivota.cc:**
```
1. 在 Vercel 创建新项目: pivota-merchants-portal
2. Settings → Domains → 添加 "merchants.pivota.cc"
3. Vercel 会提供 CNAME
```

**employee.pivota.cc:**
```
1. 在 Vercel 创建新项目: pivota-employee-portal
2. Settings → Domains → 添加 "employee.pivota.cc"
3. Vercel 会提供 CNAME
```

### 方法 B：使用 Vercel CLI

```bash
# 安装 Vercel CLI
npm i -g vercel

# 登录
vercel login

# 为项目添加域名
cd lovable-website
vercel domains add pivota.cc

# Vercel 会返回 DNS 配置信息
```

---

## 🔧 Step 2: 在你的 DNS 提供商配置

假设你在 Cloudflare/阿里云/GoDaddy 等平台管理 DNS：

### 主域名配置 (pivota.cc)

**选项 1：使用 CNAME（推荐）**
```
Type: CNAME
Name: @  或者留空
Value: cname.vercel-dns.com  (从 Vercel 获取的实际值)
TTL: Auto 或 3600
```

**选项 2：使用 A 记录**
```
Type: A
Name: @
Value: 76.76.21.21  (Vercel 的 IP 地址)
TTL: Auto 或 3600
```

### 子域名配置

#### agents.pivota.cc
```
Type: CNAME
Name: agents
Value: [从 Vercel 获取，例如: cname.vercel-dns.com]
TTL: Auto 或 3600
```

#### merchants.pivota.cc
```
Type: CNAME
Name: merchants
Value: [从 Vercel 获取]
TTL: Auto 或 3600
```

#### employee.pivota.cc
```
Type: CNAME
Name: employee
Value: [从 Vercel 获取]
TTL: Auto 或 3600
```

---

## 📝 完整配置示例（Cloudflare 格式）

```
# 主域名
Type    Name        Content                 Proxy  TTL
CNAME   @           cname.vercel-dns.com    ✅     Auto
CNAME   www         cname.vercel-dns.com    ✅     Auto

# 子域名（前端项目分离后配置）
CNAME   agents      cname.vercel-dns.com    ✅     Auto
CNAME   merchants   cname.vercel-dns.com    ✅     Auto
CNAME   employee    cname.vercel-dns.com    ✅     Auto
```

**注意**：
- 如果使用 Cloudflare，建议开启橙色云朵 🟠（Proxy）
- 如果不使用 CDN，可以关闭 Proxy（灰色云朵）

---

## ✅ 验证配置

### 1. 检查 DNS 解析
```bash
# 检查主域名
dig pivota.cc

# 检查子域名
dig agents.pivota.cc
dig merchants.pivota.cc
dig employee.pivota.cc

# 或使用 nslookup
nslookup pivota.cc
```

### 2. 检查 Vercel 状态
在 Vercel Dashboard 的 Domains 页面，应该看到：
```
✅ pivota.cc - Valid Configuration
✅ agents.pivota.cc - Valid Configuration
✅ merchants.pivota.cc - Valid Configuration
✅ employee.pivota.cc - Valid Configuration
```

### 3. 浏览器测试
```
https://pivota.cc - 应该显示宣传页面
https://agents.pivota.cc - 应该显示 Agent 门户
https://merchants.pivota.cc - 应该显示 Merchant 门户
https://employee.pivota.cc - 应该显示 Employee 门户
```

---

## 🚀 快速配置流程

### 立即可配置（已有项目）

**pivota.cc (宣传主页)**
```bash
1. 访问 Vercel Dashboard
2. 选择 "lovable-website" 项目
3. Settings → Domains → Add Domain
4. 输入: pivota.cc
5. 获取 CNAME 记录
6. 在 DNS 提供商处添加记录
7. 等待 DNS 传播（5-30分钟）
```

### 待前端分离后配置

**agents.pivota.cc, merchants.pivota.cc, employee.pivota.cc**
```bash
1. 创建三个新的 Vercel 项目
2. 为每个项目添加对应域名
3. 获取各自的 CNAME 记录
4. 在 DNS 提供商处批量添加
5. 验证配置
```

---

## 📞 常见问题

### Q: DNS 配置后多久生效？
A: 通常 5-30 分钟，最长可能需要 48 小时（全球 DNS 传播）

### Q: Vercel 的 CNAME 记录是什么？
A: 通常是 `cname.vercel-dns.com` 或项目特定的地址，需要在 Vercel Dashboard 中查看

### Q: 可以使用 A 记录代替 CNAME 吗？
A: 可以，Vercel 的 A 记录 IP 地址是 `76.76.21.21`

### Q: 需要 SSL 证书吗？
A: 不需要！Vercel 自动提供免费的 Let's Encrypt SSL 证书

### Q: 如何强制 HTTPS？
A: 在 Vercel 项目设置中，找到 "Force HTTPS" 选项并启用

---

## 📊 当前 Vercel 项目信息

### lovable-website (宣传主页)
```json
{
  "projectId": "prj_EsQX71yXULpRjlOX2I2PoQDnQLcM",
  "orgId": "team_VwhlrpTwgBFno37eBs7eKlIg",
  "projectName": "lovable-website"
}
```

**访问链接**：
- Vercel Dashboard: https://vercel.com/dashboard
- 项目直达: https://vercel.com/[team]/lovable-website

---

## 🎯 下一步行动

1. ✅ **立即配置主域名**
   ```
   - 在 Vercel 添加 pivota.cc
   - 获取 CNAME 记录
   - 在 DNS 提供商配置
   ```

2. ⏳ **等待前端项目分离**
   ```
   - 创建 pivota-agents-portal
   - 创建 pivota-merchants-portal  
   - 创建 pivota-employee-portal
   ```

3. ⏳ **配置子域名**
   ```
   - 为三个新项目添加域名
   - 获取 CNAME 记录
   - 批量配置 DNS
   ```

4. ✅ **验证和测试**
   ```
   - 检查 DNS 解析
   - 测试所有链接
   - 验证 SSL 证书
   ```

---

**准备好后，请告诉我你从 Vercel 获取到的 CNAME 记录，我可以帮你验证配置是否正确！** 🚀
