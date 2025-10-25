#!/usr/bin/env python3
"""
分析所有端点，检查冲突和组织结构
"""
import os
import re
from collections import defaultdict
import ast

def extract_routes_from_file(filepath):
    """从文件中提取路由定义"""
    routes = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
            
        # 查找 router 定义
        router_match = re.search(r'router\s*=\s*APIRouter\((.*?)\)', content, re.DOTALL)
        if router_match:
            router_args = router_match.group(1)
            prefix_match = re.search(r'prefix\s*=\s*["\']([^"\']+)["\']', router_args)
            prefix = prefix_match.group(1) if prefix_match else ""
            
            # 查找所有路由装饰器
            route_patterns = [
                r'@router\.(get|post|put|delete|patch)\(["\']([^"\']+)["\']',
                r'@router\.(get|post|put|delete|patch)\(\s*["\']([^"\']+)["\']'
            ]
            
            for pattern in route_patterns:
                for match in re.finditer(pattern, content):
                    method = match.group(1).upper()
                    path = match.group(2)
                    full_path = prefix + path
                    routes.append({
                        'method': method,
                        'path': full_path,
                        'file': os.path.basename(filepath)
                    })
    except Exception as e:
        print(f"Error processing {filepath}: {e}")
    
    return routes

def analyze_all_routes():
    """分析所有路由文件"""
    routes_dir = "/Users/pengchydan/Desktop/Pivota Infra/Pivota-cursor-create-project-directory-structure-8344/pivota_infra/routes"
    all_routes = []
    
    # 扫描所有路由文件
    for filename in os.listdir(routes_dir):
        if filename.endswith('.py') and not filename.startswith('__'):
            filepath = os.path.join(routes_dir, filename)
            routes = extract_routes_from_file(filepath)
            all_routes.extend(routes)
    
    # 按路径分组
    routes_by_path = defaultdict(list)
    for route in all_routes:
        key = f"{route['method']} {route['path']}"
        routes_by_path[key].append(route)
    
    # 分析结果
    print("=" * 80)
    print("ENDPOINT ANALYSIS REPORT")
    print("=" * 80)
    
    # 1. 查找冲突的端点
    print("\n1. CONFLICTING ENDPOINTS (同一路径被多个文件定义):")
    print("-" * 60)
    conflicts = {k: v for k, v in routes_by_path.items() if len(v) > 1}
    if conflicts:
        for path, routes in sorted(conflicts.items()):
            print(f"\n{path}:")
            for route in routes:
                print(f"  - {route['file']}")
    else:
        print("✅ No conflicts found")
    
    # 2. 按前缀分组统计
    print("\n\n2. ENDPOINTS BY PREFIX:")
    print("-" * 60)
    prefix_stats = defaultdict(lambda: {'count': 0, 'files': set()})
    for route in all_routes:
        prefix = '/' + route['path'].split('/')[1] if route['path'].count('/') > 0 else '/'
        prefix_stats[prefix]['count'] += 1
        prefix_stats[prefix]['files'].add(route['file'])
    
    for prefix, stats in sorted(prefix_stats.items()):
        print(f"\n{prefix:<20} : {stats['count']:>3} endpoints from {len(stats['files'])} files")
        for file in sorted(stats['files']):
            print(f"  - {file}")
    
    # 3. 临时/调试端点
    print("\n\n3. TEMPORARY/DEBUG ENDPOINTS (should be removed in production):")
    print("-" * 60)
    temp_keywords = ['debug', 'test', 'temp', 'fix', 'init', 'cleanup', 'simulate']
    temp_endpoints = []
    for route in all_routes:
        if any(keyword in route['path'].lower() or keyword in route['file'].lower() for keyword in temp_keywords):
            temp_endpoints.append(route)
    
    if temp_endpoints:
        for route in sorted(temp_endpoints, key=lambda x: x['path']):
            print(f"{route['method']:<6} {route['path']:<50} ({route['file']})")
    else:
        print("✅ No temporary endpoints found")
    
    # 4. 认证检查建议
    print("\n\n4. PUBLIC ENDPOINTS (may need authentication):")
    print("-" * 60)
    public_keywords = ['/setup/', '/init/', '/fix/', '/cleanup/']
    public_endpoints = []
    for route in all_routes:
        if any(keyword in route['path'] for keyword in public_keywords):
            public_endpoints.append(route)
    
    if public_endpoints:
        for route in sorted(public_endpoints, key=lambda x: x['path']):
            print(f"{route['method']:<6} {route['path']:<50} ({route['file']})")
    
    # 5. 统计总览
    print("\n\n5. SUMMARY:")
    print("-" * 60)
    print(f"Total endpoints: {len(all_routes)}")
    print(f"Total route files: {len(set(r['file'] for r in all_routes))}")
    print(f"Conflicting endpoints: {len(conflicts)}")
    print(f"Temporary/debug endpoints: {len(temp_endpoints)}")
    
    return all_routes, conflicts

if __name__ == "__main__":
    analyze_all_routes()


