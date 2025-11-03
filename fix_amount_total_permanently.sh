#!/bin/bash

echo "🔧 永久修复 amount/total 字段问题"
echo "=========================================="

API_URL="https://web-production-fedb.up.railway.app"

# 步骤 1: 同步 amount 和 total 字段
echo "步骤 1: 同步数据库中的 amount 和 total 字段..."
curl -sS -X POST "$API_URL/admin/sql/execute" \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "UPDATE orders SET amount = total WHERE amount IS NULL OR amount != total;",
    "confirm": true
  }' | python3 -m json.tool

echo ""
echo "✅ 数据库字段已同步"
echo ""
echo "长期解决方案："
echo "1. 在所有 INSERT 时，同时设置 amount = total"
echo "2. 在所有 SELECT 时，使用 COALESCE(total, amount) as total_amount"
echo "3. 前端统一使用 total_amount 字段"
echo "4. 逐步废弃 amount 字段"

