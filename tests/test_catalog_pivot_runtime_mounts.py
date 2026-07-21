import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.chdir(REPO_ROOT)

from main import app  # noqa: E402


def test_catalog_and_pivot_runtime_routes_are_mounted() -> None:
    mounted_paths = {getattr(route, "path", None) for route in app.routes}
    required_paths = {
        "/v1/pivot/query",
        "/v1/catalog/sync/jobs",
        "/v1/catalog/connectors/shopify/webhooks",
    }

    missing = required_paths - mounted_paths
    assert not missing, f"missing catalog/pivot runtime routes: {sorted(missing)}"
