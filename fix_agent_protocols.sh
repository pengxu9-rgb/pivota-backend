#!/bin/bash

# Fix Agent Protocols - Replace REST v1.0 with Phase 4 protocols
API_URL="https://web-production-fedb.up.railway.app"

if [ -z "$1" ]; then
    echo "Usage: $0 <ADMIN_TOKEN> [AGENT_ID]"
    echo ""
    echo "Examples:"
    echo "  $0 YOUR_TOKEN                    # Fix all agents"
    echo "  $0 YOUR_TOKEN agent_12345        # Fix specific agent"
    exit 1
fi

TOKEN=$1
AGENT_ID=$2

echo "========================================="
echo "修复 Agent 协议配置"
echo "========================================="
echo ""

# Wait for deployment
echo "⏳ 等待 Railway 部署完成（30秒）..."
sleep 30

# Check current status first
echo "📊 检查当前协议状态..."
curl -s "$API_URL/admin/agents/protocols-status" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    stats = data.get('protocol_statistics', [])
    print('\n当前协议分布：\n')
    for stat in stats:
        print(f'  {stat[\"protocol\"]} ({stat[\"status\"]}): {stat[\"agent_count\"]} agents')
except:
    print('  （正在部署中...）')
"
echo ""
echo ""

# Run the fix
echo "🔧 执行协议修复..."

if [ -z "$AGENT_ID" ]; then
    echo "➜ 修复所有 agents..."
    FIX_RESULT=$(curl -s -X POST "$API_URL/admin/agents/fix-protocols" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json")
else
    echo "➜ 修复 agent: $AGENT_ID..."
    FIX_RESULT=$(curl -s -X POST "$API_URL/admin/agents/fix-protocols?agent_id=$AGENT_ID" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json")
fi

echo "$FIX_RESULT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if data.get('status') == 'success':
        print(f'\n✅ {data.get(\"message\")}')
        print(f'   更新的 Agents: {data.get(\"agents_updated\", 0)}')
        print()
        
        summary = data.get('summary', {})
        print('📊 更新后状态:')
        print(f'   活跃协议总数: {summary.get(\"total_active_protocols\", 0)}')
        print(f'   已配置的 Agents: {summary.get(\"agents_with_protocols\", 0)}')
        print()
        
        samples = summary.get('sample_agent_protocols', {})
        if samples:
            print('示例配置:')
            for agent_id, protocols in samples.items():
                print(f'   {agent_id}:')
                for proto in protocols:
                    print(f'      - {proto}')
    else:
        print(f'❌ 修复失败: {data.get(\"detail\", \"Unknown error\")}')
except Exception as e:
    print(f'❌ 解析错误: {e}')
    print(f'原始响应: {sys.stdin.read()}')
"
echo ""
echo ""

# Verify the fix
echo "✓ 验证修复结果..."
if [ -z "$AGENT_ID" ]; then
    TEST_AGENT="agent_ee38f2b3645a2ec2"
else
    TEST_AGENT=$AGENT_ID
fi

echo "  查询 $TEST_AGENT 的协议..."
curl -s "$API_URL/agents/$TEST_AGENT/protocols/" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(f'\n  ✅ Agent 现在有 {len(data)} 个协议：\n')
    for p in data:
        status_emoji = '🟢' if p['status'] == 'active' else '🔴'
        print(f'    {status_emoji} {p[\"protocol_name\"]} v{p[\"version\"]} ({p[\"status\"]})')
except Exception as e:
    print(f'  错误: {e}')
"
echo ""
echo ""

echo "========================================="
echo "协议修复完成！"
echo "========================================="
echo ""
echo "现在应该显示:"
echo "  - AP2 v2.0 (active)"
echo "  - ACP v1.0 (active)"
echo "  - X-402 v3.1 (active)"
echo ""
echo "刷新 Employee Portal 查看更新后的协议列表"
echo ""

