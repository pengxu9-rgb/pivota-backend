#!/bin/bash

echo "🚀 监控 Phase 4++ 部署状态..."
echo "GitHub: https://github.com/pengxu9-rgb/pivota-backend/commit/fcee9a0f"
echo

# 配置
API_BASE_URL="https://web-production-fedb.up.railway.app"
MAX_ATTEMPTS=30
SLEEP_TIME=10

# 检查端点可用性
check_endpoint() {
    local endpoint=$1
    local response=$(curl -s -o /dev/null -w "%{http_code}" "$API_BASE_URL$endpoint")
    echo "$response"
}

echo "等待部署完成（最多 ${MAX_ATTEMPTS} 次尝试，每次 ${SLEEP_TIME} 秒）..."
echo

attempt=1
while [ $attempt -le $MAX_ATTEMPTS ]; do
    echo -n "尝试 $attempt/$MAX_ATTEMPTS: "
    
    # 检查健康端点
    health_status=$(check_endpoint "/health")
    
    # 检查 Phase 4++ 特定端点
    routing_status=$(check_endpoint "/employee/routing/analytics/conflict-summary")
    
    echo "Health=$health_status, Routing=$routing_status"
    
    # 如果路由端点返回 401（需要认证）而不是 404，说明部署成功
    if [ "$routing_status" = "401" ] || [ "$routing_status" = "200" ]; then
        echo ""
        echo "✅ Phase 4++ 部署成功！"
        echo ""
        
        # 运行快速测试
        echo "运行验证测试..."
        curl -s -X GET "$API_BASE_URL/employee/routing/analytics/conflict-summary?days=1" \
            -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJlbXBfZW1wbG95ZWUiLCJ1c2VyX2lkIjoiZW1wX2VtcGxveWVlIiwiZW1haWwiOiJlbXBsb3llZUBwaXZvdGEuY29tIiwicm9sZSI6ImFkbWluIiwiZXhwIjoxNzYyMTQyMTc4LCJpYXQiOjE3NjIwNTU3Nzh9.swQohN0dhMhagYof8iHVI17j89z9OneQfdZpHAc1jSE" \
            | python3 -m json.tool 2>/dev/null || echo "需要先运行 Migration 011"
        
        echo ""
        echo "🎉 准备运行完整测试："
        echo "   ./test_phase4pp_simple.sh"
        exit 0
    fi
    
    sleep $SLEEP_TIME
    ((attempt++))
done

echo ""
echo "⚠️ 部署可能还在进行中，请稍后再试"
echo "查看部署状态: https://railway.app/project/*/service/*/deployments"
