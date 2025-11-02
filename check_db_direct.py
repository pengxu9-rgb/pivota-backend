#!/usr/bin/env python3
"""
Direct database check for PSP configurations
"""
import psycopg2
from urllib.parse import urlparse

# Railway database URL
DATABASE_URL = "postgresql://postgres:VJSIqWOeDbZOPZsLFJBxBKKOCFfNhLJv@junction.proxy.rlwy.net:15637/railway"

try:
    # Parse URL
    result = urlparse(DATABASE_URL)
    
    # Connect
    conn = psycopg2.connect(
        database=result.path[1:],
        user=result.username,
        password=result.password,
        host=result.hostname,
        port=result.port
    )
    
    cur = conn.cursor()
    
    print("=" * 80)
    print("🔍 DIRECT DATABASE CHECK - merchant_psps TABLE")
    print("=" * 80)
    print()
    
    # Query 1: 查看特定商户的所有 PSP
    print("Query 1: PSPs for merch_208139f7600dbf42")
    print("-" * 80)
    
    cur.execute("""
        SELECT 
            psp_id,
            provider,
            LENGTH(api_key) as api_key_len,
            SUBSTRING(api_key, 1, 15) as api_key_prefix,
            account_id,
            CASE WHEN secret_key IS NOT NULL THEN LENGTH(secret_key) ELSE 0 END as secret_len,
            status,
            connected_at
        FROM merchant_psps 
        WHERE merchant_id = 'merch_208139f7600dbf42'
        ORDER BY connected_at DESC
    """)
    
    rows = cur.fetchall()
    
    if rows:
        print(f"\n找到 {len(rows)} 条记录：\n")
        for row in rows:
            print(f"Provider: {row[1]}")
            print(f"  PSP ID: {row[0]}")
            print(f"  API Key: {row[3]}... (length: {row[2]})")
            print(f"  Account ID: {row[4]}")
            print(f"  Secret Key Length: {row[5]}")
            print(f"  Status: {row[6]}")
            print(f"  Connected At: {row[7]}")
            print()
    else:
        print("\n❌ 没有找到任何 PSP 配置！\n")
    
    # Query 2: 查看最近的所有 PSP（所有商户）
    print("\n" + "=" * 80)
    print("Query 2: Recent PSPs (all merchants)")
    print("-" * 80)
    
    cur.execute("""
        SELECT 
            psp_id,
            merchant_id,
            provider,
            LENGTH(api_key) as api_key_len,
            status,
            connected_at
        FROM merchant_psps 
        ORDER BY connected_at DESC 
        LIMIT 10
    """)
    
    recent = cur.fetchall()
    
    if recent:
        print(f"\n最近的 {len(recent)} 条记录：\n")
        for row in recent:
            print(f"{row[2]:10} | merchant: {row[1]:25} | key_len: {row[3]:3} | status: {row[4]:8} | {row[5]}")
    else:
        print("\n❌ 表中没有任何数据！\n")
    
    # Query 3: 统计
    print("\n" + "=" * 80)
    print("Query 3: Statistics by Provider")
    print("-" * 80)
    
    cur.execute("""
        SELECT 
            provider,
            status,
            COUNT(*) as count
        FROM merchant_psps 
        GROUP BY provider, status
        ORDER BY provider, status
    """)
    
    stats = cur.fetchall()
    
    if stats:
        print()
        for row in stats:
            print(f"{row[0]:15} | {row[1]:10} | Count: {row[2]}")
    
    print("\n" + "=" * 80)
    
    cur.close()
    conn.close()
    
except Exception as e:
    print(f"❌ 数据库连接错误: {e}")
    print()
    print("可能的原因：")
    print("1. Railway 数据库正在重启")
    print("2. 连接限制")
    print("3. 防火墙阻止")
    print()
    print("请稍后重试，或在 Railway Dashboard 中检查数据库状态")


