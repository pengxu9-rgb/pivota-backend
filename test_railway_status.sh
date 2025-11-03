#!/bin/bash

echo "测试 Railway 后端状态..."
echo ""

# Test health endpoint
echo "1. 测试健康检查..."
curl -s https://web-production-fedb.up.railway.app/health | jq '.' || echo "Health check failed"

echo ""
echo "2. 测试 Agent API..."
curl -s https://web-production-fedb.up.railway.app/agents/health | jq '.' || echo "Agent health check failed"

echo ""
echo "3. 测试 Merchant API..."
curl -s https://web-production-fedb.up.railway.app/merchant/health | jq '.' || echo "Merchant health check failed"

echo ""
echo "测试完成！"
