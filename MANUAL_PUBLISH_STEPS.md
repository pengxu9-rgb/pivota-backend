# 📦 Manual Publishing Steps

由于发布需要交互式输入账号密码，请在你的**本地终端**（不是 Cursor）中运行以下命令。

## 准备工作

### 1. PyPI 账号设置
1. 访问 https://pypi.org/account/register/
2. 注册账号：`peng.xu9@gmail.com`
3. 验证邮箱
4. 生成 API Token:
   - 访问 https://pypi.org/manage/account/token/
   - 点击 "Add API token"
   - Name: "Pivota SDK Publishing"
   - Scope: "Entire account (all projects)"
   - 复制生成的 token（以 `pypi-` 开头）

### 2. npm 账号设置
1. 访问 https://www.npmjs.com/signup
2. 注册账号：`peng.xu9@gmail.com`
3. 验证邮箱
4. 在终端登录：
   ```bash
   npm login
   # Username: your_username
   # Password: your_password
   # Email: peng.xu9@gmail.com
   ```

---

## 发布步骤

### 方法一：使用自动化脚本

打开新的终端窗口：

```bash
cd "/Users/pengchydan/Desktop/Pivota Infra/Pivota-cursor-create-project-directory-structure-8344"
./PUBLISH_SDKS.sh
```

然后选择选项 4（发布全部三个包）

---

### 方法二：手动逐个发布

#### 1. 发布 Python SDK

```bash
cd "/Users/pengchydan/Desktop/Pivota Infra/Pivota-cursor-create-project-directory-structure-8344/pivota_sdk/python"

# 安装 twine（如果还没有）
pip3 install twine

# 发布
python3 -m twine upload dist/*

# 输入凭证：
# Username: __token__
# Password: (粘贴你的 PyPI API token)
```

#### 2. 发布 TypeScript SDK

```bash
cd "/Users/pengchydan/Desktop/Pivota Infra/Pivota-cursor-create-project-directory-structure-8344/pivota_sdk/typescript"

# 登录 npm（首次）
npm login

# 发布
npm publish --access public
```

#### 3. 发布 MCP Server

```bash
cd "/Users/pengchydan/Desktop/Pivota Infra/Pivota-cursor-create-project-directory-structure-8344/pivota_sdk/mcp-server"

# 确保已登录 npm
npm whoami

# 发布
npm publish --access public
```

---

## 验证发布成功

### Python SDK
```bash
pip3 install pivota-agent
python3 -c "from pivota_agent import PivotaAgentClient; print('✅ Python SDK 安装成功')"
```

### TypeScript SDK
```bash
npm install pivota-agent
node -e "require('pivota-agent'); console.log('✅ TypeScript SDK 安装成功')"
```

### MCP Server
```bash
npx pivota-mcp-server --help
# 应该显示帮助信息
```

---

## 查看已发布的包

- Python: https://pypi.org/project/pivota-agent/
- TypeScript: https://www.npmjs.com/package/pivota-agent
- MCP Server: https://www.npmjs.com/package/pivota-mcp-server

---

## 常见问题

### Q: PyPI 提示 "Invalid or non-existent authentication information"
A: 确保使用：
- Username: `__token__`（两个下划线）
- Password: 你的 API token（以 `pypi-` 开头）

### Q: npm 提示 "403 Forbidden"
A: 包名可能已被占用，需要使用 scoped package：
```bash
# 修改 package.json 中的 name:
# "name": "@your-username/pivota-agent"
npm publish --access public
```

### Q: "You do not have permission to publish"
A: 确保已经 `npm login` 并且账号验证通过

---

## 发布完成后

1. ✅ 更新 Agent Portal Integration 页面（已完成）
2. ✅ 测试安装命令
3. ✅ 通知用户 SDK 已可用
4. ✅ 更新文档链接

发布成功后请告诉我，我会验证安装并更新相关文档！





