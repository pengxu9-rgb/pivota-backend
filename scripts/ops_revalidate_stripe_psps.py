import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database
from services.merchant_psp_config_service import evaluate_psp_readiness


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Select canonical Stripe merchant_psps rows and optionally re-run "
            "the deployed /merchant/psp/{psp_id}/test validation/provision flow."
        )
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("PIVOTA_API_BASE_URL", "https://api.pivota.cc"),
        help="Public API base URL for the deployed backend.",
    )
    parser.add_argument(
        "--email",
        default=os.getenv("PIVOTA_ADMIN_EMAIL") or os.getenv("MERCHANT_ADMIN_EMAIL"),
        help="Admin email used to authenticate against the deployed backend.",
    )
    parser.add_argument(
        "--password",
        default=os.getenv("PIVOTA_ADMIN_PASSWORD") or os.getenv("MERCHANT_ADMIN_PASSWORD"),
        help="Admin password used to authenticate against the deployed backend.",
    )
    parser.add_argument(
        "--provider",
        default="stripe",
        help="Provider to target. Defaults to stripe.",
    )
    parser.add_argument(
        "--merchant-id",
        action="append",
        dest="merchant_ids",
        help="Restrict to one or more merchant IDs. Repeat the flag to add multiple values.",
    )
    parser.add_argument(
        "--psp-id",
        action="append",
        dest="psp_ids",
        help="Restrict to one or more PSP IDs. Repeat the flag to add multiple values.",
    )
    parser.add_argument(
        "--input",
        help=(
            "Optional audit report path from backfill_canonical_merchant_psps.py --output. "
            "If provided, changed rows in the report become the default PSP target set."
        ),
    )
    parser.add_argument("--limit", type=int, default=100, help="Maximum rows to inspect.")
    parser.add_argument(
        "--include-test",
        action="store_true",
        help="Also include test-environment rows. Default behavior targets live rows only.",
    )
    parser.add_argument(
        "--include-ready",
        action="store_true",
        help="Include rows that already look ready. Default behavior only targets rows needing revalidation.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually call the deployed validation endpoint. Default behavior is dry-run target discovery.",
    )
    parser.add_argument(
        "--delay-ms",
        type=int,
        default=250,
        help="Delay between validation calls when --apply is set.",
    )
    parser.add_argument("--output", help="Optional path to write a JSON report.")
    return parser.parse_args()


def _normalize_strings(values: Optional[Iterable[str]]) -> List[str]:
    normalized: List[str] = []
    for value in values or []:
        item = str(value or "").strip()
        if item and item not in normalized:
            normalized.append(item)
    return normalized


def _build_in_clause(prefix: str, values: List[str]) -> tuple[str, Dict[str, Any]]:
    placeholders: List[str] = []
    params: Dict[str, Any] = {}
    for index, value in enumerate(values):
        key = f"{prefix}_{index}"
        placeholders.append(f":{key}")
        params[key] = value
    return ", ".join(placeholders), params


def _load_psp_ids_from_report(path: Path) -> List[str]:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []

    ids: List[str] = []
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
            for row in payload["rows"]:
                if not isinstance(row, dict):
                    continue
                if row.get("changed"):
                    psp_id = str(row.get("psp_id") or "").strip()
                    if psp_id and psp_id not in ids:
                        ids.append(psp_id)
            return ids
    except Exception:
        pass

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if not payload.get("changed"):
            continue
        psp_id = str(payload.get("psp_id") or "").strip()
        if psp_id and psp_id not in ids:
            ids.append(psp_id)
    return ids


def _build_row_report(row: Dict[str, Any]) -> Dict[str, Any]:
    readiness = evaluate_psp_readiness(
        str(row.get("provider") or ""),
        status=row.get("status"),
        api_key=row.get("api_key"),
        account_id=row.get("account_id"),
        provider_config=row.get("provider_config"),
        environment=row.get("environment"),
        validation_status=row.get("validation_status"),
        validation_error=row.get("validation_error"),
    )
    return {
        "psp_id": str(row.get("psp_id") or "").strip(),
        "merchant_id": str(row.get("merchant_id") or "").strip(),
        "provider": str(row.get("provider") or "").strip().lower(),
        "environment": readiness["environment"],
        "validation_status": readiness["validation_status"],
        "validation_error": readiness["validation_error"],
        "live_charge_ready": readiness["live_charge_ready"],
        "webhook_ready": bool((readiness.get("provider_summary") or {}).get("webhook_ready")),
        "readiness_blockers": list(readiness.get("readiness_blockers") or []),
    }


def _should_target_row(report: Dict[str, Any], *, include_test: bool, include_ready: bool) -> bool:
    environment = str(report.get("environment") or "").strip().lower()
    if not include_test and environment != "live":
        return False
    if include_ready:
        return True
    if str(report.get("validation_status") or "").strip().lower() != "valid":
        return True
    if environment == "live" and not bool(report.get("webhook_ready")):
        return True
    if environment == "live" and not bool(report.get("live_charge_ready")):
        return True
    return False


async def _fetch_candidate_rows(args: argparse.Namespace) -> List[Dict[str, Any]]:
    provider = str(args.provider or "stripe").strip().lower()
    merchant_ids = _normalize_strings(args.merchant_ids)
    psp_ids = _normalize_strings(args.psp_ids)

    if args.input:
        report_path = Path(args.input)
        psp_ids_from_report = _load_psp_ids_from_report(report_path)
        for psp_id in psp_ids_from_report:
            if psp_id not in psp_ids:
                psp_ids.append(psp_id)

    conditions: List[str] = [
        "status = 'active'",
        "LOWER(provider) = :provider",
    ]
    params: Dict[str, Any] = {"provider": provider}

    if merchant_ids:
        merchant_clause, merchant_params = _build_in_clause("merchant_id", merchant_ids)
        conditions.append(f"merchant_id IN ({merchant_clause})")
        params.update(merchant_params)

    if psp_ids:
        psp_clause, psp_params = _build_in_clause("psp_id", psp_ids)
        conditions.append(f"psp_id IN ({psp_clause})")
        params.update(psp_params)

    where_clause = f"WHERE {' AND '.join(conditions)}"
    limit_clause = "" if psp_ids else "LIMIT :limit"
    if not psp_ids:
        params["limit"] = args.limit

    rows = await database.fetch_all(
        f"""
        SELECT psp_id, merchant_id, provider, api_key, secret_key, account_id, status,
               connected_at, environment, provider_config, validation_status,
               validation_error, last_validated_at
        FROM merchant_psps
        {where_clause}
        ORDER BY connected_at DESC NULLS LAST, psp_id ASC
        {limit_clause}
        """,
        params,
    )
    return [dict(row) for row in rows or []]


async def _login(client: httpx.AsyncClient, email: str, password: str) -> str:
    response = await client.post("/api/auth/login", json={"email": email, "password": password})
    response.raise_for_status()
    payload = response.json()
    token = str(payload.get("token") or "").strip()
    if not token:
        raise RuntimeError("Login succeeded but no bearer token was returned")
    return token


async def _run(args: argparse.Namespace) -> int:
    await database.connect()
    try:
        candidate_rows = await _fetch_candidate_rows(args)
    finally:
        await database.disconnect()

    reports = [_build_row_report(row) for row in candidate_rows]
    targets = [
        report
        for report in reports
        if _should_target_row(
            report,
            include_test=bool(args.include_test),
            include_ready=bool(args.include_ready),
        )
    ]

    result: Dict[str, Any] = {
        "status": "success",
        "base_url": args.base_url.rstrip("/"),
        "provider": str(args.provider or "").strip().lower() or None,
        "apply": bool(args.apply),
        "include_test": bool(args.include_test),
        "include_ready": bool(args.include_ready),
        "scanned": len(reports),
        "targeted": len(targets),
        "targets": targets,
    }

    if args.apply:
        email = str(args.email or "").strip()
        password = str(args.password or "").strip()
        if not email or not password:
            raise RuntimeError("--email and --password (or env defaults) are required when --apply is set")

        validation_results: List[Dict[str, Any]] = []
        async with httpx.AsyncClient(base_url=args.base_url.rstrip("/"), timeout=30.0) as client:
            token = await _login(client, email=email, password=password)
            client.headers["Authorization"] = f"Bearer {token}"

            for index, target in enumerate(targets):
                if index > 0 and args.delay_ms > 0:
                    await asyncio.sleep(args.delay_ms / 1000)

                psp_id = target["psp_id"]
                response = await client.post(f"/merchant/psp/{psp_id}/test")
                try:
                    payload = response.json()
                except Exception:
                    payload = {"raw": response.text}

                validation_results.append(
                    {
                        "psp_id": psp_id,
                        "merchant_id": target["merchant_id"],
                        "status_code": response.status_code,
                        "ok": response.is_success,
                        "response": payload,
                    }
                )

        result["validation_results"] = validation_results
        result["validated"] = len(validation_results)
        result["validation_successes"] = sum(1 for item in validation_results if item.get("ok"))
        result["validation_failures"] = sum(1 for item in validation_results if not item.get("ok"))

    output = json.dumps(result, ensure_ascii=True, indent=2)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output, encoding="utf-8")
    print(output)
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run(_parse_args())))


if __name__ == "__main__":
    main()
