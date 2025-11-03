#!/bin/bash

# [Phase 4++] 清理路由测试数据

echo "======================================================="
echo "[Phase 4++] 清理路由测试数据"
echo "======================================================="
echo

API_BASE_URL="${API_BASE_URL:-https://web-production-fedb.up.railway.app}"
EMPLOYEE_TOKEN="${EMPLOYEE_TOKEN:-eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJlbXBfZW1wbG95ZWUiLCJ1c2VyX2lkIjoiZW1wX2VtcGxveWVlIiwiZW1haWwiOiJlbXBsb3llZUBwaXZvdGEuY29tIiwicm9sZSI6ImFkbWluIiwiZXhwIjoxNzYyMTQyMTc4LCJpYXQiOjE3NjIwNTU3Nzh9.swQohN0dhMhagYof8iHVI17j89z9OneQfdZpHAc1jSE}"

echo "⚠️  这将删除以下测试数据:"
echo "   - merchant_high_risk_* 策略和日志"
echo "   - merchant_cost_sensitive_* 策略和日志"
echo "   - merchant_test_* 策略和日志"
echo "   - agent_cost_test_* 策略"
echo "   - 所有 test_order_* 路由日志"
echo "   - 所有 ap2_test_* 事务"
echo
echo "✅ 保留的数据:"
echo "   - agent_ee38f2b3645a2ec2 的真实策略（如果存在）"
echo "   - 其他非测试的路由数据"
echo
read -p "确认清理？ (y/N): " -n 1 -r
echo

if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "取消清理"
    exit 0
fi

echo
echo "🧹 开始清理..."

# 调用清理端点
response=$(curl -s -X POST "$API_BASE_URL/admin/cleanup/routing-test-data" \
  -H "Authorization: Bearer $EMPLOYEE_TOKEN" \
  -H "Content-Type: application/json")

# 检查结果
if echo "$response" | grep -q '"status":"success"'; then
    echo "✅ 清理完成!"
    echo
    echo "$response" | python3 -m json.tool
else
    echo "❌ 清理失败"
    echo "$response" | python3 -m json.tool
fi

echo
echo "======================================================="
echo "清理后的状态:"
echo "======================================================="
echo "访问 Routing Management 页面查看:"
echo "https://employee.pivota.cc/dashboard/routing"
echo
echo "页面应该显示空状态（clean slate）"
echo "[Phase 4++] 清理完成!"

# [Phase 4++] 清理路由测试数据

echo "======================================================="
echo "[Phase 4++] 清理路由测试数据"
echo "======================================================="
echo

API_BASE_URL="${API_BASE_URL:-https://web-production-fedb.up.railway.app}"
EMPLOYEE_TOKEN="${EMPLOYEE_TOKEN:-eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJlbXBfZW1wbG95ZWUiLCJ1c2VyX2lkIjoiZW1wX2VtcGxveWVlIiwiZW1haWwiOiJlbXBsb3llZUBwaXZvdGEuY29tIiwicm9sZSI6ImFkbWluIiwiZXhwIjoxNzYyMTQyMTc4LCJpYXQiOjE3NjIwNTU3Nzh9.swQohN0dhMhagYof8iHVI17j89z9OneQfdZpHAc1jSE}"

echo "⚠️  这将删除以下测试数据:"
echo "   - merchant_high_risk_* 策略和日志"
echo "   - merchant_cost_sensitive_* 策略和日志"
echo "   - merchant_test_* 策略和日志"
echo "   - agent_cost_test_* 策略"
echo "   - 所有 test_order_* 路由日志"
echo "   - 所有 ap2_test_* 事务"
echo
echo "✅ 保留的数据:"
echo "   - agent_ee38f2b3645a2ec2 的真实策略（如果存在）"
echo "   - 其他非测试的路由数据"
echo
read -p "确认清理？ (y/N): " -n 1 -r
echo

if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "取消清理"
    exit 0
fi

echo
echo "🧹 开始清理..."

# 调用清理端点
response=$(curl -s -X POST "$API_BASE_URL/admin/cleanup/routing-test-data" \
  -H "Authorization: Bearer $EMPLOYEE_TOKEN" \
  -H "Content-Type: application/json")

# 检查结果
if echo "$response" | grep -q '"status":"success"'; then
    echo "✅ 清理完成!"
    echo
    echo "$response" | python3 -m json.tool
else
    echo "❌ 清理失败"
    echo "$response" | python3 -m json.tool
fi

echo
echo "======================================================="
echo "清理后的状态:"
echo "======================================================="
echo "访问 Routing Management 页面查看:"
echo "https://employee.pivota.cc/dashboard/routing"
echo
echo "页面应该显示空状态（clean slate）"
echo "[Phase 4++] 清理完成!"
