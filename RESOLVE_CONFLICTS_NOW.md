# 立即解决冲突并部署

我已经修复了最重要的文件 `lib/api-client.ts`。现在你需要：

## 1. 标记其他冲突文件为已解决

在 merchant portal 目录中执行：

```bash
# 添加所有文件（包括已解决的冲突）
git add -A

# 查看状态
git status
```

## 2. 提交合并

```bash
git commit -m "fix: merge with remote and keep login fix for new API response format"
```

## 3. 推送到 GitHub

```bash
git push origin main
```

## 已修复的关键代码

`lib/api-client.ts` 第76-80行已经修复为：
```typescript
if ((response.data.success === true || response.data.status === 'success') && response.data.token) {
    localStorage.setItem('merchant_token', response.data.token);
    localStorage.setItem('merchant_user', JSON.stringify(response.data.user));
    localStorage.setItem('merchant_id', response.data.user.merchant_id || response.data.user.id);
}
```

这是最重要的修复，确保了新的 API 响应格式能正常工作。

## 其他冲突文件

对于其他有冲突的文件，你可以：
- 使用远程版本：`git checkout --theirs <文件名>`
- 使用本地版本：`git checkout --ours <文件名>`
- 或者手动编辑解决

由于登录功能主要依赖 `api-client.ts`，其他文件的冲突对登录功能影响不大。

## 快速完成

如果你想快速完成，可以：

```bash
# 接受所有远程更改（除了已修复的 api-client.ts）
git checkout --theirs README.md app/dashboard/analytics/page.tsx app/dashboard/integrations/page.tsx app/dashboard/mcp/page.tsx app/dashboard/payouts/page.tsx app/favicon.ico app/layout.tsx app/page.tsx app/reset-password/page.tsx components/PSPRoutingConfig.tsx

# 添加所有文件
git add -A

# 提交
git commit -m "fix: merge and keep critical login fix"

# 推送
git push origin main
```

