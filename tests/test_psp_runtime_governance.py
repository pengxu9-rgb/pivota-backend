import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")
os.environ.pop("ENABLE_INTERNAL_PSP_MAINTENANCE_ROUTES", None)
os.chdir(REPO_ROOT)

from main import app  # noqa: E402


def test_legacy_psp_maintenance_routes_are_not_mounted_by_default() -> None:
    mounted_paths = {getattr(route, "path", None) for route in app.routes}
    forbidden_paths = {
        "/debug/insert-adyen",
        "/debug/check-psps",
        "/debug/psp/validate/{merchant_id}",
        "/admin/recover/psps",
        "/admin/fix/order-psp-associations/{merchant_id}",
        "/admin/fix-orders-psp-id",
        "/admin/debug/psp-overview-diagnosis",
        "/admin/debug/psp-metrics/{merchant_id}",
        "/admin/simulate/payments/{agent_id}",
        "/admin/simulate/payments/all",
    }

    assert forbidden_paths.isdisjoint(mounted_paths)


def test_canonical_psp_runtime_routes_remain_mounted() -> None:
    mounted_paths = {getattr(route, "path", None) for route in app.routes}
    required_paths = {
        "/payment/execute",
        "/merchant/{merchant_id}/psps",
        "/merchant/psp/{psp_id}/test",
        "/merchant/integrations/psp/connect",
        "/merchant/integrations/routing",
        "/merchant/webhooks/config",
    }

    missing = required_paths - mounted_paths
    assert not missing, f"missing canonical PSP runtime routes: {sorted(missing)}"
