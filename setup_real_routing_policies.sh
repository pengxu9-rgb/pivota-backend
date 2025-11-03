#!/bin/bash

# [Phase 4++] 配置真实路由策略的示例脚本

echo "======================================================="
echo "[Phase 4++] 真实路由策略配置指南"
echo "======================================================="
echo

# 配置
API_BASE_URL="${API_BASE_URL:-https://web-production-fedb.up.railway.app}"
EMPLOYEE_TOKEN="${EMPLOYEE_TOKEN:-eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJlbXBfZW1wbG95ZWUiLCJ1c2VyX2lkIjoiZW1wX2VtcGxveWVlIiwiZW1haWwiOiJlbXBsb3llZUBwaXZvdGEuY29tIiwicm9sZSI6ImFkbWluIiwiZXhwIjoxNzYyMTQyMTc4LCJpYXQiOjE3NjIwNTU3Nzh9.swQohN0dhMhagYof8iHVI17j89z9OneQfdZpHAc1jSE}"

# 真实的 agent ID
REAL_AGENT_ID="agent_ee38f2b3645a2ec2"

echo "=== 场景 1: 高风险商户策略 ==="
echo "商户要求：只能使用 Stripe（PCI 合规性最高）"
echo

HIGH_RISK_MERCHANT_POLICY='{
  "exclude": ["paypal", "square"],
  "prefer": ["stripe"],
  "required": ["stripe"],
  "weights": {},
  "failover": [],
  "priority": 1
}'

echo "创建高风险商户策略..."
curl -s -X POST "$API_BASE_URL/employee/routing/policies/merchant/merchant_high_risk_001" \
  -H "Authorization: Bearer $EMPLOYEE_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$HIGH_RISK_MERCHANT_POLICY" | python3 -m json.tool | head -10
echo

echo "=== 场景 2: 成本敏感商户策略 ==="
echo "商户要求：避免高费用的 Adyen，偏好 PayPal"
echo

COST_SENSITIVE_MERCHANT_POLICY='{
  "exclude": ["adyen"],
  "prefer": ["paypal", "stripe"],
  "required": [],
  "weights": {},
  "failover": ["square"],
  "priority": 1
}'

echo "创建成本敏感商户策略..."
curl -s -X POST "$API_BASE_URL/employee/routing/policies/merchant/merchant_cost_sensitive_002" \
  -H "Authorization: Bearer $EMPLOYEE_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$COST_SENSITIVE_MERCHANT_POLICY" | python3 -m json.tool | head -10
echo

echo "=== 场景 3: 高性能代理策略 ==="
echo "代理偏好：基于性能选择（Stripe 最快，PayPal 最慢）"
echo

PERFORMANCE_AGENT_POLICY='{
  "exclude": [],
  "prefer": ["stripe", "adyen", "paypal"],
  "weights": {"stripe": 1.0, "adyen": 0.85, "paypal": 0.6},
  "failover": ["square"],
  "priority": 1
}'

echo "为真实代理设置性能优化策略..."
curl -s -X POST "$API_BASE_URL/employee/routing/policies/agent/$REAL_AGENT_ID" \
  -H "Authorization: Bearer $EMPLOYEE_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$PERFORMANCE_AGENT_POLICY" | python3 -m json.tool | head -10
echo

echo "=== 模拟路由冲突场景 ==="
echo "测试：高风险商户（只能 Stripe）+ 成本代理（避免 Stripe）"
echo

COST_AGENT_POLICY='{
  "exclude": ["stripe"],
  "prefer": ["paypal", "square"],
  "weights": {"paypal": 1.0, "square": 0.9},
  "failover": ["adyen"],
  "priority": 1
}'

# 创建测试代理策略
TEST_AGENT="agent_cost_test_$(date +%s)"
echo "创建测试代理策略（避免 Stripe）..."
curl -s -X POST "$API_BASE_URL/employee/routing/policies/agent/$TEST_AGENT" \
  -H "Authorization: Bearer $EMPLOYEE_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$COST_AGENT_POLICY" | python3 -m json.tool | head -10
echo

# 模拟路由
echo "模拟路由决策..."
SIMULATION='{
  "scenarios": [
    {"amount": 100.00, "currency": "USD", "description": "Small payment"},
    {"amount": 5000.00, "currency": "USD", "description": "Large payment"},
    {"amount": 50.00, "currency": "EUR", "description": "EU payment"}
  ]
}'

curl -s -X POST "$API_BASE_URL/employee/routing/simulate/merchant_high_risk_001/$TEST_AGENT" \
  -H "Authorization: Bearer $EMPLOYEE_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$SIMULATION" | python3 -m json.tool
echo

echo "======================================================="
echo "配置建议"
echo "======================================================="
echo
echo "1. 商户策略类型："
echo "   - 高风险：限制到最安全的 PSP（如 Stripe）"
echo "   - 成本敏感：排除高费用 PSP（如 Adyen）"
echo "   - 地理限制：特定地区的 PSP 要求"
echo
echo "2. 代理策略类型："
echo "   - 性能优先：基于延迟和成功率"
echo "   - 成本优化：最低交易费用"
echo "   - 可靠性：历史成功率最高"
echo
echo "3. 冲突解决："
echo "   - 默认：商户规则优先（安全第一）"
echo "   - 白名单代理：可以覆盖商户偏好（需谨慎授权）"
echo
echo "4. 监控建议："
echo "   - 定期查看冲突率（目标 <5%）"
echo "   - 分析 PSP 使用分布"
echo "   - 跟踪路由决策的执行时间"
echo
echo "访问 Employee Portal 查看效果："
echo "https://employee.pivota.cc/dashboard/routing"
echo
echo "[Phase 4++] 配置完成！"

# [Phase 4++] 配置真实路由策略的示例脚本

echo "======================================================="
echo "[Phase 4++] 真实路由策略配置指南"
echo "======================================================="
echo

# 配置
API_BASE_URL="${API_BASE_URL:-https://web-production-fedb.up.railway.app}"
EMPLOYEE_TOKEN="${EMPLOYEE_TOKEN:-eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJlbXBfZW1wbG95ZWUiLCJ1c2VyX2lkIjoiZW1wX2VtcGxveWVlIiwiZW1haWwiOiJlbXBsb3llZUBwaXZvdGEuY29tIiwicm9sZSI6ImFkbWluIiwiZXhwIjoxNzYyMTQyMTc4LCJpYXQiOjE3NjIwNTU3Nzh9.swQohN0dhMhagYof8iHVI17j89z9OneQfdZpHAc1jSE}"

# 真实的 agent ID
REAL_AGENT_ID="agent_ee38f2b3645a2ec2"

echo "=== 场景 1: 高风险商户策略 ==="
echo "商户要求：只能使用 Stripe（PCI 合规性最高）"
echo

HIGH_RISK_MERCHANT_POLICY='{
  "exclude": ["paypal", "square"],
  "prefer": ["stripe"],
  "required": ["stripe"],
  "weights": {},
  "failover": [],
  "priority": 1
}'

echo "创建高风险商户策略..."
curl -s -X POST "$API_BASE_URL/employee/routing/policies/merchant/merchant_high_risk_001" \
  -H "Authorization: Bearer $EMPLOYEE_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$HIGH_RISK_MERCHANT_POLICY" | python3 -m json.tool | head -10
echo

echo "=== 场景 2: 成本敏感商户策略 ==="
echo "商户要求：避免高费用的 Adyen，偏好 PayPal"
echo

COST_SENSITIVE_MERCHANT_POLICY='{
  "exclude": ["adyen"],
  "prefer": ["paypal", "stripe"],
  "required": [],
  "weights": {},
  "failover": ["square"],
  "priority": 1
}'

echo "创建成本敏感商户策略..."
curl -s -X POST "$API_BASE_URL/employee/routing/policies/merchant/merchant_cost_sensitive_002" \
  -H "Authorization: Bearer $EMPLOYEE_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$COST_SENSITIVE_MERCHANT_POLICY" | python3 -m json.tool | head -10
echo

echo "=== 场景 3: 高性能代理策略 ==="
echo "代理偏好：基于性能选择（Stripe 最快，PayPal 最慢）"
echo

PERFORMANCE_AGENT_POLICY='{
  "exclude": [],
  "prefer": ["stripe", "adyen", "paypal"],
  "weights": {"stripe": 1.0, "adyen": 0.85, "paypal": 0.6},
  "failover": ["square"],
  "priority": 1
}'

echo "为真实代理设置性能优化策略..."
curl -s -X POST "$API_BASE_URL/employee/routing/policies/agent/$REAL_AGENT_ID" \
  -H "Authorization: Bearer $EMPLOYEE_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$PERFORMANCE_AGENT_POLICY" | python3 -m json.tool | head -10
echo

echo "=== 模拟路由冲突场景 ==="
echo "测试：高风险商户（只能 Stripe）+ 成本代理（避免 Stripe）"
echo

COST_AGENT_POLICY='{
  "exclude": ["stripe"],
  "prefer": ["paypal", "square"],
  "weights": {"paypal": 1.0, "square": 0.9},
  "failover": ["adyen"],
  "priority": 1
}'

# 创建测试代理策略
TEST_AGENT="agent_cost_test_$(date +%s)"
echo "创建测试代理策略（避免 Stripe）..."
curl -s -X POST "$API_BASE_URL/employee/routing/policies/agent/$TEST_AGENT" \
  -H "Authorization: Bearer $EMPLOYEE_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$COST_AGENT_POLICY" | python3 -m json.tool | head -10
echo

# 模拟路由
echo "模拟路由决策..."
SIMULATION='{
  "scenarios": [
    {"amount": 100.00, "currency": "USD", "description": "Small payment"},
    {"amount": 5000.00, "currency": "USD", "description": "Large payment"},
    {"amount": 50.00, "currency": "EUR", "description": "EU payment"}
  ]
}'

curl -s -X POST "$API_BASE_URL/employee/routing/simulate/merchant_high_risk_001/$TEST_AGENT" \
  -H "Authorization: Bearer $EMPLOYEE_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$SIMULATION" | python3 -m json.tool
echo

echo "======================================================="
echo "配置建议"
echo "======================================================="
echo
echo "1. 商户策略类型："
echo "   - 高风险：限制到最安全的 PSP（如 Stripe）"
echo "   - 成本敏感：排除高费用 PSP（如 Adyen）"
echo "   - 地理限制：特定地区的 PSP 要求"
echo
echo "2. 代理策略类型："
echo "   - 性能优先：基于延迟和成功率"
echo "   - 成本优化：最低交易费用"
echo "   - 可靠性：历史成功率最高"
echo
echo "3. 冲突解决："
echo "   - 默认：商户规则优先（安全第一）"
echo "   - 白名单代理：可以覆盖商户偏好（需谨慎授权）"
echo
echo "4. 监控建议："
echo "   - 定期查看冲突率（目标 <5%）"
echo "   - 分析 PSP 使用分布"
echo "   - 跟踪路由决策的执行时间"
echo
echo "访问 Employee Portal 查看效果："
echo "https://employee.pivota.cc/dashboard/routing"
echo
echo "[Phase 4++] 配置完成！"
