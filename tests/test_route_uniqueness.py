import os
import sys
from collections import defaultdict
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")
os.chdir(REPO_ROOT)

from main import app  # noqa: E402


def test_runtime_routes_have_unique_method_path_pairs():
    seen = defaultdict(list)

    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or []
        endpoint = getattr(route, "endpoint", None)

        for method in methods:
            if method in {"HEAD", "OPTIONS"} or not path:
                continue
            seen[(method, path)].append(
                f"{getattr(endpoint, '__module__', '?')}.{getattr(endpoint, '__name__', '?')}"
            )

    duplicates = {
        (method, path): owners
        for (method, path), owners in seen.items()
        if len(owners) > 1
    }

    assert not duplicates, f"duplicate mounted routes: {duplicates}"
