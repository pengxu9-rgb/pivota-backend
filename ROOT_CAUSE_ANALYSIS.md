# Mixed Content 错误根本原因分析

## 为什么突然出现这个错误？

### 不是因为两个重复路由文件 ❌

两个路由文件的问题只影响**返回的数据字段**，不影响 HTTP/HTTPS 协议。

### 真正的原因 ✅

#### 1. **Vercel 环境变量可能被设置为 HTTP**

最有可能的情况：
```
之前的部署流程中，某人在 Vercel 设置了：
NEXT_PUBLIC_API_URL = http://web-production-fedb.up.railway.app
                      ^^^^
                      注意这里是 HTTP 不是 HTTPS
```

#### 2. **为什么之前没有报错？**

可能的时间线：
```
之前 → 项目用 HTTP 域名部署 (employee.pivota.cc 没有 HTTPS)
     → HTTP 页面调用 HTTP API ✅ 正常

现在 → 项目启用了 HTTPS (https://employee.pivota.cc)
     → HTTPS 页面调用 HTTP API ❌ 被阻止！
```

#### 3. **或者是新部署触发的**

```
之前 → 旧代码没有使用环境变量，直接硬编码 HTTPS ✅
新代码 → 添加了 process.env.NEXT_PUBLIC_API_URL 支持
       → 如果环境变量是 HTTP，就会用 HTTP ❌
```

## 🔍 相关问题检查

### 1. 检查 Git 历史

查看之前 `api-client.ts` 的版本：

<function_calls>
<invoke name="run_terminal_cmd">
<parameter name="command">cd /Users/pengchydan/Desktop/Pivota\ Infra/pivota-employee-portal-git && git log --oneline -10 lib/api-client.ts
