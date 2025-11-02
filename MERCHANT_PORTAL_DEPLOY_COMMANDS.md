# Merchant Portal 部署命令

请在终端中按顺序执行以下命令：

## 1. 进入正确的目录
```bash
cd "/Users/pengchydan/Desktop/Pivota Infra/Pivota-cursor-create-project-directory-structure-8344/pivota-merchants-portal"
```

## 2. 检查 Git 状态
```bash
# 如果目录没有 git，先初始化
git init
git remote add origin https://github.com/pengxu9-rgb/pivota-merchants-portal.git
```

或者如果已经有 git：
```bash
git status
```

## 3. 添加修改的文件
```bash
git add lib/api-client.ts
```

## 4. 提交更改
```bash
git commit -m "fix: correct token storage logic for new API response format"
```

## 5. 推送到 GitHub
```bash
git push origin main
```

如果是第一次推送：
```bash
git branch -M main
git push -u origin main
```

## 6. 验证部署

1. 访问 [Vercel Dashboard](https://vercel.com/dashboard)
2. 查看 pivota-merchants-portal 项目的部署状态
3. 部署完成后测试登录：https://merchants.pivota.cc

## 修复内容说明

修改了 `lib/api-client.ts` 第 76 行：
- 旧代码：`if (response.data.status === 'success' && response.data.token)`
- 新代码：`if ((response.data.success === true || response.data.status === 'success') && response.data.token)`

这个修复确保了新 API 响应格式（`success: true`）能被正确处理。


