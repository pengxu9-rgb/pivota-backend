from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.audit_merchant_psp_readiness as module  # noqa: E402


def _build_args(tmp_path: Path, merchant_file: Path) -> argparse.Namespace:
    return argparse.Namespace(
        database_url="postgresql://example/db",
        merchant_id=[],
        merchant_id_file=str(merchant_file),
        base_url="https://api.example",
        header=["Authorization: Bearer test"],
        validate=False,
        validate_supported_only=False,
        timeout_seconds=10.0,
        output_json=str(tmp_path / "audit.json"),
        output_md=str(tmp_path / "audit.md"),
    )


def test_build_report_distinguishes_live_ready_supported_psp() -> None:
    report = module._build_report(
        [
            {
                "merchant_id": "merch_ready",
                "psp_id": "psp_stripe_live",
                "provider": "stripe",
                "status": "active",
                "api_key": "sk_live_123",
                "account_id": None,
                "provider_config": {
                    "mode": "payment_intent",
                    "public_key": "pk_live_123",
                    "webhook_endpoint_id": "we_123",
                    "webhook_endpoint_secret": "whsec_123",
                },
                "environment": "live",
                "validation_status": "valid",
                "validation_error": None,
                "last_validated_at": None,
            },
            {
                "merchant_id": "merch_blocked",
                "psp_id": "psp_checkout_test",
                "provider": "checkout",
                "status": "active",
                "api_key": "sk_sbox_123",
                "account_id": "pc_123",
                "provider_config": {"processing_channel_id": "pc_123"},
                "environment": "test",
                "validation_status": "valid",
                "validation_error": None,
                "last_validated_at": None,
            },
        ],
        ["merch_ready", "merch_blocked"],
    )

    assert report["summary"]["ready_merchants"] == 1
    assert report["summary"]["blocked_merchants"] == 1
    merchants = {item["merchant_id"]: item for item in report["merchants"]}
    assert merchants["merch_ready"]["ready_for_order_backed_canary"] is True
    assert merchants["merch_blocked"]["ready_for_order_backed_canary"] is False
    blocked = merchants["merch_blocked"]["blocking_supported_psps"][0]
    assert blocked["provider"] == "checkout"
    assert "replace checkout test credentials with live credentials" in blocked["recommended_actions"]


def test_main_runs_validation_only_for_blocked_supported_psps(monkeypatch, tmp_path: Path) -> None:
    merchant_file = tmp_path / "merchants.txt"
    merchant_file.write_text("merch_1\n", encoding="utf-8")
    args = _build_args(tmp_path, merchant_file)
    args.validate = True
    args.validate_supported_only = True

    monkeypatch.setattr(
        module,
        "_fetch_rows",
        lambda database_url, merchant_ids: [
            {
                "merchant_id": "merch_1",
                "psp_id": "psp_stripe_live",
                "provider": "stripe",
                "status": "active",
                "api_key": "sk_live_123",
                "account_id": None,
                "provider_config": {
                    "mode": "payment_intent",
                    "public_key": "pk_live_123",
                    "webhook_endpoint_id": "we_123",
                    "webhook_endpoint_secret": "whsec_123",
                },
                "environment": "live",
                "validation_status": "valid",
                "validation_error": None,
                "last_validated_at": None,
            },
            {
                "merchant_id": "merch_1",
                "psp_id": "psp_stripe_test",
                "provider": "stripe",
                "status": "active",
                "api_key": "sk_test_123",
                "account_id": None,
                "provider_config": {"mode": "payment_intent"},
                "environment": "test",
                "validation_status": "unknown",
                "validation_error": None,
                "last_validated_at": None,
            },
            {
                "merchant_id": "merch_1",
                "psp_id": "psp_paypal_unknown",
                "provider": "paypal",
                "status": "active",
                "api_key": "paypal_live_token",
                "account_id": None,
                "provider_config": {},
                "environment": "unknown",
                "validation_status": "unknown",
                "validation_error": None,
                "last_validated_at": None,
            },
        ],
    )
    calls = []

    def _fake_validate(**kwargs):
        calls.append(kwargs["psp_id"])
        return {"http_status": 200, "body": {"status": "success"}}

    monkeypatch.setattr(module, "_validate_psp", _fake_validate)
    monkeypatch.setattr(module, "_parse_args", lambda: args)

    exit_code = module.main()

    assert exit_code == 0
    assert calls == ["psp_stripe_test"]
    payload = json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))
    merchant = payload["merchants"][0]
    attempted = [item for item in merchant["psps"] if item.get("validation_attempt")]
    assert [item["psp_id"] for item in attempted] == ["psp_stripe_test"]


def test_load_merchant_ids_reads_repeatable_and_file(tmp_path: Path) -> None:
    merchant_file = tmp_path / "merchants.txt"
    merchant_file.write_text("merch_b\nmerch_c\n", encoding="utf-8")
    args = argparse.Namespace(merchant_id=["merch_a", "merch_b"], merchant_id_file=str(merchant_file))

    merchant_ids = module._load_merchant_ids(args)

    assert merchant_ids == ["merch_a", "merch_b", "merch_c"]
