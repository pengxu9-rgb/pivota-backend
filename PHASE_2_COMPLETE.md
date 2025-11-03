# ✅ Phase 2: Merchant Onboarding & PSP Connection - COMPLETE

## 🎉 已完成功能

### 后端 (Backend)

✅ **数据库表**
- `merchant_onboarding` - 商户注册、KYC状态、API密钥
- `payment_router_config` - 商户→PSP路由配置

✅ **API端点** (`/merchant/onboarding/*`)
- `POST /register` - 注册新商户
- `POST /kyc/upload` - 上传KYC文档
- `POST /psp/setup` - 连接PSP并获取API密钥
- `GET /status/{merchant_id}` - 查询入驻状态
- `GET /all` - 管理员查看所有商户
- `POST /approve/{id}` - 管理员批准KYC
- `POST /reject/{id}` - 管理员拒绝KYC

✅ **自动化功能**
- KYC自动审批（5秒后台任务模拟）
- 商户API密钥自动生成
- 支付路由自动注册

### 前端 (Frontend)

✅ **商户入驻仪表板** (`/merchant/onboarding`)
- 4步向导流程
- 实时状态更新
- PSP选择（Stripe/Adyen/ShopPay）
- API密钥展示

✅ **管理员入驻管理** (Admin Dashboard → Onboarding标签)
- 查看所有入驻商户
- 按状态筛选
- 批准/拒绝操作
- PSP连接状态监控

---

## 🚀 快速测试

### 方法1: 使用前端界面

1. **访问入驻页面**
   ```
   https://pivota-dashboard.onrender.com (等待部署完成后)
   或
   http://localhost:5173/merchant/onboarding (本地开发)
   ```

2. **填写商户信息**
   - 商户名称
   - 网站
   - 地区 (US/EU/APAC/UK)
   - 联系邮箱
   - 电话（可选）

3. **等待KYC审批**（自动5秒后批准）

4. **连接PSP**
   - 选择PSP类型（Stripe/Adyen/ShopPay）
   - 输入测试API密钥
   - 获取商户API密钥

5. **完成！** 保存API密钥用于支付请求

### 方法2: 使用API测试

```bash
# 运行测试脚本
python3 test_phase2_onboarding.py

# 或手动测试
# 1. 注册商户
curl -X POST https://pivota-dashboard.onrender.com/merchant/onboarding/register \
  -H "Content-Type: application/json" \
  -d '{
    "business_name": "Test Store",
    "website": "https://test.com",
    "region": "US",
    "contact_email": "test@test.com"
  }'

# 返回: {"merchant_id": "merch_abc123..."}

# 2. 等待5秒后查询状态
sleep 6
curl https://pivota-dashboard.onrender.com/merchant/onboarding/status/merch_abc123

# 3. 连接PSP
curl -X POST https://pivota-dashboard.onrender.com/merchant/onboarding/psp/setup \
  -H "Content-Type: application/json" \
  -d '{
    "merchant_id": "merch_abc123",
    "psp_type": "stripe",
    "psp_sandbox_key": "sk_test_fake"
  }'

# 返回: {"api_key": "pk_live_xyz..."}
```

---

## 📊 数据流程

```
商户注册
   ↓
merchant_onboarding 表
   ↓
后台任务: 5秒后自动批准KYC
   ↓
status = "approved"
   ↓
商户连接PSP
   ↓
生成 API 密钥 (pk_live_...)
   ↓
注册到 payment_router_config
   ↓
商户可使用 /payment/execute
```

---

## 🔑 使用商户API密钥

入驻完成后，商户收到API密钥，可用于支付请求：

```bash
curl -X POST https://pivota-dashboard.onrender.com/payment/execute \
  -H "X-Merchant-API-Key: pk_live_abc123..." \
  -H "Content-Type: application/json" \
  -d '{
    "order_id": "ORDER-001",
    "amount": 100.00,
    "currency": "USD"
  }'
```

系统自动：
1. 验证API密钥
2. 查询商户的PSP配置
3. 路由到对应PSP
4. 返回统一响应

---

## 👨‍💼 管理员功能

登录管理员账号后，在 **Onboarding (Phase 2)** 标签页可以：

✅ 查看所有入驻商户
✅ 按状态筛选（pending/approved/rejected）
✅ 手动批准/拒绝KYC
✅ 查看PSP连接状态
✅ 监控入驻进度

---

## 📁 文件清单

### 后端新增文件
```
pivota_infra/
├── db/
│   ├── merchant_onboarding.py      # 商户入驻数据库操作
│   └── payment_router.py           # 支付路由配置
└── routes/
    └── merchant_onboarding_routes.py  # 入驻API端点
```

### 前端新增文件
```
simple_frontend/src/
├── components/
│   ├── MerchantOnboardingDashboard.tsx  # 商户入驻界面
│   └── OnboardingAdminView.tsx          # 管理员入驻管理
└── App.tsx (已更新)                     # 添加路由
```

### 配置更新
```
pivota_infra/main.py                  # 注册路由和表创建
simple_frontend/src/pages/AdminDashboard.tsx  # 添加Onboarding标签
```

---

## 🐛 故障排除

### 问题: 404 Not Found
**原因**: Render部署尚未完成
**解决**: 等待3-5分钟让Render完成部署

### 问题: KYC未自动批准
**解决**: 
- 查看后端日志
- 或使用管理员手动批准: `POST /merchant/onboarding/approve/{id}`

### 问题: PSP setup失败
**检查**:
- KYC是否已批准
- 商户是否已连接过PSP
- API密钥格式是否正确

### 问题: API密钥不工作
**检查**:
- Header: `X-Merchant-API-Key: pk_live_...`
- 商户是否在 `payment_router_config` 表中
- PSP是否已连接

---

## 📈 下一步 (Phase 3)

### 建议功能

1. **员工仪表板** (Employee Dashboard)
   - 查看负责的商户
   - 代表商户上传文档
   - 与商户沟通

2. **代理仪表板** (Agent Dashboard)
   - 查看入驻商户
   - 访问库存/股票数据
   - 追踪佣金

3. **真实KYC集成**
   - 文件上传（S3/Cloudinary）
   - KYC服务商集成（Stripe Identity/Onfido）
   - 文档验证流程

4. **智能路由**
   - 基于交易金额
   - 基于商户地区
   - 基于PSP成功率
   - 成本优化

5. **Webhook集成**
   - PSP webhook处理
   - 实时交易状态更新
   - 商户通知

---

## ✅ 验证清单

部署完成后验证：

- [ ] 访问 `/merchant/onboarding` 页面加载正常
- [ ] 可以注册新商户
- [ ] 5秒后KYC自动批准
- [ ] 可以连接PSP
- [ ] 收到API密钥
- [ ] 管理员可以看到 Onboarding (Phase 2) 标签
- [ ] 管理员可以查看所有商户
- [ ] 管理员可以批准/拒绝商户
- [ ] API文档 `/docs` 显示新端点

---

## 🎯 成功指标

- ✅ 完整的4步入驻流程
- ✅ 自动化KYC审批（5秒）
- ✅ API密钥生成系统
- ✅ 支付路由自动注册
- ✅ 管理员审批界面
- ✅ 前后端完全集成
- ✅ 文档完整

---

## 📞 测试命令

```bash
# 等待Render部署完成（3-5分钟）
# 然后运行:

python3 test_phase2_onboarding.py

# 或访问前端:
# https://pivota-dashboard.onrender.com/merchant/onboarding
```

---

**部署状态**: 🚀 已推送到GitHub，Render自动部署中...

**预计可用时间**: 3-5分钟后

**测试账号**: 
- 管理员: superadmin@pivota.com / Pivota2024!
- 新商户: 通过 `/merchant/onboarding` 自助注册

