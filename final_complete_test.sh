#!/bin/bash

echo "等待部署完成..."
sleep 60

echo ""
echo "1️⃣ 获取 Admin Token..."
TOKEN=$(curl -sS https://web-production-fedb.up.railway.app/auth/admin-token | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
echo "   ✅ Token obtained"

echo ""
echo "2️⃣ 运行数据库迁移（添加 fulfillment 列）..."
curl -sS -X POST https://web-production-fedb.up.railway.app/admin/migrations/apply-psp-fixes \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" | python3 -m json.tool

echo ""
echo "3️⃣ 运行完整端到端测试..."
echo ""
cd ~/Desktop/Pivota\ Infra/Pivota-cursor-create-project-directory-structure-8344
python3 end_to_end_shopify_test.py <<< ""

echo ""
echo "================================================"
echo "✅ 测试完成！"
echo "================================================"
echo ""
echo "现在检查："
echo "1. 您的邮箱 - 应该收到 Shopify 确认邮件"
echo "2. chydantest.myshopify.com 后台 - 应该看到新订单"
echo "3. Merchant Portal - 应该看到所有订单"
echo ""
