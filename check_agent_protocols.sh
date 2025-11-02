#!/bin/bash

# Check agent protocols in database
API_URL="https://web-production-fedb.up.railway.app"
AGENT_ID="agent_ee38f2b3645a2ec2"

if [ -z "$1" ]; then
    echo "Usage: $0 <TOKEN>"
    exit 1
fi

TOKEN=$1

echo "========================================="
echo "检查 Agent 协议配置"
echo "========================================="
echo ""

echo "📋 查询 Agent 的协议列表..."
curl -s "$API_URL/agents/$AGENT_ID/protocols/" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(f'\n找到 {len(data)} 个协议：\n')
for p in data:
    print(f'  协议: {p[\"protocol_name\"]} v{p[\"version\"]}')
    print(f'  状态: {p[\"status\"]}')
    print(f'  创建时间: {p[\"created_at\"]}')
    if p.get('last_verified_at'):
        print(f'  最后验证: {p[\"last_verified_at\"]}')
    print()
"
echo ""
echo "========================================="
echo ""
echo "🔍 分析："
echo "- REST v1.0 是 Phase 2 (Migration 008) 的遗留协议"
echo "- AP2 和 ACP 应该在 Migration 010 中添加"
echo "- X-402 是 beta 状态，默认未启用"
echo ""
echo "💡 建议："
echo "1. 禁用旧的 REST v1.0 协议"
echo "2. 确认 AP2 和 ACP 已正确添加"
echo "3. 可选：手动启用 X-402 (beta)"
echo ""

