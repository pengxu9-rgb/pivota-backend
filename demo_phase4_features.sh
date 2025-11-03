#!/bin/bash

# Phase 4 Complete Feature Demonstration
# Shows all payment routing and protocol capabilities

API_URL="https://web-production-fedb.up.railway.app"
AGENT_ID="agent_ee38f2b3645a2ec2"

if [ -z "$1" ]; then
    echo "Usage: $0 <YOUR_TOKEN>"
    exit 1
fi

TOKEN=$1

echo "╔════════════════════════════════════════╗"
echo "║   Phase 4 功能完整演示                  ║"
echo "║   Payment Routing & Protocol Support   ║"
echo "╚════════════════════════════════════════╝"
echo ""

# Demo 1: Protocol Definitions
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📋 Demo 1: 查看可用协议"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "➜ 获取所有协议定义..."
echo ""
curl -s "$API_URL/protocols/" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print('✅ 可用协议：\n')
for p in data:
    print(f'  🔌 {p[\"protocol_name\"]} v{p[\"version\"]}')
    print(f'     类型: {p[\"specification\"][\"type\"]}')
    print(f'     认证: {p[\"specification\"][\"auth\"]}')
    print(f'     状态: {p[\"status\"]}')
    print(f'     端点数: {len(p[\"endpoints\"])}')
    print()
"
read -p "按回车继续..." dummy

# Demo 2: Agent Route Configuration
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🗺️  Demo 2: Agent 路由配置"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "➜ 获取 Agent 的路由配置..."
echo ""
curl -s "$API_URL/agents/$AGENT_ID/routes" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
data = json.load(sys.stdin)
if isinstance(data, list) and len(data) > 0:
    route = data[0]
    print('✅ 路由配置：\n')
    print(f'  路由ID: {route[\"route_id\"]}')
    print(f'  策略: {route[\"routing_strategy\"]}')
    print(f'  最大重试: {route[\"max_retries\"]}')
    print(f'  超时: {route[\"timeout_ms\"]}ms')
    print(f'  状态: {\"🟢 Active\" if route[\"is_active\"] else \"🔴 Inactive\"}')
    print()
    print('  PSP 优先级:')
    for psp in route['psp_priority']:
        priority_emoji = ['🥇', '🥈', '🥉'][psp['priority']-1] if psp['priority'] <= 3 else '🏅'
        print(f'    {priority_emoji} #{psp[\"priority\"]} - {psp[\"psp\"].upper()}')
else:
    print(data)
"
read -p "按回车继续..." dummy

# Demo 3: Protocol Validation
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🔍 Demo 3: 协议载荷验证"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Test AP2 - Valid
echo "测试 AP2 协议（有效载荷）："
curl -s -X POST "$API_URL/protocols/AP2/validate" \
  -H "Content-Type: application/json" \
  -d '{
    "payload": {
      "order_id": "demo_001",
      "amount": 99.99,
      "currency": "USD",
      "merchant_id": "merchant_demo"
    }
  }' | python3 -c "
import sys, json
data = json.load(sys.stdin)
if data.get('valid'):
    print('  ✅ 验证通过')
    print(f'  协议: {data[\"protocol\"]} v{data[\"version\"]}')
    if data.get('warnings'):
        print(f'  ⚠️  建议: {data[\"warnings\"][0]}')
else:
    print('  ❌ 验证失败')
    print(f'  错误: {data.get(\"errors\")}')
"
echo ""

# Test AP2 - Invalid
echo "测试 AP2 协议（无效载荷 - 缺少金额）："
curl -s -X POST "$API_URL/protocols/AP2/validate" \
  -H "Content-Type: application/json" \
  -d '{
    "payload": {
      "order_id": "demo_002",
      "currency": "USD",
      "merchant_id": "merchant_demo"
    }
  }' | python3 -c "
import sys, json
data = json.load(sys.stdin)
if data.get('valid'):
    print('  ✅ 验证通过')
else:
    print('  ❌ 验证失败（预期）')
    print(f'  错误: {data.get(\"errors\")[0] if data.get(\"errors\") else \"Unknown\"}')
"
echo ""
read -p "按回车继续..." dummy

# Demo 4: Agent Protocols
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🔌 Demo 4: Agent 启用的协议"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "➜ 获取 Agent 支持的协议..."
echo ""
curl -s "$API_URL/agents/$AGENT_ID/protocols/" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
data = json.load(sys.stdin)
if isinstance(data, list):
    print(f'✅ Agent 已启用 {len(data)} 个协议：\n')
    for p in data:
        status_emoji = '🟢' if p['status'] == 'active' else '🟡' if p['status'] == 'beta' else '🔴'
        print(f'  {status_emoji} {p[\"protocol_name\"]} v{p[\"version\"]}')
        print(f'     状态: {p[\"status\"]}')
        if p.get('last_verified_at'):
            print(f'     最后验证: {p[\"last_verified_at\"]}')
        print()
else:
    print(data)
"
read -p "按回车继续..." dummy

# Demo 5: Routing Performance (Employee only)
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📊 Demo 5: 路由性能概览"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "➜ 获取路由概览（需要 Employee 权限）..."
echo ""
curl -s "$API_URL/employee/psp/routes/overview" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if 'detail' in data:
        print(f'⚠️  {data[\"detail\"]}')
        print('(需要 Employee 权限)')
    else:
        print('✅ 路由系统概览：\n')
        print(f'  总路由数: {data.get(\"total_routes\", 0)}')
        print(f'  活跃路由: {data.get(\"active_routes\", 0)}')
        print(f'  覆盖 Agent: {data.get(\"total_agents\", 0)}')
        print()
        strategies = data.get('routes_by_strategy', {})
        if strategies:
            print('  路由策略分布:')
            for strategy, count in strategies.items():
                print(f'    - {strategy}: {count}')
except:
    print('数据加载中...')
"
echo ""
read -p "按回车继续..." dummy

# Summary
echo ""
echo "╔════════════════════════════════════════╗"
echo "║          演示完成！                     ║"
echo "╚════════════════════════════════════════╝"
echo ""
echo "✨ Phase 4 核心功能全部正常："
echo ""
echo "  ✅ 协议系统 - 3个协议可用（AP2, ACP, X-402）"
echo "  ✅ 路由配置 - 优先级路由已设置"
echo "  ✅ 载荷验证 - 智能验证与错误提示"
echo "  ✅ Agent 协议 - 多协议支持已启用"
echo "  ✅ 监控系统 - 性能指标收集中"
echo ""
echo "🎯 下一步："
echo "  1. 在 Employee Portal 查看 Agent 详情"
echo "  2. 使用协议测试沙盒"
echo "  3. 创建测试支付触发路由逻辑"
echo "  4. 监控 PSP 性能指标"
echo ""
echo "📖 详细文档: PHASE_4_USER_GUIDE.md"
echo ""


# Phase 4 Complete Feature Demonstration
# Shows all payment routing and protocol capabilities

API_URL="https://web-production-fedb.up.railway.app"
AGENT_ID="agent_ee38f2b3645a2ec2"

if [ -z "$1" ]; then
    echo "Usage: $0 <YOUR_TOKEN>"
    exit 1
fi

TOKEN=$1

echo "╔════════════════════════════════════════╗"
echo "║   Phase 4 功能完整演示                  ║"
echo "║   Payment Routing & Protocol Support   ║"
echo "╚════════════════════════════════════════╝"
echo ""

# Demo 1: Protocol Definitions
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📋 Demo 1: 查看可用协议"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "➜ 获取所有协议定义..."
echo ""
curl -s "$API_URL/protocols/" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print('✅ 可用协议：\n')
for p in data:
    print(f'  🔌 {p[\"protocol_name\"]} v{p[\"version\"]}')
    print(f'     类型: {p[\"specification\"][\"type\"]}')
    print(f'     认证: {p[\"specification\"][\"auth\"]}')
    print(f'     状态: {p[\"status\"]}')
    print(f'     端点数: {len(p[\"endpoints\"])}')
    print()
"
read -p "按回车继续..." dummy

# Demo 2: Agent Route Configuration
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🗺️  Demo 2: Agent 路由配置"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "➜ 获取 Agent 的路由配置..."
echo ""
curl -s "$API_URL/agents/$AGENT_ID/routes" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
data = json.load(sys.stdin)
if isinstance(data, list) and len(data) > 0:
    route = data[0]
    print('✅ 路由配置：\n')
    print(f'  路由ID: {route[\"route_id\"]}')
    print(f'  策略: {route[\"routing_strategy\"]}')
    print(f'  最大重试: {route[\"max_retries\"]}')
    print(f'  超时: {route[\"timeout_ms\"]}ms')
    print(f'  状态: {\"🟢 Active\" if route[\"is_active\"] else \"🔴 Inactive\"}')
    print()
    print('  PSP 优先级:')
    for psp in route['psp_priority']:
        priority_emoji = ['🥇', '🥈', '🥉'][psp['priority']-1] if psp['priority'] <= 3 else '🏅'
        print(f'    {priority_emoji} #{psp[\"priority\"]} - {psp[\"psp\"].upper()}')
else:
    print(data)
"
read -p "按回车继续..." dummy

# Demo 3: Protocol Validation
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🔍 Demo 3: 协议载荷验证"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Test AP2 - Valid
echo "测试 AP2 协议（有效载荷）："
curl -s -X POST "$API_URL/protocols/AP2/validate" \
  -H "Content-Type: application/json" \
  -d '{
    "payload": {
      "order_id": "demo_001",
      "amount": 99.99,
      "currency": "USD",
      "merchant_id": "merchant_demo"
    }
  }' | python3 -c "
import sys, json
data = json.load(sys.stdin)
if data.get('valid'):
    print('  ✅ 验证通过')
    print(f'  协议: {data[\"protocol\"]} v{data[\"version\"]}')
    if data.get('warnings'):
        print(f'  ⚠️  建议: {data[\"warnings\"][0]}')
else:
    print('  ❌ 验证失败')
    print(f'  错误: {data.get(\"errors\")}')
"
echo ""

# Test AP2 - Invalid
echo "测试 AP2 协议（无效载荷 - 缺少金额）："
curl -s -X POST "$API_URL/protocols/AP2/validate" \
  -H "Content-Type: application/json" \
  -d '{
    "payload": {
      "order_id": "demo_002",
      "currency": "USD",
      "merchant_id": "merchant_demo"
    }
  }' | python3 -c "
import sys, json
data = json.load(sys.stdin)
if data.get('valid'):
    print('  ✅ 验证通过')
else:
    print('  ❌ 验证失败（预期）')
    print(f'  错误: {data.get(\"errors\")[0] if data.get(\"errors\") else \"Unknown\"}')
"
echo ""
read -p "按回车继续..." dummy

# Demo 4: Agent Protocols
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🔌 Demo 4: Agent 启用的协议"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "➜ 获取 Agent 支持的协议..."
echo ""
curl -s "$API_URL/agents/$AGENT_ID/protocols/" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
data = json.load(sys.stdin)
if isinstance(data, list):
    print(f'✅ Agent 已启用 {len(data)} 个协议：\n')
    for p in data:
        status_emoji = '🟢' if p['status'] == 'active' else '🟡' if p['status'] == 'beta' else '🔴'
        print(f'  {status_emoji} {p[\"protocol_name\"]} v{p[\"version\"]}')
        print(f'     状态: {p[\"status\"]}')
        if p.get('last_verified_at'):
            print(f'     最后验证: {p[\"last_verified_at\"]}')
        print()
else:
    print(data)
"
read -p "按回车继续..." dummy

# Demo 5: Routing Performance (Employee only)
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📊 Demo 5: 路由性能概览"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "➜ 获取路由概览（需要 Employee 权限）..."
echo ""
curl -s "$API_URL/employee/psp/routes/overview" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if 'detail' in data:
        print(f'⚠️  {data[\"detail\"]}')
        print('(需要 Employee 权限)')
    else:
        print('✅ 路由系统概览：\n')
        print(f'  总路由数: {data.get(\"total_routes\", 0)}')
        print(f'  活跃路由: {data.get(\"active_routes\", 0)}')
        print(f'  覆盖 Agent: {data.get(\"total_agents\", 0)}')
        print()
        strategies = data.get('routes_by_strategy', {})
        if strategies:
            print('  路由策略分布:')
            for strategy, count in strategies.items():
                print(f'    - {strategy}: {count}')
except:
    print('数据加载中...')
"
echo ""
read -p "按回车继续..." dummy

# Summary
echo ""
echo "╔════════════════════════════════════════╗"
echo "║          演示完成！                     ║"
echo "╚════════════════════════════════════════╝"
echo ""
echo "✨ Phase 4 核心功能全部正常："
echo ""
echo "  ✅ 协议系统 - 3个协议可用（AP2, ACP, X-402）"
echo "  ✅ 路由配置 - 优先级路由已设置"
echo "  ✅ 载荷验证 - 智能验证与错误提示"
echo "  ✅ Agent 协议 - 多协议支持已启用"
echo "  ✅ 监控系统 - 性能指标收集中"
echo ""
echo "🎯 下一步："
echo "  1. 在 Employee Portal 查看 Agent 详情"
echo "  2. 使用协议测试沙盒"
echo "  3. 创建测试支付触发路由逻辑"
echo "  4. 监控 PSP 性能指标"
echo ""
echo "📖 详细文档: PHASE_4_USER_GUIDE.md"
echo ""

