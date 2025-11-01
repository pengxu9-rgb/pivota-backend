#!/usr/bin/env python3
"""
分析重构影响范围 - 快速评估需要改动的文件
"""

import os
import re
import asyncio
from pathlib import Path
from collections import defaultdict

# 需要重构的模式
PATTERNS_TO_REFACTOR = {
    'mcp_connected': r'mcp_connected',
    'mcp_platform': r'mcp_platform', 
    'mcp_shop_domain': r'mcp_shop_domain',
    'mcp_access_token': r'mcp_access_token',
    'mcp_connected_at': r'mcp_connected_at',
    'v1_endpoint': r'/products/\{merchant_id\}(?!.*v2)',
    'legacy_check': r'merchant_onboarding.*mcp_'
}

def analyze_file(file_path):
    """分析单个文件"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
            
        findings = defaultdict(list)
        for pattern_name, pattern in PATTERNS_TO_REFACTOR.items():
            matches = list(re.finditer(pattern, content, re.IGNORECASE))
            if matches:
                for match in matches:
                    line_num = content[:match.start()].count('\n') + 1
                    findings[pattern_name].append(line_num)
                    
        return findings
    except:
        return {}

def analyze_project(root_dir):
    """分析整个项目"""
    print("🔍 开始分析项目影响范围...\n")
    
    impact_files = {}
    test_files = {}
    total_matches = 0
    
    # 遍历所有Python文件
    for path in Path(root_dir).rglob("*.py"):
        # 跳过虚拟环境和缓存
        if any(skip in str(path) for skip in ['venv', '__pycache__', '.git', 'node_modules']):
            continue
            
        findings = analyze_file(path)
        if findings:
            rel_path = os.path.relpath(path, root_dir)
            if 'test' in rel_path.lower():
                test_files[rel_path] = findings
            else:
                impact_files[rel_path] = findings
            total_matches += sum(len(lines) for lines in findings.values())
    
    # 生成报告
    print("=" * 60)
    print("📊 重构影响分析报告")
    print("=" * 60)
    
    print(f"\n📁 需要修改的业务代码文件: {len(impact_files)}个")
    for file, findings in sorted(impact_files.items()):
        print(f"\n  📄 {file}")
        for pattern, lines in findings.items():
            print(f"     - {pattern}: 第 {', '.join(map(str, lines[:5]))} 行" + 
                  (f" 等{len(lines)}处" if len(lines) > 5 else ""))
    
    print(f"\n🧪 需要更新的测试文件: {len(test_files)}个")
    for file, findings in sorted(test_files.items()):
        print(f"\n  📄 {file}")
        for pattern, lines in findings.items():
            print(f"     - {pattern}: {len(lines)}处")
    
    print("\n" + "=" * 60)
    print("📈 统计摘要:")
    print(f"  - 总影响文件数: {len(impact_files) + len(test_files)}")
    print(f"  - 业务代码文件: {len(impact_files)}")
    print(f"  - 测试代码文件: {len(test_files)}")
    print(f"  - 总匹配次数: {total_matches}")
    
    # 工作量评估
    estimated_hours = (len(impact_files) * 0.5 + len(test_files) * 0.25)
    print(f"\n⏱️  预计工作量: {estimated_hours:.1f} 小时")
    print(f"  - 代码修改: {len(impact_files) * 0.5:.1f} 小时")
    print(f"  - 测试更新: {len(test_files) * 0.25:.1f} 小时")
    
    print("\n✅ 好消息:")
    print("  - 大部分是简单的查找替换")
    print("  - API接口保持不变")
    print("  - 前端改动极少")
    
    # 生成修改脚本
    print("\n💡 下一步建议:")
    print("  1. 运行 backup_database.py 备份数据")
    print("  2. 运行 auto_refactor.py 自动修改代码")
    print("  3. 运行测试验证功能")
    
    return impact_files, test_files

if __name__ == "__main__":
    # 分析项目
    project_root = "pivota_infra"
    if os.path.exists(project_root):
        analyze_project(project_root)
    else:
        print(f"❌ 找不到目录: {project_root}")
        print("请在项目根目录运行此脚本")




