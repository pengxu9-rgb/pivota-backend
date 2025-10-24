# 🚀 快速发布 - 在本地终端运行

## ✅ Python SDK 已发布成功！
- Package: https://pypi.org/project/pivota-agent/1.0.0/
- Install: `pip install pivota-agent`

---

## ⏳ npm 包需要在本地终端发布

打开你的本地终端（Terminal.app），复制粘贴以下命令：

### 1. 登录 npm
```bash
npm login
# Username: jackxu1990
# Password: Pivota2025@peng
# Email: peng.xu9@gmail.com
```

### 2. 发布 TypeScript SDK
```bash
cd "/Users/pengchydan/Desktop/Pivota Infra/Pivota-cursor-create-project-directory-structure-8344/pivota_sdk/typescript"
npm publish --access public
```

### 3. 发布 MCP Server
```bash
cd "/Users/pengchydan/Desktop/Pivota Infra/Pivota-cursor-create-project-directory-structure-8344/pivota_sdk/mcp-server"
npm publish --access public
```

---

## ✅ 验证发布成功

### TypeScript SDK
```bash
npm view pivota-agent
# 应该显示版本 1.0.0
```

### MCP Server
```bash
npm view pivota-mcp-server
# 应该显示版本 1.0.0
```

---

## 🧪 测试安装

### Python SDK (已可用)
```bash
pip install pivota-agent
python3 -c "from pivota_agent import PivotaAgentClient; print('✅ Works!')"
```

### TypeScript SDK (发布后)
```bash
npm install pivota-agent
node -e "const {PivotaAgentClient} = require('pivota-agent'); console.log('✅ Works!')"
```

### MCP Server (发布后)
```bash
export PIVOTA_API_KEY="pk_live_test123"
npx pivota-mcp-server
# 应该启动 MCP 服务器
```

---

## 📊 当前状态

- ✅ **Python SDK**: 已发布到 PyPI
- ⏳ **TypeScript SDK**: 等待 npm 登录后发布
- ⏳ **MCP Server**: 等待 npm 登录后发布

完成后告诉我，我会验证所有包都可以正常安装！

