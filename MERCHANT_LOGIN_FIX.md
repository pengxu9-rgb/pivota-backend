# 商户登录循环问题修复方案

## 问题诊断

商户登录后被重定向回登录页面的原因可能是：

1. **Token 存储问题**：
   - API 客户端在检查 `response.data.status === 'success'` 但新 API 返回 `success: true`
   - 这导致 token 没有被正确存储到 localStorage

2. **Merchant ID 缺失**：
   - 某些端点（如 `/merchant/webhooks/config`）需要 JWT token 中包含 `merchant_id`
   - 如果 merchant_id 缺失，会返回 400/403 错误，触发重定向

3. **API 拦截器问题**：
   - 401 错误会自动清除认证并重定向到登录页

## 已应用的修复

### 1. 修复 API 客户端 token 存储逻辑
文件：`pivota-merchants-portal/lib/api-client.ts`

```typescript
// 修改前
if (response.data.status === 'success' && response.data.token) {

// 修改后
if ((response.data.success === true || response.data.status === 'success') && response.data.token) {
```

### 2. 确保正确存储 merchant_id
```typescript
localStorage.setItem('merchant_id', response.data.user.merchant_id || response.data.user.id);
```

## 需要部署的更改

### 前端（Merchant Portal）
需要部署 `pivota-merchants-portal` 的更改：
- 修复了 `lib/api-client.ts` 中的 token 存储逻辑

### 后端
后端的 auth 端点已经正确返回 merchant_id

## 部署步骤

### 1. 提交前端更改
```bash
cd pivota-merchants-portal
git add lib/api-client.ts
git commit -m "fix: correct token storage logic for new API response format"
git push origin main
```

### 2. 在 Vercel 上触发重新部署
- 等待 Vercel 自动部署
- 或手动触发部署

## 测试步骤

1. 清除浏览器 localStorage：
   ```javascript
   localStorage.clear()
   ```

2. 使用测试账号登录：
   - Email: merchant@test.com
   - Password: Admin123!

3. 检查 localStorage 中的数据：
   ```javascript
   console.log('Token:', localStorage.getItem('merchant_token'))
   console.log('User:', localStorage.getItem('merchant_user'))
   console.log('Merchant ID:', localStorage.getItem('merchant_id'))
   ```

4. 验证是否成功进入 dashboard 而不是被重定向

## 备用方案

如果问题仍然存在，可以：

1. **临时禁用 webhook 配置检查**：
   在 dashboard 页面加载时跳过 webhook 配置的 API 调用

2. **修改后端 webhook 端点**：
   让它对缺少 merchant_id 的情况更宽容

3. **检查所有 API 调用**：
   使用浏览器开发者工具查看哪个 API 调用返回 401/403

