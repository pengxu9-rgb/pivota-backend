from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List


REPO_ROOT = Path(__file__).resolve().parents[1]

CANONICAL_RUNTIME_FILES = [
    "routes/order_routes.py",
    "routes/payment_execution_routes.py",
    "routes/merchant_dashboard_routes.py",
    "routes/refund_api.py",
    "services/refund_service.py",
    "services/merchant_payment_initiation_service.py",
    "adapters/multi_psp_orchestrator.py",
    "routes/webhook_routes.py",
    "routes/psp_routes.py",
]

LEGACY_PATTERNS = {
    "merchant_onboarding_fallback": [
        "merchant.get(\"psp_connected\")",
        "merchant.get('psp_connected')",
        "merchant.get(\"psp_type\")",
        "merchant.get('psp_type')",
        "psp_sandbox_key",
        "merchant.get(\"psp_key\")",
        "merchant.get('psp_key')",
        "merchant.get(\"backup_psps\")",
        "merchant.get('backup_psps')",
    ],
    "legacy_routing_table": [
        "payment_router_config",
    ],
    "global_provider_secret_fallback": [
        "settings.stripe_secret_key",
        "settings.adyen_api_key",
        "settings.checkout_secret_key",
        "settings.checkout_api_key",
    ],
}

PSP_DEBUG_ROUTERS = [
    "debug_integrations_router",
    "init_merchant_data_router",
    "cleanup_all_duplicates_router",
    "debug_psp_router",
    "debug_psp_validation_router",
    "admin_recover_psps_router",
    "admin_fix_order_psp_router",
    "admin_debug_psp_router",
    "admin_fix_psp_id_router",
    "admin_debug_psp_metrics_router",
    "simulate_payments_router",
]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit PSP runtime truth surfaces, legacy fallbacks, and duplicate payment files."
    )
    parser.add_argument("--json", action="store_true", help="Emit compact JSON instead of pretty JSON.")
    return parser.parse_args()


def _find_duplicate_payment_files() -> List[str]:
    matches: List[str] = []
    for path in REPO_ROOT.rglob("* 2.py"):
        normalized = path.relative_to(REPO_ROOT).as_posix()
        if any(token in normalized for token in ("payment", "psp", "refund", "webhook", "merchant_onboarding")):
            matches.append(normalized)
    return sorted(matches)


def _scan_legacy_runtime_references() -> Dict[str, List[Dict[str, object]]]:
    findings: Dict[str, List[Dict[str, object]]] = {
        category: [] for category in LEGACY_PATTERNS
    }
    for relative_path in CANONICAL_RUNTIME_FILES:
        path = REPO_ROOT / relative_path
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for line_no, line in enumerate(lines, start=1):
            for category, patterns in LEGACY_PATTERNS.items():
                for pattern in patterns:
                    if pattern in line:
                        findings[category].append(
                            {
                                "file": relative_path,
                                "line": line_no,
                                "pattern": pattern,
                                "code": line.strip(),
                            }
                        )
    return findings


def _scan_main_router_surface() -> Dict[str, object]:
    main_path = REPO_ROOT / "main.py"
    lines = main_path.read_text(encoding="utf-8").splitlines()
    mounted = []
    for line_no, line in enumerate(lines, start=1):
        for router_name in PSP_DEBUG_ROUTERS:
            if f"include_router({router_name})" in line:
                mounted.append(
                    {
                        "router": router_name,
                        "line": line_no,
                        "code": line.strip(),
                    }
                )
    return {
        "mounted_debug_routers": mounted,
    }


def main() -> None:
    args = _parse_args()
    payload = {
        "repo_root": str(REPO_ROOT),
        "duplicate_payment_files": _find_duplicate_payment_files(),
        "legacy_runtime_references": _scan_legacy_runtime_references(),
        "main_router_surface": _scan_main_router_surface(),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    else:
        print(json.dumps(payload, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
