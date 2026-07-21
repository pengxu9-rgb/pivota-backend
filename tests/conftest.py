from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
root_str = str(ROOT)
if root_str not in sys.path:
    sys.path.insert(0, root_str)
elif sys.path[0] != root_str:
    sys.path.remove(root_str)
    sys.path.insert(0, root_str)

# Canonical test DATABASE_URL, pinned BEFORE any test module imports.
#
# Test modules disagree on the fallback they setdefault ("sqlite:///:memory:"
# vs a postgres placeholder), and `db.database` binds its singleton to
# whichever module happens to be imported first during collection. That made
# full-suite runs nondeterministic and cross-polluted: sqlite-written tests
# (PRAGMA, AUTOINCREMENT) ran against a live local postgres, and asyncpg
# connections leaked across event loops. conftest imports before every test
# module, so pinning here turns all per-module setdefaults into no-ops.
#
# A file-backed sqlite DB (not :memory:) is required: the `databases` library
# opens a fresh connection per query outside an explicit connection block, so
# an in-memory DB loses its tables between consecutive execute() calls.
_TEST_DB_PATH = ROOT / "pivota_test.db"
if "DATABASE_URL" not in os.environ:
    # Fresh slate per pytest process; only when we own the file.
    _TEST_DB_PATH.unlink(missing_ok=True)
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TEST_DB_PATH}"

# On sqlite, mvp.events falls back to its file sink, which appends to
# mvp_events.jsonl in the cwd — keep test runs from littering the repo root.
os.environ.setdefault(
    "MVP_EVENTS_FILE",
    os.path.join(tempfile.gettempdir(), f"mvp_events_test_{os.getpid()}.jsonl"),
)
