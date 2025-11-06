import re

# Parse agents_mgmt.py
with open('pivota_infra/routes/agents_mgmt.py') as f:
    mgmt_content = f.read()
    mgmt_routes = re.findall(r'@router\.(get|post|put|delete|patch)\("([^"]+)"\)', mgmt_content)

# Parse agent_management.py  
with open('pivota_infra/routes/agent_management.py') as f:
    management_content = f.read()
    management_routes = re.findall(r'@router\.(get|post|put|delete|patch)\("([^"]+)"\)', management_content)

print("=" * 60)
print("agents_mgmt.py (DISABLED) 提供的端点:")
print("=" * 60)
mgmt_set = set()
for method, path in mgmt_routes:
    full = f"{method.upper():6} /agents{path if not path.startswith('/') else path}"
    mgmt_set.add((method, path))
    print(full)

print("\n" + "=" * 60)
print("agent_management.py (ACTIVE) 提供的端点:")
print("=" * 60)
management_set = set()
for method, path in management_routes:
    # agent_management.py has prefix="/agents"
    full = f"{method.upper():6} /agents{path}"
    management_set.add((method, path))
    print(full)

print("\n" + "=" * 60)
print("❌ 缺失的端点（只在 agents_mgmt.py 有）:")
print("=" * 60)
missing = mgmt_set - management_set
if missing:
    for method, path in sorted(missing):
        print(f"{method.upper():6} /agents{path}")
else:
    print("✅ 无缺失！agent_management.py 覆盖所有功能")

print("\n" + "=" * 60)
print("🆕 额外的端点（只在 agent_management.py 有）:")
print("=" * 60)
extra = management_set - mgmt_set
for method, path in sorted(extra):
    print(f"{method.upper():6} /agents{path}")
