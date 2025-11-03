#!/usr/bin/env python3
"""批量修复所有语法错误的文件 - 删除重复代码"""

import os
import sys

files_to_fix = [
    ("admin_fix_agent_protocols.py", 156),
    ("protocol_routes.py", 487),
    ("payment_routing_routes.py", 493),
    ("agent_integration_status.py", 74),
    ("admin_seed_agent_routing_history.py", 217),
    ("employee_routing_dashboard.py", 505),
    ("agent_protocol_test.py", 79),
    ("admin_run_migration_011.py", 204),
    ("agent_settlement_routes.py", 117),
    ("routing_governance.py", 587),
    ("admin_run_migration_010.py", 245),
    ("agent_revenue_api.py", 446),
    ("admin_governance.py", 196),
    ("admin_run_migration_013.py", 156),
    ("agent_routing_api.py", 321),
    ("admin_run_migration_012.py", 263),
    ("admin_seed_routing_logs.py", 185),
    ("merchant_commission_api.py", 105),
    ("admin_cleanup_routing_test_data.py", 113),
]

fixed = 0
failed = 0

for filename, error_line in files_to_fix:
    if not os.path.exists(filename):
        print(f"⏭️  {filename} 不存在，跳过")
        continue
    
    try:
        # 读取前 N-1 行（删除重复部分）
        with open(filename, 'r') as f:
            lines = f.readlines()
        
        # 保留到错误行之前
        keep_lines = error_line - 1
        
        # 写回
        with open(filename, 'w') as f:
            f.writelines(lines[:keep_lines])
        
        # 验证语法
        with open(filename, 'r') as f:
            compile(f.read(), filename, 'exec')
        
        print(f"✅ {filename} (保留 {keep_lines} 行，删除 {len(lines) - keep_lines} 行)")
        fixed += 1
    except Exception as e:
        print(f"❌ {filename} 修复失败: {e}")
        failed += 1

print(f"\n========================================")
print(f"✅ 成功修复: {fixed} 个文件")
print(f"❌ 失败: {failed} 个文件")
print(f"========================================")
