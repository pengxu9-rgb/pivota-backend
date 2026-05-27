from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
root_str = str(ROOT)
if root_str not in sys.path:
    sys.path.insert(0, root_str)
elif sys.path[0] != root_str:
    sys.path.remove(root_str)
    sys.path.insert(0, root_str)
