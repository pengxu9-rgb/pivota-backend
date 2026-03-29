#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MERCHANT_ID = "merch_efbc46b4619cfbdf"
PAID_TERMINAL_STATUSES = {"paid", "completed"}
REFUND_TERMINAL_STATUSES = {"refunded", "partially_refunded"}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Structured Phase B signoff wrapper for real payment completion readiness."
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument(
        "--internal-key",
        required=True,
        help="Readiness internal key for the underlying alpha smoke flow.",
    )
    parser.add_argument(
        "--merchant-id",
        default=os.getenv("READINESS_ALPHA_MERCHANT_ID") or DEFAULT_MERCHANT_ID,
    )
    parser.add_argument(
        "--mode",
        choices=("preflight", "bridge_paid_reference", "payment_status_sync"),
        default="preflight",
        help=(
            "preflight: canary write only; "
            "bridge_paid_reference: attach an already-successful payment reference; "
            "payment_status_sync: create readiness payment intent and poll PSP status."
        ),
    )
    parser.add_argument(
        "--payment-reference",
        default=None,
        help="Required for bridge_paid_reference. Already-successful PSP payment reference.",
    )
    parser.add_argument("--payment-psp", default="stripe")
    parser.add_argument(
        "--payment-intent-preferred-psps",
        default=None,
        help="Optional CSV passed through to readiness payment-intent creation.",
    )
    parser.add_argument(
        "--payment-intent-psp-mode",
        default=None,
        help="Optional psp_mode passed through to readiness payment-intent creation.",
    )
    parser.add_argument(
        "--refund",
        action="store_true",
        help="After paid-state convergence, attempt readiness refund validation.",
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--work-dir", default=None)
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--output-md", default=None)
    return parser.parse_args()


def _write_if_requested(path_str: Optional[str], content: str) -> None:
    if not path_str:
        return
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _run_id(raw: Optional[str]) -> str:
    if raw:
        return raw
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _work_dir(args: argparse.Namespace, run_id: str) -> Path:
    if args.work_dir:
        path = Path(args.work_dir)
    else:
        path = Path("/tmp") / f"pivota-phase-b-signoff-{run_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _build_smoke_command(args: argparse.Namespace, run_id: str, work_dir: Path) -> list[str]:
    cmd = [
        "bash",
        str(REPO_ROOT / "scripts" / "smoke_readiness_alpha.sh"),
        "--base-url",
        str(args.base_url),
        "--internal-key",
        str(args.internal_key),
        "--merchant-id",
        str(args.merchant_id),
        "--run-id",
        run_id,
        "--out-dir",
        str(work_dir),
        "--canary-write",
    ]
    if args.mode == "bridge_paid_reference":
        if not args.payment_reference:
            raise ValueError("--payment-reference is required for --mode bridge_paid_reference")
        cmd.extend(["--payment-reference", str(args.payment_reference)])
        cmd.extend(["--payment-psp", str(args.payment_psp)])
    if args.mode == "payment_status_sync":
        cmd.append("--create-payment-intent")
        cmd.append("--payment-status-sync")
        if args.payment_intent_preferred_psps:
            cmd.extend(["--payment-intent-preferred-psps", str(args.payment_intent_preferred_psps)])
        if args.payment_intent_psp_mode:
            cmd.extend(["--payment-intent-psp-mode", str(args.payment_intent_psp_mode)])
    if args.refund:
        cmd.append("--refund")
    return cmd


def _run_smoke_command(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _load_json_if_exists(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else {"value": payload}


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        redacted: Dict[str, Any] = {}
        for key, value in obj.items():
            key_str = str(key).lower()
            if "secret" in key_str:
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = _redact(value)
        return redacted
    if isinstance(obj, list):
        return [_redact(item) for item in obj]
    return obj


def _sanitize_command(cmd: list[str]) -> list[str]:
    sanitized = list(cmd)
    secret_flags = {"--internal-key", "--payment-reference"}
    for index, item in enumerate(sanitized[:-1]):
        if item in secret_flags:
            sanitized[index + 1] = "[REDACTED]"
    return sanitized


def _pick(payload: Optional[Dict[str, Any]], *fields: str) -> Optional[Dict[str, Any]]:
    if not payload:
        return None
    return {field: _redact(payload.get(field)) for field in fields if field in payload}


def _audit_refund_eligible(audit: Optional[Dict[str, Any]]) -> Optional[bool]:
    if not audit:
        return None
    return bool((((audit.get("sync_signals") or {}).get("refund_sync") or {}).get("refund_eligible")))


def _payment_status_value(payload: Optional[Dict[str, Any]]) -> Optional[str]:
    if not payload:
        return None
    for key in ("normalized_payment_status", "payment_status"):
        value = payload.get(key)
        if value:
            return str(value)
    return None


def _build_summary(args: argparse.Namespace, artifacts: Dict[str, Optional[Dict[str, Any]]], completed: subprocess.CompletedProcess[str]) -> Dict[str, Any]:
    order_sync = artifacts.get("order_sync")
    order_sync_audit = artifacts.get("order_sync_audit")
    payment_intent = artifacts.get("payment_intent")
    payment_status_sync = artifacts.get("payment_status_sync")
    payment_bridge = artifacts.get("payment_bridge")
    order_sync_audit_after_status_sync = artifacts.get("order_sync_audit_after_status_sync")
    order_sync_audit_after_payment = artifacts.get("order_sync_audit_after_payment")
    refund = artifacts.get("refund")

    preflight_ok = bool(
        (order_sync or {}).get("status") == "state_synced"
        and (((order_sync_audit or {}).get("sync_signals") or {}).get("merchant_writeback") or {}).get("status") == "ready"
    )
    payment_intent_ok = None
    paid_terminal_ok = None
    refund_ready_ok = None
    refund_ok = None

    if args.mode == "payment_status_sync":
        payment_intent_ok = bool((payment_intent or {}).get("payment_intent_id"))
        normalized = _payment_status_value(payment_status_sync)
        refund_ready_ok = _audit_refund_eligible(order_sync_audit_after_status_sync)
        paid_terminal_ok = bool(normalized in PAID_TERMINAL_STATUSES and refund_ready_ok)
    elif args.mode == "bridge_paid_reference":
        normalized = _payment_status_value(payment_bridge)
        refund_ready_ok = _audit_refund_eligible(order_sync_audit_after_payment)
        paid_terminal_ok = bool(normalized in PAID_TERMINAL_STATUSES and refund_ready_ok)

    if args.refund:
        refund_status = str((refund or {}).get("refund_status") or "")
        refund_ok = refund_status in REFUND_TERMINAL_STATUSES

    if completed.returncode != 0:
        overall_ok = False
    elif args.mode == "preflight":
        overall_ok = preflight_ok
    elif args.mode == "payment_status_sync":
        overall_ok = bool(preflight_ok and payment_intent_ok and paid_terminal_ok and (refund_ok if args.refund else True))
    else:
        overall_ok = bool(preflight_ok and paid_terminal_ok and (refund_ok if args.refund else True))

    return {
        "preflight_ok": preflight_ok,
        "payment_intent_ok": payment_intent_ok,
        "paid_terminal_ok": paid_terminal_ok,
        "refund_ready_ok": refund_ready_ok,
        "refund_ok": refund_ok,
        "underlying_returncode": completed.returncode,
        "overall_ok": overall_ok,
    }


def _render_markdown(report: Dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Real Payment Completion Signoff",
        "",
        f"- mode: `{report['mode']}`",
        f"- merchant_id: `{report['merchant_id']}`",
        f"- overall_ok: `{summary['overall_ok']}`",
        f"- preflight_ok: `{summary['preflight_ok']}`",
        f"- payment_intent_ok: `{summary['payment_intent_ok']}`",
        f"- paid_terminal_ok: `{summary['paid_terminal_ok']}`",
        f"- refund_ready_ok: `{summary['refund_ready_ok']}`",
        f"- refund_ok: `{summary['refund_ok']}`",
        f"- work_dir: `{report['work_dir']}`",
        "",
        "## Artifacts",
        "",
    ]
    for key, value in (report.get("artifact_paths") or {}).items():
        lines.append(f"- `{key}`: `{value}`")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = _parse_args()
    run_id = _run_id(args.run_id)
    work_dir = _work_dir(args, run_id)
    cmd = _build_smoke_command(args, run_id, work_dir)
    completed = _run_smoke_command(cmd)

    artifact_paths = {
        "report": str(work_dir / "report.json"),
        "checkout": str(work_dir / "checkout.json"),
        "order_sync": str(work_dir / "order_sync.json"),
        "order_sync_audit": str(work_dir / "order_sync_audit.json"),
        "payment_intent": str(work_dir / "payment_intent.json"),
        "payment_status_sync": str(work_dir / "payment_status_sync.json"),
        "order_sync_audit_after_status_sync": str(work_dir / "order_sync_audit_after_status_sync.json"),
        "payment_bridge": str(work_dir / "payment_bridge.json"),
        "order_sync_audit_after_payment": str(work_dir / "order_sync_audit_after_payment.json"),
        "refund": str(work_dir / "refund.json"),
        "order_sync_audit_after_refund": str(work_dir / "order_sync_audit_after_refund.json"),
    }
    artifacts = {
        "report": _load_json_if_exists(work_dir / "report.json"),
        "checkout": _load_json_if_exists(work_dir / "checkout.json"),
        "order_sync": _load_json_if_exists(work_dir / "order_sync.json"),
        "order_sync_audit": _load_json_if_exists(work_dir / "order_sync_audit.json"),
        "payment_intent": _load_json_if_exists(work_dir / "payment_intent.json"),
        "payment_status_sync": _load_json_if_exists(work_dir / "payment_status_sync.json"),
        "order_sync_audit_after_status_sync": _load_json_if_exists(work_dir / "order_sync_audit_after_status_sync.json"),
        "payment_bridge": _load_json_if_exists(work_dir / "payment_bridge.json"),
        "order_sync_audit_after_payment": _load_json_if_exists(work_dir / "order_sync_audit_after_payment.json"),
        "refund": _load_json_if_exists(work_dir / "refund.json"),
        "order_sync_audit_after_refund": _load_json_if_exists(work_dir / "order_sync_audit_after_refund.json"),
    }
    summary = _build_summary(args, artifacts, completed)
    report = {
        "mode": args.mode,
        "merchant_id": args.merchant_id,
        "run_id": run_id,
        "work_dir": str(work_dir),
        "command": _sanitize_command(cmd),
        "summary": summary,
        "artifact_paths": artifact_paths,
        "artifacts": {
            "report": _pick(artifacts.get("report"), "merchant_id", "merchant_alpha_mode"),
            "checkout": _pick(artifacts.get("checkout"), "checkout_id", "status", "variant_id"),
            "order_sync": _pick(artifacts.get("order_sync"), "checkout_id", "status", "order_id", "replayed"),
            "order_sync_audit": _pick(artifacts.get("order_sync_audit"), "checkout_id", "order_id", "checkout_status", "order_state", "sync_signals", "warnings", "recommendations"),
            "payment_intent": _pick(artifacts.get("payment_intent"), "checkout_id", "order_id", "status", "payment_status", "payment_intent_id", "psp_used", "payment_intent_status", "bridged_to_paid", "replayed", "payment_action"),
            "payment_status_sync": _pick(artifacts.get("payment_status_sync"), "checkout_id", "order_id", "status", "payment_status", "payment_intent_id", "payment_reference", "payment_reference_type", "psp_used", "payment_intent_status", "normalized_payment_status", "bridged_to_paid", "replayed", "transaction_sync"),
            "payment_bridge": _pick(artifacts.get("payment_bridge"), "checkout_id", "order_id", "status", "payment_status", "payment_reference", "psp_used", "transaction_sync", "replayed"),
            "order_sync_audit_after_status_sync": _pick(artifacts.get("order_sync_audit_after_status_sync"), "checkout_id", "order_id", "checkout_status", "order_state", "sync_signals", "warnings", "recommendations"),
            "order_sync_audit_after_payment": _pick(artifacts.get("order_sync_audit_after_payment"), "checkout_id", "order_id", "checkout_status", "order_state", "sync_signals", "warnings", "recommendations"),
            "refund": _pick(artifacts.get("refund"), "checkout_id", "order_id", "status", "payment_status", "refund_status", "refund_id", "psp_refund_id", "platform_refund_id", "amount", "remaining_refundable_before", "transaction_sync", "replayed"),
            "order_sync_audit_after_refund": _pick(artifacts.get("order_sync_audit_after_refund"), "checkout_id", "order_id", "checkout_status", "order_state", "sync_signals", "warnings", "recommendations"),
        },
        "underlying_process": {
            "returncode": completed.returncode,
            "stdout_tail": (completed.stdout or "")[-2000:],
            "stderr_tail": (completed.stderr or "")[-2000:],
        },
    }

    json_blob = json.dumps(report, ensure_ascii=False, indent=2)
    markdown = _render_markdown(report)
    _write_if_requested(args.output_json, json_blob)
    _write_if_requested(args.output_md, markdown)
    print(json_blob)
    return 0 if summary["overall_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
