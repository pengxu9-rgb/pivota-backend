#!/bin/bash

# [Phase 5] 填充 Agent Routing Decisions 演示数据

echo "======================================================="
echo "[Phase 5] 填充 Agent Routing History 演示数据"
echo "======================================================="

API_BASE_URL="${API_BASE_URL:-https://web-production-fedb.up.railway.app}"
EMPLOYEE_TOKEN="${EMPLOYEE_TOKEN:-eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJlbXBfZW1wbG95ZWUiLCJ1c2VyX2lkIjoiZW1wX2VtcGxveWVlIiwiZW1haWwiOiJlbXBsb3llZUBwaXZvdGEuY29tIiwicm9sZSI6ImFkbWluIiwiZXhwIjoxNzYyMTQyMTc4LCJpYXQiOjE3NjIwNTU3Nzh9.swQohN0dhMhagYof8iHVI17j89z9OneQfdZpHAc1jSE}"
AGENT_ID="${1:-agent_ee38f2b3645a2ec2}"

echo "Agent ID: $AGENT_ID"
echo "API: $API_BASE_URL"
echo

echo "📊 创建演示路由历史..."
echo "将创建 5 条记录:"
echo "  - 2x Stripe (共识)"
echo "  - 1x Adyen (商户规则，有冲突)"
echo "  - 1x PayPal (代理偏好)"
echo "  - 1x Adyen (共识)"
echo

response=$(curl -s -X POST "$API_BASE_URL/admin/seed/agent-routing-history/$AGENT_ID" \
  -H "Authorization: Bearer $EMPLOYEE_TOKEN" \
  -H "Content-Type: application/json")

if echo "$response" | grep -q '"status":"success"'; then
    echo "✅ 演示数据创建成功!"
    echo
    echo "$response" | python3 -m json.tool
    
    echo
    echo "======================================================="
    echo "数据已填充！"
    echo "======================================================="
    echo
    echo "现在刷新 Agent 详情页:"
    echo "https://employee.pivota.cc/dashboard/agents"
    echo
    echo "展开 'Agent Routing Decisions' 部分，应该看到:"
    echo "  - 5 条路由决策记录"
    echo "  - 🔵 蓝色 = Merchant Rule"
    echo "  - 🟢 绿色 = Agent Preference"  
    echo "  - 🟣 紫色 = Consensus"
    echo "  - 1 条标记为冲突的记录"
    echo
    echo "[Phase 5] 完成!"
    
else
    echo "❌ 创建失败"
    echo "$response" | python3 -m json.tool
    echo
    echo "可能原因:"
    echo "  - Railway 还在部署（等待2-3分钟）"
    echo "  - Token 过期（重新登录获取新token）"
fi
