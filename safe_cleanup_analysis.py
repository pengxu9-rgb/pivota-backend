#!/usr/bin/env python3
"""
安全的端点清理分析工具
识别可以安全合并或删除的路由文件
"""
import os
import re
from collections import defaultdict
import json

def analyze_router_file(filepath):
    """分析单个路由文件"""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 提取基本信息
    info = {
        'file': os.path.basename(filepath),
        'size': len(content),
        'endpoints': [],
        'has_auth': 'Depends(' in content,
        'is_debug': 'debug' in filepath.lower() or 'test' in filepath.lower(),
        'is_temp': any(x in filepath.lower() for x in ['temp', 'fix', 'init', 'cleanup']),
        'imports': [],
        'description': ''
    }
    
    # 提取文件描述
    desc_match = re.search(r'"""(.*?)"""', content, re.DOTALL)
    if desc_match:
        info['description'] = desc_match.group(1).strip().split('\n')[0]
    
    # 提取路由
    route_pattern = r'@router\.(get|post|put|delete|patch)\(["\']([^"\']+)["\']'
    for match in re.finditer(route_pattern, content):
        info['endpoints'].append({
            'method': match.group(1).upper(),
            'path': match.group(2)
        })
    
    # 检查是否在 main.py 中被引用
    main_path = filepath.replace(os.path.basename(filepath), '../main.py')
    if os.path.exists(main_path):
        with open(main_path, 'r') as f:
            main_content = f.read()
        info['in_main'] = os.path.basename(filepath).replace('.py', '') in main_content
    else:
        info['in_main'] = False
    
    return info

def categorize_routers(routers):
    """将路由文件分类"""
    categories = {
        'core': [],          # 核心功能
        'debug': [],         # 调试/测试
        'temp': [],          # 临时/修复
        'duplicate': [],     # 功能重复
        'unused': [],        # 未使用
        'consolidate': []    # 可合并
    }
    
    # 识别重复功能的文件组
    duplicate_groups = {
        'agent_sdk': ['agent_sdk_ready.py', 'agent_sdk_fixed.py'],
        'agent_metrics': ['agent_metrics.py', 'agent_metrics_routes.py', 'agent_metrics_v1.py'],
        'auth': ['auth.py', 'auth_routes.py', 'auth_ws_routes.py'],
        'dashboard': ['dashboard_routes.py', 'dashboard_api.py'],
    }
    
    for router in routers:
        filename = router['file']
        
        # 未在 main.py 中使用
        if not router['in_main']:
            categories['unused'].append(router)
        # 调试/测试文件
        elif router['is_debug']:
            categories['debug'].append(router)
        # 临时文件
        elif router['is_temp']:
            categories['temp'].append(router)
        # 检查是否属于重复组
        else:
            is_duplicate = False
            for group_name, files in duplicate_groups.items():
                if filename in files:
                    router['duplicate_group'] = group_name
                    categories['duplicate'].append(router)
                    is_duplicate = True
                    break
            
            if not is_duplicate:
                categories['core'].append(router)
    
    return categories

def generate_recommendations(categories):
    """生成清理建议"""
    recommendations = []
    
    # 1. 未使用的文件
    if categories['unused']:
        recommendations.append({
            'action': 'SAFE_TO_DELETE',
            'files': [r['file'] for r in categories['unused']],
            'reason': 'Not referenced in main.py',
            'risk': 'LOW'
        })
    
    # 2. 调试文件
    debug_files = [r for r in categories['debug'] if r['in_main']]
    if debug_files:
        recommendations.append({
            'action': 'MOVE_TO_DEBUG_MODE',
            'files': [r['file'] for r in debug_files],
            'reason': 'Debug endpoints should be conditional',
            'risk': 'MEDIUM',
            'suggestion': 'Wrap in DEBUG_MODE check in main.py'
        })
    
    # 3. 临时文件
    temp_auth_required = [r for r in categories['temp'] if not r['has_auth']]
    if temp_auth_required:
        recommendations.append({
            'action': 'ADD_AUTHENTICATION',
            'files': [r['file'] for r in temp_auth_required],
            'reason': 'Temporary endpoints need authentication',
            'risk': 'HIGH',
            'suggestion': 'Add require_admin dependency'
        })
    
    # 4. 重复文件
    duplicate_groups = defaultdict(list)
    for router in categories['duplicate']:
        if 'duplicate_group' in router:
            duplicate_groups[router['duplicate_group']].append(router)
    
    for group_name, routers in duplicate_groups.items():
        if len(routers) > 1:
            # 选择保留最完整的版本
            routers_sorted = sorted(routers, key=lambda x: len(x['endpoints']), reverse=True)
            keep = routers_sorted[0]
            merge = routers_sorted[1:]
            
            recommendations.append({
                'action': 'MERGE_DUPLICATES',
                'keep': keep['file'],
                'merge': [r['file'] for r in merge],
                'reason': f'Duplicate {group_name} functionality',
                'risk': 'MEDIUM',
                'endpoints_to_migrate': sum(len(r['endpoints']) for r in merge)
            })
    
    return recommendations

def main():
    routes_dir = "/Users/pengchydan/Desktop/Pivota Infra/Pivota-cursor-create-project-directory-structure-8344/pivota_infra/routes"
    
    # 分析所有路由文件
    routers = []
    for filename in os.listdir(routes_dir):
        if filename.endswith('.py') and not filename.startswith('__'):
            filepath = os.path.join(routes_dir, filename)
            try:
                router_info = analyze_router_file(filepath)
                routers.append(router_info)
            except Exception as e:
                print(f"Error analyzing {filename}: {e}")
    
    # 分类
    categories = categorize_routers(routers)
    
    # 生成建议
    recommendations = generate_recommendations(categories)
    
    # 输出报告
    print("=" * 80)
    print("SAFE CLEANUP ANALYSIS REPORT")
    print("=" * 80)
    
    print(f"\nTotal router files: {len(routers)}")
    print(f"Files in main.py: {sum(1 for r in routers if r['in_main'])}")
    print(f"Unused files: {len(categories['unused'])}")
    print(f"Debug files: {len(categories['debug'])}")
    print(f"Temp files: {len(categories['temp'])}")
    print(f"Duplicate groups: {len(set(r.get('duplicate_group') for r in categories['duplicate'] if r.get('duplicate_group')))}")
    
    print("\n" + "=" * 80)
    print("RECOMMENDATIONS (Safest to Riskiest)")
    print("=" * 80)
    
    for i, rec in enumerate(recommendations, 1):
        print(f"\n{i}. {rec['action']} (Risk: {rec['risk']})")
        print(f"   Reason: {rec['reason']}")
        
        if rec['action'] == 'SAFE_TO_DELETE':
            print(f"   Files: {', '.join(rec['files'])}")
        elif rec['action'] == 'MERGE_DUPLICATES':
            print(f"   Keep: {rec['keep']}")
            print(f"   Merge: {', '.join(rec['merge'])}")
            print(f"   Endpoints to migrate: {rec['endpoints_to_migrate']}")
        elif 'suggestion' in rec:
            print(f"   Suggestion: {rec['suggestion']}")
            print(f"   Files: {', '.join(rec['files'])}")
    
    # 保存详细报告
    report = {
        'summary': {
            'total_files': len(routers),
            'in_use': sum(1 for r in routers if r['in_main']),
            'unused': len(categories['unused']),
            'debug': len(categories['debug']),
            'temp': len(categories['temp'])
        },
        'categories': {k: [r['file'] for r in v] for k, v in categories.items()},
        'recommendations': recommendations
    }
    
    with open('cleanup_report.json', 'w') as f:
        json.dump(report, f, indent=2)
    
    print("\n✅ Detailed report saved to cleanup_report.json")

if __name__ == "__main__":
    main()
