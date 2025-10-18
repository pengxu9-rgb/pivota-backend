# 🔄 域名迁移：从 lovable-website 到 pivota-marketing

## 🎯 目标

将 `pivota.cc` 从旧的 lovable-website 项目迁移到新的 pivota-marketing 项目。

---

## ✅ 当前状态

### 新的 Marketing Site（推荐使用）
- ✅ GitHub: https://github.com/pengxu9-rgb/pivota-marketing
- ✅ Vercel: 已部署并自动连接 GitHub
- ✅ 构建: 成功
- ✅ 功能: 完整（两个入口卡片、Features、Footer）
- ⏳ 域名: 等待配置 pivota.cc

### 旧的 lovable-website（将被替换）
- ⚠️ 占用域名: pivota.cc
- ⚠️ 问题: Lovable 平台导入，多次部署失败
- 🗑️ 建议: 归档或删除

---

## 📝 迁移步骤

### Step 1: 从 lovable-website 移除域名

访问：https://vercel.com/dashboard

1. 选择 **lovable-website** 项目
2. 进入 **Settings** → **Domains**
3. 找到 `pivota.cc`
4. 点击域名右侧的 "..." 菜单
5. 选择 **"Remove"**
6. 确认移除

### Step 2: 为 pivota-marketing 添加域名

```bash
cd /Users/pengchydan/Desktop/Pivota\ Infra/Pivota-cursor-create-project-directory-structure-8344/pivota-marketing
vercel domains add pivota.cc
```

或在 Vercel Dashboard：
1. 选择 **pivota-marketing** 项目
2. 进入 **Settings** → **Domains**
3. 点击 **"Add Domain"**
4. 输入：`pivota.cc`
5. 点击 **"Add"**
6. Vercel 会自动验证（DNS 已配置）

### Step 3: 添加 www 子域名（可选）

```bash
vercel domains add www.pivota.cc
```

---

## 🔍 验证迁移成功

### 1. DNS 验证
```bash
dig pivota.cc +short
# 应该仍然返回 Vercel IP 或 CNAME
```

### 2. HTTPS 访问
```bash
curl -I https://pivota.cc
# 应该返回 HTTP/2 200
# Server: Vercel
```

### 3. 浏览器测试
- 访问 https://pivota.cc
- 应该看到新的干净设计
- 两个入口卡片（Agent / Merchant）
- 清晰的 CTA 按钮

---

## 🎨 新 Marketing Site 的优势

### vs Lovable 版本：

| 特性 | lovable-website | pivota-marketing |
|------|----------------|------------------|
| 框架 | Vite + React | Next.js 15 |
| 部署 | Lovable 平台 | GitHub 自动部署 |
| 可维护性 | ⚠️ Lovable 依赖 | ✅ 完全控制 |
| 构建稳定性 | ⚠️ 多次失败 | ✅ 稳定可靠 |
| 代码清晰度 | ⚠️ 复杂 | ✅ 简洁明了 |
| SEO | ✅ | ✅ |
| 性能 | ✅ | ✅ |

---

## 🗑️ lovable-website 处理建议

### 选项 1: 归档（推荐）
- 在 Vercel 保留项目但移除域名
- 在 GitHub 保留代码作为参考
- 如果需要可以随时回滚

### 选项 2: 完全删除
- 在 Vercel 删除项目
- 在 GitHub 归档或删除仓库
- 节省资源

---

## ✨ 迁移完成后的效果

### 所有四个域名都由干净的项目托管：

```
GitHub 仓库 → Vercel 项目 → 域名
────────────────────────────────────────────
pivota-marketing → pivota.cc
pivota-agents-portal → agents.pivota.cc
pivota-merchants-portal → merchants.pivota.cc
pivota-employee-portal → employee.pivota.cc
```

### 自动部署工作流：

```
任何代码修改
    ↓
git push
    ↓
Vercel 自动检测并部署
    ↓
1-2 分钟后网站自动更新
```

---

## 🚀 下一步

1. **在 Vercel Dashboard 移除 lovable-website 的 pivota.cc 域名**
2. **运行**: `cd pivota-marketing && vercel domains add pivota.cc`
3. **测试**: 访问 https://pivota.cc
4. **完成**：所有四个网站都使用 GitHub 自动部署！

**准备好后告诉我，或者我可以等你完成移除域名的操作！** 🎯
