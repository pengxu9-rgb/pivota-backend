#!/usr/bin/env python3
import ast
import sys

file_path = "pivota_infra/main.py"

try:
    with open(file_path, 'r') as f:
        code = f.read()
    
    ast.parse(code)
    print(f"✅ {file_path} - No syntax errors found!")
    sys.exit(0)
except SyntaxError as e:
    print(f"❌ Syntax Error in {file_path}:")
    print(f"   Line {e.lineno}: {e.msg}")
    print(f"   Text: {e.text}")
    print(f"   Offset: {' ' * (e.offset - 1) if e.offset else ''}^")
    sys.exit(1)
except Exception as e:
    print(f"❌ Error: {e}")
    sys.exit(1)



