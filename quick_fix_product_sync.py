#!/usr/bin/env python3
"""
快速修复产品同步问题
"""

import asyncio
import sys
import os

# 添加项目路径到Python路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'pivota_infra'))

# 设置环境变量（如果需要）
if not os.getenv('DATABASE_URL'):
    print("❌ 需要设置 DATABASE_URL 环境变量")
    print("请运行: export DATABASE_URL='postgresql://user:password@host:5432/database'")
    sys.exit(1)

from db.database import database
from datetime import datetime, timedelta

async def fix_products_cache():
    """修复products_cache表的问题"""
    await database.connect()
    
    try:
        # 1. 修复所有缺少expires_at的记录
        print("🔧 修复缺少expires_at的记录...")
        await database.execute("""
            UPDATE products_cache 
            SET expires_at = datetime(cached_at, '+1 day')
            WHERE expires_at IS NULL
        """)
        
        # 2. 更新所有过期的产品为未过期（临时措施）
        print("🔧 延长过期产品的有效期...")
        await database.execute("""
            UPDATE products_cache 
            SET expires_at = datetime('now', '+7 days'),
                cache_status = 'fresh'
            WHERE expires_at < datetime('now')
        """)
        
        # 3. 检查结果
        result = await database.fetch_one("""
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN expires_at > datetime('now') THEN 1 ELSE 0 END) as valid
            FROM products_cache
        """)
        
        print(f"✅ 修复完成: {result['valid']}/{result['total']} 产品有效")
        
    finally:
        await database.disconnect()

if __name__ == "__main__":
    asyncio.run(fix_products_cache())
