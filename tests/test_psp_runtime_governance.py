import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.chdir(REPO_ROOT)

from main import app  # noqa: E402
from scripts.audit_psp_runtime_truth import _scan_legacy_runtime_references  # noqa: E402


def test_legacy_psp_maintenance_routes_are_not_mounted_by_default() -> None:
    mounted_paths = {getattr(route, "path", None) for route in app.routes}
    forbidden_paths = {
        "/debug/integrations/tables",
        "/debug/integrations/test-insert",
        "/debug/insert-adyen",
        "/debug/check-psps",
        "/debug/psp/validate/{merchant_id}",
        "/init-merchant-data",
        "/cleanup-all-duplicates",
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
        "/payment/internal/canary/merchants/{merchant_id}/execute",
        "/merchant/{merchant_id}/psps",
        "/merchant/psp/{psp_id}/test",
        "/merchant/integrations/psp/connect",
        "/merchant/integrations/routing",
        "/merchant/webhooks/config",
    }

    missing = required_paths - mounted_paths
    assert not missing, f"missing canonical PSP runtime routes: {sorted(missing)}"


def test_canonical_runtime_files_do_not_reference_legacy_psp_truth_fields() -> None:
    findings = _scan_legacy_runtime_references()

    assert findings["merchant_onboarding_fallback"] == []
    assert findings["global_provider_secret_fallback"] == []
