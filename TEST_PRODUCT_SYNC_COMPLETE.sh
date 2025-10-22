#!/bin/bash

echo "🧪 完整产品同步测试"
echo "========================================="
echo ""

BASE_URL="https://web-production-fedb.up.railway.app"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}📝 测试说明:${NC}"
echo "本脚本将测试完整的产品同步流程："
echo "1. 检查backend健康状态"
echo "2. 检查当前产品缓存"
echo "3. 测试产品同步endpoint（需要auth token）"
echo "4. 验证产品已同步"
echo ""

# Test 1: Backend health
echo -e "${YELLOW}Test 1: Backend健康检查${NC}"
python3 << 'PYTHON1'
import urllib.request, json
try:
    data = json.loads(urllib.request.urlopen('https://web-production-fedb.up.railway.app/health').read())
    if data.get('status') == 'ok':
        print('✅ Backend健康')
    else:
        print('❌ Backend不健康')
        exit(1)
except Exception as e:
    print(f'❌ 无法连接backend: {e}')
    exit(1)
PYTHON1
echo ""

# Test 2: Check current products
echo -e "${YELLOW}Test 2: 检查当前产品缓存${NC}"
python3 << 'PYTHON2'
import urllib.request, json
try:
    data = json.loads(urllib.request.urlopen('https://web-production-fedb.up.railway.app/test/check-products-cache').read())
    total = data.get('total_products', 0)
    print(f'📦 当前缓存产品总数: {total}')
    
    # Group by merchant
    merchants = {}
    for product in data.get('sample', []):
        mid = product.get('merchant_id')
        if mid:
            merchants[mid] = merchants.get(mid, 0) + 1
    
    print('\n按merchant分组:')
    for mid, count in merchants.items():
        print(f'  • {mid}: {count} products')
    print('')
except Exception as e:
    print(f'❌ 检查失败: {e}')
PYTHON2
echo ""

# Test 3: Instructions for sync test
echo -e "${YELLOW}Test 3: 产品同步测试${NC}"
echo ""
echo -e "${BLUE}📋 请按以下步骤操作:${NC}"
echo ""
echo "1️⃣ 登录Employee Portal"
echo "   URL: https://employee.pivota.cc/login"
echo "   账号: employee@pivota.com"
echo "   密码: Admin123!"
echo ""
echo "2️⃣ 找到有Shopify连接的merchant"
echo "   查看MCP列有 ✓ shopify 的merchant"
echo "   例如: chydantest (merch_208139f7600dbf42)"
echo ""
echo "3️⃣ 点击Actions (⋮) → 'Sync Products'"
echo ""
echo "4️⃣ 应该看到成功消息:"
echo "   ✅ Successfully synced X products from shopify!"
echo ""
echo "5️⃣ 刷新页面，检查Products列"
echo "   应该显示产品数量和同步时间"
echo ""
echo -e "${GREEN}或者使用curl测试:${NC}"
echo ""
echo "# 获取auth token (在Employee Portal的DevTools Console运行):"
echo "localStorage.getItem('employee_token')"
echo ""
echo "# 然后运行:"
echo "curl -X POST '$BASE_URL/products/sync/' \\"
echo "  -H 'Content-Type: application/json' \\"
echo "  -H 'Authorization: Bearer YOUR_TOKEN' \\"
echo "  -d '{\"merchant_id\": \"merch_208139f7600dbf42\", \"force_refresh\": false, \"limit\": 250}'"
echo ""
echo "========================================="
echo -e "${GREEN}✅ 测试脚本准备完成！${NC}"
echo ""
echo "💡 提示: 确保Railway和Vercel都已部署完成"
echo "   Railway: commit b05923cc + 3939f666 + 8b481d65"
echo "   Vercel: commit bfa8779"

