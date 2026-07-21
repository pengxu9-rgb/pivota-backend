from __future__ import annotations

import os
import sys
from pathlib import Path

# Canonical test database. Individual test modules setdefault their own
# DATABASE_URL fallbacks (often Postgres); pinning here (conftest imports run
# first) makes the whole suite deterministic on a throwaway sqlite file instead
# of whatever happens to listen on localhost:5432. An explicitly exported
# DATABASE_URL still wins (e.g. tests/integration against a real database).
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///pivota_test.db")

ROOT = Path(__file__).resolve().parents[1]
root_str = str(ROOT)
if root_str not in sys.path:
    sys.path.insert(0, root_str)
elif sys.path[0] != root_str:
    sys.path.remove(root_str)
    sys.path.insert(0, root_str)
