#!/usr/bin/env python3
"""
自动重构脚本 - 将 mcp_* 系统迁移到 merchant_stores
"""

import os
import re
import shutil
from datetime import datetime
from pathlib import Path

# 创建备份目录
BACKUP_DIR = f"backup_before_refactor_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

def backup_file(file_path):
    """备份文件"""
    backup_path = os.path.join(BACKUP_DIR, file_path)
    os.makedirs(os.path.dirname(backup_path), exist_ok=True)
    shutil.copy2(file_path, backup_path)

def refactor_file(file_path):
    """重构单个文件"""
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    original_content = content
    
    # 1. 替换简单的字段引用
    replacements = {
        # merchant_onboarding 表字段
        'merchant.get("mcp_connected")': 'True',  # 如果走到这里说明已连接
        'merchant["mcp_connected"]': 'True',
        'mcp_connected = true': '1 = 1',  # SQL中的替换
        'mcp_connected = :mcp_connected': '1 = 1',
        
        # 获取平台信息
        'merchant.get("mcp_platform")': 'store_info.get("platform")',
        'merchant["mcp_platform"]': 'store_info["platform"]',
        
        # 获取商店域名
        'merchant.get("mcp_shop_domain")': 'store_info.get("domain")',
        'merchant["mcp_shop_domain"]': 'store_info["domain"]',
        
        # 获取访问令牌
        'merchant.get("mcp_access_token")': 'store_info.get("api_key")',
        'merchant["mcp_access_token"]': 'store_info["api_key"]',
    }
    
    for old, new in replacements.items():
        content = content.replace(old, new)
    
    # 2. 替换复杂的模式
    # 检查 mcp_connected 的条件语句
    content = re.sub(
        r'if\s+not\s+merchant\.get\(["\'"]mcp_connected["\'"]?\)\s*:',
        'stores = await get_merchant_active_stores(merchant_id)\nif not stores:',
        content
    )
    
    # 3. 更新导入语句
    if 'mcp_connected' in original_content and 'from services.merchant_store_service import' not in content:
        # 在文件开头添加导入
        import_line = "from services.merchant_store_service import get_merchant_active_stores, get_primary_store\n"
        
        # 找到其他导入语句的位置
        import_match = re.search(r'^from\s+\w+', content, re.MULTILINE)
        if import_match:
            insert_pos = import_match.start()
            content = content[:insert_pos] + import_line + content[insert_pos:]
        else:
            content = import_line + content
    
    # 4. 更新产品获取逻辑
    # 旧的 v1 端点调用
    content = re.sub(
        r'["\']\/products\/\{merchant_id\}["\']',
        '"/products/v2/{merchant_id}"',
        content
    )
    
    # 只有内容真正改变时才写入
    if content != original_content:
        backup_file(file_path)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return True
    
    return False

def update_product_routes():
    """专门更新产品路由"""
    print("\n🔧 更新产品路由...")
    
    routes_file = "pivota_infra/routes/product_routes.py"
    if os.path.exists(routes_file):
        with open(routes_file, 'r') as f:
            content = f.read()
        
        # 简化产品获取逻辑
        new_logic = '''
    # 直接从缓存获取，不再检查 mcp_connected
    try:
        # 获取商家的所有活跃商店
        stores = await get_merchant_active_stores(merchant_id)
        if not stores:
            raise HTTPException(
                status_code=404,
                detail=f"No active stores found for merchant {merchant_id}"
            )
        
        # 获取主商店
        primary_store = stores[0]
        platform = primary_store["platform"]
        
        # 从缓存获取产品
        cached = await get_cached_products(merchant_id, platform)
        products = [c["product_data"] for c in cached[:limit]]
'''
        
        # 替换旧逻辑
        content = re.sub(
            r'#\s*1\.\s*获取商户信息.*?#\s*3\.\s*尝试从缓存读取',
            new_logic + '\n    # 3. 尝试从缓存读取',
            content,
            flags=re.DOTALL
        )
        
        backup_file(routes_file)
        with open(routes_file, 'w') as f:
            f.write(content)

def generate_migration_sql():
    """生成数据迁移SQL"""
    sql = '''
-- 数据迁移脚本：将 mcp_* 数据迁移到 merchant_stores

-- 1. 备份现有数据
CREATE TABLE merchant_onboarding_backup AS SELECT * FROM merchant_onboarding;
CREATE TABLE merchant_stores_backup AS SELECT * FROM merchant_stores;

-- 2. 迁移数据到 merchant_stores
INSERT INTO merchant_stores (
    store_id,
    merchant_id,
    platform,
    name,
    domain,
    api_key,
    status,
    connected_at
)
SELECT 
    'legacy_' || merchant_id || '_' || mcp_platform as store_id,
    merchant_id,
    mcp_platform as platform,
    business_name as name,
    mcp_shop_domain as domain,
    mcp_access_token as api_key,
    'active' as status,
    COALESCE(mcp_connected_at, NOW()) as connected_at
FROM merchant_onboarding
WHERE mcp_connected = true
AND merchant_id NOT IN (
    SELECT DISTINCT merchant_id FROM merchant_stores
);

-- 3. 验证迁移
SELECT 
    'Original MCP merchants:' as description,
    COUNT(*) as count
FROM merchant_onboarding 
WHERE mcp_connected = true
UNION ALL
SELECT 
    'Migrated to merchant_stores:' as description,
    COUNT(DISTINCT merchant_id) as count
FROM merchant_stores;

-- 4. 清理（在确认无误后执行）
-- UPDATE merchant_onboarding SET mcp_connected = null, mcp_platform = null, 
--        mcp_shop_domain = null, mcp_access_token = null;
'''
    
    with open('migrate_mcp_to_stores.sql', 'w') as f:
        f.write(sql)
    
    print("✅ 生成迁移SQL: migrate_mcp_to_stores.sql")

def main():
    print("🚀 开始自动重构...")
    print(f"📁 创建备份目录: {BACKUP_DIR}")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    
    # 统计
    total_files = 0
    modified_files = 0
    
    # 遍历所有Python文件
    for path in Path("pivota_infra").rglob("*.py"):
        if any(skip in str(path) for skip in ['venv', '__pycache__', '.git', BACKUP_DIR]):
            continue
            
        total_files += 1
        if refactor_file(str(path)):
            modified_files += 1
            print(f"  ✅ 修改: {path}")
    
    # 更新特定文件
    update_product_routes()
    
    # 生成SQL
    generate_migration_sql()
    
    print(f"\n📊 重构完成:")
    print(f"  - 扫描文件: {total_files}")
    print(f"  - 修改文件: {modified_files}")
    print(f"  - 备份位置: {BACKUP_DIR}")
    
    print("\n📝 下一步:")
    print("  1. 检查修改: git diff")
    print("  2. 运行测试: pytest")
    print("  3. 执行SQL: psql < migrate_mcp_to_stores.sql")
    print("  4. 部署代码: git commit && git push")

if __name__ == "__main__":
    main()




