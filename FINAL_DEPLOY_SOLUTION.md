# 最终部署方案

由于你已经在 `pivota-merchants-portal` 目录中，请按以下步骤操作：

## 方案 A：安全合并（推荐）

```bash
# 1. 先拉取远程代码
git pull origin main

# 2. 如果成功，检查登录修复是否还在
grep -n "response.data.success === true" lib/api-client.ts

# 3. 如果修复丢失，重新添加
# 编辑 lib/api-client.ts，确保第 76 行附近有：
# if ((response.data.success === true || response.data.status === 'success') && response.data.token) {

# 4. 提交并推送
git add lib/api-client.ts
git commit -m "fix: ensure login works with new API response format"
git push origin main
```

## 方案 B：强制推送（如果A失败）

⚠️ **警告**：这会覆盖远程仓库！

```bash
# 只在确认本地代码正确时使用
git push --force origin main
```

## 方案 C：重新克隆并应用修复

```bash
# 1. 备份当前修复
cp lib/api-client.ts ~/api-client-fixed.ts

# 2. 去到上级目录
cd ..

# 3. 重新克隆
rm -rf pivota-merchants-portal
git clone https://github.com/pengxu9-rgb/pivota-merchants-portal.git

# 4. 进入目录
cd pivota-merchants-portal

# 5. 应用修复
cp ~/api-client-fixed.ts lib/api-client.ts

# 6. 提交并推送
git add lib/api-client.ts
git commit -m "fix: login with new API response format"
git push origin main
```

## 验证修复

修复的关键代码（lib/api-client.ts 第76行）：
```typescript
if ((response.data.success === true || response.data.status === 'success') && response.data.token) {
    localStorage.setItem('merchant_token', response.data.token);
    localStorage.setItem('merchant_user', JSON.stringify(response.data.user));
    localStorage.setItem('merchant_id', response.data.user.merchant_id || response.data.user.id);
}
```

选择最适合你的方案！


