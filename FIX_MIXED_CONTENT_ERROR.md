# Mixed Content Error 修复

## 错误信息
```
Mixed Content: The page at 'https://employee.pivota.cc/dashboard/agents' 
was loaded over HTTPS, but requested an insecure XMLHttpRequest endpoint 
'http://web-production-fedb.up.railway.app/employee/agents/?date_range=7d'. 
This request has been blocked; the content must be served over HTTPS.
```

## 问题原因

页面使用 **HTTPS** 但 API 调用使用了 **HTTP**，浏览器出于安全考虑阻止了请求。

## 可能的原因

### 1. Vercel 环境变量设置错误 ⚠️
检查 Vercel 项目设置中的环境变量：
- 变量名: `NEXT_PUBLIC_API_URL`
- ❌ 错误值: `http://web-production-fedb.up.railway.app`
- ✅ 正确值: `https://web-production-fedb.up.railway.app`

### 2. 浏览器缓存 ⚠️
旧的 HTTP URL 可能被缓存了

## 修复步骤

### 步骤 1: 检查 Vercel 环境变量

1. 登录 [Vercel Dashboard](https://vercel.com/dashboard)
2. 选择 `pivota-employee-portal` 项目
3. 进入 **Settings** → **Environment Variables**
4. 检查 `NEXT_PUBLIC_API_URL` 的值
5. 如果是 `http://...`，改为 `https://...`
6. **重新部署**项目

### 步骤 2: 代码层面的修复（已完成）✅

添加了强制 HTTPS 的逻辑：
```typescript
const getApiBaseUrl = () => {
  const url = process.env.NEXT_PUBLIC_API_URL || 'https://web-production-fedb.up.railway.app';
  // Force HTTPS if page is loaded over HTTPS
  if (typeof window !== 'undefined' && window.location.protocol === 'https:') {
    return url.replace(/^http:/, 'https:');
  }
  return url;
};
```

### 步骤 3: 清除浏览器缓存

1. 打开浏览器开发者工具（F12）
2. 右键点击刷新按钮
3. 选择 **"清空缓存并硬性重新加载"**
4. 或在开发者工具打开时按 `Cmd+Shift+R` (Mac) 或 `Ctrl+Shift+R` (Windows)

## Vercel 环境变量设置指南

### 正确的配置

在 Vercel 项目设置中添加/更新：

| 变量名 | 值 | 环境 |
|--------|-----|------|
| `NEXT_PUBLIC_API_URL` | `https://web-production-fedb.up.railway.app` | Production, Preview, Development |

**注意**: 
- 必须是 `NEXT_PUBLIC_` 前缀才能在浏览器中访问
- 必须使用 **HTTPS** 不是 HTTP
- 修改后需要重新部署

### 快速修复命令（如果没有设置环境变量）

在 Vercel 上不需要设置环境变量，代码中已经有默认值了。只需要重新部署：

```bash
cd /Users/pengchydan/Desktop/Pivota\ Infra/pivota-employee-portal-git

# 触发重新部署（空提交）
git commit --allow-empty -m "chore: force HTTPS in API client"
git push origin main
```

## 验证

部署完成后，检查浏览器控制台应该看到：
```
🔧 [API Client] Initializing with baseURL: https://web-production-fedb.up.railway.app
✅ [Employee API] 200 /employee/agents
```

**不应该再有** `http://` 的请求。

## 状态

- ✅ **代码修复已推送** - 添加了强制 HTTPS 逻辑
- ⏳ **等待 Vercel 部署**
- 📝 **检查环境变量**（如果问题持续）

---

**重要**: 这是一个安全特性。现代浏览器不允许 HTTPS 页面调用 HTTP 接口，必须都使用 HTTPS。
