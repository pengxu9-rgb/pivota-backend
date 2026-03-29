from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.smoke_real_payment_completion_signoff as module  # noqa: E402


def _build_args(tmp_path: Path, *, mode: str, refund: bool = False, payment_reference: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        base_url="https://api.example",
        internal_key="internal-test-key",
        merchant_id="merch_1",
        mode=mode,
        payment_reference=payment_reference,
        payment_psp="stripe",
        payment_intent_preferred_psps=None,
        payment_intent_psp_mode=None,
        refund=refund,
        run_id="20260330T000000Z",
        work_dir=str(tmp_path / "work"),
        output_json=str(tmp_path / "report.json"),
        output_md=str(tmp_path / "report.md"),
    )


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_phase_b_preflight_wrapper_passes_when_order_sync_is_ready(monkeypatch, tmp_path: Path) -> None:
    args = _build_args(tmp_path, mode="preflight")

    def _fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        work_dir = Path(args.work_dir)
        _write_json(work_dir / "order_sync.json", {"checkout_id": "chk_1", "status": "state_synced", "order_id": "ORD_1", "replayed": False})
        _write_json(
            work_dir / "order_sync_audit.json",
            {
                "checkout_id": "chk_1",
                "order_id": "ORD_1",
                "sync_signals": {
                    "merchant_writeback": {"status": "ready"},
                },
            },
        )
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(module, "_parse_args", lambda: args)
    monkeypatch.setattr(module, "_run_smoke_command", _fake_run)

    exit_code = module.main()

    assert exit_code == 0
    payload = json.loads(Path(args.output_json).read_text(encoding="utf-8"))
    assert payload["summary"]["preflight_ok"] is True
    assert payload["summary"]["paid_terminal_ok"] is None
    assert payload["summary"]["overall_ok"] is True


def test_phase_b_bridge_wrapper_passes_when_paid_reference_converges(monkeypatch, tmp_path: Path) -> None:
    args = _build_args(tmp_path, mode="bridge_paid_reference", payment_reference="pi_live_123")

    def _fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        work_dir = Path(args.work_dir)
        _write_json(work_dir / "order_sync.json", {"checkout_id": "chk_1", "status": "state_synced", "order_id": "ORD_1", "replayed": False})
        _write_json(
            work_dir / "order_sync_audit.json",
            {
                "checkout_id": "chk_1",
                "order_id": "ORD_1",
                "sync_signals": {"merchant_writeback": {"status": "ready"}},
            },
        )
        _write_json(
            work_dir / "payment_bridge.json",
            {
                "checkout_id": "chk_1",
                "order_id": "ORD_1",
                "status": "paid",
                "payment_status": "paid",
                "payment_reference": "pi_live_123",
                "psp_used": "stripe",
                "transaction_sync": {"status": "ready"},
            },
        )
        _write_json(
            work_dir / "order_sync_audit_after_payment.json",
            {
                "checkout_id": "chk_1",
                "order_id": "ORD_1",
                "sync_signals": {"refund_sync": {"refund_eligible": True}},
            },
        )
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(module, "_parse_args", lambda: args)
    monkeypatch.setattr(module, "_run_smoke_command", _fake_run)

    exit_code = module.main()

    assert exit_code == 0
    payload = json.loads(Path(args.output_json).read_text(encoding="utf-8"))
    assert payload["summary"]["preflight_ok"] is True
    assert payload["summary"]["paid_terminal_ok"] is True
    assert payload["summary"]["refund_ready_ok"] is True
    assert payload["summary"]["overall_ok"] is True


def test_phase_b_status_sync_wrapper_fails_when_payment_is_not_terminal(monkeypatch, tmp_path: Path) -> None:
    args = _build_args(tmp_path, mode="payment_status_sync")

    def _fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        work_dir = Path(args.work_dir)
        _write_json(work_dir / "order_sync.json", {"checkout_id": "chk_1", "status": "state_synced", "order_id": "ORD_1", "replayed": False})
        _write_json(
            work_dir / "order_sync_audit.json",
            {
                "checkout_id": "chk_1",
                "order_id": "ORD_1",
                "sync_signals": {"merchant_writeback": {"status": "ready"}},
            },
        )
        _write_json(
            work_dir / "payment_intent.json",
            {
                "checkout_id": "chk_1",
                "order_id": "ORD_1",
                "payment_intent_id": "pi_test_123",
                "psp_used": "stripe",
                "payment_intent_status": "requires_payment_method",
                "payment_action": {"client_secret": "pi_secret_123"},
            },
        )
        _write_json(
            work_dir / "payment_status_sync.json",
            {
                "checkout_id": "chk_1",
                "order_id": "ORD_1",
                "payment_intent_id": "pi_test_123",
                "payment_intent_status": "requires_payment_method",
                "normalized_payment_status": "requires_payment_method",
                "psp_used": "stripe",
            },
        )
        _write_json(
            work_dir / "order_sync_audit_after_status_sync.json",
            {
                "checkout_id": "chk_1",
                "order_id": "ORD_1",
                "sync_signals": {"refund_sync": {"refund_eligible": False}},
            },
        )
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(module, "_parse_args", lambda: args)
    monkeypatch.setattr(module, "_run_smoke_command", _fake_run)

    exit_code = module.main()

    assert exit_code == 1
    payload = json.loads(Path(args.output_json).read_text(encoding="utf-8"))
    assert payload["summary"]["payment_intent_ok"] is True
    assert payload["summary"]["paid_terminal_ok"] is False
    assert payload["summary"]["overall_ok"] is False
    assert payload["artifacts"]["payment_intent"]["payment_action"]["client_secret"] == "[REDACTED]"
    internal_key_index = payload["command"].index("--internal-key")
    assert payload["command"][internal_key_index + 1] == "[REDACTED]"
