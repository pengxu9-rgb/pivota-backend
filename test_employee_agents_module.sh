#!/bin/bash

# Employee Agents Management Module - API 测试脚本
# 使用方法: ./test_employee_agents_module.sh <employee_token>

set -e

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# API Base URL
API_URL="https://web-production-fedb.up.railway.app"

# 检查参数
if [ -z "$1" ]; then
    echo -e "${RED}❌ 错误: 需要提供 employee token${NC}"
    echo "使用方法: $0 <employee_token>"
    exit 1
fi

TOKEN="$1"

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  Employee Agents Module API 测试${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# 测试函数
test_endpoint() {
    local name="$1"
    local method="$2"
    local endpoint="$3"
    local data="$4"
    
    echo -e "${YELLOW}测试: ${name}${NC}"
    echo -e "端点: ${method} ${endpoint}"
    
    if [ "$method" = "GET" ]; then
        response=$(curl -s -w "\n%{http_code}" \
            -H "Authorization: Bearer $TOKEN" \
            "${API_URL}${endpoint}")
    else
        if [ -z "$data" ]; then
            response=$(curl -s -w "\n%{http_code}" -X POST \
                -H "Authorization: Bearer $TOKEN" \
                "${API_URL}${endpoint}")
        else
            response=$(curl -s -w "\n%{http_code}" -X POST \
                -H "Authorization: Bearer $TOKEN" \
                -H "Content-Type: application/json" \
                -d "$data" \
                "${API_URL}${endpoint}")
        fi
    fi
    
    http_code=$(echo "$response" | tail -n 1)
    body=$(echo "$response" | sed '$d')
    
    if [ "$http_code" = "200" ]; then
        echo -e "${GREEN}✅ 成功 (HTTP $http_code)${NC}"
        echo "$body" | jq '.' 2>/dev/null || echo "$body"
    else
        echo -e "${RED}❌ 失败 (HTTP $http_code)${NC}"
        echo "$body"
    fi
    
    echo ""
    sleep 1
}

# ============================================================================
# 1. 测试获取所有 agents
# ============================================================================
echo -e "${BLUE}[1] 获取所有 Agents${NC}"
test_endpoint "获取所有 agents" "GET" "/employee/agents"

echo -e "${BLUE}[2] 获取活跃 Agents (过滤)${NC}"
test_endpoint "过滤活跃 agents" "GET" "/employee/agents?status_filter=active"

# ============================================================================
# 2. 获取一个 agent ID 用于后续测试
# ============================================================================
echo -e "${BLUE}[3] 获取 Agent ID 用于测试${NC}"
agents_response=$(curl -s -H "Authorization: Bearer $TOKEN" "${API_URL}/employee/agents")
AGENT_ID=$(echo "$agents_response" | jq -r '.agents[0].agent_id // empty' 2>/dev/null)

if [ -z "$AGENT_ID" ] || [ "$AGENT_ID" = "null" ]; then
    echo -e "${RED}❌ 没有找到 agents，无法继续测试${NC}"
    echo -e "${YELLOW}提示: 请先在系统中创建至少一个 agent${NC}"
    exit 1
fi

echo -e "${GREEN}✅ 找到 Agent ID: ${AGENT_ID}${NC}"
echo ""

# ============================================================================
# 3. 测试 Agent 详情
# ============================================================================
echo -e "${BLUE}[4] 获取 Agent 详情${NC}"
test_endpoint "Agent 详情" "GET" "/employee/agents/${AGENT_ID}/details"

# ============================================================================
# 4. 测试 API 调用日志
# ============================================================================
echo -e "${BLUE}[5] 获取 API 调用日志 (分页)${NC}"
test_endpoint "调用日志 (前50条)" "GET" "/employee/agents/${AGENT_ID}/calls?limit=50&offset=0"

test_endpoint "调用日志 (第2页)" "GET" "/employee/agents/${AGENT_ID}/calls?limit=50&offset=50"

# ============================================================================
# 5. 测试 Rate Limit 更新
# ============================================================================
echo -e "${BLUE}[6] 更新 Rate Limit${NC}"
test_endpoint "更新 Rate Limit 为 200" "POST" "/employee/agents/${AGENT_ID}/update-rate-limit?new_limit=200"

# 验证更新
echo -e "${BLUE}[7] 验证 Rate Limit 更新${NC}"
updated_agent=$(curl -s -H "Authorization: Bearer $TOKEN" "${API_URL}/employee/agents/${AGENT_ID}/details")
current_rate_limit=$(echo "$updated_agent" | jq -r '.agent.rate_limit // .agent.governance.max_requests_per_minute')
echo -e "当前 Rate Limit: ${current_rate_limit}"
if [ "$current_rate_limit" = "200" ]; then
    echo -e "${GREEN}✅ Rate Limit 更新成功${NC}"
else
    echo -e "${YELLOW}⚠️  Rate Limit: ${current_rate_limit} (可能已有其他值)${NC}"
fi
echo ""

# ============================================================================
# 6. 测试 API Key 重置 (谨慎操作)
# ============================================================================
echo -e "${YELLOW}[8] API Key 重置测试${NC}"
echo -e "${RED}⚠️  警告: 这将使当前 API Key 失效！${NC}"
read -p "是否继续测试 API Key 重置? (y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    test_endpoint "重置 API Key" "POST" "/employee/agents/${AGENT_ID}/reset-api-key"
else
    echo -e "${YELLOW}⏭️  跳过 API Key 重置测试${NC}"
    echo ""
fi

# ============================================================================
# 7. 测试停用/激活 (谨慎操作)
# ============================================================================
echo -e "${YELLOW}[9] Agent 停用/激活测试${NC}"
echo -e "${RED}⚠️  警告: 这将暂时停用 agent！${NC}"
read -p "是否继续测试停用/激活? (y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    # 停用
    test_endpoint "停用 Agent" "POST" "/employee/agents/${AGENT_ID}/deactivate" '{"reason":"测试停用"}'
    
    # 等待
    sleep 2
    
    # 重新激活
    test_endpoint "重新激活 Agent" "POST" "/employee/agents/${AGENT_ID}/reactivate"
else
    echo -e "${YELLOW}⏭️  跳过停用/激活测试${NC}"
    echo ""
fi

# ============================================================================
# 总结
# ============================================================================
echo -e "${BLUE}========================================${NC}"
echo -e "${GREEN}✅ API 测试完成${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo -e "测试的端点:"
echo "  • GET  /employee/agents"
echo "  • GET  /employee/agents?status_filter=active"
echo "  • GET  /employee/agents/{id}/details"
echo "  • GET  /employee/agents/{id}/calls"
echo "  • POST /employee/agents/{id}/update-rate-limit"
echo "  • POST /employee/agents/{id}/reset-api-key (可选)"
echo "  • POST /employee/agents/{id}/deactivate (可选)"
echo "  • POST /employee/agents/{id}/reactivate (可选)"
echo ""
echo -e "${YELLOW}下一步:${NC}"
echo "1. 访问 Employee Portal: https://pivota-employee-portal.vercel.app"
echo "2. 登录并导航到 'Agents' 页面"
echo "3. 测试前端所有功能"
echo ""

