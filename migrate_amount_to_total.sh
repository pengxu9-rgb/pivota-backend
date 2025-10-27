#!/bin/bash

echo "🔧 迁移 amount 到 total 字段"
echo "=========================================="

API_URL="https://web-production-fedb.up.railway.app"

echo "步骤 1: 将所有 amount 值复制到 total（如果 total 为空）"
curl -sS -X POST "$API_URL/admin/sql/execute" \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "UPDATE orders SET total = amount WHERE total IS NULL AND amount IS NOT NULL;",
    "confirm": true
  }' | python3 -m json.tool

echo ""
echo "步骤 2: 确保所有 total 值不为空"
curl -sS -X POST "$API_URL/admin/sql/execute" \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "UPDATE orders SET total = 0 WHERE total IS NULL;",
    "confirm": true
  }' | python3 -m json.tool

echo ""
echo "✅ 迁移完成！"
echo ""
echo "注意："
echo "- 所有代码现在只使用 'total' 字段"
echo "- 'amount' 字段已被废弃"
echo "- 前端应该使用 'total' 或 'total_amount'"
echo ""
echo "未来可以执行："
echo "ALTER TABLE orders DROP COLUMN amount;"
echo "来彻底删除 amount 列"
